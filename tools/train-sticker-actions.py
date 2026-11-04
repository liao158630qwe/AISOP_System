"""Train site contact/clear/other evidence on reviewed video, with provenance.

Intervals below are annotation aids ONLY, never imported by runtime code.
Temporal held-out frames are a development check, not independent accuracy.
"""
import argparse,json,sys,time
from pathlib import Path
import cv2,numpy as np,joblib
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.metrics import classification_report
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.sticker_actions import StickerActionDetector,action_crop,action_features,foreground_fraction
from algorithms.video import file_hash
OUT=ROOT/'outputs/sticker-inspection-2026-10-17';SOURCE=Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4')
CONTACT={'T01':[(22.75,23.75)],'T02':[(19.75,20.75)],'R01':[(33.5,34.75)],'R02':[(34.5,35.25)],
    'R03':[(25.75,26.25),(39.5,40)],'R04':[(30,30.75),(40,41.25)],'R05':[(46,47)],'R06':[(43.875,44.5)]}

def prepare(cfg):
    detector=StickerActionDetector({'config':dict(cfg,mode='prepare')});folder=OUT/'action-frames';folder.mkdir(exist_ok=True)
    cap=cv2.VideoCapture(str(SOURCE));fps=cap.get(5);rows=[];next_sample=12
    for index in range(round(50*fps)):
        t=index/fps
        if t+1e-6<next_sample:
            if not cap.grab():break
            continue
        ok,image=cap.read()
        if not ok:break
        next_sample+=.125;located=detector.locate(image)
        if not located:continue
        aligned,_,_=located;name=f'{t:07.3f}.jpg';cv2.imwrite(str(folder/name),aligned,[cv2.IMWRITE_JPEG_QUALITY,96]);rows.append(dict(time=t,file=name))
    cap.release();(OUT/'action-samples.json').write_text(json.dumps(rows,indent=2));print('prepared',len(rows),flush=True)

def train(cfg):
    rng=np.random.default_rng(17);ref=cv2.imread(str(ROOT/cfg['reference']));data={t['id']:[] for t in cfg['targets']};audit=[]
    for index,record in enumerate(json.loads((OUT/'action-samples.json').read_text())):
        im=cv2.imread(str(OUT/'action-frames'/record['file']));t=record['time'];split='test' if index%4==1 else 'train'
        for target in cfg['targets']:
            crop=action_crop(im,target);reference=action_crop(ref,target);fraction=foreground_fraction(crop,reference)
            contact=any(a-.035<=t<=b+.035 for a,b in CONTACT[target['id']])
            label='contact' if contact else 'clear' if fraction<.12 else 'other'
            data[target['id']].append((action_features(crop,reference),label,split))
            audit.append(dict(time=t,id=target['id'],label=label,split=split,foreground=fraction))
            if split=='train':
                for _ in range(2 if contact else 1):
                    # JPEG, lighting and registration tolerance; source frame
                    # stays in the same split for all its derived samples.
                    dx,dy=rng.integers(-3,4,2);warp=np.float32([[1,0,dx],[0,1,dy]])
                    aug=cv2.warpAffine(crop,warp,(180,180),borderMode=cv2.BORDER_REFLECT)
                    aug=np.uint8(np.clip(aug.astype(float)*rng.uniform(.9,1.1),0,255))
                    data[target['id']].append((action_features(aug,reference),label,split))
    models={};metrics={}
    for identity,rows in data.items():
        training=[r for r in rows if r[2]=='train'];test=[r for r in rows if r[2]=='test']
        model=make_pipeline(StandardScaler(),SVC(C=5,kernel='rbf',class_weight='balanced',probability=True,random_state=17))
        model.fit([r[0] for r in training],[r[1] for r in training]);models[identity]=model
        metrics[identity]=classification_report([r[1] for r in test],model.predict([r[0] for r in test]),output_dict=True,zero_division=0)
        print(identity,metrics[identity]['accuracy'],flush=True)
    model_path=ROOT/'models/sticker-actions-v1.joblib';joblib.dump(dict(sites=models,features='foreground-hog-color-v1',source_sha256=file_hash(SOURCE)),model_path)
    cfg=dict(cfg,mode='action',version='sticker-action-v1',model='models/sticker-actions-v1.joblib',model_name='贴条动作模型 v1 · 位置动作与顺序',
        scope='观察到对应位置的贴放/按压动作即计完成；先两处三角后六处方形；不核验贴片外观、粘附或实际存在',
        action=dict(contact_threshold=.6,single_contact_threshold=.85,min_contact_seconds=.20,contact_frames=2,clear_frames=2,max_gap=1.5,min_motion=2.5),
        training_result='outputs/sticker-inspection-2026-10-17/action-training-result.json')
    cfg.pop('material_model',None);cfg.pop('material_bare_threshold',None)
    (ROOT/'config/sticker-action-candidate.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf8')
    (OUT/'action-label-audit.json').write_text(json.dumps(dict(source=str(SOURCE),source_sha256=file_hash(SOURCE),contact_intervals=CONTACT,rows=audit),ensure_ascii=False,indent=2),encoding='utf8')
    (OUT/'action-training-result.json').write_text(json.dumps(dict(model=cfg['model'],sha256=file_hash(model_path),metrics=metrics,
        samples=len(audit),split='temporal frame holdout within one video; augmentations never cross split',
        limitation='同一视频开发核对，不能代表独立工位准确率；监督标签为人工核对动作时间段，运行时不使用视频时间。'),ensure_ascii=False,indent=2),encoding='utf8')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');args=parser.parse_args()
    cfg=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf8'))
    if args.prepare:prepare(cfg)
    train(cfg)
