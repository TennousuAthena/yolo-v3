# Copyright 2022 Dakewe Biotech Corporation. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import math
import os
import shutil
from pathlib import Path
from typing import Any, List, Dict

import numpy as np
import torch
from torch import nn, Tensor
from torch.nn import Module
from torch.nn import functional as F_torch

__all__ = [
    "Darknet", "load_torch_weights", "load_darknet_weights", "save_torch_weights", "save_darknet_weights",
    "convert_model_weights", "yolov3_tiny_voc", "yolov3_tiny_coco", "yolov3_voc", "yolov3_coco",
]


class Darknet(nn.Module):
    def __init__(
            self,
            model_config: str,
            image_size: tuple = (416, 416),
            gray: bool = False,
            onnx_export: bool = False,
    ) -> None:
        """

        Args:
            model_config (str): Model configuration file path.
            image_size (tuple, optional): Image size. Default: (416, 416).
            gray (bool, optional): Whether to use grayscale images. Default: ``False``.
            onnx_export (bool, optional): Whether to export to onnx. Default: ``False``.

        """
        super(Darknet, self).__init__()
        self.module_define = parse_model_config(model_config)
        self.module_list, self.routs = _create_modules(self.module_define, image_size, model_config, gray, onnx_export)
        self.yolo_layers = _get_yolo_layers(self)
        self.version = np.array([0, 2, 5], dtype=np.int32)  # (int32) version info: major, minor, revision
        self.seen = np.array([0], dtype=np.int64)  # (int64) number of images seen during training
        self.onnx_export = onnx_export

    def forward(
            self, x: Tensor,
            augment: bool = False
    ) -> list[Any] | tuple[Tensor, Tensor] | tuple[Tensor, Any] | tuple[Tensor, None]:
        if not augment:
            return self.forward_once(x)
        else:
            image_size = x.shape[-2:]  # height, width
            s = [0.83, 0.67]  # scales
            y = []
            for i, xi in enumerate((x, _scale_image(x.flip(3), s[0], False), _scale_image(x, s[1], False))):
                y.append(self.forward_once(xi)[0])

            y[1][..., :4] /= s[0]  # scale
            y[1][..., 0] = image_size[1] - y[1][..., 0]  # flip lr
            y[2][..., :4] /= s[1]  # scale

            y = torch.cat(y, 1)
            return y, None

    def forward_once(
            self,
            x: Tensor,
            augment: bool = False) -> list[Any] | tuple[Tensor, Tensor] | tuple[Tensor, Any]:
        image_size = x.shape[-2:]  # height, width
        yolo_out, out = [], []

        # Augment images (inference and test only)
        if augment:
            nb = x.shape[0]  # batch size
            s = [0.83, 0.67]  # scales
            x = torch.cat((x, _scale_image(x.flip(3), s[0]), _scale_image(x, s[1])), 0)

        for i, module in enumerate(self.module_list):
            name = module.__class__.__name__
            if name == "_WeightedFeatureFusion":
                x = module(x, out)
            elif name == "_FeatureConcat":
                x = module(out)
            elif name == "_YOLOLayer":
                yolo_out.append(module(x))
            else:  # run module directly, i.e. mtype = "convolutional", "upsample", "maxpool", "batchnorm2d" etc.
                x = module(x)

            out.append(x if self.routs[i] else [])

        if self.training:  # train
            return yolo_out
        elif self.onnx_export:  # export
            x = [torch.cat(x, 0) for x in zip(*yolo_out)]
            return x[0], torch.cat(x[1:3], 1)  # scores, boxes: 3780x80, 3780x4
        else:  # inference or test
            x, p = zip(*yolo_out)  # inference output, training output
            x = torch.cat(x, 1)  # cat yolo outputs
            if augment:  # de-augment results
                x = torch.split(x, nb, dim=0)
                x[1][..., :4] /= s[0]  # scale
                x[1][..., 0] = image_size[1] - x[1][..., 0]  # flip lr
                x[2][..., :4] /= s[1]  # scale
                x = torch.cat(x, 1)
            return x, p

    def fuse(self):
        # Fuse Conv2d + BatchNorm2d layers throughout model
        print("Fusing layers...")
        fused_list = nn.ModuleList()
        for a in list(self.children())[0]:
            if isinstance(a, nn.Sequential):
                for i, b in enumerate(a):
                    if isinstance(b, nn.modules.batchnorm.BatchNorm2d):
                        # fuse this bn layer with the previous conv2d layer
                        conv = a[i - 1]
                        fused = _fuse_conv_and_bn(conv, b)
                        a = nn.Sequential(fused, *list(a.children())[i + 1:])
                        break
            fused_list.append(a)
        self.module_list = fused_list


