"""Full known-video development evaluation with current-frame evidence only."""
import argparse
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.stickers import StickerDetector,final_observations,compliance
from algorithms.sticker_sequence import StickerSequence
from algorithms.video import file_hash
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
SOURCE=Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--camera',action='store_true');args=parser.parse_args()
    path=ROOT/'config/sticker-inspection-candidate.json';config=json.loads(path.read_text(encoding='utf-8'))
    assets=[config['model'],config['reference']]+([config['material_model']] if config.get('material_model') else [])
    frozen=dict(config=config,config_hash=file_hash(path),assets={p:file_hash(ROOT/p) for p in assets})
    detector=StickerDetector(frozen);sequence=StickerSequence(config['targets'],
        confirmation_frames=config.get('sequence',{}).get('confirmation_frames',2));frames=[];next_sample=0
    cap=cv2.VideoCapture(str(SOURCE));fps=cap.get(5);count=int(cap.get(7));started=time.time()
    for index in range(count):
        if index/fps+1e-6<next_sample:
            if not cap.grab():raise RuntimeError('Decode interrupted')
            continue
        ok,image=cap.read()
        if not ok:raise RuntimeError('Decode interrupted')
        next_sample+=.25;t=index/fps
        if args.camera:
            image=cv2.resize(image,(1080,1920));ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94]);assert ok
            image=cv2.imdecode(jpeg,cv2.IMREAD_COLOR)
        inspection=detector.inspect(image);inspection['sequence']=sequence.update(t,inspection)
        frames.append(dict(time=round(t,4),inspection=inspection))
        if len(frames)%40==0:print('Frames',len(frames),'time',round(t,1),flush=True)
    cap.release();targets,assessment=final_observations(frames,config['targets']);process=sequence.snapshot()
    result=dict(source_sha256=file_hash(SOURCE),model=frozen,summary=dict(analyzedFrames=len(frames),
        registeredFrames=sum(f['inspection']['registered'] for f in frames),assessmentTime=assessment,
        processingSeconds=time.time()-started),targets=targets,sequence=process,compliance=compliance(targets,process),frames=frames)
    controls=[]
    for label,image in [('blank',np.zeros((1080,608,3),np.uint8))]+[
            (f'frame-{t}',cv2.imread(str(OUT/f'correct-video/frame-{t:03}.jpg'))) for t in [4,16,28,40,48,52,60]]:
        r=detector.inspect(image)
        controls.append(dict(case=label,registered=r['registered'],states={p['id']:p['state'] for p in r['targets']},boxes=len(r['displayTargets'])))
        if label=='frame-48':
            canvas=cv2.resize(image,(1080,1920))
            for p in r['displayTargets']:
                a,b,c,d=[round(v*s) for v,s in zip(p['bbox'],[1080,1920,1080,1920])]
                color=(40,220,50) if p['state']=='PRESENT' else (0,0,255)
                cv2.rectangle(canvas,(a,b),(c,d),color,2);cv2.putText(canvas,p['id'],(a,b-5),0,.5,color,1)
            cv2.imwrite(str(OUT/'native-detections-48.jpg'),canvas[700:1320,170:800])
    final=cv2.imread(str(OUT/'correct-video/frame-048.jpg'));realtime=cv2.resize(final,(608,1080))
    _,jpeg=cv2.imencode('.jpg',realtime,[cv2.IMWRITE_JPEG_QUALITY,82]);r=detector.inspect(cv2.imdecode(jpeg,cv2.IMREAD_COLOR))
    controls.append(dict(case='camera-sized-jpeg82-final',registered=r['registered'],states={p['id']:p['state'] for p in r['targets']},boxes=len(r['displayTargets'])))
    result['controls']=controls
    installed=cv2.imread(str(OUT/'aligned/048.0.jpg'));bare=cv2.imread(str(OUT/'aligned/016.0.jpg'))
    missing_controls=[]
    for target in config['targets']:
        a,b,c,d=target['reference_bbox'];changed=installed.copy();changed[b:d,a:c]=bare[b:d,a:c]
        _,payload=cv2.imencode('.jpg',changed,[cv2.IMWRITE_JPEG_QUALITY,94])
        observation=detector.inspect(cv2.imdecode(payload,cv2.IMREAD_COLOR))
        observed=next(t for t in observation['targets'] if t['id']==target['id'])
        missing_controls.append(dict(id=target['id'],state=observed['state'],score=observed['score'],
            provenance='synthetic transplant of known real bare pixels at the same site; not independent defect data'))
    result['synthetic_missing_controls']=missing_controls
    result['scope']='one known training video, assisted labels and temporal crop splits; not independent field defect accuracy'
    result['input_mode']='camera-sized JPEG94' if args.camera else 'native source'
    (OUT/('candidate-evaluation-camera.json' if args.camera else 'candidate-evaluation.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(summary=result['summary'],compliance=result['compliance'],sequence=process,controls=controls),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
