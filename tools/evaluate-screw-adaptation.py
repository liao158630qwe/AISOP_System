"""Development gates on both known videos before activating mixed-view weights."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector,current_display
from algorithms.video import file_hash
OUT=ROOT/'outputs/screw-adaptation-v4-2026-10-17'


def main():
    frozen=json.loads((OUT/'candidate-before-training.json').read_text(encoding='utf-8'))
    baseline=copy.deepcopy(frozen)
    name=json.loads((OUT/'training-result.json').read_text(encoding='utf-8'))['model']
    primary='models/screw-head-v4-yolo26m.pt'
    frozen['config']['calibrations'][0]['target_models']={'S02':name}
    frozen['config'].update(model=primary,version='screw-video-v4.1-multiview',
        model_name='YOLO26m 螺丝增强 v4.1 · S02专用增强',
        scope='按当前帧匹配的标定视角核对可见位置：原视角12处，新俯视视角11处。视角内编号对应各自的位置清单；不以孔位或工序总次数替代可见螺丝证据。不包含扭矩验证。',
        training_result='outputs/screw-adaptation-v4-2026-10-17/training-result.json')
    frozen['assets']={p:file_hash(ROOT/p) for p in [primary,name,frozen['config']['reference']]+[p['reference'] for p in frozen['config']['calibrations']]}
    (OUT/'candidate.json').write_text(json.dumps(frozen,ensure_ascii=False,indent=2),encoding='utf-8')
    spec=importlib.util.spec_from_file_location('focus',ROOT/'tools/evaluate-screw-refinement.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    oldroot=ROOT/'outputs/screw-inspection-2026-10-17'
    oldsource=json.loads((oldroot/'source.json').read_text())
    results={};records=[]
    for model,f in [('v3-plus-calibration',baseline),('v4',frozen)]:
        detector=ScrewDetector(f);old_hits=old_wrong=old_total=0;latencies=[];controls=[]
        for t,truth in module.FOCUS.items():
            entry=min(oldsource['samples'],key=lambda x:abs(x['time']-t))
            r=detector.inspect(cv2.imread(str(oldroot/'frames'/entry['file'])))
            values={p['id']:p['state'] for p in r['targets']}
            old_hits+=sum(expected=='PRESENT' and values.get(k)=='PRESENT' for k,expected in truth.items())
            old_total+=sum(expected=='PRESENT' for expected in truth.values())
            old_wrong+=sum(values.get(k) not in (expected,'UNKNOWN') for k,expected in truth.items())
            records.append(dict(model=model,source='old',time=t,inspection=r))
        new=[]
        for resolution in ['native','1080p']:
            for t in [22,36,48,60,66,72,76,77,78,79.5]:
                image=cv2.imread(str(OUT/f'frame-{t:g}.jpg'))
                if resolution=='1080p':image=cv2.resize(image,(1920,1080))
                started=time.perf_counter();r=detector.inspect(image);ms=(time.perf_counter()-started)*1000
                latencies.append(ms)
                item=dict(resolution=resolution,time=t,registered=r['registered'],
                    heads=sum(p['state']=='PRESENT' for p in r['targets']),
                    empty=[p['id'] for p in r['targets'] if p['state']=='EMPTY'],ms=ms,
                    unknown=[p['id'] for p in r['targets'] if p['state']=='UNKNOWN'])
                new.append(item);records.append(dict(model=model,source='new',resolution=resolution,time=t,inspection=r))
                if model=='v4' and t==76:
                    canvas=cv2.resize(image,(1920,1080))
                    for p in current_display(r)['displayTargets']:
                        a,b,c,d=[round(v*s) for v,s in zip(p['bbox'],[1920,1080,1920,1080])]
                        color=(40,220,80) if p['state']=='PRESENT' else (0,0,255)
                        cv2.rectangle(canvas,(a,b),(c,d),color,2);cv2.putText(canvas,p['id'],(a,b-5),0,.5,color,1)
                    cv2.imwrite(str(OUT/f'v4-{resolution}-76.jpg'),canvas)
        oldremoved=min(oldsource['samples'],key=lambda x:abs(x['time']-53))
        cases=[('blank',np.zeros((1080,1920,3),np.uint8)),
            ('old-removed',cv2.imread(str(oldroot/'frames'/oldremoved['file']))),
            ('new-removed84',cv2.imread(str(OUT/'frame-84.jpg'))),
            ('new-removed88',cv2.imread(str(OUT/'frame-88.jpg')))]
        for label,image in cases:
            r=current_display(detector.inspect(image))
            controls.append(dict(case=label,registered=r['registered'],boxes=len(r['displayTargets'])))
        results[model]=dict(old_heads=old_hits,old_total=old_total,old_wrong=old_wrong,new=new,controls=controls,
            median_ms=float(np.median(latencies)),p95_ms=float(np.percentile(latencies,95)))
        print(model,json.dumps(results[model]),flush=True)
    candidate=results['v4'];baseline=results['v3-plus-calibration']
    checks=dict(old_heads_not_worse=candidate['old_heads']>=baseline['old_heads'],
        old_wrong_not_worse=candidate['old_wrong']<=baseline['old_wrong'],
        new_clear_frame_all_visible=all(x['heads']==11 and not x['empty'] for x in candidate['new'] if x['time']==76),
        new_clear_frame_improved=sum(x['heads'] for x in candidate['new'] if x['time']==76)>
            sum(x['heads'] for x in baseline['new'] if x['time']==76),
        new_reviewed_empty_not_heads=all(
            p['state']!='PRESENT' for record in records if record['model']=='v4' and record['source']=='new' and record['time']==22
            for p in record['inspection']['targets'] if p['id'] in ['S01','S02','S09','S10','S11']),
        no_removed_or_blank_boxes=all(x['boxes']==0 and not x['registered'] for x in candidate['controls']),
        p95_below_500ms=candidate['p95_ms']<500)
    payload=dict(scope='development checks on both known videos; new video is used for calibration/training',
        summary=results,checks=checks,accepted=all(checks.values()),records=records)
    (OUT/'comparison.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print('GATES',json.dumps(checks),flush=True)


if __name__=='__main__':main()