class _YOLOLayer(nn.Module):
    def __init__(
            self,
            anchors: list,
            num_classes: int,
            image_size: tuple,
            yolo_index: int,
            layers: list,
            stride: int,
            onnx_export: bool = False,
    ) -> None:
        """

        Args:
            anchors (list): List of anchors.
            num_classes (int): Number of classes.
            image_size (tuple): Image size.
            yolo_index (int): Yolo layer index.
            layers (list): List of layers.
            stride (int): Stride.
            onnx_export (bool, optional): Whether to export to onnx. Default: ``False``.

        """
        super(_YOLOLayer, self).__init__()
        self.anchors = torch.Tensor(anchors)
        self.index = yolo_index  # index of this layer in layers
        self.layers = layers  # model output layer indices
        self.stride = stride  # layer stride
        self.nl = len(layers)  # number of output layers (3)
        self.na = len(anchors)  # number of anchors (3)
        self.num_classes = num_classes  # number of classes (80)
        self.num_classes_output = num_classes + 5  # number of outputs (85)
        self.nx, self.ny, self.ng = 0, 0, 0  # initialize number of x, y gridpoints
        self.anchor_vec = self.anchors / self.stride
        self.anchor_wh = self.anchor_vec.view(1, self.na, 1, 1, 2)
        self.onnx_export = onnx_export
        self.grid = None

        if onnx_export:
            self.training = False
            self.create_grids((image_size[1] // stride, image_size[0] // stride))  # number x, y grid points

    def create_grids(self, ng=(13, 13), device="cpu"):
        self.nx, self.ny = ng  # x and y grid size
        self.ng = torch.tensor(ng, dtype=torch.float)

        # build xy offsets
        if not self.training:
            yv, xv = torch.meshgrid([torch.arange(self.ny, device=device),
                                     torch.arange(self.nx, device=device)],
                                    indexing="ij")
            self.grid = torch.stack((xv, yv), 2).view((1, 1, self.ny, self.nx, 2)).float()

        if self.anchor_vec.device != device:
            self.anchor_vec = self.anchor_vec.to(device)
            self.anchor_wh = self.anchor_wh.to(device)

    def forward(self, p):
        if self.onnx_export:
            bs = 1  # batch size
        else:
            bs, _, ny, nx = p.shape  # bs, 255, 13, 13
            if (self.nx, self.ny) != (nx, ny):
                self.create_grids((nx, ny), p.device)

        # p.view(bs, 255, 13, 13) -- > (bs, 3, 13, 13, 85)  # (bs, anchors, grid, grid, classes + xywh)
        p = p.view(bs, self.na, self.num_classes_output, self.ny, self.nx)
        p = p.permute(0, 1, 3, 4, 2).contiguous()  # prediction

        if self.training:
            return p

        elif self.onnx_export:
            # Avoid broadcasting for ANE operations
            m = self.na * self.nx * self.ny
            ng = 1. / self.ng.repeat(m, 1)
            grid = self.grid.repeat(1, self.na, 1, 1, 1).view(m, 2)
            anchor_wh = self.anchor_wh.repeat(1, 1, self.nx, self.ny, 1).view(m, 2) * ng

            p = p.view(m, self.num_classes_output)
            xy = torch.sigmoid(p[:, 0:2]) + grid  # x, y
            wh = torch.exp(p[:, 2:4]) * anchor_wh  # width, height
            p_cls = torch.sigmoid(p[:, 4:5]) if self.num_classes == 1 else \
                torch.sigmoid(p[:, 5:self.num_classes_output]) * torch.sigmoid(p[:, 4:5])  # conf
            return p_cls, xy * ng, wh

        else:  # inference
            io = p.clone()  # inference output
            io[..., :2] = torch.sigmoid(io[..., :2]) + self.grid  # xy
            io[..., 2:4] = torch.exp(io[..., 2:4]) * self.anchor_wh  # wh yolo method
            io[..., :4] *= self.stride
            torch.sigmoid_(io[..., 4:])
            return io.view(bs, -1, self.num_classes_output), p  # view [1, 3, 13, 13, 85] as [1, 507, 85]


class _FeatureConcat(nn.Module):
    def __init__(self, layers: nn.ModuleList) -> None:
        """

        Args:
            layers (nn.ModuleList):

        """
        super(_FeatureConcat, self).__init__()
        self.layers = layers  # layer indices
        self.multiple = len(layers) > 1  # multiple layers flag

    def forward(self, x: Tensor) -> Tensor:
        x = torch.cat([x[i] for i in self.layers], 1) if self.multiple else x[self.layers[0]]

        return x


class _MixConv2d(nn.Module):
    def __init__(
            self,
            in_channels: int,
            out_channels: int,
            kernel_size_tuple: tuple = (3, 5, 7),
            stride: int = 1,
            dilation: int = 1,
            bias: bool = True,
            method: str = "equal_params") -> None:
        """MixConv: Mixed Depthwise Convolutional Kernels https://arxiv.org/abs/1907.09595
        
        Args:
            in_channels (int): Number of channels in the input image
            out_channels (int): Number of channels produced by the convolution
            kernel_size_tuple (tuple, optional): A tuple of 3 different kernel sizes. Defaults to (3, 5, 7).
            stride (int, optional): Stride of the convolution. Defaults to 1.
            dilation (int, optional): Spacing between kernel elements. Defaults to 1.
            bias (bool, optional): If True, adds a learnable bias to the output. Defaults to True.
            method (str, optional): Method to split channels. Defaults to "equal_params".
            
        """
        super(_MixConv2d, self).__init__()

        groups = len(kernel_size_tuple)

        if method == "equal_ch":  # equal channels per group
            i = torch.linspace(0, groups - 1E-6, out_channels).floor()  # out_channels indices
            ch = [(i == g).sum() for g in range(groups)]
        else:  # "equal_params": equal parameter count per group
            b = [out_channels] + [0] * groups
            a = np.eye(groups + 1, groups, k=-1)
            a -= np.roll(a, 1, axis=1)
            a *= np.array(kernel_size_tuple) ** 2
            a[0] = 1
            ch = np.linalg.lstsq(a, b, rcond=None)[0].round().astype(int)  # solve for equal weight indices, ax = b

        mix_conv2d = []
        for g in range(groups):
            mix_conv2d.append(nn.Conv2d(in_channels=in_channels,
                                        out_channels=ch[g],
                                        kernel_size=kernel_size_tuple[g],
                                        stride=stride,
                                        padding=kernel_size_tuple[g] // 2,
                                        dilation=dilation,
                                        bias=bias))
        self.mix_conv2d = nn.ModuleList(*mix_conv2d)

    def forward(self, x: Tensor) -> Tensor:
        x = torch.cat([m(x) for m in self.mix_conv2d], dim=1)

        return x


class _WeightedFeatureFusion(nn.Module):
    def __init__(self, layers: nn.ModuleList, weight: bool = False) -> None:
        """

        Args:
            layers:
            weight:

        """
        super(_WeightedFeatureFusion, self).__init__()
        self.layers = layers  # layer indices
        self.weight = weight  # apply weights boolean
        self.n = len(layers) + 1  # number of layers
        if weight:
            self.w = nn.Parameter(torch.zeros(self.n), requires_grad=True)  # layer weights

    def forward(self, x: Tensor, outputs: Tensor) -> Tensor:
        # Weights
        if self.weight:
            w = torch.sigmoid(self.w) * (2 / self.n)  # sigmoid weights (0-1)
            x = x * w[0]

        # Fusion
        nx = x.shape[1]  # input channels
        for i in range(self.n - 1):
            a = outputs[self.layers[i]] * w[i + 1] if self.weight else outputs[self.layers[i]]  # feature to add
            na = a.shape[1]  # feature channels

            # Adjust channels
            if nx == na:  # same shape
                x = x + a
            elif nx > na:  # slice input
                x[:, :na] = x[:, :na] + a  # or a = nn.ZeroPad2d((0, 0, 0, 0, 0, dc))(a); x = x + a
            else:  # slice feature
                x = x + a[:, :nx]

        return x


def _create_modules(
        module_define: list,
        image_size: int or tuple,
        model_config: str,
        gray: bool = False,
        onnx_export: bool = False,
) -> [nn.ModuleList, list]:
    """Constructs module list of layer blocks from module configuration in module_define

    Args:
        module_define (nn.ModuleList): Module definition of model.
        image_size (int or tuple): size of input image
        model_config (str): Path to model model_config file
        gray (bool): If True, model is grayscale
        onnx_export (bool, optional): If ONNX export is ON. Defaults to False.

    Returns:
        module_define (nn.ModuleList): Module list
        routs_binary (list): Hyper-parameters

    """
    image_size = [image_size] * 2 if isinstance(image_size, int) else image_size  # expand if necessary
    _ = module_define.pop(0)  # cfg training hyper-params (unused)
    output_filters = [3] if not gray else [1]
    module_list = nn.ModuleList()
    routs = []  # list of layers which rout to deeper layers
    yolo_index = -1
    i = 0
    filters = 3

    for i, module in enumerate(module_define):
        modules = nn.Sequential()

        if module["type"] == "convolutional":
            bn = module["batch_normalize"]
            filters = module["filters"]
            k = module["size"]  # kernel size
            stride = module["stride"] if "stride" in module else (module["stride_y"], module["stride_x"])
            if isinstance(k, int):  # single-size conv
                modules.add_module("Conv2d", nn.Conv2d(in_channels=output_filters[-1],
                                                       out_channels=filters,
                                                       kernel_size=k,
                                                       stride=stride,
                                                       padding=k // 2 if module["pad"] else 0,
                                                       groups=module["groups"] if "groups" in module else 1,
                                                       bias=not bn))
            else:  # multiple-size conv
                modules.add_module("MixConv2d", _MixConv2d(in_channels=output_filters[-1],
                                                           out_channels=filters,
                                                           kernel_size_tuple=k,
                                                           stride=stride,
                                                           bias=not bn))

            if bn:
                modules.add_module("BatchNorm2d", nn.BatchNorm2d(filters, momentum=0.03, eps=1E-4))
            else:
                routs.append(i)  # detection output (goes into yolo layer)

            if module["activation"] == "leaky":
                modules.add_module("activation", nn.LeakyReLU(0.1, inplace=True))
            elif module["activation"] == "swish":
                modules.add_module("activation", nn.Hardswish(True))
            elif module["activation"] == "mish":
                modules.add_module("activation", nn.Mish(True))

        elif module["type"] == "BatchNorm2d":
            filters = output_filters[-1]
            modules = nn.BatchNorm2d(filters, momentum=0.03, eps=1e-4)
            if i == 0 and filters == 3:  # normalize RGB image
                modules.running_mean = torch.tensor([0.485, 0.456, 0.406])
                modules.running_var = torch.tensor([0.0524, 0.0502, 0.0506])

        elif module["type"] == "maxpool":
            k = module["size"]  # kernel size
            stride = module["stride"]
            maxpool = nn.MaxPool2d(kernel_size=k, stride=stride, padding=(k - 1) // 2)
            if k == 2 and stride == 1:  # yolov3-tiny
                modules.add_module("ZeroPad2d", nn.ZeroPad2d((0, 1, 0, 1)))
                modules.add_module("MaxPool2d", maxpool)
            else:
                modules = maxpool

        elif module["type"] == "upsample":
            if onnx_export:  # explicitly state size, avoid scale_factor
                g = (yolo_index + 1) * 2 / 32  # gain
                modules = nn.Upsample(size=tuple(int(x * g) for x in image_size))  # image_size = (320, 192)
            else:
                modules = nn.Upsample(scale_factor=module["stride"])

        elif module["type"] == "route":  # nn.Sequential() placeholder for "route" layer
            layers = module["layers"]
            filters = sum([output_filters[l + 1 if l > 0 else l] for l in layers])
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = _FeatureConcat(layers)

        elif module["type"] == "shortcut":  # nn.Sequential() placeholder for "shortcut" layer
            layers = module["from"]
            filters = output_filters[-1]
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = _WeightedFeatureFusion(layers=layers, weight="weights_type" in module)

        elif module["type"] == "reorg3d":  # yolov3-spp-pan-scale
            pass

        elif module["type"] == "yolo":
            yolo_index += 1
            stride = [32, 16, 8]  # P5, P4, P3 strides
            if any(x in model_config for x in ["panet", "yolov4", "cd53"]):  # stride order reversed
                stride = list(reversed(stride))
            layers = module["from"] if "from" in module else []
            modules = _YOLOLayer(anchors=module["anchors"][module["mask"]],  # anchor list
                                 num_classes=module["classes"],  # number of classes
                                 image_size=image_size,  # (416, 416)
                                 yolo_index=yolo_index,  # 0, 1, 2...
                                 layers=layers,  # output layers
                                 stride=stride[yolo_index])

            # Initialize preceding Conv2d() bias (https://arxiv.org/pdf/1708.02002.pdf section 3.3)
            try:
                j = layers[yolo_index] if "from" in module else -1
                # If previous layer is a dropout layer, get the one before
                if module_define[j].__class__.__name__ == "Dropout":
                    j -= 1
                bias_ = module_define[j][0].bias  # shape(255,)
                bias = bias_[:modules.num_classes_output * modules.na].view(modules.na, -1)  # shape(3,85)
                bias[:, 4] += -4.5  # obj
                bias[:, 5:] += math.log(0.6 / (modules.num_classes - 0.99))  # cls (sigmoid(p) = 1/nc)
                module_define[j][0].bias = torch.nn.Parameter(bias_, requires_grad=bias_.requires_grad)
            except:
                print("WARNING: smart bias initialization failure.")

        elif module["type"] == "dropout":
            perc = float(module["probability"])
            modules = nn.Dropout(p=perc)
        else:
            print("Warning: Unrecognized Layer Type: " + module["type"])

        # Register module list and number of output filters
        module_list.append(modules)
        output_filters.append(filters)

    routs_binary = [False] * (i + 1)

    # Set YOLO route layer
    for i in routs:
        routs_binary[i] = True

    return module_list, routs_binary


def _get_yolo_layers(model):
    return [i for i, m in enumerate(model.module_list) if m.__class__.__name__ == "_YOLOLayer"]  # [89, 101, 113]


def load_torch_weights(
        model: nn.Module,
        model_weights_path: str,
        ema_model: nn.Module = None,
        optimizer: torch.optim.Optimizer = None,
        scheduler: torch.optim.lr_scheduler = None,
        load_mode: str = None,
) -> tuple[Module, Any, Any, Any, Any, Any] or Module:
    # Load model weights
    checkpoint = torch.load(model_weights_path, map_location=lambda storage, loc: storage)

    if load_mode == "resume":
        # Restore the parameters in the training node to this point
        start_epoch = checkpoint["epoch"]
        best_map50 = checkpoint["best_map50"] if checkpoint["best_map50"] else 0.0
        # Load model state dict. Extract the fitted model weights
        model_state_dict = model.state_dict()
        state_dict = {k: v for k, v in checkpoint["state_dict"].items() if k in model_state_dict.keys()}
        # Overwrite the model weights to the current model (base model)
        model_state_dict.update(state_dict)
        model.load_state_dict(model_state_dict)
        # Load the optimizer model
        optimizer.load_state_dict(checkpoint["optimizer"])

        if scheduler is not None:
            # Load the scheduler model
            scheduler.load_torch_weights(checkpoint["scheduler"])

        if ema_model is not None:
            # Load ema model state dict. Extract the fitted model weights
            ema_model_state_dict = ema_model.state_dict()
            ema_state_dict = {k: v for k, v in checkpoint["ema_state_dict"].items() if k in ema_model_state_dict.keys()}
            # Overwrite the model weights to the current model (ema model)
            ema_model_state_dict.update(ema_state_dict)
            ema_model.load_state_dict(ema_model_state_dict)

        return model, ema_model, start_epoch, best_map50, optimizer, scheduler
    else:
        # Load model state dict. Extract the fitted model weights
        model_state_dict = model.state_dict()
        state_dict = {k: v for k, v in checkpoint["state_dict"].items() if
                      k in model_state_dict.keys() and v.size() == model_state_dict[k].size()}
        # Overwrite the model weights to the current model
        model_state_dict.update(state_dict)
        model.load_state_dict(model_state_dict)

        return model


def load_darknet_weights(self, weights, cutoff=-1):
    # Parses and loads the weights stored in "weights"

    # Establish cutoffs (load layers between 0 and cutoff. if cutoff = -1 all are loaded)
    file = Path(weights).name
    if file == "darknet53.conv.74":
        cutoff = 75
    elif file == "yolov3-tiny.conv.15":
        cutoff = 15

    # Read weights file
    with open(weights, "rb") as f:
        self.version = np.fromfile(f, dtype=np.int32, count=3)  # (int32) version info: major, minor, revision
        self.seen = np.fromfile(f, dtype=np.int64, count=1)  # (int64) number of images seen during training

        weights = np.fromfile(f, dtype=np.float32)  # the rest are weights

    ptr = 0
    for i, (module_define, module) in enumerate(zip(self.module_define[:cutoff], self.module_list[:cutoff])):
        if module_define["type"] == "convolutional":
            conv = module[0]
            if module_define["batch_normalize"]:
                # Load BN bias, weights, running mean and running variance
                bn = module[1]
                nb = bn.bias.numel()  # number of biases
                # Bias
                bn.bias.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.bias))
                ptr += nb
                # Weight
                bn.weight.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.weight))
                ptr += nb
                # Running Mean
                bn.running_mean.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.running_mean))
                ptr += nb
                # Running Var
                bn.running_var.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.running_var))
                ptr += nb
            else:
                # Load conv. bias
                nb = conv.bias.numel()
                conv_b = torch.from_numpy(weights[ptr:ptr + nb]).view_as(conv.bias)
                conv.bias.data.copy_(conv_b)
                ptr += nb
            # Load conv. weights
            nw = conv.weight.numel()  # number of weights
            conv.weight.data.copy_(torch.from_numpy(weights[ptr:ptr + nw]).view_as(conv.weight))
            ptr += nw


