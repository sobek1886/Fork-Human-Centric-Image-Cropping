import os
import sys
import numpy as np
from tensorboardX import SummaryWriter
import torch
import time
import datetime
import csv
from tqdm import tqdm
import shutil
import pickle
from scipy.stats import spearmanr, pearsonr
import torch.backends.cudnn as cudnn
import math
import torchvision.transforms as transforms
from PIL import Image, ImageDraw

from cropping_dataset import FCDBDataset, FLMSDataset, GAICDataset, generate_target_size_crop_mask
from config_GAICD import cfg
from cropping_model import HumanCentricCroppingModel
from BBox_adjusting import RegionDetector
from BBox_adjusting import BoundingBox
from outpaint import OutpaintingFeature
from super_gradients.common.object_names import Models
from super_gradients.training import models
#from outpaint import outpaint_image

device = torch.device('cuda:{}'.format(cfg.gpu_id))
torch.cuda.set_device(cfg.gpu_id)
IMAGE_NET_MEAN = [0.485, 0.456, 0.406]
IMAGE_NET_STD = [0.229, 0.224, 0.225]

def compute_acc(gt_scores, pr_scores):
    assert (len(gt_scores) == len(pr_scores)), '{} vs. {}'.format(len(gt_scores), len(pr_scores))
    sample_cnt = 0
    acc4_5  = [0 for i in range(4)]
    acc4_10 = [0 for i in range(4)]
    for i in range(len(gt_scores)):
        gts, preds = gt_scores[i], pr_scores[i]
        id_gt = sorted(range(len(gts)), key=lambda j : gts[j], reverse=True)
        id_pr = sorted(range(len(preds)), key=lambda j : preds[j], reverse=True)
        for k in range(4):
            temp_acc4_5  = 0.
            temp_acc4_10 = 0.
            for j in range(k+1):
                if gts[id_pr[j]] >= gts[id_gt[4]]:
                    temp_acc4_5 += 1.0
                if gts[id_pr[j]] >= gts[id_gt[9]]:
                    temp_acc4_10 += 1.0
            acc4_5[k]  += (temp_acc4_5 / (k+1.0))
            acc4_10[k] += ((temp_acc4_10) / (k+1.0))
        sample_cnt += 1
    acc4_5  = [i / sample_cnt for i in acc4_5]
    acc4_10 = [i / sample_cnt for i in acc4_10]
    # print('acc4_5', acc4_5)
    # print('acc4_10', acc4_10)
    avg_acc4_5  = sum(acc4_5)  / len(acc4_5)
    avg_acc4_10 = sum(acc4_10) / len(acc4_10)
    return avg_acc4_5, avg_acc4_10

'''def compute_acc(gt_scores, pr_scores):
    assert (len(gt_scores) == len(pr_scores)), '{} vs. {}'.format(len(gt_scores), len(pr_scores))
    sample_cnt = 0
    acc1_5  = [0 for i in range(1)]
    acc1_10 = [0 for i in range(1)]
    for i in range(len(gt_scores)):
        gts, preds = gt_scores[i], pr_scores[i]
        id_gt = sorted(range(len(gts)), key=lambda j : gts[j], reverse=True)
        id_pr = sorted(range(len(preds)), key=lambda j : preds[j], reverse=True)
        for k in range(1):
            temp_acc1_5  = 0.
            temp_acc1_10 = 0.
            for j in range(k+1):
                if gts[id_pr[j]] >= gts[id_gt[4]]:
                    temp_acc1_5 += 1.0
                if gts[id_pr[j]] >= gts[id_gt[9]]:
                    temp_acc1_10 += 1.0
            acc1_5[k]  += (temp_acc1_5 / (k+1.0))
            acc1_10[k] += ((temp_acc1_10) / (k+1.0))
        sample_cnt += 1
    acc1_5  = [i / sample_cnt for i in acc1_5]
    acc1_10 = [i / sample_cnt for i in acc1_10]
    # print('acc4_5', acc4_5)
    # print('acc4_10', acc4_10)
    avg_acc1_5  = sum(acc1_5)  / len(acc1_5)
    avg_acc1_10 = sum(acc1_10) / len(acc1_10)
    return avg_acc1_5, avg_acc1_10'''

