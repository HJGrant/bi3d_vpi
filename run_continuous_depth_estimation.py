# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

import argparse
import os
import time
import torch
import torchvision.transforms as transforms
from PIL import Image

import models
import cv2
import numpy as np
from util import disp2rgb, str2bool

import random
from gstreamer.gstreamer_base_code import __gstreamer_pipeline

model_names = sorted(name for name in models.__dict__ if name.islower() and not name.startswith("__"))


# Parse Arguments
parser = argparse.ArgumentParser(allow_abbrev=False)

# Experiment Type
parser.add_argument("--arch", type=str, default="bi3dnet_continuous_depth_2D")

#these arguments are left to default, since the params are needed by the models
parser.add_argument("--bi3dnet_featnet_arch", type=str, default="featextractnetspp")
parser.add_argument("--bi3dnet_segnet_arch", type=str, default="segnet2d")
parser.add_argument("--bi3dnet_refinenet_arch", type=str, default="disprefinenet")
parser.add_argument("--bi3dnet_regnet_arch", type=str, default="segregnet3d")
parser.add_argument("--bi3dnet_max_disparity", type=int, default=192)
parser.add_argument("--regnet_out_planes", type=int, default=16)
parser.add_argument("--disprefinenet_out_planes", type=int, default=32)
parser.add_argument("--bi3dnet_disps_per_example_true", type=str2bool, default=True)

# Input
parser.add_argument("--pretrained", type=str, default="/home/itr/Documents/Bi3D/src/kitti15_continuous_depth_no_conf_reg.pth.tar")
parser.add_argument("--img_left", type=str, default="left.jpg")
parser.add_argument("--img_right", type=str, default="right.jpg")
parser.add_argument("--crop_height", type=int, default=1056)
parser.add_argument("--crop_width", type=int, default=1920)
parser.add_argument("--disp_range_min", type=int, default=0)
parser.add_argument("--disp_range_max", type=int, default=192)


args, unknown = parser.parse_known_args()

#initiate gstreamer cams
cam1 = cv2.VideoCapture(__gstreamer_pipeline(camera_id=0, flip_method=0), cv2.CAP_GSTREAMER)
cam2 = cv2.VideoCapture(__gstreamer_pipeline(camera_id=1, flip_method=0), cv2.CAP_GSTREAMER)

#check if the cams opened correctly
if not cam1.isOpened():
        print("Cannot open camera 1")
        exit()

if not cam2.isOpened():
        print("Cannot open camera 2")
        exit()