def save_torch_weights(
        state_dict: dict,
        file_name: str,
        samples_dir: str,
        results_dir: str,
        best_file_name: str,
        last_file_name: str,
        is_best: bool = False,
        is_last: bool = False,
) -> None:
    checkpoint_path = os.path.join(samples_dir, file_name)
    torch.save(state_dict, checkpoint_path)

    if is_best:
        shutil.copyfile(checkpoint_path, os.path.join(results_dir, best_file_name))
    if is_last:
        shutil.copyfile(checkpoint_path, os.path.join(results_dir, last_file_name))


def save_darknet_weights(self, model_weights_path: str, cutoff=-1) -> None:
    """Saves model weights to a file.

    Args:
        self:
        model_weights_path (str): Path to save model weights.
        cutoff (int, optional): Cutoff layer. Defaults: -1.

    """
    with open(model_weights_path, "wb") as f:
        self.version.tofile(f)  # (int32) version info: major, minor, revision
        self.seen.tofile(f)  # (int64) number of images seen during training

        # Iterate through layers
        for i, (module_define, module) in enumerate(zip(self.module_define[:cutoff], self.module_list[:cutoff])):
            if module_define["type"] == "convolutional":
                conv_layer = module[0]
                # If batch norm, load bn first
                if module_define["batch_normalize"]:
                    bn_layer = module[1]
                    bn_layer.bias.data.cpu().numpy().tofile(f)
                    bn_layer.weight.data.cpu().numpy().tofile(f)
                    bn_layer.running_mean.data.cpu().numpy().tofile(f)
                    bn_layer.running_var.data.cpu().numpy().tofile(f)
                # Load conv bias
                else:
                    conv_layer.bias.data.cpu().numpy().tofile(f)
                # Load conv weights
                conv_layer.weight.data.cpu().numpy().tofile(f)


