import copy
import json
from pathlib import Path
import sys
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import snapshot
from algorithms.screw_refined import RefinedScrewDetector
from algorithms.video import file_hash
base=ROOT/'outputs/screw-inspection-2026-10-17'
frozen=snapshot();path='models/screw-head-v2s.pt'
frozen['config'].update(model=path,threshold=.15,detector_input=256,local_context_threshold=-1)
frozen['assets']={path:file_hash(ROOT/path),frozen['config']['reference']:file_hash(ROOT/frozen['config']['reference'])}
d=RefinedScrewDetector(frozen)
source=json.loads((base/'source.json').read_text())
for t in [8,20,28,35,45,51,53]:
    e=min(source['samples'],key=lambda e:abs(e['time']-t));r=d.inspect(cv2.imread(str(base/'frames'/e['file'])))
    print(t,r.get("componentScores"),r.get("componentInliers"),[(p['id'],p['state'],p['score'],p.get('contextScore')) for p in r['targets']])
