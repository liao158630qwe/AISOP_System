"""Reviewed same-video sticker data, with actual YOLO training and provenance."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import cv2
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.video import file_hash
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
SOURCE=Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4')


def write(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def candidate():
    image=cv2.resize(cv2.imread(str(OUT/'correct-video/frame-048.jpg')),(1080,1920))
    reference='models/sticker-reference-v1.jpg';cv2.imwrite(str(ROOT/reference),image)
    entries=[('T01','左侧三角贴','triangle',[250,885,312,977]),('T02','右侧三角贴','triangle',[681,891,748,984]),
        ('R01','左上方形贴','rectangle',[296,803,387,884]),('R02','右上方形贴','rectangle',[605,806,696,889]),
        ('R03','左中方形贴','rectangle',[298,946,359,1046]),('R04','右中方形贴','rectangle',[633,951,695,1051]),
        ('R05','左下方形贴','rectangle',[251,1094,301,1173]),('R06','右下方形贴','rectangle',[695,1100,747,1181])]
    targets=[dict(id=i,name=n,expected=e,reference_bbox=b,point=[(b[0]+b[2])/2,(b[1]+b[3])/2]) for i,n,e,b in entries]
    config=dict(version='sticker-v1',name='贴条工序检测',model_name='YOLO26m 贴条专用 v1',
        model='models/sticker-patches-v1.pt',reference=reference,reference_size=[1080,1920],targets=targets,
        product_bbox=[190,700,780,1290],registration_regions=[[[200,760],[770,760],[780,1250],[190,1250]]],
        station_roi=[.28,.40,.68,.68],crop_size=160,detector_input=320,threshold=.55,bare_threshold=.75,
        assignment_radius=35,class_map={'0':'triangle','1':'rectangle','2':'bare'},
        scope='核对两处三角、六处方形贴的形状/位置及先三角后方形的视觉工序证据；不测量胶粘强度',
        sequence=dict(triangles=['T01','T02'],rectangles=[f'R{i:02}' for i in range(1,7)],confirmation_frames=2),
        source_video=SOURCE.name,source_sha256=file_hash(SOURCE),cycle_start='explicit',
        training_result='outputs/sticker-inspection-2026-10-17/training-result.json')
    write(ROOT/'config/sticker-inspection-candidate.json',config)
    for t in targets:
        a,b,c,d=map(round,t['reference_bbox']);cv2.rectangle(image,(a,b),(c,d),(0,0,255) if t['expected']=='triangle' else (0,210,0),2)
        cv2.putText(image,t['id'],(a,b-5),0,.5,(0,0,255),1)
    cv2.imwrite(str(OUT/'candidate-targets.jpg'),image[700:1310,170:800])
    return config


class Registration:
    def __init__(self,config):
        self.ref=cv2.imread(str(ROOT/config['reference']));self.sift=cv2.SIFT_create(nfeatures=3000,contrastThreshold=.015)
        mask=np.zeros((960,540),np.uint8)
        cv2.fillPoly(mask,[np.round(np.array(p)/2).astype(np.int32) for p in config['registration_regions']],255)
        self.key,self.desc=self.sift.detectAndCompute(cv2.cvtColor(cv2.resize(self.ref,(540,960)),cv2.COLOR_BGR2GRAY),mask)
        self.matcher=cv2.BFMatcher()
    def align(self,image):
        small=cv2.resize(image,(540,960));key,desc=self.sift.detectAndCompute(cv2.cvtColor(small,cv2.COLOR_BGR2GRAY),None)
        if desc is None:return None
        matches=[p[0] for p in self.matcher.knnMatch(self.desc,desc,k=2) if len(p)==2 and p[0].distance<.72*p[1].distance]
        if len(matches)<14:return None
        matrix,mask=cv2.findHomography(np.float32([self.key[m.queryIdx].pt for m in matches]),np.float32([key[m.trainIdx].pt for m in matches]),cv2.RANSAC,2)
        if matrix is None or mask.sum()<12 or mask.mean()<.3:return None
        H=np.diag([2.,2.,1.])@matrix@np.diag([.5,.5,1.])
        normalized=cv2.resize(image,(1080,1920));return cv2.warpPerspective(normalized,np.linalg.inv(H),(1080,1920))


def review():
    config=candidate();registration=Registration(config);folder=OUT/'aligned';folder.mkdir(exist_ok=True)
    cap=cv2.VideoCapture(str(SOURCE));tiles=[];samples=[]
    for t in np.arange(14,49.51,.5):
        cap.set(cv2.CAP_PROP_POS_MSEC,float(t*1000));ok,image=cap.read()
        if not ok:continue
        aligned=registration.align(image)
        if aligned is None:continue
        path=folder/f'{t:05.1f}.jpg';cv2.imwrite(str(path),aligned,[cv2.IMWRITE_JPEG_QUALITY,96]);samples.append(dict(time=float(t),file=path.name))
        if t%2:continue
        for target in config['targets']:
            a,b,c,d=map(round,target['reference_bbox']);crop=aligned[b-10:d+10,a-10:c+10]
            tile=cv2.resize(crop,(110,130));tile=cv2.copyMakeBorder(tile,22,0,0,0,cv2.BORDER_CONSTANT,value=(245,245,245))
            cv2.putText(tile,f'{t:g}s {target["id"]}',(2,15),0,.38,(10,10,190),1);tiles.append(tile)
    cap.release();write(OUT/'aligned-samples.json',samples)
    cv2.imwrite(str(OUT/'target-timeline.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
    print('Aligned samples',len(samples),flush=True)


def prepare():
    config=json.loads((ROOT/'config/sticker-inspection-candidate.json').read_text(encoding='utf-8'))
    dataset=OUT/'dataset'
    if dataset.exists():raise RuntimeError('Dataset exists; preserve the audited version before rebuilding')
    for split in ['train','val','test']:
        for kind in ['images','labels']:(dataset/kind/split).mkdir(parents=True)
    # Visually reviewed conservative intervals, used for LABELS ONLY. Runtime
    # inference receives pixels and has no access to this video timetable.
    intervals={'T01':(22.5,25.5),'T02':(19,21.5),'R01':(33,36),'R02':(37,40),
               'R03':(25,28),'R04':(29,32),'R05':(40,43),'R06':(44,47)}
    samples=json.loads((OUT/'aligned-samples.json').read_text());ref=cv2.imread(str(ROOT/config['reference']))
    gray_ref=cv2.cvtColor(ref,cv2.COLOR_BGR2GRAY);masks={}
    for target in config['targets']:
        a,b,c,d=target['reference_bbox'];mask=np.uint8(gray_ref[b:d,a:c]<70)
        masks[target['id']]=cv2.erode(mask,np.ones((5,5),np.uint8)).astype(bool)
    val={19,29,39,44};test={20,30,40,45,49};held=val|test
    records=[];tiles=[];excluded=[]
    for entry in samples:
        t=entry['time']
        if t<16:continue
        if t in val:split='val'
        elif t in test:split='test'
        elif any(abs(t-h)<1 for h in held):continue
        else:split='train'
        image=cv2.imread(str(OUT/'aligned'/entry['file']));gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        observed={}
        for target in config['targets']:
            identity=target['id'];before,after=intervals[identity]
            cls=2 if t<=before else 0 if t>=after and target['expected']=='triangle' else 1 if t>=after else None
            if cls is None:continue
            a,b,c,d=target['reference_bbox'];roi=image[b:d,a:c];mask=masks[identity]
            if mask.sum()<20:continue
            values=gray[b:d,a:c][mask]
            colored=roi[mask].astype(float)
            glove=(values>105).mean()>.1
            table=((colored[:,1]>colored[:,2]*1.4)&(colored[:,1]>colored[:,0]*1.05)).mean()>.35
            if glove or table:
                excluded.append(dict(time=t,id=identity,reason='glove/skin or visible green table; no reliable bare/patch label'))
                continue
            observed[identity]=cls
        for target in config['targets']:
            identity=target['id']
            if identity not in observed:continue
            a,b,c,d=target['reference_bbox'];side=round(max(config['crop_size'],(c-a)*1.6,(d-b)*1.6))
            x,y=round(target['point'][0]-side/2),round(target['point'][1]-side/2)
            neighbors=[];complete=True
            for other in config['targets']:
                left,top,right,bottom=other['reference_bbox'];left,top,right,bottom=max(left,x),max(top,y),min(right,x+side),min(bottom,y+side)
                if right-left<4 or bottom-top<4:continue
                if other['id'] not in observed:complete=False;break
                neighbors.append(dict(id=other['id'],cls=observed[other['id']],bbox=[left,top,right,bottom]))
            if not complete:continue
            crop=image[y:y+side,x:x+side]
            if crop.shape[:2]!=(side,side):continue
            stem=f'{t:05.1f}-{identity}';labels=[]
            for item in neighbors:
                l,u,r,v=item['bbox'];labels.append(f'{item["cls"]} {((l+r)/2-x)/side:.6f} {((u+v)/2-y)/side:.6f} {(r-l)/side:.6f} {(v-u)/side:.6f}')
            cv2.imwrite(str(dataset/'images'/split/(stem+'.jpg')),crop,[cv2.IMWRITE_JPEG_QUALITY,96])
            (dataset/'labels'/split/(stem+'.txt')).write_text('\n'.join(labels)+'\n')
            records.append(dict(file=stem+'.jpg',time=t,split=split,center=identity,labels=neighbors))
            if len(records)%4==0 or split!='train':
                tile=cv2.resize(crop,(160,160))
                for item in neighbors:
                    l,u,r,v=[round((z-base)*160/side) for z,base in zip(item['bbox'],[x,y,x,y])]
                    color=(0,0,255) if item['cls']==2 else (0,200,0)
                    cv2.rectangle(tile,(l,u),(r,v),color,1);cv2.putText(tile,str(item['cls']),(l,u+12),0,.4,color,1)
                cv2.putText(tile,f'{t:g}s {identity} {split}',(2,15),0,.35,(30,30,210),1);tiles.append(tile)
    # Visually checked empty bench and glove crops contain no patch sites.
    for t in [16,24,32,40,48]:
        image=cv2.imread(str(OUT/'aligned'/f'{t:05.1f}.jpg'))
        for i,(x,y) in enumerate([(20,500),(100,600),(850,800),(850,1000),(850,1200),(600,1450)]):
            crop=image[y:y+160,x:x+160]
            if crop.shape[:2]!=(160,160):continue
            name=f'background-{t}-{i}'
            cv2.imwrite(str(dataset/'images/train'/(name+'.jpg')),crop)
            (dataset/'labels/train'/(name+'.txt')).write_text('')
    counts={s:len(list((dataset/'images'/s).glob('*.jpg'))) for s in ['train','val','test']}
    write(OUT/'dataset-provenance.json',dict(source=str(SOURCE),source_sha256=file_hash(SOURCE),counts=counts,
        records=records,excluded=excluded,reviewed_intervals=intervals,
        label_method='current model visual review of per-position before/after; SIFT-assisted propagation and occlusion rejection',
        split='held whole frame times; .5s guard removed near held frames',
        scope='one correct video, same fixture and assisted reference; crop metrics are internal development only'))
    (dataset/'dataset.yaml').write_text(f'path: {dataset.as_posix()}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: triangle_patch\n  1: rectangle_patch\n  2: bare_site\n',encoding='utf-8')
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%8)
        cv2.imwrite(str(OUT/'label-review.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
    print(counts,flush=True)


def hard_examples():
    config=json.loads((ROOT/'config/sticker-inspection-candidate.json').read_text(encoding='utf-8'));reg=Registration(config)
    dataset=OUT/'dataset';cap=cv2.VideoCapture(str(SOURCE));fps=cap.get(5);records=[];tiles=[]
    wanted={round(t*fps):t for t in [14,14.25,14.5,14.75,15,15.25,15.5,15.75,16,22.75,23,23.25,23.5,23.75,24,24.25,24.5,25,47,47.25,47.5,47.75,48]}
    for index in range(max(wanted)+1):
        if index not in wanted:
            if not cap.grab():break
            continue
        ok,image=cap.read()
        if not ok:break
        t=index/fps;aligned=reg.align(image)
        if aligned is None:continue
        sites=['R06'] if t<17 else ['R03'] if t<30 else [s['id'] for s in config['targets']]
        for identity in sites:
            site=next(s for s in config['targets'] if s['id']==identity);a,b,c,d=site['reference_bbox']
            cls=2 if t<30 else 0 if site['expected']=='triangle' else 1
            ref_gray=cv2.cvtColor(reg.ref[b:d,a:c],cv2.COLOR_BGR2GRAY)
            mask=cv2.erode(np.uint8(ref_gray<70),np.ones((5,5),np.uint8)).astype(bool)
            current=cv2.cvtColor(aligned[b:d,a:c],cv2.COLOR_BGR2GRAY)
            if mask.sum()<20 or (current[mask]>105).mean()>.1:continue
            for margin in [0,8,16]:
                left,top,right,bottom=a-margin,b-margin,c+margin,d+margin
                crop=aligned[top:bottom,left:right]
                stem=f'hard-{index}-{identity}-{margin}'
                cv2.imwrite(str(dataset/'images/train'/(stem+'.jpg')),crop)
                w,h=right-left,bottom-top
                (dataset/'labels/train'/(stem+'.txt')).write_text(f'{cls} {((a+c)/2-left)/w:.6f} {((b+d)/2-top)/h:.6f} {(c-a)/w:.6f} {(d-b)/h:.6f}\n')
                records.append(dict(file=stem+'.jpg',time=t,id=identity,cls=cls,source='sequential original decode of audited settling/hand-shadow/clear final position'))
                if margin==8:
                    tile=cv2.resize(crop,(140,160));cv2.putText(tile,f'{t:.2f} {identity} {cls}',(3,16),0,.35,(0,0,255),1);tiles.append(tile)
    cap.release();write(OUT/'hard-example-audit.json',records)
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%8)
        cv2.imwrite(str(OUT/'hard-example-review.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
    print('Hard example crops',len(records),flush=True)


def camera_examples():
    config=json.loads((ROOT/'config/sticker-inspection-candidate.json').read_text(encoding='utf-8'));reg=Registration(config)
    provenance=json.loads((OUT/'dataset-provenance.json').read_text(encoding='utf-8'))
    intervals=provenance['reviewed_intervals'];intervals['T01'][1]=24.0
    # Only previously training times, plus visually reviewed T01 after its
    # application. Reserved validation/test frame times remain untouched.
    times=sorted(set(r['time'] for r in provenance['records'] if r['split']=='train'))
    times=sorted(set(times)|{24,24.5,25,25.5})
    cap=cv2.VideoCapture(str(SOURCE));fps=cap.get(5);wanted={round(t*fps):t for t in times};records=[];tiles=[]
    ref_gray=cv2.cvtColor(reg.ref,cv2.COLOR_BGR2GRAY);masks={}
    for target in config['targets']:
        a,b,c,d=target['reference_bbox'];masks[target['id']]=cv2.erode(np.uint8(ref_gray[b:d,a:c]<70),np.ones((5,5),np.uint8)).astype(bool)
    for index in range(max(wanted)+1):
        if index not in wanted:
            if not cap.grab():break
            continue
        ok,image=cap.read()
        if not ok:break
        t=wanted[index];image=cv2.resize(image,(1080,1920));ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94]);assert ok
        aligned=reg.align(cv2.imdecode(jpeg,cv2.IMREAD_COLOR))
        if aligned is None:continue
        observed={};gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY)
        for site in config['targets']:
            before,after=intervals[site['id']];cls=2 if t<=before else 0 if t>=after and site['expected']=='triangle' else 1 if t>=after else None
            if cls is None:continue
            a,b,c,d=site['reference_bbox'];mask=masks[site['id']];values=gray[b:d,a:c][mask]
            rgb=aligned[b:d,a:c][mask].astype(float)
            if mask.sum()<20 or (values>105).mean()>.1:continue
            if ((rgb[:,1]>rgb[:,2]*1.4)&(rgb[:,1]>rgb[:,0]*1.05)).mean()>.35:continue
            observed[site['id']]=cls
        for site in config['targets']:
            if site['id'] not in observed:continue
            side=config['crop_size'];x,y=round(site['point'][0]-side/2),round(site['point'][1]-side/2);neighbors=[];complete=True
            for other in config['targets']:
                a,b,c,d=other['reference_bbox'];a,b,c,d=max(a,x),max(b,y),min(c,x+side),min(d,y+side)
                if c-a<4 or d-b<4:continue
                if other['id'] not in observed:complete=False;break
                neighbors.append(dict(id=other['id'],cls=observed[other['id']],bbox=[a,b,c,d]))
            if not complete:continue
            stem=f'camera-{index}-{site["id"]}';crop=aligned[y:y+side,x:x+side]
            cv2.imwrite(str(OUT/'dataset/images/train'/(stem+'.jpg')),crop)
            lines=[]
            for p in neighbors:
                a,b,c,d=p['bbox'];lines.append(f'{p["cls"]} {((a+c)/2-x)/side:.6f} {((b+d)/2-y)/side:.6f} {(c-a)/side:.6f} {(d-b)/side:.6f}')
            (OUT/'dataset/labels/train'/(stem+'.txt')).write_text('\n'.join(lines)+'\n')
            records.append(dict(file=stem+'.jpg',time=t,labels=neighbors,source='sequential 1080x1920 JPEG94 camera-input pipeline'))
            if site['id']=='T01' and t<=26:
                tile=cv2.resize(crop,(180,180));cv2.putText(tile,f'{t:g} T01 {observed[site["id"]]}',(3,16),0,.4,(0,0,255),1);tiles.append(tile)
    cap.release();write(OUT/'camera-example-audit.json',records)
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%6)
        cv2.imwrite(str(OUT/'camera-example-review.jpg'),np.vstack([np.hstack(tiles[i:i+6]) for i in range(0,len(tiles),6)]))
    print('Camera example crops',len(records),flush=True)


def synthetic_missing():
    config=json.loads((ROOT/'config/sticker-inspection-candidate.json').read_text(encoding='utf-8'))
    provenance=json.loads((OUT/'dataset-provenance.json').read_text(encoding='utf-8'))
    records=[];tiles=[]
    for site in config['targets']:
        identity=site['id'];candidates=sorted(set(r['time'] for r in provenance['records'] if r['split']=='train' and
            r['center']==identity and r['time']!=16 and any(p['id']==identity and p['cls']==2 for p in r['labels'])))
        for before in candidates[:2]:
            bare=cv2.imread(str(OUT/'aligned'/f'{before:05.1f}.jpg'));a,b,c,d=site['reference_bbox']
            for after in [47,47.5]:
                image=cv2.imread(str(OUT/'aligned'/f'{after:05.1f}.jpg'))
                image[b:d,a:c]=bare[b:d,a:c]
                for quality in [90,96]:
                    ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,quality]);assert ok
                    combined=cv2.imdecode(jpeg,cv2.IMREAD_COLOR);side=config['crop_size'];x,y=round(site['point'][0]-side/2),round(site['point'][1]-side/2)
                    crop=combined[y:y+side,x:x+side];objects=[];lines=[]
                    for other in config['targets']:
                        l,u,r,v=other['reference_bbox'];l,u,r,v=max(l,x),max(u,y),min(r,x+side),min(v,y+side)
                        if r-l<4 or v-u<4:continue
                        cls=2 if other['id']==identity else 0 if other['expected']=='triangle' else 1
                        objects.append(dict(id=other['id'],cls=cls));lines.append(f'{cls} {((l+r)/2-x)/side:.6f} {((u+v)/2-y)/side:.6f} {(r-l)/side:.6f} {(v-u)/side:.6f}')
                    stem=f'synthetic-missing-{identity}-{before}-{after}-{quality}'
                    cv2.imwrite(str(OUT/'dataset/images/train'/(stem+'.jpg')),crop)
                    (OUT/'dataset/labels/train'/(stem+'.txt')).write_text('\n'.join(lines)+'\n')
                    records.append(dict(file=stem+'.jpg',synthetic=True,missing=identity,bare_source_time=before,
                        installed_context_time=after,labels=objects,source='synthetic transplant of verified real bare site into installed context; NOT a real defective finished item'))
                    if quality==96:
                        tile=cv2.resize(crop,(160,160));cv2.putText(tile,identity+' missing SYNTH',(2,16),0,.35,(0,0,255),1);tiles.append(tile)
    write(OUT/'synthetic-missing-training-audit.json',records)
    if tiles:
        tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%8)
        cv2.imwrite(str(OUT/'synthetic-missing-training-review.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
    print('Synthetic missing training crops',len(records),flush=True)


def train(epochs,initial='models/yolo26m.pt',run='training-v1',export='models/sticker-patches-v1.pt'):
    from ultralytics import YOLO
    started=time.time();model=YOLO(str(ROOT/initial))
    model.train(data=str(OUT/'dataset/dataset.yaml'),epochs=epochs,imgsz=320,batch=8,device=0,workers=0,
        seed=20261017,project=str(OUT),name=run,exist_ok=False,optimizer='AdamW',lr0=.0006 if run=='training-v1' else .0002,
        patience=25,cos_lr=True,degrees=12,translate=.08,scale=.18,fliplr=.0,flipud=.0,
        mosaic=0,mixup=0,hsv_h=.005,hsv_s=.15,hsv_v=.12,close_mosaic=0,plots=True,verbose=False)
    best=OUT/run/'weights/best.pt';export=ROOT/export;shutil.copy2(best,export)
    metrics=YOLO(str(best)).val(data=str(OUT/'dataset/dataset.yaml'),split='test',imgsz=320,batch=8,device=0,workers=0,
        project=str(OUT),name=run+'-crop-test',exist_ok=True,plots=True,verbose=False)
    write(OUT/'training-result.json',dict(model=export.relative_to(ROOT).as_posix(),model_sha256=file_hash(export),
        epochs_requested=epochs,seconds=time.time()-started,metrics={k:float(v) for k,v in metrics.results_dict.items()},
        execution='current primary model directly prepared, trained and audited after user revoked delegation',
        scope='known same-video assisted crop labels, not independent defect or order accuracy'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--review',action='store_true');p.add_argument('--prepare',action='store_true');p.add_argument('--hard-examples',action='store_true');p.add_argument('--camera-examples',action='store_true');p.add_argument('--synthetic-missing',action='store_true');p.add_argument('--train',action='store_true');p.add_argument('--epochs',type=int,default=100);p.add_argument('--initial',default='models/yolo26m.pt');p.add_argument('--run',default='training-v1');p.add_argument('--export',default='models/sticker-patches-v1.pt');a=p.parse_args()
    if a.review:review()
    if a.prepare:prepare()
    if a.hard_examples:hard_examples()
    if a.camera_examples:camera_examples()
    if a.synthetic_missing:synthetic_missing()
    if a.train:train(a.epochs,a.initial,a.run,a.export)