def convert_model_weights(model_config_path: str, model_weights_path: str) -> None:
    """Convert darknet model to pytorch model

    Args:
        model_config_path (str): path to darknet model model_config file
        model_weights_path (str): path to darknet model weights file

    """
    # Initialize model
    model = Darknet(model_config_path)

    # Load weights and save
    if model_weights_path.endswith(".pth.tar"):  # if PyTorch format
        model.load_state_dict(torch.load(model_weights_path, map_location="cpu")["state_dict"])
        target = model_weights_path[:-8] + ".weights"
        save_darknet_weights(model, model_weights_path=target, cutoff=-1)
        print(f"Success: converted {model_weights_path} to {target}")

    elif model_weights_path.endswith(".weights"):  # darknet format
        load_darknet_weights(model, model_weights_path)

        chkpt = {"epoch": 0,
                 "best_map50": None,
                 "state_dict": model.state_dict(),
                 "ema_state_dict": model.state_dict(),
                 "optimizer": None,
                 "scheduler": None}

        target = model_weights_path[:-8] + ".pth.tar"
        torch.save(chkpt, target)
        print(f"Success: converted {model_weights_path} to {target}")
    else:
        print("Error: extension not supported.")


def parse_model_config(model_config_path: str) -> List[Dict[str, Any]]:
    """Parses the yolo-v3 layer configuration file and returns module definitions.

    Args:
        model_config_path (str): path to model model_config file

    Returns:
        module_define (List[Dict[str, Any]]): module definitions

    """
    if not model_config_path.endswith(".cfg"):  # add .cfg suffix if omitted
        model_config_path += ".cfg"
    if not os.path.exists(model_config_path) and os.path.exists(
            "cfg" + os.sep + model_config_path):  # add cfg/ prefix if omitted
        model_config_path = "cfg" + os.sep + model_config_path

    with open(model_config_path, "r") as f:
        lines = f.read().split("\n")
    lines = [x for x in lines if x and not x.startswith("#")]
    lines = [x.rstrip().lstrip() for x in lines]  # get rid of fringe whitespaces
    module_define = []  # module definitions
    for line in lines:
        if line.startswith("["):  # This marks the start of a new block
            module_define.append({})
            module_define[-1]["type"] = line[1:-1].rstrip()
            if module_define[-1]["type"] == "convolutional":
                module_define[-1]["batch_normalize"] = 0  # pre-populate with zeros (may be overwritten later)
        else:
            key, val = line.split("=")
            key = key.rstrip()

            if key == "anchors":  # return nparray
                module_define[-1][key] = np.array([float(x) for x in val.split(",")]).reshape((-1, 2))  # np anchors
            elif (key in ["from", "layers", "mask"]) or (key == "size" and "," in val):  # return array
                module_define[-1][key] = [int(x) for x in val.split(",")]
            else:
                val = val.strip()
                if val.isnumeric():  # return int or float
                    module_define[-1][key] = int(val) if (int(val) - float(val)) == 0 else float(val)
                else:
                    module_define[-1][key] = val  # return string

    # Check all fields are supported
    supported = ["type", "batch_normalize", "filters", "size", "stride", "pad", "activation", "layers", "groups",
                 "from", "mask", "anchors", "classes", "num", "jitter", "ignore_thresh", "truth_thresh", "random",
                 "stride_x", "stride_y", "weights_type", "weights_normalization", "scale_x_y", "beta_nms", "nms_kind",
                 "iou_loss", "iou_normalizer", "cls_normalizer", "iou_thresh", "probability"]

    f = []  # fields

    for x in module_define[1:]:
        [f.append(k) for k in x if k not in f]

    u = [x for x in f if x not in supported]  # unsupported fields

    assert not any(u), f"Unsupported fields {model_config_path}"

    return module_define


