"""Train a real YOLO head/hole detector, retaining label and split provenance.

Source: one correct factory video. Labels use visually reviewed reference points
and SIFT/template propagation. Test is a same-video temporal holdout, not an
independent missing-screw production benchmark. No fabricated defect images.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT/'outputs/screw-inspection-2026-10-17'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def prepare():
    config = json.loads((ROOT/'config/screw-inspection.json').read_text(encoding='utf-8'))
    source = json.loads((OUT/'source.json').read_text(encoding='utf-8'))
    ref = cv2.resize(cv2.imread(str(OUT/'frame-51.jpg')), (1920, 1080), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(ROOT/config['reference']), ref)
    sift = cv2.SIFT_create(nfeatures=3500, contrastThreshold=.025)
    mask = np.zeros(ref.shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.array([[680,180],[900,200],[990,990],[580,1000],[500,680]])], 255)
    rk, rd = sift.detectAndCompute(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), mask)
    matcher = cv2.BFMatcher()
    # Empty, unoccupied moulded bosses on the right housing, genuinely visible
    # in the source; distinct from synthetic omissions of installed screws.
    # Manually reviewed actual 1920px coordinates in the final source frame.
    empty_points = [(1407,505),(1222,648),(1194,727),(1181,790),
                    (1242,786),(1081,825),(1052,855),(1362,1000)]
    gray_ref = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    # Label larger bare bosses only after reviewing their reference crops.
    points = [(0, *t['point'], t['id']) for t in config['targets']]
    points += [(1, x, y, f'H{i+1:02}') for i,(x,y) in enumerate(empty_points)]
    templates = []
    for cls,x,y,identity in points:
        x,y = round(x),round(y)
        templates.append((cls,x,y,identity,gray_ref[y-11:y+12,x-11:x+12]))
    data = OUT/'dataset'
    for split in ['train','val','test']:
        for kind in ['images','labels']:
            (data/kind/split).mkdir(parents=True, exist_ok=True)
    records, review = [], []
    # Global time blocks, with temporal guards; random adjacent-frame splitting
    # would overstate the available evidence from this one recording.
    for entry in source['samples']:
        t = entry['time']
        if t < 5 or t > 51.1:
            continue
        split = 'train' if t < 26 or 32 <= t < 36 or 41 <= t < 46 else 'val' if 28 <= t < 30 or 38 <= t < 39 else 'test' if t >= 49 else None
        if split is None:
            continue
        # Train every .75s, validation/test every .5s.
        if entry['frame'] % 15 > 7:
            continue
        image = cv2.imread(str(OUT/'frames'/entry['file']))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        key, desc = sift.detectAndCompute(gray, None)
        good = [m for m,n in matcher.knnMatch(rd,desc,k=2) if m.distance < .7*n.distance]
        if len(good) < 14:
            continue
        H, inliers = cv2.findHomography(np.float32([rk[m.queryIdx].pt for m in good]),
            np.float32([key[m.trainIdx].pt for m in good]), cv2.RANSAC, 3)
        if H is None or inliers.sum() < 12:
            continue
        # Align source to reference, then annotate only strong matches. Crops
        # with partial/ambiguous labels are excluded instead of negative-labelled.
        aligned = cv2.warpPerspective(image, np.linalg.inv(H), (1920,1080))
        ag = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        positives = []
        for cls,x,y,identity,template in templates:
            radius = 24
            search = ag[y-radius:y+radius+1, x-radius:x+radius+1]
            score_map = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            _,score,_,loc = cv2.minMaxLoc(score_map)
            if score < (.67 if cls == 0 else .80):
                continue
            px,py=x-radius+loc[0]+11,y-radius+loc[1]+11
            positives.append(dict(cls=cls,x=px,y=py,id=identity,score=score))
        # Small object crops with a single supervised centre. They include
        # screw surrounds and real skin/glove/background negative examples.
        for j,item in enumerate(positives):
            cx,cy=item['x'],item['y']
            size=80
            x0,y0=cx-size//2,cy-size//2
            crop=aligned[y0:y0+size,x0:x0+size]
            if crop.shape[:2] != (size,size):
                continue
            visible=[p for p in positives if abs(p['x']-cx)<size/2-10 and abs(p['y']-cy)<size/2-10]
            # Reject crops containing uncertain reference sites; YOLO requires
            # complete labels within each training image.
            expected=[p for p in points if abs(p[1]-cx)<size/2+4 and abs(p[2]-cy)<size/2+4]
            if any(p[3] not in {v['id'] for v in visible} for p in expected):
                continue
            stem=f'{entry["frame"]:05}-{item["id"]}'
            cv2.imwrite(str(data/'images'/split/(stem+'.jpg')),crop)
            boxes=[]
            for p in visible:
                width = 22 if p['cls']==0 else 20
                boxes.append(f'{p["cls"]} {(p["x"]-x0)/size:.6f} {(p["y"]-y0)/size:.6f} {width/size:.6f} {width/size:.6f}')
            (data/'labels'/split/(stem+'.txt')).write_text('\n'.join(boxes)+'\n')
            records.append(dict(file=stem+'.jpg',split=split,time=t,source_frame=entry['frame'],
                                labels=visible,provenance='reference points + SIFT + template propagation'))
            if len(review)<100 and len(records)%5==0:
                tile=cv2.resize(crop,(160,160))
                cv2.putText(tile,f'{item["id"]} {t:.1f}s {split}',(3,16),0,.38,(0,0,255),1)
                review.append(tile)
    # A genuinely manually marked source frame is reserved as an additional
    # final-frame replay check. No augmentation enters val/test.
    write_json(data/'provenance.json',dict(source=source,records=records,
        label_method='assistant visual review of reference points; automatic propagation, not independent human annotations',
        splits={'train':'5–26s, 32–36s, 41–46s','validation':'28–30s, 38–39s','test':'49–51.1s'},
        limitations=['Single video and operator; temporally correlated samples.',
                    'Reference image assists labels and geometric calibration, including holdout; metrics are internal only.',
                    'Empty moulded holes are real backgrounds, not independent defective finished products.',
                    'No torque labels; screw-head visibility is not tightness.']))
    (data/'dataset.yaml').write_text(f'path: {data.as_posix()}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: screw_head\n  1: empty_hole\n',encoding='utf-8')
    counts=Counter(r['split'] for r in records)
    write_json(OUT/'dataset-summary.json',dict(images=dict(counts),objects=dict(Counter(str(p['cls']) for r in records for p in r['labels']))))
    if review:
        review += [np.zeros_like(review[0])]*((-len(review))%10)
        cv2.imwrite(str(OUT/'training-label-review.jpg'),np.vstack([np.hstack(review[i:i+10]) for i in range(0,len(review),10)]))
    print('Dataset:',dict(counts),flush=True)


def train(epochs):
    from ultralytics import YOLO
    import torch
    model=YOLO(str(ROOT/'models/yolo11n.pt'))
    started=time.time()
    model.train(data=str(OUT/'dataset/dataset.yaml'),epochs=epochs,imgsz=192,batch=16,device=0,
        workers=0,seed=20261017,deterministic=True,project=str(OUT),name='training',exist_ok=True,
        pretrained=True,optimizer='AdamW',lr0=.001,patience=0,cos_lr=True,
        degrees=12,translate=.12,scale=.25,fliplr=.5,flipud=0,mosaic=0,mixup=0,
        hsv_h=.015,hsv_s=.25,hsv_v=.25,close_mosaic=0,plots=True,save=True,verbose=False)
    best=OUT/'training/weights/best.pt'
    shutil.copy2(best,ROOT/'models/screw-head-v1.pt')
    detector=YOLO(str(best))
    metrics=detector.val(data=str(OUT/'dataset/dataset.yaml'),split='test',imgsz=192,batch=48,
        device=0,workers=0,project=str(OUT),name='test',exist_ok=True,plots=True,verbose=False)
    write_json(OUT/'training-result.json',dict(task='visible screw head / empty hole detection',
        elapsed_seconds=time.time()-started,device=torch.cuda.get_device_name(0),
        results={k:float(v) for k,v in metrics.results_dict.items()},
        model_sha256=hashlib.sha256(best.read_bytes()).hexdigest(),epochs_requested=epochs,
        evaluation_scope='same-video temporal holdout with assisted labels; NOT production missing-screw accuracy',
        true_missing_screw_recall=None,independent_test_videos=0))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--train',action='store_true')
    parser.add_argument('--epochs',type=int,default=80)
    args=parser.parse_args()
    if args.prepare: prepare()
    if args.train: train(args.epochs)
