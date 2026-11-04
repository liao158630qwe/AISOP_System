"""Audited second-view development data and mixed-view YOLO fine-tuning."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import time
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from algorithms.screws import snapshot
from algorithms.screw_refined import RefinedScrewDetector
from algorithms.video import file_hash

OUT=ROOT/'outputs/screw-adaptation-v4-2026-10-17'
VIDEO=Path('C:/Users/Administrator/Downloads/lv_0_20261017141529.mp4')


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def profile():
    reference='models/screw-reference-overhead-v4.jpg'
    shutil.copy2(OUT/'reference-new.jpg',ROOT/reference)
    points=[(652,861),(937,769),(871,801),(844,766),(816,774),(765,719),
            (808,707),(854,717),(883,631),(985,521),(908,615)]
    names=['长件左下端','外侧长件中部','下侧三角支座','灯板下端预装螺丝','透明支架下端',
           '内侧支座左端','灯板上端预装螺丝','透明支架上端','内侧长件连接端',
           '顶部连接件','内侧长件末端']
    targets=[dict(id=f'S{i+1:02}',name=names[i],point=list(p),**({'preassembled':True} if i in (3,6) else {}))
             for i,p in enumerate(points)]
    regions=[[[640,850],[665,820],[955,750],[985,490],[1010,500],[990,785],[670,885]],
             [[746,713],[882,606],[922,601],[910,646],[771,741]],
             [[795,700],[871,710],[854,790],[810,790]]]
    p=dict(id='overhead-v4',reference=reference,targets=targets,registration_regions=regions,assignment_radius=14,
        component_regions=regions,product_polygon=[[620,480],[1040,480],[1040,900],[620,900]],
        component_anchors=[[680,835,720,850],[946,650,960,700],[820,738,836,754]])
    write(OUT/'calibration-overhead.json',p)
    frozen=snapshot()
    write(OUT/'config-before.json',frozen)
    frozen['config'].update(algorithm='multi-view-local-v1',calibrations=[p])
    frozen['assets'][reference]=file_hash(ROOT/reference)
    write(OUT/'candidate-before-training.json',frozen)
    local=copy.deepcopy(frozen);local['config'].update(p)
    return local


def review():
    local=profile();detector=RefinedScrewDetector(local)
    rows=[];tiles=[]
    for t in [22,28,36,42,48,54,60,66,72,76,79.5,84]:
        image=cv2.imread(str(OUT/f'frame-{t:g}.jpg'))
        result=detector.inspect(image)
        rows.append(dict(time=t,inspection=result))
        small=cv2.resize(image,(1920,1080))
        for target in local['config']['targets']:
            x,y=map(round,target['point']);crop=small[y-25:y+25,x-25:x+25]
            tile=cv2.resize(crop,(110,110));tile=cv2.copyMakeBorder(tile,24,0,0,0,cv2.BORDER_CONSTANT,value=(245,245,245))
            cv2.putText(tile,f'{t:g} {target["id"]}',(2,16),0,.4,(20,20,190),1);tiles.append(tile)
        print(t,result['registered'],[(p['id'],p['state'],p['score']) for p in result['targets']],flush=True)
    write(OUT/'review-inference.json',rows)
    width=len(local['config']['targets'])
    cv2.imwrite(str(OUT/'target-timeline.jpg'),np.vstack([np.hstack(tiles[i:i+width]) for i in range(0,len(tiles),width)]))


def prepare():
    local=profile();detector=RefinedScrewDetector(local)
    dataset=OUT/'dataset'
    if dataset.exists():raise RuntimeError('Keep the existing dataset; do not overwrite it')
    for split in ['train','val','test']:
        for kind in ['images','labels']:
            destination=dataset/kind/split;destination.mkdir(parents=True)
            for source in (ROOT/'outputs/screw-inspection-2026-10-17/refinement/dataset'/kind/split).iterdir():
                shutil.copy2(source,destination/('old-'+source.name))
    reference=detector.reference
    # Every point was checked in target-timeline.jpg. The unused boss just
    # above the top connection is an EMPTY negative, never a required screw.
    points=[dict(id=t['id'],cls=0,point=t['point']) for t in local['config']['targets']]
    points += [dict(id=f'H{i}',cls=1,point=p) for i,p in enumerate([(985,503),(757,309),(850,312),(880,397),(905,411)])]
    gray=cv2.cvtColor(reference,cv2.COLOR_BGR2GRAY)
    for point in points:
        x,y=map(round,point['point']);point['template']=gray[y-10:y+11,x-10:x+11]
    records=[];tiles=[];cap=cv2.VideoCapture(str(VIDEO))
    earliest=[36,75.75,60,60,60,48,72,60,60,54,54]
    times=list(range(30,77,2))+[73,75,75.5,76,77,77.5,78,78.5,79,79.5]
    for t in sorted(set(times)):
        split='test' if t>=77 else 'val' if t in (64,70,74) else 'train'
        cap.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,image=cap.read()
        if not ok:continue
        small=cv2.resize(image,(960,540));key,desc=detector.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        registration=detector.localize(key,desc)
        if registration is None:continue
        H=registration['H'];normalized=cv2.resize(image,(1920,1080))
        aligned=cv2.warpPerspective(normalized,np.linalg.inv(H),(1920,1080))
        ag=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY);visible=[]
        for item in points:
            if item['cls']==0 and t<earliest[int(item['id'][1:])-1]:continue
            x,y=map(round,item['point']);search=ag[y-18:y+19,x-18:x+19]
            _,score,_,location=cv2.minMaxLoc(cv2.matchTemplate(search,item['template'],cv2.TM_CCOEFF_NORMED))
            if score<(.80 if item['cls']==0 else .88):continue
            visible.append(dict(id=item['id'],cls=item['cls'],x=x-18+location[0]+10,y=y-18+location[1]+10,score=score))
        for center in visible:
            x,y=round(center['x'])-40,round(center['y'])-40
            neighbors=[p for p in visible if x+7<p['x']<x+73 and y+7<p['y']<y+73]
            expected=[p for p in points if x-4<p['point'][0]<x+84 and y-4<p['point'][1]<y+84]
            if any(p['id'] not in {n['id'] for n in neighbors} for p in expected):continue
            crop=aligned[y:y+80,x:x+80]
            if crop.shape[:2]!=(80,80):continue
            stem=f'new-{t:g}-{center["id"]}'
            cv2.imwrite(str(dataset/'images'/split/(stem+'.jpg')),crop)
            labels=[]
            for p in neighbors:
                side=16 if p['cls']==0 else 14
                labels.append(f'{p["cls"]} {(p["x"]-x)/80:.6f} {(p["y"]-y)/80:.6f} {side/80:.6f} {side/80:.6f}')
            (dataset/'labels'/split/(stem+'.txt')).write_text('\n'.join(labels)+'\n')
            records.append(dict(file=stem+'.jpg',time=t,split=split,center=center['id'],labels=neighbors))
            if split!='train' or len(records)%3==0:
                tile=cv2.resize(crop,(160,160))
                for p in neighbors:
                    a,b=round((p['x']-x)*2),round((p['y']-y)*2)
                    cv2.rectangle(tile,(a-16,b-16),(a+16,b+16),(0,200,0) if p['cls']==0 else (0,0,255),1)
                cv2.putText(tile,f'{t:g}s {center["id"]} {split}',(2,16),0,.35,(0,0,255),1);tiles.append(tile)
    cap.release()
    (dataset/'dataset.yaml').write_text(f'path: {dataset.as_posix()}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: screw_head\n  1: empty_hole\n',encoding='utf-8')
    counts={s:len(list((dataset/'images'/s).glob('*.jpg'))) for s in ['train','val','test']}
    write(OUT/'dataset-provenance.json',dict(source=str(VIDEO),source_sha256=file_hash(VIDEO),records=records,counts=counts,
        exclusions=['Unused boss at 985,503 is not a screw target','Occlusion and uncertain templates excluded'],
        label_method='assistant-reviewed native points and installation times, SIFT/template assisted propagation',
        scope='mixed known videos; temporal crop test, reference-assisted development evaluation, not independent field accuracy'))
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%8)
        cv2.imwrite(str(OUT/'label-review.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
    print(counts,flush=True)


def hard_negatives():
    local=json.loads((OUT/'candidate-before-training.json').read_text(encoding='utf-8'))
    local['config'].update(local['config']['calibrations'][0]);detector=RefinedScrewDetector(local)
    dataset=OUT/'dataset';audit=[];tiles=[]
    for t,identities in [(22,['S01','S02','S09','S10','S11']),(28,['S01','S02','S09','S10','S11'])]:
        image=cv2.imread(str(OUT/f'frame-{t}.jpg'));small=cv2.resize(image,(960,540))
        key,desc=detector.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        reg=detector.localize(key,desc)
        if reg is None:continue
        aligned=cv2.warpPerspective(cv2.resize(image,(1920,1080)),np.linalg.inv(reg['H']),(1920,1080))
        points=[dict(id=p['id'],point=p['point']) for p in local['config']['targets'] if p['id'] in identities]
        points.append(dict(id='H0',point=[985,503]))
        for identity in identities:
            point=next(p for p in points if p['id']==identity)
            for offset in [-6,0,6]:
                x,y=round(point['point'][0])-40+offset,round(point['point'][1])-40
                crop=aligned[y:y+80,x:x+80];stem=f'hard-empty-{t}-{identity}-{offset+6}'
                nearby=[p for p in points if x+7<p['point'][0]<x+73 and y+7<p['point'][1]<y+73]
                cv2.imwrite(str(dataset/'images/train'/(stem+'.jpg')),crop)
                lines=[f'1 {(p["point"][0]-x)/80:.6f} {(p["point"][1]-y)/80:.6f} .175 .175' for p in nearby]
                (dataset/'labels/train'/(stem+'.txt')).write_text('\n'.join(lines)+'\n')
                audit.append(dict(file=stem+'.jpg',time=t,labels=nearby,cls='empty_hole',source='assistant visual audit of real open holes before installation'))
                if offset==0:
                    tile=cv2.resize(crop,(200,200))
                    for p in nearby:
                        a,b=round((p['point'][0]-x)*2.5),round((p['point'][1]-y)*2.5)
                        cv2.rectangle(tile,(a-18,b-18),(a+18,b+18),(0,0,255),2)
                    cv2.putText(tile,f'{t}s {identity} EMPTY',(3,18),0,.45,(0,0,255),1);tiles.append(tile)
    write(OUT/'hard-negative-audit.json',audit)
    cv2.imwrite(str(OUT/'hard-negative-review.jpg'),np.vstack([np.hstack(tiles[i:i+5]) for i in range(0,len(tiles),5)]))


def native_positives():
    local=json.loads((OUT/'candidate-before-training.json').read_text(encoding='utf-8'))
    local['config'].update(local['config']['calibrations'][0]);detector=RefinedScrewDetector(local)
    dataset=OUT/'dataset';records=[];tiles=[]
    reference=detector.reference_gray[759:780,927:948]
    cap=cv2.VideoCapture(str(VIDEO));fps=cap.get(5)
    indices={round(t*fps) for t in [73,73.25,73.5,73.75,74,74.25,74.5,74.75,75,75.25,75.5,75.75,76,76.25,76.5,76.75,77,77.25,77.5,77.75,78,78.25,78.5,78.75,79,79.25,79.5]}
    for index in range(max(indices)+1):
        ok,image=cap.read()
        if not ok:break
        if index not in indices:continue
        small=cv2.resize(image,(960,540));key,desc=detector.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        reg=detector.localize(key,desc)
        if reg is None:continue
        inv=np.linalg.inv(reg['H']);normalize=np.diag([1920/image.shape[1],1080/image.shape[0],1.])
        aligned=cv2.warpPerspective(image,inv@normalize,(1920,1080))
        gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY)
        search=gray[751:788,919:956]
        _,score,_,loc=cv2.minMaxLoc(cv2.matchTemplate(search,reference,cv2.TM_CCOEFF_NORMED))
        if score<.70:continue
        cx,cy=919+loc[0]+10,751+loc[1]+10
        for offset in [-8,0,8]:
            x,y=cx-40+offset,cy-40
            move=np.array([[1.,0.,-x],[0.,1.,-y],[0.,0.,1.]])
            for scale in [1,2]:
                crop=cv2.warpPerspective(image,np.diag([scale,scale,1.])@move@inv@normalize,(80*scale,80*scale))
                cls=0 if index/fps>=75.75 else 1
                stem=f'native-{"head" if cls==0 else "empty"}-{index}-S02-{offset+8}-{scale}'
                cv2.imwrite(str(dataset/'images/train'/(stem+'.jpg')),crop)
                (dataset/'labels/train'/(stem+'.txt')).write_text(f'{cls} {(cx-x)/80:.6f} {(cy-y)/80:.6f} .2 .2\n')
                records.append(dict(file=stem+'.jpg',time=index/fps,point=[cx,cy],cls=cls,score=score,source='native pixels of visually reviewed S02 before/after installation; template-assisted crop'))
                if scale==2 and offset==0:
                    tile=cv2.resize(crop,(180,180));cv2.rectangle(tile,(72,72),(108,108),(0,220,0) if cls==0 else (0,0,255),1)
                    cv2.putText(tile,f'{index/fps:.2f}s {score:.2f}',(3,17),0,.4,(0,0,255),1);tiles.append(tile)
    cap.release();write(OUT/'native-positive-audit.json',records)
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%6)
        cv2.imwrite(str(OUT/'native-positive-review.jpg'),np.vstack([np.hstack(tiles[i:i+6]) for i in range(0,len(tiles),6)]))
    print('Native positive crops:',len(records),flush=True)


def train(epochs,run_name='training',initial='models/screw-head-v3-yolo26m.pt',export='models/screw-head-v4-yolo26m.pt'):
    from ultralytics import YOLO
    started=time.time();model=YOLO(str(ROOT/initial))
    model.train(data=str(OUT/'dataset/dataset.yaml'),epochs=epochs,imgsz=320,batch=8,device=0,workers=0,
        seed=20261017,project=str(OUT),name=run_name,exist_ok=False,optimizer='AdamW',lr0=.0003,
        patience=15,cos_lr=True,degrees=35,translate=.12,scale=.25,fliplr=.5,flipud=.25,
        mosaic=0,mixup=0,hsv_h=.015,hsv_s=.25,hsv_v=.3,close_mosaic=0,plots=True,verbose=False)
    best=OUT/run_name/'weights/best.pt';exported=ROOT/export
    shutil.copy2(best,exported)
    selected=YOLO(str(best))
    metrics=selected.val(data=str(OUT/'dataset/dataset.yaml'),split='test',imgsz=320,batch=8,device=0,workers=0,
        project=str(OUT),name=run_name+'-crop-test',exist_ok=True,plots=True,verbose=False)
    write(OUT/'training-result.json',dict(run=run_name,initial=initial,epochs_requested=epochs,seconds=time.time()-started,model=exported.relative_to(ROOT).as_posix(),
        model_sha256=file_hash(exported),metrics={k:float(v) for k,v in metrics.results_dict.items()},
        scope='known mixed-view videos and reference-assisted labels; not independent field accuracy'))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',action='store_true');parser.add_argument('--prepare',action='store_true');parser.add_argument('--hard-negatives',action='store_true');parser.add_argument('--native-positives',action='store_true');parser.add_argument('--train',action='store_true');parser.add_argument('--epochs',type=int,default=60);parser.add_argument('--run-name',default='training');parser.add_argument('--initial',default='models/screw-head-v3-yolo26m.pt');parser.add_argument('--export',default='models/screw-head-v4-yolo26m.pt');args=parser.parse_args()
    if args.review:review()
    if args.prepare:prepare()
    if args.hard_negatives:hard_negatives()
    if args.native_positives:native_positives()
    if args.train:train(args.epochs,args.run_name,args.initial,args.export)