def _scale_image(image: Tensor, ratio: float = 1.0, same_shape: bool = True) -> Tensor:
    """Scales an image by a ratio. If same_shape is True, the image is padded with zeros to maintain the same shape.

    Args:
        image (Tensor): image to be scaled
        ratio (float): ratio to scale image by
        same_shape (bool): whether to pad image with zeros to maintain same shape

    Returns:
        image (Tensor): scaled image

    """
    # scales img(bs,3,y,x) by ratio
    h, w = image.shape[2:]
    s = (int(h * ratio), int(w * ratio))  # new size
    image = F_torch.interpolate(image, size=s, mode="bilinear", align_corners=False)  # resize
    if not same_shape:  # pad/crop img
        gs = 64  # (pixels) grid size
        h, w = [math.ceil(x * ratio / gs) * gs for x in (h, w)]

    image = F_torch.pad(image, [0, w - s[1], 0, h - s[0]], value=0.447)

    return image


def _fuse_conv_and_bn(conv: nn.Conv2d, bn: nn.BatchNorm2d) -> nn.Module:
    """Fuse convolution and batchnorm layers.

    Args:
        conv (nn.Conv2d): convolution layer
        bn (nn.BatchNorm2d): batchnorm layer

    Returns:
        fused_conv_bn (nn.Module): fused convolution layer

    """
    with torch.no_grad():
        # init
        fused_conv_bn = nn.Conv2d(conv.in_channels,
                                  conv.out_channels,
                                  kernel_size=conv.kernel_size,
                                  stride=conv.stride,
                                  padding=conv.padding,
                                  bias=True)

        # prepare filters
        w_conv = conv.weight.clone().view(conv.out_channels, -1)
        w_bn = torch.diag(bn.weight.div(torch.sqrt(bn.eps + bn.running_var)))
        fused_conv_bn.weight.copy_(torch.mm(w_bn, w_conv).view(fused_conv_bn.weight.size()))

        # prepare spatial bias
        if conv.bias is not None:
            b_conv = conv.bias
        else:
            b_conv = torch.zeros(conv.weight.size(0))
        b_bn = bn.bias - bn.weight.mul(bn.running_mean).div(torch.sqrt(bn.running_var + bn.eps))
        fused_conv_bn.bias.copy_(torch.mm(w_bn, b_conv.reshape(-1, 1)).reshape(-1) + b_bn)

        return fused_conv_bn