def compute_iou_and_disp(gt_crop, pre_crop, im_w, im_h):
    ''''
    :param gt_crop: [[x1,y1,x2,y2]]
    :param pre_crop: [[x1,y1,x2,y2]]
    :return:
    '''
    gt_crop = gt_crop[gt_crop[:,0] >= 0]
    zero_t  = torch.zeros(gt_crop.shape[0])
    over_x1 = torch.maximum(gt_crop[:,0], pre_crop[:,0])
    over_y1 = torch.maximum(gt_crop[:,1], pre_crop[:,1])
    over_x2 = torch.minimum(gt_crop[:,2], pre_crop[:,2])
    over_y2 = torch.minimum(gt_crop[:,3], pre_crop[:,3])
    over_w  = torch.maximum(zero_t, over_x2 - over_x1)
    over_h  = torch.maximum(zero_t, over_y2 - over_y1)
    inter   = over_w * over_h
    area1   = (gt_crop[:,2] - gt_crop[:,0]) * (gt_crop[:,3] - gt_crop[:,1])
    area2   = (pre_crop[:,2] - pre_crop[:,0]) * (pre_crop[:,3] - pre_crop[:,1])
    union   = area1 + area2 - inter
    iou     = inter / union
    disp    = (torch.abs(gt_crop[:, 0] - pre_crop[:, 0]) + torch.abs(gt_crop[:, 2] - pre_crop[:, 2])) / im_w + \
              (torch.abs(gt_crop[:, 1] - pre_crop[:, 1]) + torch.abs(gt_crop[:, 3] - pre_crop[:, 3])) / im_h
    iou_idx = torch.argmax(iou, dim=-1)
    dis_idx = torch.argmin(disp, dim=-1)
    index   = dis_idx if (iou[iou_idx] == iou[dis_idx]) else iou_idx
    return iou[index].item(), disp[index].item()


def evaluate_on_GAICD(model, only_human=True, make_square = False, make_square_type = 'naive', user_study = False, subjects_preserving = False):
    model.eval()
    print('='*5, 'Evaluating on GAICD dataset', '='*5)
    srcc_list = []
    gt_scores = []
    pr_scores = []
    count = 0
    test_dataset = GAICDataset(only_human_images=only_human,
                               split='test',
                               keep_aspect_ratio=cfg.keep_aspect_ratio, only_square_anchors = make_square, user_study = user_study)
    test_loader  = torch.utils.data.DataLoader(
                        test_dataset, batch_size=1,
                        shuffle=False, num_workers=cfg.num_workers,
                        drop_last=False)
    device = next(model.parameters()).device
    with torch.no_grad():
        for batch_idx, batch_data in enumerate(tqdm(test_loader)):
            im        = batch_data[0].to(device)
            crop      = batch_data[1].to(device)
            humanbox  = batch_data[2].to(device)
            heat_map  = batch_data[3]
            crop_mask = batch_data[4].to(device)
            part_mask = batch_data[5].to(device)
            if not user_study:
              scores    = batch_data[6].reshape(-1).numpy().tolist()
            width     = batch_data[7]
            height    = batch_data[8]
            resized_image = batch_data[9]
            resized_image = resized_image.cpu().detach().numpy()
            resized_image = np.squeeze(resized_image)
            resized_image = Image.fromarray(resized_image.astype('uint8'), 'RGB')
            count    += im.shape[0]

            part_feat, heat_map, pre_scores = model(im, crop, humanbox, crop_mask, part_mask)
            pre_scores = pre_scores.cpu().detach().numpy().reshape(-1)
            
            idx = np.argmax(pre_scores)
            crop = crop.cpu().detach().numpy()

            crop = np.squeeze(crop)
            pred_x1 = int(crop[idx][0])
            pred_y1 = int(crop[idx][1])
            pred_x2 = int(crop[idx][2])
            pred_y2 = int(crop[idx][3])

            if subjects_preserving == True:
              # get most important region for bbox adjusting
              most_important_region = [pred_x1, pred_y1, pred_x2, pred_y2]
              region_detector = RegionDetector(resized_image, make_square)
              YOLO_predictions = region_detector.YOLO_prediction()
              detected_objects = region_detector.detect_objects(YOLO_predictions)
              most_important_region = BoundingBox(most_important_region)
              objects_in_region = region_detector.determine_objects_in_region(detected_objects, most_important_region)
              expanded_region = region_detector.adjust_most_important_region(objects_in_region, most_important_region)
              pred_x1, pred_y1, pred_x2, pred_y2 = expanded_region
            
            cropped_image = resized_image.crop((pred_x1, pred_y1, pred_x2, pred_y2))

            if make_square:
              #check if the image is not square already
              w,h = cropped_image.size
              if w/h != 1:
                if make_square_type == 'outpaint':
                  Outpainter = OutpaintingFeature(cropped_image, num_samples = 3)
                  cropped_image = Outpainter.outpaint_image()
  
            # Check for existing files
            original_dir = cfg.original_dir
            cropped_dir = cfg.cropped_dir

            base_filename = '1.png'
            counter = 1
            filename = base_filename

            while os.path.exists(os.path.join(original_dir, filename)):
                counter += 1
                filename = f'{counter}.png'
            
            original_path = os.path.join(original_dir, filename)
            crop_path = os.path.join(cfg.cropped_dir, filename)

            # resize to desired size
            desired_size = cfg.image_size
            resized_image.thumbnail(desired_size)
            cropped_image.thumbnail(desired_size)
            # Save the image
            resized_image.save(original_path)
            cropped_image.save(crop_path)

            if not user_study:
              srcc_list.append(spearmanr(scores, pre_scores)[0])
              gt_scores.append(scores)
              pr_scores.append(pre_scores)
    if not user_study:
      srcc = sum(srcc_list) / len(srcc_list)
      acc5, acc10 = compute_acc(gt_scores, pr_scores)
      print('Test on GAICD {} images, SRCC={:.3f}, acc5={:.3f}, acc10={:.3f}'.format(
          count, srcc, acc5, acc10
      ))
      return srcc, acc5, acc10
    return None

