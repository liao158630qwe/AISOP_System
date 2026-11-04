"""Train the reviewed two-video sticker action model and write a candidate.

The interval tables are offline annotation aids only. Runtime code never reads
video timestamps or these tables.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.metrics import classification_report
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from algorithms.sticker_actions import StickerActionDetector,action_crop,action_features,foreground_fraction
from algorithms.video import file_hash

OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
MODEL=ROOT/'models/sticker-actions-v2.joblib'
CANDIDATE=ROOT/'config/sticker-action-candidate.json'
INTERVALS={
    'original':{
        'T01':[(22.75,23.75)],'T02':[(19.75,20.75)],'R01':[(33.5,34.75)],
        'R02':[(34.5,35.25)],'R03':[(25.75,26.25),(39.5,40.0)],
        'R04':[(30.0,30.75),(40.0,41.25)],'R05':[(46.0,47.0)],'R06':[(43.875,44.5)]},
    'new':{
        'T01':[(18.25,18.95)],'T02':[(14.75,16.0)],'R01':[(25.5,26.65)],
        'R02':[(26.5,27.8)],'R03':[(31.0,31.8)],'R04':[(32.0,32.8)],
        'R05':[(37.7,38.4)],'R06':[(34.0,34.8)]}}
SOURCES={
    'original':dict(video=Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4'),
        frames=OUT/'action-frames',samples=OUT/'action-samples.json',seconds=50.0),
    'new':dict(video=Path('C:/Users/Administrator/Downloads/lv_0_20261018150631.mp4'),
        frames=OUT/'new-video-action-frames-v2',samples=OUT/'new-video-action-samples-v2.json',seconds=55.5)}


def prepare(name,source,cfg):
    if source['samples'].is_file() and source['frames'].is_dir():
        records=json.loads(source['samples'].read_text(encoding='utf-8'))
        if records and all((source['frames']/r['file']).is_file() for r in records):
            print(name,'frames cached:',len(records),flush=True);return records
    source['frames'].mkdir(parents=True,exist_ok=True)
    detector=StickerActionDetector({'config':dict(cfg,mode='prepare')})
    cap=cv2.VideoCapture(str(source['video']));fps=cap.get(cv2.CAP_PROP_FPS);records=[];next_sample=0.0
    started=time.perf_counter()
    for index in range(round(source['seconds']*fps)+1):
        t=index/fps
        if t+1e-6<next_sample:
            if not cap.grab():break
            continue
        ok,image=cap.read()
        if not ok:break
        next_sample+=.125
        located=detector.locate(image)
        if not located:continue
        aligned=located[0];filename=f'{t:07.3f}.jpg'
        cv2.imwrite(str(source['frames']/filename),aligned,[cv2.IMWRITE_JPEG_QUALITY,96])
        records.append(dict(time=round(t,4),file=filename))
    cap.release();source['samples'].write_text(json.dumps(records,indent=2),encoding='utf-8')
    print(name,'prepared:',len(records),'in',round(time.perf_counter()-started,1),'s',flush=True)
    return records


def contact_at(t,intervals):return any(a-.035<=t<=b+.035 for a,b in intervals)


def load(cfg):
    records=[]
    for name,source in SOURCES.items():
        for record in prepare(name,source,cfg):records.append(dict(source=name,folder=source['frames'],**record))
    return records


def build(cfg,records):
    reference=cv2.imread(str(ROOT/cfg['reference']));rng=np.random.default_rng(23)
    rows={t['id']:[] for t in cfg['targets']};audit=[]
    for index,record in enumerate(records):
        image=cv2.imread(str(record['folder']/record['file']))
        if image is None:continue
        split='test' if index%5==0 else 'train'
        for target in cfg['targets']:
            crop=action_crop(image,target);ref=action_crop(reference,target)
            fraction=foreground_fraction(crop,ref)
            contact=contact_at(record['time'],INTERVALS[record['source']][target['id']])
            label='contact' if contact else 'clear' if fraction<.12 else 'other'
            rows[target['id']].append(dict(features=action_features(crop,ref),label=label,split=split,
                source=record['source'],time=record['time']))
            audit.append(dict(source=record['source'],time=record['time'],id=target['id'],label=label,
                split=split,foreground=round(fraction,5)))
            if split=='train':
                for _ in range(3 if contact else 1):
                    dx,dy=rng.integers(-4,5,2);scale=rng.uniform(.94,1.06)
                    warp=np.float32([[scale,0,dx],[0,scale,dy]])
                    aug=cv2.warpAffine(image,warp,(image.shape[1],image.shape[0]),borderMode=cv2.BORDER_REFLECT)
                    aug=np.uint8(np.clip(aug.astype(float)*rng.uniform(.9,1.1),0,255))
                    aug_crop=action_crop(aug,target)
                    rows[target['id']].append(dict(features=action_features(aug_crop,ref),label=label,split=split,
                        source=record['source'],time=record['time']))
    return rows,audit


def fit(rows,all_data=False):
    selected=[r for r in rows if all_data or r['split']=='train']
    model=make_pipeline(StandardScaler(),SVC(C=6,kernel='rbf',class_weight='balanced',probability=True,random_state=23))
    model.fit([r['features'] for r in selected],[r['label'] for r in selected]);return model


def train(cfg):
    records=load(cfg);rows,audit=build(cfg,records);models={};metrics={}
    for identity,items in rows.items():
        training=[r for r in items if r['split']=='train'];test=[r for r in items if r['split']=='test']
        evaluated=fit(training)
        metrics[identity]=classification_report([r['label'] for r in test],evaluated.predict([r['features'] for r in test]),
            output_dict=True,zero_division=0)
        models[identity]=fit(items,all_data=True)
        print(identity,'held-out accuracy',round(metrics[identity]['accuracy'],3),flush=True)
    sources=[dict(name=name,video=str(source['video']),sha256=file_hash(source['video'])) for name,source in SOURCES.items()]
    joblib.dump(dict(sites=models,features='foreground-hog-color-v2-180',sources=sources),MODEL)
    result=dict(model='models/sticker-actions-v2.joblib',sha256=file_hash(MODEL),metrics=metrics,
        samples=len(audit),sources=sources,contact_intervals=INTERVALS,
        split='每5帧留出1帧做开发核对；最终模型再用两条已复核视频全部帧拟合',
        limitation='两条正确过程视频的开发训练与同源整段回放，不代表独立工位准确率；运行时不使用视频时间。')
    (OUT/'action-training-result-v2.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'action-label-audit-v2.json').write_text(json.dumps(dict(sources=sources,rows=audit),ensure_ascii=False,indent=2),encoding='utf-8')
    candidate=dict(cfg,version='sticker-action-v2',model='models/sticker-actions-v2.joblib',
        model_name='贴条动作模型 v2 · 双视频动作与顺序',source_video=SOURCES['new']['video'].name,
        source_sha256=file_hash(SOURCES['new']['video']),training_sources=sources,
        training_result='outputs/sticker-inspection-2026-10-17/action-training-result-v2.json',
        scope='观察到对应位置的贴放/按压并在动作结束后确认；先两处三角后六处方形；不核验贴片外观、粘附或实际存在',
        action=dict(contact_threshold=.6,single_contact_threshold=.85,min_contact_seconds=.2,
            contact_frames=2,clear_frames=2,max_gap=1.5,min_motion=2.5,
            strong_contact_threshold=.85,strong_contact_frames=2,other_release_frames=1))
    candidate.pop('material_model',None);candidate.pop('material_bare_threshold',None)
    CANDIDATE.write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding='utf-8')
    print('wrote',MODEL,'and',CANDIDATE,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');args=parser.parse_args()
    cfg=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf-8'))
    if args.prepare:
        for name,source in SOURCES.items():prepare(name,source,cfg)
    train(cfg)