##############################################################################################################
def main():

    #arguments are saved as dictionaries with the keys as strings and values as defaults passed during initation
    options = vars(args)
    print("==> ALL PARAMETERS")
    for key in options:
        print("{} : {}".format(key, options[key]))

    # Model
    #read in the pretrained model
    if args.pretrained:
        network_data = torch.load(args.pretrained)
    else:
        print("Need an input model")
        exit()


    print("=> using pre-trained model '{}'".format(args.arch))
    model = models.__dict__[args.arch](options, network_data).cuda()

    # Inputs
    #read in the images and transform to tensor and perform normalization
    ret1, frame_left = cam1.read()
    ret2, frame_right = cam2.read()
    img_left = transforms.functional.to_tensor(np.asarray(frame_left))
    img_right = transforms.functional.to_tensor(np.asarray(frame_right))
    img_left = transforms.functional.normalize(img_left, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
    img_right = transforms.functional.normalize(img_right, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
    img_left = img_left.type(torch.cuda.FloatTensor)[None, :, :, :]                                 #i think we are adding a dummy dimension to interpret a image as one batch unit
    img_right = img_right.type(torch.cuda.FloatTensor)[None, :, :, :]

    # Prepare Disparities
    max_disparity = args.disp_range_max
    min_disparity = args.disp_range_min

    #make sure the min and max diparities are dividable by 3
    assert max_disparity % 3 == 0 and min_disparity % 3 == 0, "disparities should be divisible by 3"

    #calculate the max disp levels
    max_disp_levels = (max_disparity - min_disparity) + 1

    #looks like we are defining the downsampling parameters for the model?
    #not quite sure what is happening here
    max_disparity_3x = int(max_disparity / 3)
    min_disparity_3x = int(min_disparity / 3)
    max_disp_levels_3x = (max_disparity_3x - min_disparity_3x) + 1
    disp_3x = np.linspace(min_disparity_3x, max_disparity_3x, max_disp_levels_3x, dtype=np.int32)
    disp_long_3x_main = torch.from_numpy(disp_3x).type(torch.LongTensor).cuda()
    disp_float_main = np.linspace(min_disparity, max_disparity, max_disp_levels, dtype=np.float32)
    disp_float_main = torch.from_numpy(disp_float_main).type(torch.float32).cuda()
    delta = 1
    d_min_GT = min_disparity - 0.5 * delta
    d_max_GT = max_disparity + 0.5 * delta
    disp_long_3x = disp_long_3x_main[None, :].expand(img_left.shape[0], -1)
    disp_float = disp_float_main[None, :].expand(img_left.shape[0], -1)

    # Pad Inputs
    #how should we crop the output image ? 
    #read up on padding ? 
    tw = args.crop_width
    th = args.crop_height
    assert tw % 96 == 0, "image dimensions should be multiple of 96"
    assert th % 96 == 0, "image dimensions should be multiple of 96"
    h = img_left.shape[2]
    w = img_left.shape[3]
    x1 = random.randint(0, max(0, w - tw))
    y1 = random.randint(0, max(0, h - th))
    pad_w = tw - w if tw - w > 0 else 0
    pad_h = th - h if th - h > 0 else 0
    pad_opr = torch.nn.ZeroPad2d((pad_w, 0, pad_h, 0))
    img_left = img_left[:, :, y1 : y1 + min(th, h), x1 : x1 + min(tw, w)]           #dummy dimension, RGB channels, height, width
    img_right = img_right[:, :, y1 : y1 + min(th, h), x1 : x1 + min(tw, w)]
    img_left_pad = pad_opr(img_left)                                                #what is happening here ? 
    img_right_pad = pad_opr(img_right)

    # Inference
    #turn on the evaluation mode of the model
    model.eval()
    with torch.no_grad():
        if args.arch == "bi3dnet_continuous_depth_2D":
            output_seg_low_res_upsample, output_disp_normalized = model(
                img_left_pad, img_right_pad, disp_long_3x
            )
            output_seg = output_seg_low_res_upsample
        else:
            (
                output_seg_low_res_upsample,
                output_seg_low_res_upsample_refined,
                output_disp_normalized_no_reg,
                output_disp_normalized,
            ) = model(img_left_pad, img_right_pad, disp_long_3x)
            output_seg = output_seg_low_res_upsample_refined

        output_seg = output_seg[:, :, pad_h:, pad_w:]
        output_disp_normalized = output_disp_normalized[:, :, pad_h:, pad_w:]
        output_disp = torch.clamp(
            output_disp_normalized * delta * max_disp_levels + d_min_GT, min=d_min_GT, max=d_max_GT
        )

    # Write Results
    max_disparity_color = 192
    output_disp_clamp = output_disp[0, 0, :, :].cpu().clone().numpy()
    output_disp_clamp[output_disp_clamp < min_disparity] = 0
    output_disp_clamp[output_disp_clamp > max_disparity] = max_disparity_color
    disp_np_ours_color = disp2rgb(output_disp_clamp / max_disparity_color) * 255.0
    
    cv2.imshow("DISPARITY", disp_np_ours_color)
    cv2.moveWindow("DISPARITY", 100, 100)
    
    cv2.waitKey(0)

    cv2.destroyAllWindows()

    return


if __name__ == "__main__":
    main()
