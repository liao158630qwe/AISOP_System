"""Auditable sticker appearance fine-tuning from complete production cycles.

Preparation retains real frames and existing-model observations for review.
Annotations are external, reviewed intervals; predictions never become labels.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import copy
import shutil
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'outputs/sticker-strengthening-20261109'
SOURCE = Path('C:/Users/Administrator/Downloads/贴条.mp4')


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def runtime():
    from backend.inference_runtime import configure_inference_runtime, configure_torch_runtime
    configure_inference_runtime(4)
    configure_torch_runtime(4)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()


def observations():
    return [json.loads(line) for line in (OUT/'baseline-frames.jsonl').read_text(encoding='utf-8').splitlines()]


def build(args):
    """Capture the actual runtime crops, independently assign reviewed labels."""
    import cv2
    import numpy as np
    from algorithms.stickers import AppearanceStickerDetector
    runtime()
    annotation = read(OUT/'reviewed-annotations.json')
    frozen = read(OUT/'baseline-frozen.json')
    detector = AppearanceStickerDetector(frozen)
    cfg = frozen['config']
    dataset = OUT/'dataset'
    if dataset.exists():
        raise RuntimeError('Dataset already exists; preserve provenance and use a new output directory')
    for split in ('train', 'val', 'test', 'regression', 'legacy_val', 'legacy_test'):
        for kind in ('images','labels'):
            (dataset/kind/split).mkdir(parents=True,exist_ok=True)
    records, truth, reasons = [], [], Counter()
    legacy = ROOT/'outputs/sticker-inspection-2026-10-17/dataset'
    for old_split in ('train','val','test'):
        split = 'train' if old_split=='train' else 'legacy_'+old_split
        for src in sorted((legacy/'images'/old_split).glob('*')):
            label = legacy/'labels'/old_split/(src.stem+'.txt')
            assert label.is_file(), src
            name='legacy-'+src.name
            shutil.copy2(src,dataset/'images'/split/name)
            shutil.copy2(label,dataset/'labels'/split/('legacy-'+label.name))
            records.append(dict(file=name,split=split,source='legacy rehearsal',synthetic=src.name.startswith('synthetic'),sha256=digest(src)))
    crops=[]
    original_warp=cv2.warpPerspective
    def capture(*a,**kw):
        value=original_warp(*a,**kw)
        if value.shape[:2]==(160,160):
            crops.append(value.copy())
        return value
    cv2.warpPerspective=capture
    try:
        for row in observations():
            cycle=next((c for c in annotation['cycles'] if c['range'][0]<=row['time']<=c['range'][1]),None)
            if cycle is None:continue
            crops.clear()
            image=cv2.imread(str(OUT/'frames'/row['file']))
            detector.inspect(image)
            if len(crops)!=len(cfg['targets']):
                reasons['registration_or_anchor']+=1
                continue
            labels={};regions={};visible={}
            for site,crop in zip(cfg['targets'],crops):
                identity=site['id'];side=crop.shape[0]
                x,y=round(site['point'][0]-side/2),round(site['point'][1]-side/2)
                regions[identity]=(x,y,side)
                a,b,c,d=site['reference_bbox'];pixels=crop[b-y:d-y,a-x:c-x];mask=detector.site_masks[identity]
                values=cv2.cvtColor(pixels,cv2.COLOR_BGR2GRAY)[mask];rgb=pixels[mask].astype(float)
                valid=mask.sum()>=20 and (values>105).mean()<=.1 and ((rgb[:,1]>rgb[:,2]*1.4)&(rgb[:,1]>rgb[:,0]*1.05)).mean()<=.35
                # Annotation must be more conservative than production's
                # visibility heuristic: dark sleeves/skin can pass that gate.
                skin=(rgb[:,2]>rgb[:,1]*1.2)&(rgb[:,1]>rgb[:,0]*1.1)&(rgb[:,2]>45)
                blue=(rgb[:,0]>rgb[:,2]*1.4)&(rgb[:,1]>rgb[:,2]*1.15)&(rgb[:,0]>45)
                if skin.mean()>.08 or blue.mean()>.08:valid=False
                for start,end,ids in annotation.get('manual_occlusions',[]):
                    if start-.04<=row['time']<=end+.04 and identity in ids:valid=False
                visible[identity]=bool(valid)
                before,after=cycle['targets'][identity]
                cls=2 if row['time']<=before+.04 else (0 if site['expected']=='triangle' else 1) if row['time']>=after-.04 else None
                if not valid:reasons['occlusion']+=1
                elif cls is None:reasons['transition']+=1
                else:labels[identity]=cls
            truth.append(dict(file=row['file'],time=row['time'],cycle=cycle['id'],split=cycle['split'],labels=labels,visible=visible))
            for site,crop in zip(cfg['targets'],crops):
                identity=site['id']
                if identity not in labels:continue
                x,y,side=regions[identity];neighbors=[];complete=True
                for other in cfg['targets']:
                    a,b,c,d=other['reference_bbox'];a,b,c,d=max(a,x),max(b,y),min(c,x+side),min(d,y+side)
                    if c-a<4 or d-b<4:continue
                    if other['id'] not in labels:complete=False;break
                    neighbors.append(dict(id=other['id'],cls=labels[other['id']],bbox=[a-x,b-y,c-x,d-y]))
                if not complete:
                    reasons['ambiguous_neighbor']+=1
                    continue
                stem=f'cycle{cycle["id"]}-{row["frame"]}-{identity}'
                dest=dataset/'images'/cycle['split']/(stem+'.png')
                assert cv2.imwrite(str(dest),crop)
                lines=[]
                for n in neighbors:
                    a,b,c,d=n['bbox'];lines.append(f'{n["cls"]} {(a+c)/2/side:.7f} {(b+d)/2/side:.7f} {(c-a)/side:.7f} {(d-b)/side:.7f}')
                (dataset/'labels'/cycle['split']/(stem+'.txt')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
                records.append(dict(file=dest.name,split=cycle['split'],cycle=cycle['id'],frame=row['frame'],time=row['time'],center=identity,
                                    cls=labels[identity],labels=neighbors,source='reviewed real footage; exact runtime crop',sha256=digest(dest)))
    finally:
        cv2.warpPerspective=original_warp
    hashes=defaultdict(set)
    for row in records:
        if row['split'] in ('train','val','test'):hashes[row['sha256']].add(row['split'])
    assert all(len(s)==1 for s in hashes.values()), 'Exact-image leakage across train/val/test'
    import yaml
    (dataset/'dataset.yaml').write_text(yaml.safe_dump(dict(path=dataset.as_posix(),train='images/train',val='images/val',test='images/test',
        names={0:'triangle_patch',1:'rectangle_patch',2:'bare_site'})),encoding='utf-8')
    write(OUT/'dataset-manifest.json',records)
    write(OUT/'reviewed-truth.json',truth)
    summary=dict(counts=dict(Counter(r['split'] for r in records)),center_classes=dict(Counter(f'{r["split"]}/{r["center"]}/{r["cls"]}' for r in records if 'center' in r)),
        exclusions=dict(reasons),source_sha256=read(OUT/'source.json')['sha256'],annotation_sha256=digest(OUT/'reviewed-annotations.json'),
        cross_split_exact_duplicates=0,preprocessing='Actual detector crops, captured before inference, lossless PNG; visibility independent of model predictions',
        label_provenance=annotation['annotation_basis'])
    write(OUT/'dataset-audit.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def train(args):
    import torch
    from ultralytics import YOLO
    runtime()
    assert torch.cuda.is_available(), 'Training requires CUDA'
    started=time.perf_counter()
    run=OUT/args.run
    if run.exists():raise RuntimeError('Run already exists; choose a fresh name')
    model=YOLO(str(ROOT/'models/sticker-patches-v5.pt'))
    model.train(data=str(OUT/'dataset/dataset.yaml'),epochs=args.epochs,imgsz=320,batch=16,device=0,workers=0,
        seed=20261109,project=str(OUT),name=args.run,exist_ok=False,optimizer='AdamW',lr0=.00015,lrf=.1,
        patience=8,cos_lr=True,degrees=5,translate=.04,scale=.10,fliplr=0.,flipud=0.,
        mosaic=0.,mixup=0.,cutmix=0.,copy_paste=0.,hsv_h=.003,hsv_s=.1,hsv_v=.1,
        close_mosaic=0,plots=True,verbose=False,amp=True,save_period=5)
    best=run/'weights/best.pt'
    write(run/'training-result.json',dict(candidate=str(best),sha256=digest(best),seconds=time.perf_counter()-started,
        epochs_requested=args.epochs,device=torch.cuda.get_device_name(0),dataset_audit_sha256=digest(OUT/'dataset-audit.json'),
        selection='Ultralytics best checkpoint using cycle 4 validation only. Final cycle 5 test remains unevaluated.'))


def frozen_candidate(path):
    frozen=read(OUT/'baseline-frozen.json')
    config=frozen['config']
    del frozen['assets'][config['model']]
    config['model']=path.resolve().relative_to(ROOT).as_posix()
    config['version']='sticker-appearance-v6-candidate'
    config['model_name']='YOLO26m sticker appearance v6 candidate'
    frozen['assets'][config['model']]=digest(path)
    frozen['config_hash']=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    return frozen


def evaluate(args):
    """Paired full-pipeline comparison, keeping UNKNOWN in the denominator."""
    import cv2
    import numpy as np
    import torch
    from algorithms.stickers import AppearanceStickerDetector,create_sequence,summarize,compliance
    runtime()
    candidate=args.model or OUT/args.run/'weights/best.pt'
    configs={'v5':read(OUT/'baseline-frozen.json'),'candidate':frozen_candidate(candidate)}
    selected={'validation':{'val','regression'},'test':{'test'},'all':{'train','val','test','regression'}}[args.split]
    if args.split in ('test','all'):
        selection=read(OUT/'selection.json')
        assert selection['sha256']==digest(candidate), 'Test requires a checkpoint frozen using validation only'
    cycles=[c for c in read(OUT/'reviewed-annotations.json')['cycles'] if c['split'] in selected]
    truths={r['file']:r for r in read(OUT/'reviewed-truth.json') if r['split'] in selected}
    models={name:AppearanceStickerDetector(cfg) for name,cfg in configs.items()}
    for model in models.values():
        for _ in range(3):model.inspect(model.reference)
    output=OUT/f'evaluation-{args.split}-{args.run}'
    output.mkdir(exist_ok=False)
    frame_logs={n:[] for n in models};scores={n:[] for n in models};timings={n:[] for n in models}
    sequences={n:{c['id']:create_sequence(configs[n]['config']) for c in cycles} for n in models}
    cycle_frames={n:defaultdict(list) for n in models}
    rows=[r for r in observations() if any(c['sequence_range'][0]<=r['time']<=c['sequence_range'][1] for c in cycles)]
    for index,row in enumerate(rows):
        cycle=next(c for c in cycles if c['sequence_range'][0]<=row['time']<=c['sequence_range'][1])
        image=cv2.imread(str(OUT/'frames'/row['file']))
        for name in (list(models) if index%2==0 else list(models)[::-1]):
            cv2.setRNGSeed(0)
            torch.cuda.synchronize();started=time.perf_counter()
            obs=models[name].inspect(image)
            torch.cuda.synchronize();elapsed=(time.perf_counter()-started)*1000
            timings[name].append(elapsed)
            sequence=sequences[name][cycle['id']].update(row['time'],obs)
            frame_logs[name].append(dict(file=row['file'],time=row['time'],cycle=cycle['id'],split=cycle['split'],inspection=obs,elapsedMs=elapsed,sequence=sequence))
            cycle_frames[name][cycle['id']].append(dict(time=row['time'],inspection=obs))
            truth=truths.get(row['file'])
            if truth:
                for target in obs['targets']:
                    cls=truth['labels'].get(target['id'])
                    if cls is None:continue
                    expected='EMPTY' if cls==2 else 'PRESENT'
                    scores[name].append(dict(file=row['file'],time=row['time'],cycle=cycle['id'],split=cycle['split'],id=target['id'],expected=expected,actual=target['state'],score=target.get('score')))
        if index%40==0:print(f'Evaluated {index+1}/{len(rows)} paired frames',flush=True)
    def metric(rows):
        count=len(rows);bare=[r for r in rows if r['expected']=='EMPTY'];installed=[r for r in rows if r['expected']=='PRESENT']
        correct=sum(r['expected']==r['actual'] for r in rows)
        return dict(n=count,correct=correct,accuracy=correct/count if count else None,
            bare=len(bare),false_present=sum(r['actual']=='PRESENT' for r in bare),bare_unknown=sum(r['actual']=='UNKNOWN' for r in bare),
            installed=len(installed),false_empty=sum(r['actual']=='EMPTY' for r in installed),installed_unknown=sum(r['actual']=='UNKNOWN' for r in installed),
            wrong_shape=sum(r['actual']=='WRONG' for r in rows),unknown=sum(r['actual']=='UNKNOWN' for r in rows))
    report=dict(candidate=str(candidate),candidate_sha256=digest(candidate),source_sha256=read(OUT/'source.json')['sha256'],
        sampling_fps=2,scope='Same-video held-out cycles; assistant-reviewed visible sites. UNKNOWN counts as incorrect. No real defective finished items supplied.',
        annotations_sha256=digest(OUT/'reviewed-annotations.json'),models={})
    for name in models:
        with (output/(name+'-frames.jsonl')).open('w',encoding='utf-8') as f:
            for row in frame_logs[name]:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        write(output/(name+'-site-scores.json'),scores[name])
        cycle_results={}
        for c in cycles:
            sequence=sequences[name][c['id']].snapshot()
            targets,assessment=summarize(cycle_frames[name][c['id']],configs[name]['config'],sequences[name][c['id']],True)
            cycle_results[str(c['id'])]=dict(split=c['split'],sequence=sequence,compliance=compliance(targets,sequence,True),assessment=assessment,
                targets=[dict(id=t['id'],state=t['state']) for t in targets])
        report['models'][name]=dict(overall=metric(scores[name]),
            splits={s:metric([r for r in scores[name] if r['split']==s]) for s in sorted(selected)},
            per_site={s:metric([r for r in scores[name] if r['id']==s]) for s in [t['id'] for t in configs[name]['config']['targets']]},
            latency_ms=dict(median=float(np.median(timings[name])),p95=float(np.percentile(timings[name],95)),frames=len(timings[name])),cycles=cycle_results)
    # Blank inputs must not produce a completed product or order evidence.
    controls={}
    for name,model in models.items():
        controls[name]=[]
        for value in (0,127,255):
            obs=model.inspect(np.full(model.reference.shape,value,np.uint8));sequence=create_sequence(configs[name]['config'])
            for t in range(6):sequence.update(float(t),obs)
            controls[name].append(dict(gray=value,registered=obs['registered'],states=[t['state'] for t in obs['targets']],sequence=sequence.snapshot()['status']))
        assert all(not c['registered'] and c['sequence']!='PASS' for c in controls[name])
    report['blank_controls']=controls
    write(output/'report.json',report)
    print(json.dumps({n:dict(overall=r['overall'],latency=r['latency_ms'],cycles={k:v['compliance']['status'] for k,v in r['cycles'].items()}) for n,r in report['models'].items()},ensure_ascii=False),flush=True)


def replay(args):
    """Replay complete procedures at the station's actual 10 FPS target."""
    import cv2
    import numpy as np
    from algorithms.stickers import AppearanceStickerDetector,create_sequence,summarize,compliance
    runtime()
    candidate=args.model or OUT/args.run/'weights/best.pt'
    if args.split in ('test','all'):
        assert read(OUT/'selection.json')['sha256']==digest(candidate)
    configs={'v5':read(OUT/'baseline-frozen.json'),'candidate':frozen_candidate(candidate)}
    models={name:AppearanceStickerDetector(cfg) for name,cfg in configs.items()}
    selected={'validation':{'val','regression'},'test':{'test'},'all':{'train','val','test','regression'}}[args.split]
    cycles=[c for c in read(OUT/'reviewed-annotations.json')['cycles'] if c['split'] in selected]
    output=OUT/f'replay10-{args.split}-{args.run}'
    output.mkdir(exist_ok=False)
    report=dict(candidate_sha256=digest(candidate),sample_fps=10,source_sha256=read(OUT/'source.json')['sha256'],
        scope='Full appearance detector and unchanged sequence rules, browser-style input, source timestamps, no person detection or HTTP; offline replay, not real-time throughput.',cycles={})
    for cycle in cycles:
        cap=cv2.VideoCapture(str(args.video));assert cap.set(cv2.CAP_PROP_ORIENTATION_AUTO,1)
        fps=cap.get(cv2.CAP_PROP_FPS);lo,hi=cycle['sequence_range'];start=round(lo*fps)
        assert cap.set(cv2.CAP_PROP_POS_FRAMES,start)
        sequences={n:create_sequence(configs[n]['config']) for n in models};frames={n:[] for n in models}
        logs={n:(output/f'cycle{cycle["id"]}-{n}.jsonl').open('w',encoding='utf-8') for n in models}
        next_sample=lo;sample=0
        try:
            for index in range(start,int(hi*fps)+1):
                if not cap.grab():raise RuntimeError('Unexpected replay decode end')
                stamp=index/fps
                if stamp+1e-6<next_sample:continue
                next_sample+=.1
                ok,native=cap.retrieve();assert ok
                h,w=native.shape[:2];scale=min(1.,1920/max(h,w),(1920*1080/(h*w))**.5)
                image=cv2.resize(native,(int(w*scale),int(h*scale)),interpolation=cv2.INTER_AREA)
                ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,82]);assert ok
                image=cv2.imdecode(encoded,cv2.IMREAD_COLOR)
                for name in (list(models) if sample%2==0 else list(models)[::-1]):
                    cv2.setRNGSeed(0)
                    obs=models[name].inspect(image)
                    sequence=sequences[name].update(stamp,obs)
                    frames[name].append(dict(time=stamp,inspection=obs))
                    logs[name].write(json.dumps(dict(time=stamp,inspection=obs,sequence=sequence),ensure_ascii=False)+'\n')
                sample+=1
                if sample%100==0:print(f'Cycle {cycle["id"]}: {sample} paired samples at {stamp:.1f}s',flush=True)
        finally:
            cap.release()
            for f in logs.values():f.close()
        results={}
        for name in models:
            sequence=sequences[name].snapshot()
            targets,assessment=summarize(frames[name],configs[name]['config'],sequences[name],True)
            results[name]=dict(sequence=sequence,compliance=compliance(targets,sequence,True),assessment=assessment,
                targets=[dict(id=t['id'],state=t['state']) for t in targets],sample_count=len(frames[name]))
        report['cycles'][str(cycle['id'])]=dict(split=cycle['split'],models=results)
        write(output/'report.json',report)
        print(json.dumps(dict(cycle=cycle['id'],result={n:r['compliance'] for n,r in results.items()}),ensure_ascii=False),flush=True)