def compute_loss(p: Tensor, targets: Tensor, model: nn.Module):  # predictions, targets, model
    """Computes loss for YOLOv3.

    Args:
        p (Tensor): predictions
        targets (Tensor): targets
        model (nn.Module): model

    Returns:
        loss (Tensor): loss

    """
    lcls = torch.FloatTensor([0]).to(device=targets.device)
    lbox = torch.FloatTensor([0]).to(device=targets.device)
    lobj = torch.FloatTensor([0]).to(device=targets.device)
    tcls, tbox, indices, anchors = build_targets(p, targets, model)  # targets
    hyper_parameters_dict = model.hyper_parameters_dict  # hyperparameters

    # Define criteria
    BCEcls = nn.BCEWithLogitsLoss(
        pos_weight=torch.FloatTensor([hyper_parameters_dict["cls_pw"]]).to(device=targets.device))
    BCEobj = nn.BCEWithLogitsLoss(
        pos_weight=torch.FloatTensor([hyper_parameters_dict["obj_pw"]]).to(device=targets.device))

    # class label smoothing https://arxiv.org/pdf/1902.04103.pdf eqn 3
    cp, cn = smooth_bce(eps=0.0)

    # focal loss
    g = hyper_parameters_dict["fl_gamma"]  # focal loss gamma
    if g > 0:
        BCEcls, BCEobj = FocalLoss(BCEcls, g), FocalLoss(BCEobj, g)

    # per output
    nt = 0  # targets
    for i, pi in enumerate(p):  # layer index, layer predictions
        b, a, gj, gi = indices[i]  # image, anchor, gridy, gridx
        tobj = torch.zeros_like(pi[..., 0])  # target obj

        nb = b.shape[0]  # number of targets
        if nb:
            nt += nb  # cumulative targets
            ps = pi[b, a, gj, gi]  # prediction subset corresponding to targets

            # GIoU
            pxy = ps[:, :2].sigmoid()
            pwh = ps[:, 2:4].exp().clamp(max=1E3) * anchors[i]
            pbox = torch.cat((pxy, pwh), 1)  # predicted box
            giou = bbox_iou(pbox.t(), tbox[i], x1y1x2y2=False, g_iou=True)  # giou(prediction, target)
            lbox += (1.0 - giou).mean()  # giou loss

            # Obj
            tobj[b, a, gj, gi] = (1.0 - model.gr) + model.gr * giou.detach().clamp(0).type(tobj.dtype)  # giou ratio

            # Class
            if model.num_classes > 1:  # cls loss (only if multiple classes)
                t = torch.full_like(ps[:, 5:], cn)  # targets
                t[range(nb), tcls[i]] = cp
                lcls += BCEcls(ps[:, 5:], t)  # BCE

        lobj += BCEobj(pi[..., 4], tobj)  # obj loss

    lbox *= hyper_parameters_dict["giou"]
    lobj *= hyper_parameters_dict["obj"]
    lcls *= hyper_parameters_dict["cls"]

    loss = lbox + lobj + lcls
    return loss, torch.cat((lbox, lobj, lcls, loss)).detach()


