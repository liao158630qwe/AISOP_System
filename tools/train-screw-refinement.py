"""Refit a larger detector after auditing real empty-hole label noise.

This is a same-video development experiment. The previous test segment is
already known and is not an independent defect benchmark.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import time

import cv2
from ultralytics import YOLO

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'outputs/screw-inspection-2026-10-17'
OUT=BASE/'refinement'


def prepare():
    dataset=OUT/'dataset'
    if dataset.exists():raise RuntimeError('Refinement dataset already exists; retain it before preparing again')
    provenance=json.loads((BASE/'dataset/provenance.json').read_text(encoding='utf-8'))
    corrections=[];records=[]
    for record in provenance['records']:
        split=record['split'];stem=Path(record['file']).stem
        labels=record['labels']
        # Visual crop audit shows the S09 metal tab is an open hole through
        # these training times. Context-based propagation had labelled it head.
        lines=(BASE/'dataset/labels'/split/(stem+'.txt')).read_text().strip().splitlines()
        corrected=[]
        for label,line in zip(labels,lines):
            if label['id']=='S03' and record['time']<26:
                break  # ambiguous early S03 crops are excluded in full
            if label['id']=='S09' and record['time']<41 or label['id']=='S01' and record['time']<12:
                parts=line.split();parts[0]='1';line=' '.join(parts)
                corrections.append({'file':record['file'],'id':label['id'],'time':record['time'],'from':'screw_head','to':'empty_hole','source':'visual review of an open hole'})
            corrected.append(line)
        else:
            for kind in ['images','labels']:(dataset/kind/split).mkdir(parents=True,exist_ok=True)
            shutil.copy2(BASE/'dataset/images'/split/record['file'],dataset/'images'/split/record['file'])
            (dataset/'labels'/split/(stem+'.txt')).write_text('\n'.join(corrected)+'\n')
            records.append(record)
    source=json.loads((BASE/'source.json').read_text())
    reviewed=json.loads((OUT/'gate-diagnosis.json').read_text(encoding='utf-8'))
    # The following classes were explicitly reviewed in candidate-review.jpg.
    negatives={(0,'S05'),(0,'S09'),(8,'S01'),(12,'S09'),(15,'S09'),(18,'S09'),(20,'S05'),(20,'S09'),(24,'S09'),(28,'S09'),(31,'S09'),(35,'S09')}
    for record in reviewed:
        t=round(record['time'])
        split='train' if t<26 or 32<=t<36 or 41<=t<46 else 'val' if 28<=t<30 or 38<=t<39 else None
        if split is None:continue
        entry=min(source['samples'],key=lambda e:abs(e['time']-record['time']))
        image=cv2.imread(str(BASE/'frames'/entry['file']))
        for target in record['candidate']:
            if target['state']!='PRESENT':continue
            negative=(t,target['id']) in negatives
            # Dense board/reflector targets need complete multi-object labels;
            # retain the original fully labelled crops for those locations.
            if not negative and target['id'] not in ['S01','S02','S11','S12'] and (t,target['id'])!=(35,'S04'):continue
            if not negative and target['score']<.55 and (t,target['id'])!=(45,'S11'):continue
            a,b,c,d=target['detectedBbox'];cx,cy=(a+c)*960,(b+d)*540
            for offset in ([-8,0,8] if split=='train' else [0]):
                x,y=round(cx)-40+offset,round(cy)-40
                crop=image[y:y+80,x:x+80]
                if crop.shape[:2]!=(80,80):continue
                name=f'review-{entry["frame"]}-{target["id"]}-{offset+8}'
                cv2.imwrite(str(dataset/'images'/split/(name+'.jpg')),crop)
                lines=[]
                for neighbor in record['candidate']:
                    if neighbor['state']!='PRESENT' or not neighbor.get('detectedBbox'):continue
                    left,top,right,bottom=[v*s for v,s in zip(neighbor['detectedBbox'],[1920,1080,1920,1080])]
                    left,top,right,bottom=max(left,x),max(top,y),min(right,x+80),min(bottom,y+80)
                    if right-left<3 or bottom-top<3:continue
                    cls=1 if (t,neighbor['id']) in negatives else 0
                    lines.append(f'{cls} {((left+right)/2-x)/80:.6f} {((top+bottom)/2-y)/80:.6f} {(right-left)/80:.6f} {(bottom-top)/80:.6f}')
                (dataset/'labels'/split/(name+'.txt')).write_text('\n'.join(lines)+'\n')
                corrections.append({'file':name,'time':record['time'],'id':target['id'],'class':'empty_hole' if negative else 'screw_head','source':'assistant visual review of real source crop'})
    (dataset/'dataset.yaml').write_text(f'path: {dataset.as_posix()}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: screw_head\n  1: empty_hole\n')
    counts={split:len(list((dataset/'images'/split).glob('*.jpg'))) for split in ['train','val','test']}
    (OUT/'label-audit.json').write_text(json.dumps({'counts':counts,'corrections':corrections,'scope':'same known video, reviewed corrections; not independent human/defect ground truth'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(counts,flush=True)


def train(epochs):
    started=time.time()
    model=YOLO(str(ROOT/'models/yolo11s.pt'))
    model.train(data=str(OUT/'dataset/dataset.yaml'),epochs=epochs,imgsz=256,batch=16,device=0,workers=0,
        seed=20261017,project=str(OUT),name='training-s',exist_ok=True,optimizer='AdamW',lr0=.0006,
        patience=0,cos_lr=True,degrees=15,translate=.15,scale=.25,fliplr=.5,mosaic=0,mixup=0,
        hsv_h=.015,hsv_s=.25,hsv_v=.25,close_mosaic=0,plots=True,verbose=False)
    best=OUT/'training-s/weights/best.pt'
    shutil.copy2(best,ROOT/'models/screw-head-v2s.pt')
    detector=YOLO(str(best))
    metrics=detector.val(data=str(OUT/'dataset/dataset.yaml'),split='test',imgsz=256,device=0,workers=0,
        project=str(OUT),name='test-s',exist_ok=True,verbose=False)
    (OUT/'training-result.json').write_text(json.dumps({'model':'YOLO11s','epochs':epochs,'input_size':256,
        'seconds':time.time()-started,'metrics':{k:float(v) for k,v in metrics.results_dict.items()},
        'scope':'same-video already-inspected development test; not independent missing-screw sensitivity'},indent=2),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--train',action='store_true');p.add_argument('--epochs',type=int,default=100);a=p.parse_args()
    if a.prepare:prepare()
    if a.train:train(a.epochs)
