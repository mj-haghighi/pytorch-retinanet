import numpy as np
import torch
import torchvision
import csv
import os
from os import path as osp
import argparse
from typing import Tuple
import collections
from math import inf
import torch.optim as optim
from torchvision import transforms
import shutil

import retinanet.utils
from retinanet import model
from retinanet.dataloader import CocoDataset, CSVDataset, collater, Resizer, AspectRatioBasedSampler, Augmenter, \
    Normalizer
from torch.utils.data import DataLoader
from retinanet import csv_eval
from utils.log_utils import log_history
from prediction import imageloader, predict_boxes
import labeling
from retinanet import utils
from retinanet.settings import NAME, X, Y, ALPHA, LABEL
import visualize

parser = argparse.ArgumentParser(description="Get required values for box prediction and labeling.")
parser.add_argument("-f", "--filename-path", required=True, type=str, dest="filenames_path",
                    help="Path to the file that reads the name of image files")
parser.add_argument("-p", "--partition", required=True, type=str, dest="partition",
                    choices=["supervised", "unsupervised", "validation", "test"], help="which part of file names")
parser.add_argument("-c", "--class-list", type=str, required=True, dest="class_list",
                    help="path to the class_list file")

parser.add_argument("-i", "--image-dir", type=str, required=True, dest="image_dir",
                    help="The directory where images are in.")
parser.add_argument("-e", "--extension", type=str, required=False, dest="ext", default=".jpg",
                    choices=[".jpg", ".png"], help="image extension")
parser.add_argument("-m", "--model", required=True, type=str, dest="model",
                    help="path to the model")
parser.add_argument("-s", "state-dict", required=True, type=str, dest="state_dict",
                    help="pahr to the state_dict")
parser.add_argument("-o", "--output-dir", type=str, required=True, dest="output_dir",
                    help="where to save output")
args = parser.parse_args()


