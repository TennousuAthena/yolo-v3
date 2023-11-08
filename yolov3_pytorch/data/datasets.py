# Copyright 2023 AlphaBetter Corporation. All Rights Reserved.
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
"""
Data loader for YOLOv3.
"""
import glob
import os
import random
import time
from pathlib import Path
from threading import Thread
from typing import Any, Tuple, List, Union

import cv2
import numpy as np
import torch
from PIL import ExifTags, Image
from torch.utils.data import Dataset
from torchvision.transforms import functional as F_vision
from tqdm import tqdm

from yolov3_pytorch.utils.common import xywh2xyxy, xyxy2xywh
from .data_augment import adjust_hsv, letterbox, random_affine

__all__ = [
    "LoadImages", "LoadWebcam", "LoadStreams", "LoadDatasets",
]

# Parameters
IMG_FORMATS = ["bmp", "jpg", "jpeg", "png", "tif", "tiff", "dng", "webp", "mpo"]
VID_FORMATS = ["mp4", "mov", "avi", "mkv"]
IMG_FORMATS.extend([f.upper() for f in IMG_FORMATS])
VID_FORMATS.extend([f.upper() for f in VID_FORMATS])
# Get orientation exif tag
for k, v in ExifTags.TAGS.items():
    if v == "Orientation":
        ORIENTATION = k
        break

# Get orientation exif tag
for orientation in ExifTags.TAGS.keys():
    if ExifTags.TAGS[orientation] == "Orientation":
        break


