import numpy as np
import torch
import torchvision
import csv
import os
from os import path as osp
import argparse
from typing import Tuple
from math import inf
import torch.optim as optim
from torchvision import transforms
import shutil

import retinanet.utils
from retinanet import model
from retinanet.dataloader import CSVDataset, collater, Resizer, AspectRatioBasedSampler, Augmenter, Normalizer
from torch.utils.data import DataLoader
from retinanet import csv_eval
from utils.log_utils import log_history
from prediction import imageloader, predict_boxes
import labeling
from retinanet import utils
from utils.meta_utils import save_models
from retinanet.settings import NAME, X, Y, ALPHA, LABEL


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
            partition="unsupervised",
            class_list=self.class_list_file,
            images_dir=args.image_dir,
            image_extension=args.ext,
            transform=torchvision.transforms.Compose([imageloader.Normalizer(), imageloader.Resizer()]),
        )

        self.dataset_val = CSVDataset(
            train_file=self.validation_file,
            class_list=self.class_list_file,
            transform=transforms.Compose([Normalizer(), Resizer()]),
            images_dir=args.image_dir,
            image_extension=args.ext,
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

    def get_corrected_and_active_boxes(
            self,
            previous_cycle_model_path: str,
    ) -> Tuple[np.array, np.array]:
        groundtruth_annotations_path = self.unsupervised_file
        fileIO = open(previous_cycle_model_path, "rb")
        loaded_model = torch.load(fileIO)
        fileIO.close()
        pred_boxes = Training.detect(dataset=self.loader, retinanet=loaded_model)
        if osp.isfile(self.corrected_annotations_file):
            previous_corrected_annotations = self.load_annotations(self.corrected_annotations_file)
            previous_corrected_names = previous_corrected_annotations[:, NAME]
        else:
            previous_corrected_names = np.array(list(), dtype=pred_boxes.dtype)
        uncertain_boxes, noisy_boxes = predict_boxes.split_uncertain_and_noisy(
            boxes=pred_boxes,
            previous_corrected_boxes_names=previous_corrected_names,
        )

        ground_truth_annotations = self.load_annotations(groundtruth_annotations_path)
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

    def train(self, checkpoint, save_model_path, save_state_dict_path):
        max_mAp = 0
        min_loss = inf

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
        elif self.args.depth == 34:
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

        optimizer = optim.Adam(retinanet.parameters(), lr=1e-5)

        scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, patience=3, verbose=True)
        # checkpoint = torch.load(previous_state_dict_path)
        retinanet.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])

        retinanet.train()
        retinanet.module.freeze_bn()

        print('Num training images: {}'.format(len(dataset_train)))
        loss_hist = []
        for epoch_num in range(self.args.epochs):

            retinanet.train()
            retinanet.module.freeze_bn()

            epoch_loss = []
            epoch_CLASSIFICATION_loss = []
            epoch_XY_REG_loss = []
            epoch_ANGLE_REG_loss = []

            for iter_num, data in enumerate(dataloader_train):
                try:
                    optimizer.zero_grad()
                    if torch.cuda.is_available():
                        classification_loss, xydistance_regression_loss, angle_distance_regression_losses = retinanet(
                            [data['img'].cuda().float(), data['annot']])
                    else:
                        classification_loss, xydistance_regression_loss, angle_distance_regression_losses = retinanet(
                            [data['img'].float(), data['annot']])
                    classification_loss = classification_loss.mean()
                    xydistance_regression_loss = xydistance_regression_loss.mean()
                    angle_distance_regression_losses = angle_distance_regression_losses.mean()

                    loss = classification_loss + xydistance_regression_loss + \
                           angle_distance_regression_losses

                    if bool(loss == 0):
                        continue

                    loss.backward()

                    torch.nn.utils.clip_grad_norm_(retinanet.parameters(), 0.1)

                    optimizer.step()

                    loss_hist.append(float(loss))

                    epoch_loss.append(float(loss))
                    epoch_CLASSIFICATION_loss.append(float(classification_loss))
                    epoch_XY_REG_loss.append(float(xydistance_regression_loss))
                    epoch_ANGLE_REG_loss.append(
                        float(angle_distance_regression_losses))
                    print(
                        'Epoch: {} | Iteration: {} | Classification loss: {:1.5f} | XY Regression loss: {:1.5f} | Angle Regression loss: {:1.5f}| Running loss: {:1.5f}'.format(
                            epoch_num, iter_num, float(classification_loss), float(xydistance_regression_loss),
                            float(angle_distance_regression_losses), loss))

                    del classification_loss
                    del xydistance_regression_loss
                    del angle_distance_regression_losses

                except Exception as e:
                    print(e)
                    continue

            mean_epoch_loss = np.mean(epoch_loss)
            print('Evaluating dataset')
            if min_loss > mean_epoch_loss:
                print("loss improved from {} to {}".format(min_loss, mean_epoch_loss))
                min_loss = mean_epoch_loss

            mAP = csv_eval.evaluate(self.dataset_val, retinanet)
            if mAP[0][0] > max_mAp:
                print('mAp improved from {} to {}'.format(max_mAp, mAP[0][0]))
                max_mAp = mAP[0][0]

                save_models(
                    model_path=save_model_path,
                    state_dict_path=save_state_dict_path,
                    model=retinanet,
                    optimizer=optimizer,
                    scheduler=scheduler,
                    loss=np.mean(epoch_loss),
                    mAP=max_mAp,
                    epoch=epoch_num,
                )

            log_history(epoch_num,
                        {'c-loss': np.mean(epoch_CLASSIFICATION_loss),
                         'rxy-loss': np.mean(epoch_XY_REG_loss),
                         'ra-loss': np.mean(epoch_ANGLE_REG_loss),
                         'mAp': mAP},
                        os.path.join(os.path.dirname(self.args.save_dir), 'history.json'))
            scheduler.step(np.mean(epoch_loss))

        retinanet.eval()
        if self.args.save_dir:
            torch.save(retinanet, os.path.join(self.args.save_dir, 'model_final.pt'))
        else:
            torch.save(retinanet, 'model_final.pt')

    def manage_cycles(self):
        for i in range(1, self.args.num_cycles + 1):
            print("\nCycle {0}\n".format(i))
            if i == 1:
                model_path = self.args.model
                state_dict_path = self.args.state_dict
            else:
                model_path = self.model_path_pattern.format(i - 1)
                state_dict_path = self.state_dict_path_pattern.format(i - 1)

            fileModelIO = open(model_path, "rb")
            fileStateDictIO = open(state_dict_path, "rb")
            loaded_model = torch.load(fileModelIO)
            state_dict = torch.load(fileStateDictIO)
            fileModelIO.close()
            fileStateDictIO.close()

            corrected_boxes, active_boxes = self.get_corrected_and_active_boxes(
                previous_cycle_model_path=loaded_model,
            )

            labeling.write_active_boxes(
                boxes=active_boxes,
                path=self.train_file,
                class_dict=self.index_to_class,
            )

            labeling.write_corrected_boxes(
                boxes=corrected_boxes,
                path=self.corrected_annotations_file,
                class_dict=self.index_to_class,
            )

            self.train(
                checkpoint=state_dict,
                save_model_path=self.model_path_pattern.format(i),
                save_state_dict_path=self.state_dict_path_pattern.format(i),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Get required values for box prediction and labeling.")
    parser.add_argument("-i", "--image-dir", type=str, required=True, dest="image_dir",
                        help="The directory where images are in.")
    parser.add_argument("-e", "--extension", type=str, required=False, dest="ext", default=".jpg",
                        choices=[".jpg", ".png"], help="image extension")
    parser.add_argument("-m", "--model", required=True, type=str, dest="model",
                        help="path to the model")
    parser.add_argument("-s", "--state-dict", required=True, type=str, dest="state_dict",
                        help="path to the state_dict")
    parser.add_argument("-o", "--save-dir", type=str, required=True, dest="save_dir",
                        help="where to save output")
    parser.add_argument("-c", "--num-cycles", type=int, required=True, dest="num_cycles",
                        help="number of active cycles")
    parser.add_argument("-d", "--depth", type=int, required=True, dest="depth",
                        choices=(18, 34, 50, 101, 52), default=50, help="ResNet depth")
    parser.add_argument("-p", "--epochs", type=int, required=True, dest="epochs",
                        default=20, help="Number of Epochs")
    args = parser.parse_args()

    trainer = Training(args=args)
    trainer.manage_cycles()
