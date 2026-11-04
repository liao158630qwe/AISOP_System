"""Compare current/new models under unchanged current-frame inspection rules."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from algorithms.screws import snapshot
from algorithms.screw_refined import RefinedScrewDetector
from algorithms.video import file_hash
spec=importlib.util.spec_from_file_location('screw_focus',ROOT/'tools/evaluate-screw-refinement.py')
focus_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(focus_module)
FOCUS=focus_module.FOCUS

BASE=ROOT/'outputs/screw-inspection-2026-10-17'


def transform(image,variant):
    if variant=='native':return image
    if variant=='jpeg40':
        _,payload=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,40]);return cv2.imdecode(payload,cv2.IMREAD_COLOR)
    if variant=='dim70':return np.clip(image.astype(np.float32)*.7,0,255).astype(np.uint8)
    if variant=='720p':return cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    if variant=='blur06':return cv2.GaussianBlur(image,(3,3),.6)
    raise ValueError(variant)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',default='yolo26m');args=parser.parse_args()
    out=BASE/'next-models'/args.model;out.mkdir(parents=True,exist_ok=True)
    source=json.loads((BASE/'source.json').read_text())
    frozen=snapshot();summaries={};records=[]
    specs=[('yolo11s-v2','models/screw-head-v2s.pt',256),
           (args.model+'-v3','models/screw-head-v3-'+args.model+'.pt',320)]
    for name,weight,size in specs:
        config=copy.deepcopy(frozen);config['config'].update(model=weight,detector_input=size,threshold=.45)
        config['assets']={weight:file_hash(ROOT/weight),config['config']['reference']:file_hash(ROOT/config['config']['reference'])}
        detector=RefinedScrewDetector(config);model_summary={}
        for variant in ['native','jpeg40','dim70','720p','blur06']:
            positive_hits=positive_total=empty_heads=correct=wrong=unknown=0;latencies=[]
            for t,truth in FOCUS.items():
                entry=min(source['samples'],key=lambda e:abs(e['time']-t))
                image=transform(cv2.imread(str(BASE/'frames'/entry['file'])),variant)
                start=time.perf_counter();result=detector.inspect(image);latencies.append((time.perf_counter()-start)*1000)
                values={p['id']:p['state'] for p in result['targets']}
                for identity,expected in truth.items():
                    predicted=values[identity]
                    positive_total+=expected=='PRESENT';positive_hits+=expected=='PRESENT' and predicted=='PRESENT'
                    empty_heads+=expected=='EMPTY' and predicted=='PRESENT';correct+=predicted==expected
                    wrong+=predicted!='UNKNOWN' and predicted!=expected;unknown+=predicted=='UNKNOWN'
                records.append({'model':name,'variant':variant,'time':entry['time'],'truth':truth,
                    'predicted':values,'registered':result['registered'],'processing_ms':latencies[-1]})
            controls=[]
            for t in [52.75,53,53.25,53.5]:
                entry=min(source['samples'],key=lambda e:abs(e['time']-t))
                result=detector.inspect(transform(cv2.imread(str(BASE/'frames'/entry['file'])),variant))
                controls.append({'time':entry['time'],'heads':sum(p['state']=='PRESENT' for p in result['targets']),
                    'registered':result['registered']})
            model_summary[variant]={'correct':correct,'wrong':wrong,'unknown':unknown,
                'visible_heads_found':positive_hits,'visible_heads':positive_total,'empty_holes_called_heads':empty_heads,
                'median_ms':float(np.median(latencies[2:])),'p95_ms':float(np.percentile(latencies[2:],95)),
                'removed_product_controls':controls}
            print(name,variant,json.dumps(model_summary[variant]),flush=True)
        summaries[name]=model_summary
        del detector
    payload={'scope':'same known development video/positions; JPEG/dim/downsample/blur are synthetic stress transforms, not independent field testing',
        'threshold':.45,'pipeline':'identical local-roi-v2 current-frame-only rules','summary':summaries,'records':records}
    (out/'comparison.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