def get_pdefined_anchor():
    # get predefined boxes(x1, y1, x2, y2)
    pdefined_anchors = np.array(pickle.load(open(cfg.predefined_pkl, 'rb'), encoding='iso-8859-1')).astype(np.float32)
    print('num of pre-defined anchors: ', pdefined_anchors.shape)
    return pdefined_anchors

'''def get_pdefined_anchor(make_square):
    # get predefined boxes(x1, y1, x2, y2)
    pdefined_anchors = np.array(pickle.load(open(cfg.predefined_pkl, 'rb'), encoding='iso-8859-1')).astype(np.float32)
    if make_square == True:
      filtered_indexes = [index for index, coord in enumerate(pdefined_anchors) if np.isclose(coord[2] - coord[0], coord[3] - coord[1])]
      filtered_anchors = pdefined_anchors[filtered_indexes]
      print('only square anchors')
      print('num of pre-defined anchors: ', filtered_anchors.shape)
      return filtered_anchors
    else:
      print('num of pre-defined anchors: ', pdefined_anchors.shape)
      return pdefined_anchors'''

def get_pdefined_anchor_v1(im_w, im_h):
    bins = 12.0
    step_h = im_h / bins
    step_w = im_w / bins
    pdefined_anchors = []
    for x1 in range(0,4):
        for y1 in range(0,4):
            for x2 in range(8,12):
                for y2 in range(8,12):
                    if (x2 - x1) * (y2 - y1) > 0.4999 * bins * bins and (y2 - y1) * step_w / (
                            x2 - x1) / step_h > 0.5 and (y2 - y1) * step_w / (x2 - x1) / step_h < 2.0:
                        x1 = float(step_h*(0.5+x1)) / im_w
                        y1 = float(step_w*(0.5+y1)) / im_h
                        x2 = float(step_h * (0.5 + x2)) / im_w
                        y2 = float(step_w*(0.5+y2)) / im_h
                        pdefined_anchors.append([x1, y1, x2, y2])
    pdefined_anchors = np.array(pdefined_anchors).reshape(-1,4)
    print('num of pre-defined anchors: ', pdefined_anchors.shape)
    return pdefined_anchors