def bbox_iou(box1, box2, x1y1x2y2=True, g_iou=False, d_iou=False, c_iou=False):
    # Returns the IoU of box1 to box2. box1 is 4, box2 is nx4
    box2 = box2.t()

    # Get the coordinates of bounding boxes
    if x1y1x2y2:  # x1, y1, x2, y2 = box1
        b1_x1, b1_y1, b1_x2, b1_y2 = box1[0], box1[1], box1[2], box1[3]
        b2_x1, b2_y1, b2_x2, b2_y2 = box2[0], box2[1], box2[2], box2[3]
    else:  # transform from xywh to xyxy
        b1_x1, b1_x2 = box1[0] - box1[2] / 2, box1[0] + box1[2] / 2
        b1_y1, b1_y2 = box1[1] - box1[3] / 2, box1[1] + box1[3] / 2
        b2_x1, b2_x2 = box2[0] - box2[2] / 2, box2[0] + box2[2] / 2
        b2_y1, b2_y2 = box2[1] - box2[3] / 2, box2[1] + box2[3] / 2

    # Intersection area
    inter = (torch.min(b1_x2, b2_x2) - torch.max(b1_x1, b2_x1)).clamp(0) * \
            (torch.min(b1_y2, b2_y2) - torch.max(b1_y1, b2_y1)).clamp(0)

    # Union Area
    w1, h1 = b1_x2 - b1_x1, b1_y2 - b1_y1
    w2, h2 = b2_x2 - b2_x1, b2_y2 - b2_y1
    union = (w1 * h1 + 1e-16) + w2 * h2 - inter

    iou = inter / union  # iou
    if g_iou or d_iou or c_iou:
        cw = torch.max(b1_x2, b2_x2) - torch.min(b1_x1, b2_x1)  # convex (smallest enclosing box) width
        ch = torch.max(b1_y2, b2_y2) - torch.min(b1_y1, b2_y1)  # convex height
        if g_iou:  # Generalized IoU https://arxiv.org/pdf/1902.09630.pdf
            c_area = cw * ch + 1e-16  # convex area
            return iou - (c_area - union) / c_area  # GIoU
        if d_iou or c_iou:  # Distance or Complete IoU https://arxiv.org/abs/1911.08287v1
            # convex diagonal squared
            c2 = cw ** 2 + ch ** 2 + 1e-16
            # centerpoint distance squared
            rho2 = ((b2_x1 + b2_x2) - (b1_x1 + b1_x2)) ** 2 / 4 + ((b2_y1 + b2_y2) - (b1_y1 + b1_y2)) ** 2 / 4
            if d_iou:
                return iou - rho2 / c2  # DIoU
            elif c_iou:  # https://github.com/Zzh-tju/DIoU-SSD-pytorch/blob/master/utils/box/box_utils.py#L47
                v = (4 / math.pi ** 2) * torch.pow(torch.atan(w2 / h2) - torch.atan(w1 / h1), 2)
                with torch.no_grad():
                    alpha = v / (1 - iou + v)
                return iou - (rho2 / c2 + v * alpha)  # CIoU

    return iou


