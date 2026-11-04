"""Train a newer/larger candidate without touching the active screw model."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

import torch
from ultralytics import YOLO

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'outputs/screw-inspection-2026-10-17'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',default='yolo26m')
    parser.add_argument('--epochs',type=int,default=100)
    parser.add_argument('--batch',type=int,default=8)
    parser.add_argument('--imgsz',type=int,default=320)
    args=parser.parse_args()
    out=BASE/'next-models'/args.model
    out.mkdir(parents=True,exist_ok=True)
    model=YOLO(str(ROOT/'models'/(args.model+'.pt')))
    start=time.time()
    result=model.train(data=str(BASE/'refinement/dataset/dataset.yaml'),epochs=args.epochs,imgsz=args.imgsz,
        batch=args.batch,device=0,workers=0,seed=20261017,project=str(out),name='training',exist_ok=True,
        optimizer='AdamW',lr0=.0006,patience=0,cos_lr=True,degrees=15,translate=.15,scale=.25,fliplr=.5,
        mosaic=0,mixup=0,hsv_h=.015,hsv_s=.25,hsv_v=.25,close_mosaic=0,plots=True,verbose=False)
    best=out/'training/weights/best.pt'
    exported=ROOT/'models'/('screw-head-v3-'+args.model+'.pt')
    shutil.copy2(best,exported)
    selected=YOLO(str(best))
    metrics=selected.val(data=str(BASE/'refinement/dataset/dataset.yaml'),split='test',imgsz=args.imgsz,
        device=0,workers=0,batch=args.batch,project=str(out),name='test',exist_ok=True,plots=True,verbose=False)
    payload={'model':args.model,'epochs':args.epochs,'batch':args.batch,'input_size':args.imgsz,
        'parameters':sum(p.numel() for p in selected.model.parameters()),
        'seconds':time.time()-start,'device':torch.cuda.get_device_name(0),
        'metrics':{k:float(v) for k,v in metrics.results_dict.items()},
        'model_file':exported.relative_to(ROOT).as_posix(),'model_sha256':hashlib.sha256(exported.read_bytes()).hexdigest(),
        'dataset':'same frozen 237/17/42 corrected image crops as v2',
        'selection':'best checkpoint by validation; test segment already inspected in previous work',
        'scope':'same-video development results; no independent real defect sensitivity'}
    (out/'training-result.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
