"""Measured current-frame comparison on explicitly reviewed development cases."""
import copy
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector, snapshot
from algorithms.screw_refined import RefinedScrewDetector
from algorithms.video import file_hash

BASE=ROOT/'outputs/screw-inspection-2026-10-17'
OUT=BASE/'refinement'
# Reviewed from real images before evaluation. This is development evidence,
# including known training times and test frames, not an external accuracy set.
FOCUS={8:{'S01':'EMPTY'},12:{'S09':'EMPTY'},15:{'S09':'EMPTY','S12':'PRESENT'},18:{'S09':'EMPTY'},
 20:{'S02':'PRESENT','S09':'EMPTY','S12':'PRESENT'},24:{'S01':'PRESENT','S02':'PRESENT','S09':'EMPTY'},
 28:{'S01':'PRESENT','S02':'PRESENT','S03':'PRESENT','S09':'EMPTY','S11':'PRESENT','S12':'PRESENT'},
 31:{'S01':'PRESENT','S02':'PRESENT','S03':'PRESENT','S07':'PRESENT','S08':'PRESENT','S09':'EMPTY','S12':'PRESENT'},
 35:{'S01':'PRESENT','S02':'PRESENT','S03':'PRESENT','S04':'PRESENT','S05':'PRESENT','S07':'PRESENT','S08':'PRESENT','S09':'EMPTY'},
 40:{'S01':'PRESENT','S10':'PRESENT','S11':'PRESENT','S12':'PRESENT'},
 45:{'S01':'PRESENT','S02':'PRESENT','S06':'PRESENT','S07':'PRESENT','S08':'PRESENT','S10':'PRESENT','S11':'PRESENT','S12':'PRESENT'},
 49:{'S01':'PRESENT','S02':'PRESENT'},51:{f'S{i:02}':'PRESENT' for i in range(1,13)}}


def main():
    source=json.loads((BASE/'source.json').read_text())
    frozen=snapshot()
    candidates={}
    legacy=copy.deepcopy(frozen);legacy['config'].update(algorithm='legacy',model='models/screw-head-v1.pt',threshold=.35)
    legacy['assets']={'models/screw-head-v1.pt':file_hash(ROOT/'models/screw-head-v1.pt'),legacy['config']['reference']:file_hash(ROOT/legacy['config']['reference'])}
    candidates['legacy-n']=ScrewDetector(legacy)
    for name,path,input_size,threshold in [('local-n','models/screw-head-v1.pt',192,.35),('local-s','models/screw-head-v2s.pt',256,.45)]:
        current=copy.deepcopy(frozen);current['config'].update(model=path,detector_input=input_size,threshold=threshold,local_context_threshold=.1)
        current['assets']={path:file_hash(ROOT/path),current['config']['reference']:file_hash(ROOT/current['config']['reference'])}
        candidates[name]=RefinedScrewDetector(current)
    records=[];summary={};tiles=[]
    for name,detector in candidates.items():
        hits=wrong=unknown=0;positive_hits=positive_total=0;empty_heads=0;times=[]
        for t,truth in FOCUS.items():
            entry=min(source['samples'],key=lambda e:abs(e['time']-t))
            image=cv2.imread(str(BASE/'frames'/entry['file']))
            start=time.perf_counter();result=detector.inspect(image);times.append((time.perf_counter()-start)*1000)
            prediction={p['id']:p['state'] for p in result['targets']}
            for identity,expected in truth.items():
                value=prediction[identity];hits+=value==expected;unknown+=value=='UNKNOWN';wrong+=value!='UNKNOWN' and value!=expected
                if expected=='PRESENT':positive_total+=1;positive_hits+=value=='PRESENT'
                if expected=='EMPTY' and value=='PRESENT':empty_heads+=1
            records.append({'variant':name,'time':entry['time'],'truth':truth,'predicted':prediction,'registered':result['registered'],'ms':times[-1]})
            if name=='local-s' and t in [8,15,24,35,45,51]:
                tile=cv2.resize(image,(960,540))
                for p in result['targets']:
                    if p['state']=='UNKNOWN' or not p.get('detectedBbox'):continue
                    a,b,c,d=[round(v*s) for v,s in zip(p['detectedBbox'],[960,540,960,540])]
                    color=(70,220,50) if p['state']=='PRESENT' else (0,90,240)
                    cv2.rectangle(tile,(a,b),(c,d),color,2);cv2.putText(tile,p['id'],(a,b-5),0,.4,color,1)
                cv2.putText(tile,f'{t}s YOLO11s local',(15,30),0,.75,(0,0,255),2);tiles.append(tile)
        controls=[]
        removed=next(e for e in source['samples'] if e['time']>=53)
        for label,image in [('blank',np.zeros((1080,1920,3),np.uint8)),('product-removed',cv2.imread(str(BASE/'frames'/removed['file'])))]:
            observation=detector.inspect(image);heads=sum(t['state']=='PRESENT' for t in observation['targets'])
            controls.append({'case':label,'heads':heads,'registered':observation['registered']})
        summary[name]={'correct':hits,'wrong':wrong,'unknown':unknown,'reviewed_visible_heads_found':positive_hits,
            'reviewed_visible_heads':positive_total,'empty_holes_called_heads':empty_heads,
            'median_ms_after_warmup':float(np.median(times[2:])),'p95_ms_after_warmup':float(np.percentile(times[2:],95)),
            'controls':controls}
        print(name,json.dumps(summary[name]),flush=True)
    payload={'scope':'assistant-reviewed development positions from the same already-used video; no independent real defect recall',
        'summary':summary,'records':records}
    (OUT/'comparison.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    if tiles:cv2.imwrite(str(OUT/'local-s-comparison.jpg'),np.vstack([np.hstack(tiles[i:i+2]) for i in range(0,len(tiles),2)]))


if __name__=='__main__':main()