def smooth_bce(eps: float = 0.1):
    # return positive, negative label smoothing BCE targets
    return 1.0 - 0.5 * eps, 0.5 * eps


class FocalLoss(nn.Module):
    def __init__(self, loss_fcn: nn.Module, gamma: float = 1.5, alpha: float = 0.25):
        """Focal loss for binary classification

        Args:
            loss_fcn (nn.Module): loss function
            gamma (float, optional): gamma. Defaults to 1.5.
            alpha (float, optional): alpha. Defaults to 0.25.
        """
        super(FocalLoss, self).__init__()
        self.loss_fcn = loss_fcn  # must be nn.BCEWithLogitsLoss()
        self.gamma = gamma
        self.alpha = alpha
        self.reduction = loss_fcn.reduction
        self.loss_fcn.reduction = "none"  # required to apply FL to each element

    def forward(self, pred, true):
        loss = self.loss_fcn(pred, true)

        pred_prob = torch.sigmoid(pred)  # prob from logits
        p_t = true * pred_prob + (1 - true) * (1 - pred_prob)
        alpha_factor = true * self.alpha + (1 - true) * (1 - self.alpha)
        modulating_factor = (1.0 - p_t) ** self.gamma
        loss *= alpha_factor * modulating_factor

        if self.reduction == "mean":
            return loss.mean()
        elif self.reduction == "sum":
            return loss.sum()
        else:  # "none"
            return loss


def build_targets(
        p: Tensor,
        targets: Tensor,
        model: nn.Module
) -> tuple[list[Any], list[Tensor], list[tuple[Any, Tensor | list[Any] | Any, Any, Any]], list[Any]]:
    """Build targets for compute_loss(), input targets(image,class,x,y,w,h)

    Args:
        p (Tensor): predictions
        targets (Tensor): targets
        model (nn.Module): model

    Returns:
        tuple[list[Any], list[Tensor], list[tuple[Any, Tensor | list[Any] | Any, Any, Any]], list[Any]]: targets, indices, anchors, regression
    """
    # Build targets for compute_loss(), input targets(image,class,x,y,w,h)
    nt = targets.shape[0]
    tcls, tbox, indices, anch = [], [], [], []
    gain = torch.ones(6, device=targets.device)  # normalized to gridspace gain

    for i, j in enumerate(model.yolo_layers):
        anchors = model.module_list[j].anchor_vec
        gain[2:] = torch.tensor(p[i].shape)[[3, 2, 3, 2]].to(device=targets.device)  # xyxy gain
        na = anchors.shape[0]  # number of anchors
        at = torch.arange(na).view(na, 1).repeat(1, nt).to(
            device=targets.device)  # anchor tensor, same as .repeat_interleave(nt)

        # Match targets to anchors
        a, t, offsets = [], targets * gain, 0
        if nt:
            j = wh_iou(anchors, t[:, 4:6]) > model.hyper_parameters_dict[
                "iou_t"]  # iou(3,n) = wh_iou(anchors(3,2), gwh(n,2))
            a, t = at[j], t.repeat(na, 1, 1)[j]  # filter

        # Define
        b, c = t[:, :2].long().T  # image, class
        gxy = t[:, 2:4]  # grid xy
        gwh = t[:, 4:6]  # grid wh
        gij = (gxy - offsets).long()
        gi, gj = gij.T  # grid xy indices

        # Append# image, anchor, grid indices
        indices.append((b, a, gj.clamp_(0, gain[3].long() - 1), gi.clamp_(0, gain[2].long() - 1)))
        tbox.append(torch.cat((gxy - gij, gwh), 1))  # box
        anch.append(anchors[a])  # anchors
        tcls.append(c)  # class
        if c.shape[0]:  # if any targets
            assert c.max() < model.num_classes, f"Model accepts {model.num_classes} classes labeled from 0-{model.num_classes - 1}, however you labelled a class {c.max()}. "
    return tcls, tbox, indices, anch


def wh_iou(wh1, wh2):
    """Returns the IoU of two wh tensors

    Args:
        wh1 (Tensor): width and height of first tensor
        wh2 (Tensor): width and height of second tensor

    Returns:

    """
    # Returns the nxm IoU matrix. wh1 is nx2, wh2 is mx2
    wh1 = wh1[:, None]  # [N,1,2]
    wh2 = wh2[None]  # [1,M,2]
    inter = torch.min(wh1, wh2).prod(2)  # [N,M]
    return inter / (wh1.prod(2) + wh2.prod(2) - inter)  # iou = inter / (area1 + area2 - inter)


def yolov3_tiny_voc(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3_tiny-voc.cfg", **kwargs)

    return model


def yolov3_tiny_coco(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3_tiny-coco.cfg", **kwargs)

    return model


def yolov3_voc(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3-voc.cfg", **kwargs)

    return model


def yolov3_coco(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3-coco.cfg", **kwargs)

    return model


def yolov3_spp_voc(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3_spp-voc.cfg", **kwargs)

    return model


def yolov3_spp_coco(**kwargs) -> Darknet:
    model = Darknet(model_config="./model_config/yolov3_spp-coco.cfg", **kwargs)

    return model