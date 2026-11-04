"""Compare the production gate to per-position candidates on actual source frames."""
import json
from pathlib import Path
import sys
import time
import cv2

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector

OUT=ROOT/'outputs/screw-inspection-2026-10-17/refinement'
OUT.mkdir(exist_ok=True)
source=json.loads((OUT.parent/'source.json').read_text())
detector=ScrewDetector()
records=[]
for t in [0,5,8,12,15,18,20,24,28,31,35,40,45,49,51,53]:
    entry=min(source['samples'],key=lambda e:abs(e['time']-t))
    frame=cv2.imread(str(OUT.parent/'frames'/entry['file']))
    detector.config['minimum_visible_components']=2
    baseline=detector.inspect(frame)
    detector.config['minimum_visible_components']=0
    candidate=detector.inspect(frame)
    record={'time':entry['time'],'baseline':[p['id'] for p in baseline['targets'] if p['state']=='PRESENT'],
      'candidate':[{k:p.get(k) for k in ['id','state','score','detectedBbox']} for p in candidate['targets']],
      'component_scores':candidate.get('componentScores'),'inliers':candidate['inliers'],
      'reason':candidate['reason'],'processingMs':candidate.get('inferenceMs')}
    records.append(record)
    print(round(entry['time'],2),len(record['baseline']),[(p['id'],p['score']) for p in record['candidate'] if p['state']=='PRESENT'],flush=True)
(OUT/'gate-diagnosis.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