def evaluate_on_FCDB_and_FLMS(model, dataset='both', only_human=True):
    from config_CPC import cfg
    model.eval()
    device = next(model.parameters()).device
    pdefined_anchors = get_pdefined_anchor() # n,4, (x1,y1,x2,y2)
    accum_disp = 0
    accum_iou  = 0
    crop_cnt = 0
    alpha = 0.75
    alpha_cnt = 0
    cnt = 0

    print('=' * 5, 'Evaluating on FCDB&FLMS', '=' * 5)
    with torch.no_grad():
        if dataset == 'FCDB':
            test_set = [FCDBDataset]
        elif dataset == 'FLMS':
            test_set = [FLMSDataset]
        else:
            test_set = [FCDBDataset,FLMSDataset]
        for dataset in test_set:
            test_dataset= dataset(only_human_images=only_human,
                                  keep_aspect_ratio=cfg.keep_aspect_ratio)
            test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=1,
                                                      shuffle=False, num_workers=cfg.num_workers,
                                                      drop_last=False)
            for batch_idx, batch_data in enumerate(tqdm(test_loader)):
                im = batch_data[0].to(device)
                gt_crop = batch_data[1]
                hbox = batch_data[2].to(device)
                part_mask = batch_data[3].to(device)
                width = batch_data[4].item()
                height = batch_data[5].item()

                crop = np.zeros((len(pdefined_anchors), 4), dtype=np.float32)
                crop[:, 0::2] = pdefined_anchors[:, 0::2] * im.shape[-1]
                crop[:, 1::2] = pdefined_anchors[:, 1::2] * im.shape[-2]
                crop_mask = generate_target_size_crop_mask(crop, im.shape[-1], im.shape[-2], 64, 64)

                crop = torch.from_numpy(crop).unsqueeze(0).to(device)  # 1,n,4
                crop_mask = torch.from_numpy(crop_mask).unsqueeze(0).to(device)
                part_feat, heat_map, scores = model(im, crop, hbox, crop_mask, part_mask)
                # get best crop
                scores = scores.reshape(-1).cpu().detach().numpy()
                idx = np.argmax(scores)
                pred_x1 = int(pdefined_anchors[idx][0] * width)
                pred_y1 = int(pdefined_anchors[idx][1] * height)
                pred_x2 = int(pdefined_anchors[idx][2] * width)
                pred_y2 = int(pdefined_anchors[idx][3] * height)
                #print(pred_x1,pred_y1,pred_x2,pred_y2)
                pred_crop = torch.tensor([[pred_x1, pred_y1, pred_x2, pred_y2]])
                gt_crop   = gt_crop.reshape(-1,4)
                #print(pred_crop, gt_crop)

                iou, disp = compute_iou_and_disp(gt_crop, pred_crop, width, height)
                if iou >= alpha:
                    alpha_cnt += 1
                accum_iou += iou
                accum_disp += disp
                cnt += 1

    avg_iou  = accum_iou / cnt
    avg_disp = accum_disp / (cnt * 4.0)
    avg_recall = float(alpha_cnt) / cnt
    print('Test on {} images, IoU={:.4f}, Disp={:.4f}, recall={:.4f}(iou>={:.2f})'.format(
        cnt, avg_iou, avg_disp, avg_recall, alpha
    ))
    return avg_iou, avg_disp

def weight_translate():
    model = HumanCentricCroppingModel(loadweights=False, cfg=cfg)
    model_dir = './experiments/ablation_study/GAICD_PA_CP'

    src_weight_path = os.path.join(model_dir, 'checkpoints_origin')
    tar_weight_path = os.path.join(model_dir, 'checkpoints')
    for file in os.listdir(src_weight_path):
        if not file.endswith('.pth'):
            continue
        weight = os.path.join(src_weight_path, file)
        weight_dict = torch.load(weight)
        model_state_dict = model.state_dict()
        new_state_dict = dict()
        for name,params in weight_dict.items():
            if name in model_state_dict:
                new_state_dict[name] = params
            else:
                if 'p_conv' in name:
                    name = name.replace('p_conv', 'group_conv')
                    new_state_dict[name] = params
                else:
                    print(name)
        try:
            model.load_state_dict(new_state_dict)
            torch.save(new_state_dict, os.path.join(tar_weight_path, file))
            print(f'trans {file} successfully...')
        except:
            print(f'trans {file} failed...')
            break


if __name__ == '__main__':
    from config_GAICD import cfg
    cfg.use_partition_aware = False
    cfg.partition_aware_type = 9
    cfg.use_content_preserve = True
    cfg.content_preserve_type = 'gcn'
    cfg.only_content_preserve = False
    cfg.make_square = True
    cfg.make_square_type = 'naive' #['naive', 'outpaint']
    cfg.subjects_preserving = False

    model = HumanCentricCroppingModel(loadweights=False, cfg=cfg)
    #model.load_state_dict(torch.load('/content/Fork-Human-Centric-Image-Cropping/experiments/GAICD_PA_CP_repeat8/checkpoints/best-human_srcc.pth'))
    #model.load_state_dict(torch.load('/content/Fork-Human-Centric-Image-Cropping/experiments/30epochs/checkpoints/best-srcc.pth'))
    model.load_state_dict(torch.load('/content/Fork-Human-Centric-Image-Cropping/experiments/GAICD_CP/checkpoints/best-srcc.pth'))

    model = model.eval().to(device)

    cfg.create_path_visual()
    evaluate_on_GAICD(model, only_human=False, make_square = True, make_square_type = 'naive', user_study = True, subjects_preserving = False)
    # evaluate_on_GAICD(model, only_human=True)
    # evaluate_on_FCDB_and_FLMS(model, dataset='FCDB&FLMS', only_human=True)
    #evaluate_on_FCDB_and_FLMS(model, dataset='FCDB', only_human=False)
    # evaluate_on_FCDB_and_FLMS(model, dataset='FLMS', only_human=False)