class LoadImages:  # for inference
    def __init__(
            self,
            img_path: Union[str, Path],
            image_size: int = 416,
            gray: bool = False,
    ) -> None:
        """Load images from a path.

        Args:
            img_path (str or Path): The path to the images
            image_size (int, optional): The size of the images. Defaults: 416
            gray (bool, optional): Whether to convert the images to grayscale. Defaults: ``False``
        """
        files = []

        if os.path.isdir(img_path):
            files = sorted(glob.glob(os.path.join(img_path, "*.*")))
        elif os.path.isfile(img_path):
            files = [img_path]

        images = [x for x in files if x.split(".")[-1].lower() in IMG_FORMATS]
        videos = [x for x in files if x.split(".")[-1].lower() in VID_FORMATS]
        nI, nV = len(images), len(videos)

        self.image_size = image_size
        self.gray = gray
        self.files = images + videos
        self.nF = nI + nV  # number of files
        self.video_flag = [False] * nI + [True] * nV
        self.mode = "images"
        if any(videos):
            self.new_video(videos[0])  # new video
        else:
            self.cap = None
        assert self.nF > 0, f"No images or videos found in {img_path}. " \
                            f"Supported formats are:\n" \
                            f"images: {IMG_FORMATS}\n" \
                            f"videos: {VID_FORMATS}"

    def __iter__(self):
        """Iterate over the images."""
        self.count = 0
        return self

    def __next__(self):
        """Get the next image."""
        if self.count == self.nF:
            raise StopIteration
        path = self.files[self.count]

        if self.video_flag[self.count]:
            # Read video
            self.mode = "video"
            ret_val, raw_image = self.cap.read()
            if not ret_val:
                self.count += 1
                self.cap.release()
                if self.count == self.nF:  # last video
                    raise StopIteration
                else:
                    path = self.files[self.count]
                    self.new_video(path)
                    ret_val, raw_image = self.cap.read()

            self.frame += 1
            print(f"video {self.count + 1}/{self.nF} ({self.frame}/{self.nframes}) {path}: ", end="")

        else:
            # Read image
            self.count += 1
            raw_image = cv2.imread(path)  # BGR
            assert raw_image is not None, "Image Not Found " + path
            print(f"image {self.count}/{self.nF} {path}: ", end="")

        # Padded resize
        image = letterbox(raw_image, new_shape=self.image_size)[0]

        # Convert
        image = image[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
        image = np.ascontiguousarray(image)

        # RGB numpy convert RGB tensor
        image = torch.from_numpy(image)

        if self.gray:
            # RGB tensor convert GRAY tensor
            image = F_vision.rgb_to_grayscale(image)

        return path, image, raw_image, self.cap

    def new_video(self, path: str) -> None:
        """Open a new video.

        Args:
            path (str): The path to the video.

        """
        self.frame = 0
        self.cap = cv2.VideoCapture(path)
        self.nframes = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

    def __len__(self):
        return self.nF  # number of files


class LoadWebcam:  # for inference
    def __init__(self, pipe: int = 0, image_size: int = 416, gray: bool = False) -> None:
        """Load images from a webcam.

        Args:
            pipe (int, optional): The webcam to use. Defaults: 0.
            image_size (int, optional): The size of the images. Defaults: 416.
            gray (bool, optional): Whether to convert the images to grayscale. Defaults: ``False``.

        """
        self.image_size = image_size
        self.gray = gray

        if pipe == "0":
            pipe = 0  # local camera
        self.pipe = pipe
        self.cap = cv2.VideoCapture(pipe)  # video capture object
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)  # set buffer size

    def __iter__(self):
        """Iterate over the images."""
        self.count = -1
        return self

    def __next__(self):
        """Get the next image."""
        self.count += 1
        if cv2.waitKey(1) == ord("q"):  # q to quit
            self.cap.release()
            cv2.destroyAllWindows()
            raise StopIteration

        # Read frame
        if self.pipe == 0:  # local camera
            ret_val, raw_image = self.cap.read()
            raw_image = cv2.flip(raw_image, 1)  # flip left-right
        else:  # IP camera
            n = 0
            while True:
                n += 1
                self.cap.grab()
                if n % 30 == 0:  # skip frames
                    ret_val, raw_image = self.cap.retrieve()
                    if ret_val:
                        break

        # Print
        assert ret_val, f"Camera Error {self.pipe}"
        image_path = "webcam.jpg"
        print(f"webcam {self.count}: ", end="")

        # Padded resize
        image = letterbox(raw_image, new_shape=self.image_size)[0]

        # Convert
        image = image[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
        image = np.ascontiguousarray(image)

        # RGB numpy convert RGB tensor
        image = torch.from_numpy(image)

        if self.gray:
            # RGB tensor convert GRAY tensor
            image = F_vision.rgb_to_grayscale(image)

        return image_path, image, raw_image, None

    def __len__(self):
        """Number of images in the dataset."""
        return 0


class LoadStreams:  # multiple IP or RTSP cameras
    def __init__(self, sources="streams.txt", img_size=416, gray: bool = False) -> None:
        """Load multiple IP or RTSP cameras.

        Args:
            sources (str, optional): The path to the file with the sources. Defaults: "streams.txt".
            img_size (int, optional): The size of the images. Defaults: 416.

        """
        self.mode = "images"
        self.img_size = img_size
        self.gray = gray

        if os.path.isfile(sources):
            with open(sources, "r") as f:
                sources = [x.strip() for x in f.read().splitlines() if len(x.strip())]
        else:
            sources = [sources]

        n = len(sources)
        self.images = [None] * n
        self.sources = sources
        for i, s in enumerate(sources):
            # Start the thread to read frames from the video stream
            print(f"{i + 1}/{n}: {s}... ", end="")
            cap = cv2.VideoCapture(0 if s == "0" else s)
            assert cap.isOpened(), "Failed to open %s" % s
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) % 100
            _, self.images[i] = cap.read()  # guarantee first frame
            thread = Thread(target=self.update, args=([i, cap]), daemon=True)
            print(f" success ({w}x{h} at {fps:.2f} FPS).")
            thread.start()
        print("")  # newline

        # check for common shapes
        s = np.stack([letterbox(x, new_shape=self.img_size)[0].shape for x in self.images], 0)  # inference shapes
        self.rect = np.unique(s, axis=0).shape[0] == 1  # rect inference if all shapes equal
        if not self.rect:
            print("WARNING: Different stream shapes detected. For optimal performance supply similarly-shaped streams.")

    def update(self, index, cap):
        """Update a single stream."""
        n = 0
        while cap.isOpened():
            n += 1
            # _, self.images[index] = cap.read()
            cap.grab()
            if n == 4:  # read every 4th frame
                _, self.images[index] = cap.retrieve()
                n = 0
            time.sleep(0.01)  # wait time

    def __iter__(self):
        """Iterate over the images."""
        self.count = -1
        return self

    def __next__(self):
        """Get the next image."""
        self.count += 1
        raw_image = self.images.copy()
        if cv2.waitKey(1) == ord("q"):  # q to quit
            cv2.destroyAllWindows()
            raise StopIteration

        # Letterbox
        image = [letterbox(x, new_shape=self.img_size, auto=self.rect)[0] for x in raw_image]

        # Stack
        image = np.stack(image, 0)

        # Convert BGR to RGB
        image = image[:, :, :, ::-1].transpose(0, 3, 1, 2)
        image = np.ascontiguousarray(image)

        # RGB numpy convert RGB tensor
        image = torch.from_numpy(image)

        if self.gray:
            # RGB tensor convert GRAY tensor
            image = F_vision.rgb_to_grayscale(image)

        return self.sources, image, raw_image, None

    def __len__(self):
        """Number of images in the dataset."""
        return 0  # 1E12 frames = 32 streams at 30 FPS for 30 years