def prepare(args):
    import cv2
    import numpy as np
    from algorithms.stickers import AppearanceStickerDetector, snapshot
    runtime()
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ('frames', 'aligned'):
        (OUT/name).mkdir(exist_ok=True)
    frozen = snapshot()
    write(OUT/'baseline-frozen.json', frozen)
    detector = AppearanceStickerDetector(frozen)
    cap = cv2.VideoCapture(str(args.video))
    raw_rotation = cap.get(cv2.CAP_PROP_ORIENTATION_META)
    assert cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1), 'Orientation metadata must be honored'
    fps, count = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start, next_sample, sampled = time.perf_counter(), 0., 0
    captured = {}
    find_homography = cv2.findHomography
    def record_matrix(*a, **kw):
        matrix, keep = find_homography(*a, **kw)
        captured.update(matrix=matrix, keep=keep)
        return matrix, keep
    cv2.findHomography = record_matrix
    try:
        with (OUT/'baseline-frames.jsonl').open('w', encoding='utf-8') as log:
            for index in range(count):
                if not cap.grab():
                    raise RuntimeError('Video decode interrupted')
                stamp = index/fps
                if stamp + 1e-6 < next_sample:
                    continue
                next_sample += 1/args.fps
                ok, native = cap.retrieve()
                if not ok:
                    raise RuntimeError('Cannot retrieve video frame')
                h,w = native.shape[:2]
                scale = min(1.,1920/max(h,w),(1920*1080/(h*w))**.5)
                image = cv2.resize(native,(int(w*scale),int(h*scale)),interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,82])
                assert ok
                image = cv2.imdecode(encoded,cv2.IMREAD_COLOR)
                filename = f'{sampled:05}.jpg'
                (OUT/'frames'/filename).write_bytes(encoded.tobytes())
                captured.clear()
                observation = detector.inspect(image)
                matrix,keep = captured.get('matrix'),captured.get('keep')
                aligned_name = None
                if matrix is not None and keep is not None and keep.sum()>=12 and keep.mean()>=.3:
                    scaling = np.diag([detector.scale,detector.scale,1.])
                    H = np.linalg.inv(scaling)@matrix@scaling
                    corners = cv2.perspectiveTransform(np.float32([[[190,700],[780,700],[780,1290],[190,1290]]]),H)[0]
                    area = abs(cv2.contourArea(corners))
                    if np.isfinite(corners).all() and .45*590**2<area<2*590**2:
                        aligned = cv2.warpPerspective(image,np.linalg.inv(H),(detector.width,detector.height))
                        aligned_name = filename
                        cv2.imwrite(str(OUT/'aligned'/filename),aligned[690:1300,180:800],[cv2.IMWRITE_JPEG_QUALITY,96])
                log.write(json.dumps(dict(time=stamp,frame=index,file=filename,aligned=aligned_name,
                                          observation=observation),ensure_ascii=False)+'\n')
                sampled += 1
                if sampled%50==0:
                    print(json.dumps(dict(samples=sampled,seconds=round(stamp,2),elapsed=round(time.perf_counter()-start,1))),flush=True)
    finally:
        cap.release()
        cv2.findHomography = find_homography
    write(OUT/'preparation.json',dict(video=str(args.video),rotationMetadata=raw_rotation,
        autoOrientation=True,samples=sampled,fps=args.fps,seconds=time.perf_counter()-start,
        preprocessing='OpenCV metadata orientation; INTER_AREA to 1920 long edge and 2073600 pixels; JPEG82',
        alignedOffset=[180,690],labels='Not generated. Baseline predictions are diagnostic only.'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','build','train','evaluate','replay'])
    p.add_argument('--video',type=Path,default=SOURCE)
    p.add_argument('--fps',type=float,default=2)
    p.add_argument('--run',default='training-v6')
    p.add_argument('--epochs',type=int,default=30)
    p.add_argument('--model',type=Path)
    p.add_argument('--split',choices=['validation','test','all'],default='validation')
    args=p.parse_args()
    globals()[args.command](args)


if __name__=='__main__':
    main()