class Training:
    def __init__(self, args):
        self.supervised_file = "./annotations/supervised.csv"
        self.unsupervised_file = "./annotations/unsupervised.csv"
        self.validation_file = "annotations/validation.csv"
        self.class_list_file = "annotations/labels.csv"
        self.corrected_annotations_file = "active_annotations/corrected.csv"
        self.train_file = "active_annotations/train.csv"
        self.class_to_index, self.index_to_class = utils.load_classes(csv_class_list_path=self.class_list_file)
        self.model_path_pattern, self.state_dict_path_pattern = Training.get_model_saving_pattern()

        self.loader = imageloader.CSVDataset(
            filenames_path="annotations/filenames.json",
            partition=args.partition,
            class_list=self.class_list_file,
            images_dir=args.image_dir,
            image_extension=args.ext,
            transform=torchvision.transforms.Compose([imageloader.Normalizer(), imageloader.Resizer()]),
        )

        self.dataset_val = CSVDataset(
            train_file=self.validation_file,
            class_list=self.class_list_file,
            transform=transforms.Compose([Normalizer(), Resizer()]),
            images_dir=args.images_dir,
            image_extension=
            args.ext,
        )

        self.args = args

    @staticmethod
    def get_model_saving_pattern() -> Tuple[str, str]:
        saving_model_dir = "/mnt/2tra/saeedi/models/SaffronNet"
        model_dir = osp.join(saving_model_dir, "model")
        state_dict_dir = osp.join(saving_model_dir, "state_dict")
        if osp.isdir(model_dir):
            shutil.rmtree(model_dir)
        if osp.isdir(state_dict_dir):
            shutil.rmtree(state_dict_dir)
        os.makedirs(model_dir, exist_ok=False)
        os.makedirs(state_dict_dir, exist_ok=False)
        model_path_pattern = osp.join(model_dir, "{0}.pt")
        state_dict_path_pattern = osp.join(state_dict_dir, "{0}.pt")
        return model_path_pattern, state_dict_path_pattern

    def load_annotations(self, path: str) -> np.array:
        assert osp.isfile(path), "File does not exist."
        boxes = list()
        fileIO = open(path, "r")
        reader = csv.reader(fileIO, delimiter=",")
        for row in reader:
            if row[X] == row[Y] == row[ALPHA] == row[LABEL] == "":
                continue
            box = [None, None, None, None, None]
            box[NAME] = float(row[NAME])
            box[X] = float(row[X])
            box[Y] = float(row[Y])
            box[ALPHA] = float(row[ALPHA])
            box[LABEL] = float(self.class_to_index[row[LABEL]])
            boxes.append(box)
        fileIO.close()
        boxes = np.asarray(boxes, dtype=np.float64)
        return np.asarray(boxes[:, [NAME, X, Y, ALPHA, LABEL]], dtype=np.float64)

    @staticmethod
    def detect(dataset, retinanet):
        """ Get the detections from the retinanet using the generator.
        The result is a list of lists such that the size is:
            all_detections[num_images][num_classes] = detections[num_detections, 4 + num_classes]
        # Arguments
            dataset         : The generator used to run images through the retinanet.
            retinanet           : The retinanet to run on the images.
        # Returns
            A list of lists containing the detections for each image in the generator.
        """
        all_detections = list()

        retinanet.eval()

        print("detecting")
        with torch.no_grad():

            for index in range(len(dataset)):
                data = dataset[index]
                scale = data['scale']
                img_name = float(int(data["name"]))

                # run network
                if torch.cuda.is_available():
                    scores, labels, boxes = retinanet(data['img'].permute(
                        2, 0, 1).cuda().float().unsqueeze(dim=0))
                else:
                    scores, labels, boxes = retinanet(
                        data['img'].permute(2, 0, 1).float().unsqueeze(dim=0))
                scores = scores.cpu().numpy()
                labels = labels.cpu().numpy()
                boxes = boxes.cpu().numpy()
                if boxes.shape[0] == 0:
                    continue
                # correct boxes for image scale
                boxes /= scale

                # select detections
                image_boxes = boxes
                image_scores = scores
                image_labels = labels
                img_name_col = np.full(shape=(len(image_scores), 1), fill_value=img_name, dtype=np.int32)
                image_detections = np.concatenate([img_name_col, image_boxes, np.expand_dims(
                    image_scores, axis=1), np.expand_dims(image_labels, axis=1)], axis=1)
                all_detections.extend(image_detections.tolist())
                print('\rimage {0:02d}/{1:02d}'.format(index + 1, len(dataset)), end='')
        print()
        return np.asarray(all_detections, dtype=np.float64)

    @staticmethod
    def get_corrected_and_active_boxes(
            previous_cycle_model_path: str,
            loader: imageloader.CSVDataset,
            corrected_box_annotations_path: str,
            groundtruth_annotations_path: str,
    ) -> Tuple[np.array, np.array]:

        model = torch.load(previous_cycle_model_path)
        pred_boxes = Training.detect(dataset=loader, retinanet=model)
        if osp.isfile(corrected_box_annotations_path):
            previous_corrected_annotations = Training.load_annotations(corrected_box_annotations_path)
            previous_corrected_names = previous_corrected_annotations[:, NAME]
        else:
            previous_corrected_names = np.array(list(), dtype=pred_boxes.dtype)
        uncertain_boxes, noisy_boxes = predict_boxes.split_uncertain_and_noisy(
            boxes=pred_boxes,
            previous_corrected_boxes_names=previous_corrected_names,
        )

        ground_truth_annotations = Training.load_annotations(groundtruth_annotations_path)
        corrected_boxes = labeling.label(all_gts=ground_truth_annotations, all_uncertain_preds=uncertain_boxes)

        corrected_mode = np.full(shape=(corrected_boxes.shape[0], 1),
                                 fill_value=retinanet.utils.ActiveLabelMode.corrected.value,
                                 dtype=corrected_boxes.dtype)
        noisy_mode = np.full(shape=(noisy_boxes.shape[0], 1), fill_value=retinanet.utils.ActiveLabelMode.noisy.value,
                             dtype=noisy_boxes.dtype)
        corrected_boxes = np.concatenate([corrected_boxes[:, [NAME, X, Y, ALPHA, LABEL]], corrected_mode], axis=1)
        noisy_boxes = np.concatenate([noisy_boxes[:, [NAME, X, Y, ALPHA, LABEL]], noisy_mode], axis=1)
        active_boxes = np.concatenate([corrected_boxes, noisy_boxes], axis=0)
        active_boxes = active_boxes[active_boxes[:, NAME].argsort()]
        return corrected_boxes, active_boxes

    def train(self, model_path, state_dict_path):

        corrected_boxes, active_boxes = self.get_corrected_and_active_boxes(
            previous_cycle_model_path=model_path,
            loader=self.loader,
            corrected_box_annotations_path=args.corrected_path,
            groundtruth_annotations_path="annotations/unsupervised.csv")
        labeling.write_active_boxes(boxes=active_boxes, path=self.train_file, class_dict=self.index_to_class)
        labeling.write_corrected_boxes(boxes=corrected_boxes, path=self.corrected_annotations_file,
                                       class_dict=self.index_to_class)

        dataset_train = CSVDataset(
            train_file=self.train_file,
            class_list=self.class_list_file,
            transform=transforms.Compose([Normalizer(), Augmenter(), Resizer()]),
            images_dir=self.args.images_dir,
            image_extension=self.args.ext,
        )
        sampler = AspectRatioBasedSampler(
            dataset_train, batch_size=1, drop_last=False)
        dataloader_train = DataLoader(
            dataset_train, num_workers=3, collate_fn=collater, batch_sampler=sampler)

        # Create the model
        if self.args.depth == 18:
            retinanet = model.resnet18(
                num_classes=dataset_train.num_classes(), pretrained=True)
        elif parser.depth == 34:
            retinanet = model.resnet34(
                num_classes=dataset_train.num_classes(), pretrained=True)
        elif self.args.depth == 50:
            retinanet = model.resnet50(
                num_classes=dataset_train.num_classes(), pretrained=True)
        elif self.args.depth == 101:
            retinanet = model.resnet101(
                num_classes=dataset_train.num_classes(), pretrained=True)
        elif self.args.depth == 152:
            retinanet = model.resnet152(
                num_classes=dataset_train.num_classes(), pretrained=True)
        else:
            raise ValueError(
                'Unsupported model depth, must be one of 18, 34, 50, 101, 152')

        if torch.cuda.is_available():
            retinanet = torch.nn.DataParallel(retinanet.cuda()).cuda()
        else:
            retinanet = torch.nn.DataParallel(retinanet)




