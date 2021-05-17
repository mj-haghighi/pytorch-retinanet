import torch
import numpy as np
import time
import os
import csv
import cv2
import argparse
import json
# from retinanet.nms import nms
from utils.visutils import draw_line


def load_classes(csv_reader):
    result = {}

    for line, row in enumerate(csv_reader):
        line += 1

        try:
            class_name, class_id = row
        except ValueError:
            raise(ValueError(
                'line {}: format should be \'class_name,class_id\''.format(line)))
        class_id = int(class_id)

        if class_name in result:
            raise ValueError(
                'line {}: duplicate class name: \'{}\''.format(line, class_name))
        result[class_name] = class_id
    return result


def detect_image(image_dir, filenames, model_path, class_list, output_dir, ext=".jpg"):

    with open(class_list, 'r') as f:
        classes = load_classes(csv.reader(f, delimiter=','))

    labels = {}
    for key, value in classes.items():
        labels[value] = key

    model = torch.load(model_path)

    if torch.cuda.is_available():
        model = model.cuda()

    model.training = False
    model.eval()

    for img_name in filenames:

        image = cv2.imread(os.path.join(image_dir, img_name+ext))
        if image is None:
            continue
        image_orig = image.copy()

        rows, cols, cns = image.shape

        pad_w = 32 - rows % 32
        pad_h = 32 - cols % 32

        new_image = np.zeros(
            (rows + pad_w, cols + pad_h, cns)).astype(np.float32)
        new_image[:rows, :cols, :] = image.astype(np.float32)
        image = new_image.astype(np.float32)

        image = np.expand_dims(image, 0)
        image = np.transpose(image, (0, 3, 1, 2))
        with torch.no_grad():

            image = torch.from_numpy(image)
            if torch.cuda.is_available():
                image = image.cuda()

            st = time.time()

            scores, classification, transformed_anchors = model(
                image.cuda().float())
            print('Elapsed time: {}'.format(time.time() - st))
            idxs = np.where(scores.cpu() > 0.95)
            transformed_anchors = transformed_anchors.cpu().detach().numpy()
            for j in range(idxs[0].shape[0]):
                center_alpha = transformed_anchors[idxs[0][j], :]
                x, y, alpha = int(center_alpha[0]), int(
                    center_alpha[1]), int(center_alpha[2])
                # label_name = labels[int(classification[idxs[0][j]])]
                score = scores[j]
                # caption = '{} {:.3f}'.format(label_name, score)
                image_orig = draw_line(
                    image=image_orig,
                    p=(x, y),
                    alpha=alpha,
                    line_color=(0, 255, 0),
                    center_color=(255, 0, 0),
                    half_line=True)
            cv2.imwrite(
                os.path.join(output_dir, "{0}.jpg".format(img_name)), image_orig)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description='Simple script for visualizing result of training.')

    parser.add_argument(
        '--image_dir', help='Path to directory containing images')
    parser.add_argument('--model_path', help='Path to model')
    parser.add_argument(
        "--path_mod", help="supervised | unsupervised | validation | test")
    parser.add_argument(
        '--class_list', help='Path to CSV file listing class names (see README)')
    parser.add_argument(
        '--output_dir', help='direction for output images')

    parser = parser.parse_args()

    with open("annotations/filenames.json", "r") as fileIO:
        str_names = fileIO.read()
    names = json.loads(str_names)
    assert parser.path_mod in "supervised | unsupervised | validation | test"

    detect_image(
        image_dir=parser.image_dir,
        filenames=names[parser.path_mod],
        model_path=parser.model_path,
        class_list=parser.class_list,
        output_dir=parser.output_dir
    )