class LoadDatasets(Dataset):
    def __init__(
            self,
            path: str,
            img_size: int = 416,
            batch_size: int = 16,
            augment: bool = False,
            augment_dict: Any = None,
            rect_label: bool = False,
            img_weights: bool = False,
            cache_images: bool = False,
            single_classes: bool = False,
            pad: float = 0.0,
            gray: bool = False,
    ) -> None:
        """Load images and labels.

        Args:
            path (str): The path to the images.
            img_size (int, optional): The size of the images. Defaults: 416.
            batch_size (int, optional): The size of the batch. Defaults: 16.
            augment (bool, optional): Whether to augment the images. Defaults: ``False``.
            augment_dict (Any, optional): The image augment configure. Defaults: None.
            rect_label (bool, optional): Whether to use rectangular trainning. Defaults: ``False``.
            img_weights (bool, optional): Whether to use image weights. Defaults: ``False``.
            cache_images (bool, optional): Whether to cache the images. Defaults: ``False``.
            single_classes (bool, optional): Whether to use single class. Defaults: ``False``.
            pad (float, optional): The padding. Defaults: 0.0.
            gray (bool, optional): Whether to use grayscale. Defaults: ``False``.

        """
        try:
            path = str(Path(path))  # os-agnostic
            parent = str(Path(path).parent) + os.sep
            if os.path.isfile(path):  # file
                with open(path, "r") as f:
                    f = f.read().splitlines()
                    f = [x.replace("./", parent) if x.startswith("./") else x for x in f]  # local to global path
            elif os.path.isdir(path):  # folder
                f = glob.iglob(path + os.sep + "*.*")
            else:
                raise Exception(f"{path} does not exist")
            self.image_files = [x.replace("/", os.sep) for x in f if
                                x.split(".")[-1].lower() in IMG_FORMATS]
        except:
            raise Exception(f"Error loading data from {path}")

        num_images = len(self.image_files)
        assert num_images > 0, f"No images found in {path}"
        batch_index = np.floor(np.arange(num_images) / batch_size).astype(np.int16)  # batch index
        nb = batch_index[-1] + 1  # number of batches

        self.num_images = num_images  # number of images
        self.batch_index = batch_index  # batch index of image
        self.img_size = img_size
        self.augment = augment
        self.augment_dict = augment_dict
        self.image_weights = img_weights
        self.rect_label = False if img_weights else rect_label
        self.mosaic = self.augment and not self.rect_label  # load 4 images at a time into a mosaic (only during training)
        self.gray = gray

        # Define labels
        self.label_files = [x.replace("images", "labels").replace(os.path.splitext(x)[-1], ".txt")
                            for x in self.image_files]

        # Read image shapes (wh)
        sp = path.replace(".txt", "") + ".shapes"  # shapefile path
        try:
            with open(sp, "r") as f:  # read existing shapefile
                s = [x.split() for x in f.read().splitlines()]
                assert len(s) == num_images, "Shapefile out of sync"
        except:
            s = [self._exif_size(Image.open(f)) for f in tqdm(self.image_files, desc="Reading image shapes")]

        self.shapes = np.asarray(s, dtype=np.float64)

        # Rectangular Training  https://github.com/ultralytics/yolov3/issues/232
        if self.rect_label:
            # Sort by aspect ratio
            s = self.shapes  # wh
            aspect_ratio = s[:, 1] / s[:, 0]  # aspect ratio
            index_rect = aspect_ratio.argsort()
            self.image_files = [self.image_files[i] for i in index_rect]
            self.label_files = [self.label_files[i] for i in index_rect]
            self.shapes = s[index_rect]  # wh
            aspect_ratio = aspect_ratio[index_rect]

            # Set training image shapes
            shapes = [[1, 1]] * nb
            for i in range(nb):
                ari = aspect_ratio[batch_index == i]
                mini, maxi = ari.min(), ari.max()
                if maxi < 1:
                    shapes[i] = [maxi, 1]
                elif mini > 1:
                    shapes[i] = [1, 1 / mini]

            self.batch_shapes = np.ceil(np.array(shapes) * img_size / 32. + pad).astype(np.int16) * 32

        # Cache labels
        self.images = [None] * num_images
        self.labels = [np.zeros((0, 5), dtype=np.float32)] * num_images
        create_data_subset, extract_bounding_boxes, labels_loaded = False, False, False
        nm, nf, ne, ns, nd = 0, 0, 0, 0, 0  # number missing, found, empty, datasubset, duplicate
        s = path.replace("images", "labels")

        pbar = tqdm(self.label_files)
        for i, file in enumerate(pbar):
            if labels_loaded:
                labels = self.labels[i]
            else:
                try:
                    with open(file, "r") as f:
                        labels = np.asarray([x.split() for x in f.read().splitlines()], dtype=np.float32)
                except:
                    nm += 1
                    continue

            if labels.shape[0]:
                assert labels.shape[1] == 5, f"> 5 label columns: {file}"
                assert (labels >= 0).all(), f"negative labels: {file}"
                assert (labels[:, 1:] <= 1).all(), f"non-normalized or out of bounds coordinate labels: {file}"
                if np.unique(labels, axis=0).shape[0] < labels.shape[0]:  # duplicate rows
                    nd += 1
                if single_classes:
                    labels[:, 0] = 0  # force dataset into single-class mode
                self.labels[i] = labels
                nf += 1  # file found

                # Create sub dataset (a smaller dataset)
                if create_data_subset and ns < 1E4:
                    if ns == 0:
                        os.makedirs(os.path.join("samples", "data_subset", "images"), exist_ok=True)
                    exclude_classes = 43
                    if exclude_classes not in labels[:, 0]:
                        ns += 1
                        with open(os.path.join("data_subset", "images.txt"), "a") as f:
                            f.write(self.image_files[i] + "\n")

                # Extract object detection boxes for a second stage classifier
                if extract_bounding_boxes:
                    p = Path(self.image_files[i])
                    image = cv2.imread(str(p))
                    h, w = image.shape[:2]
                    for j, x in enumerate(labels):
                        f = "%s%sclassifier%s%g_%g_%s" % (p.parent.parent, os.sep, os.sep, x[0], j, p.name)
                        if not os.path.exists(Path(f).parent):
                            os.makedirs(Path(f).parent, exist_ok=True)  # make new output folder

                        b = x[1:] * [w, h, w, h]  # box
                        b[2:] = b[2:].max()  # rectangle to square
                        b[2:] = b[2:] * 1.3 + 30  # pad
                        b = xywh2xyxy(b.reshape(-1, 4)).ravel().astype(np.int16)

                        b[[0, 2]] = np.clip(b[[0, 2]], 0, w)  # clip boxes outside of image
                        b[[1, 3]] = np.clip(b[[1, 3]], 0, h)
                        assert cv2.imwrite(f, image[b[1]:b[3], b[0]:b[2]]), "Failure extracting classifier boxes"
            else:
                ne += 1  # file empty

            pbar.desc = f"Caching labels {s} ({nf} found, {nm} missing, {ne} empty, {nd} duplicate, for {num_images} images)"
        assert nf > 0 or num_images == 20288, f"No labels found in {os.path.dirname(file) + os.sep}."

        # Cache images into memory for faster training (WARNING: large data may exceed system RAM)
        if cache_images:  # if training
            gb = 0  # Gigabytes of cached images
            pbar = tqdm(range(len(self.image_files)), desc="Caching images")
            self.image_hw0, self.image_hw = [None] * num_images, [None] * num_images
            for i in pbar:  # max 10k images
                self.images[i], self.image_hw0[i], self.image_hw[i] = self.load_image(i)
                gb += self.images[i].nbytes
                pbar.desc = f"Caching images ({gb / 1e9:.1f}GB)"

        detect_corrupted_images = False
        if detect_corrupted_images:
            from skimage import io  # conda install -c conda-forge scikit-image
            for file in tqdm(self.image_files, desc="Detecting corrupted images"):
                try:
                    _ = io.imread(file)
                except:
                    print(f"Corrupted image detected: {file}")

    @staticmethod
    def _exif_size(img: Image.Image) -> tuple:
        """Get the size of an image from its EXIF data.

        Args:
            img (Image.Image): The image to get the size from.

        Returns:
            image_size (tuple): The size of the image.
        """

        # Returns exif-corrected PIL size
        img_size = img.size  # (width, height)
        try:
            rotation = dict(img._getexif().items())[orientation]
            if rotation == 6:  # rotation 270
                img_size = (img_size[1], img_size[0])
            elif rotation == 8:  # rotation 90
                img_size = (img_size[1], img_size[0])
        except:
            pass

        return img_size

    def load_image(self, index: int) -> Tuple[np.ndarray, Tuple[int, int], Tuple[int, int]]:
        """Loads an image from a file into a numpy array.

        Args:
            self: Dataset object
            index (int): Index of the image to load

        Returns:
            image (np.ndarray): Image as a numpy array

        """
        # loads 1 image from dataset, returns image, original hw, resized hw
        image = self.images[index]
        if image is None:  # not cached
            path = self.image_files[index]
            image = cv2.imread(path)  # BGR
            assert image is not None, "Image Not Found " + path
            h0, w0 = image.shape[:2]  # orig hw
            r = self.img_size / max(h0, w0)  # resize image to image_size
            if r != 1:  # always resize down, only resize up if training with augmentation
                interp = cv2.INTER_AREA if r < 1 and not self.augment else cv2.INTER_LINEAR
                image = cv2.resize(image, (int(w0 * r), int(h0 * r)), interpolation=interp)
            return image, (h0, w0), image.shape[:2]  # image, hw_original, hw_resized
        else:
            return self.images[index], self.image_hw0[index], self.image_hw[index]  # image, hw_original, hw_resized

    def load_mosaic(self, index: int) -> Tuple[np.ndarray, List]:
        """loads images in a mosaic

        Args:
            self: Dataset object
            index (int): Index of the image to load

        Returns:
            image (ndarray): Image as a numpy array

        """
        # loads images in a mosaic
        labels4 = []
        s = self.img_size
        xc, yc = [int(random.uniform(s * 0.5, s * 1.5)) for _ in range(2)]  # mosaic center x, y
        indices = [index] + [random.randint(0, len(self.labels) - 1) for _ in range(3)]  # 3 additional image indices
        for i, index in enumerate(indices):
            # Load image
            image, _, (h, w) = self.load_image(index)

            # place image in image4
            if i == 0:  # top left
                image4 = np.full((s * 2, s * 2, image.shape[2]), 114, dtype=np.uint8)  # base image with 4 tiles
                x1a, y1a, x2a, y2a = max(xc - w, 0), max(yc - h, 0), xc, yc  # xmin, ymin, xmax, ymax (large image)
                x1b, y1b, x2b, y2b = w - (x2a - x1a), h - (y2a - y1a), w, h  # xmin, ymin, xmax, ymax (small image)
            elif i == 1:  # top right
                x1a, y1a, x2a, y2a = xc, max(yc - h, 0), min(xc + w, s * 2), yc
                x1b, y1b, x2b, y2b = 0, h - (y2a - y1a), min(w, x2a - x1a), h
            elif i == 2:  # bottom left
                x1a, y1a, x2a, y2a = max(xc - w, 0), yc, xc, min(s * 2, yc + h)
                x1b, y1b, x2b, y2b = w - (x2a - x1a), 0, max(xc, w), min(y2a - y1a, h)
            elif i == 3:  # bottom right
                x1a, y1a, x2a, y2a = xc, yc, min(xc + w, s * 2), min(s * 2, yc + h)
                x1b, y1b, x2b, y2b = 0, 0, min(w, x2a - x1a), min(y2a - y1a, h)

            image4[y1a:y2a, x1a:x2a] = image[y1b:y2b, x1b:x2b]  # image4[ymin:ymax, xmin:xmax]
            padw = x1a - x1b
            padh = y1a - y1b

            # Labels
            x = self.labels[index]
            labels = x.copy()
            if x.size > 0:  # Normalized xywh to pixel xyxy format
                labels[:, 1] = w * (x[:, 1] - x[:, 3] / 2) + padw
                labels[:, 2] = h * (x[:, 2] - x[:, 4] / 2) + padh
                labels[:, 3] = w * (x[:, 1] + x[:, 3] / 2) + padw
                labels[:, 4] = h * (x[:, 2] + x[:, 4] / 2) + padh
            labels4.append(labels)

        # Concat/clip labels
        if len(labels4):
            labels4 = np.concatenate(labels4, 0)
            np.clip(labels4[:, 1:], 0, 2 * s, out=labels4[:, 1:])  # use with random_affine

        # Augment
        image4, labels4 = random_affine(image4,
                                        labels4,
                                        degrees=int(self.augment_dict["DEGREES"]),
                                        translate=float(self.augment_dict["TRANSLATE"]),
                                        scale=float(self.augment_dict["SCALE"]),
                                        shear=int(self.augment_dict["SHEAR"]),
                                        border=-s // 2)  # border to remove

        return image4, labels4

    def __len__(self):
        """Number of images."""
        return len(self.image_files)

    def __getitem__(self, index: int):
        """Returns the image and label at the specified index."""
        if self.image_weights:
            index = self.indices[index]

        if self.mosaic:
            # Load mosaic
            image, labels = self.load_mosaic(index)
            shapes = None

        else:
            # Load image
            image, (h0, w0), (h, w) = self.load_image(index)

            # Letterbox
            shape = self.batch_shapes[
                self.batch_index[index]] if self.rect_label else self.img_size  # final letterboxed shape
            image, ratio, pad = letterbox(image, shape, auto=False, scaleup=self.augment)
            shapes = (h0, w0), ((h / h0, w / w0), pad)  # for COCO mAP rescaling

            # Load labels
            labels = []
            x = self.labels[index]
            if x.size > 0:
                # Normalized xywh to pixel xyxy format
                labels = x.copy()
                labels[:, 1] = ratio[0] * w * (x[:, 1] - x[:, 3] / 2) + pad[0]  # pad width
                labels[:, 2] = ratio[1] * h * (x[:, 2] - x[:, 4] / 2) + pad[1]  # pad height
                labels[:, 3] = ratio[0] * w * (x[:, 1] + x[:, 3] / 2) + pad[0]
                labels[:, 4] = ratio[1] * h * (x[:, 2] + x[:, 4] / 2) + pad[1]

        if self.augment:
            # Augment image space
            if not self.mosaic:
                image, labels = random_affine(image, labels,
                                              degrees=self.augment_dict["DEGREES"],
                                              translate=self.augment_dict["TRANSLATE"],
                                              scale=self.augment_dict["SCALE"],
                                              shear=self.augment_dict["SHEAR"])

            # Augment colorspace
            image = adjust_hsv(image,
                               h_gain=self.augment_dict["HSV_H"],
                               s_gain=self.augment_dict["HSV_S"],
                               v_gain=self.augment_dict["HSV_V"])

        nL = len(labels)  # number of labels
        if nL:
            # convert xyxy to xywh
            labels[:, 1:5] = xyxy2xywh(labels[:, 1:5])

            # Normalize coordinates 0 - 1
            labels[:, [2, 4]] /= image.shape[0]  # height
            labels[:, [1, 3]] /= image.shape[1]  # width

        if self.augment:
            # random left-right flip
            if self.augment_dict["USE_LR_FLIP"] and random.random() < 0.5:
                image = np.fliplr(image)
                if nL:
                    labels[:, 1] = 1 - labels[:, 1]

            # random up-down flip
            if self.augment_dict["USE_UD_FLIP"] and random.random() < 0.5:
                image = np.flipud(image)
                if nL:
                    labels[:, 2] = 1 - labels[:, 2]

        labels_out = torch.zeros((nL, 6))
        if nL:
            labels_out[:, 1:] = torch.from_numpy(labels)

        # Convert
        image = image[:, :, ::-1].transpose(2, 0, 1)  # BGR to RGB, to 3x416x416
        image = np.ascontiguousarray(image)

        # RGB numpy convert RGB tensor
        image = torch.from_numpy(image)

        if self.gray:
            # RGB tensor convert GRAY tensor
            image = F_vision.rgb_to_grayscale(image)

        return image, labels_out, self.image_files[index], shapes

    @staticmethod
    def collate_fn(batch):
        image, label, path, shapes = zip(*batch)  # transposed
        for i, l in enumerate(label):
            l[:, 0] = i  # add target image index for build_targets()
        return torch.stack(image, 0), torch.cat(label, 0), path, shapes
