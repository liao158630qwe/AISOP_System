"""Check unchanged completion rules on the actual final recording segment."""
import argparse
import json
from pathlib import Path
import sys
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector,final_observations,compliance
OUT=ROOT/'outputs/screw-adaptation-v4-2026-10-17'

p=argparse.ArgumentParser();p.add_argument('--fps',type=float,default=4);a=p.parse_args()
frozen=json.loads((OUT/'candidate.json').read_text(encoding='utf-8'))
d=ScrewDetector(frozen);cap=cv2.VideoCapture('C:/Users/Administrator/Downloads/lv_0_20261017141529.mp4')
fps=cap.get(5);count=int(cap.get(7));frames=[];next_sample=0
for index in range(count):
    if index/fps<72:
        if not cap.grab():break
        if index/fps+1e-6>=next_sample:next_sample+=1/a.fps
        continue
    ok,image=cap.read()
    if not ok:raise RuntimeError('Video decode interrupted')
    t=index/fps
    if t+1e-6<next_sample:continue
    next_sample+=1/a.fps
    r=d.inspect(image);frames.append(dict(time=round(t,4),inspection=r))
cap.release()
targets,assessment=final_observations(frames,frozen['config']['targets'])
result=dict(fps=a.fps,assessment=assessment,model_assets=frozen['assets'],compliance=compliance(targets),targets=targets,frames=frames)
(OUT/f'completion-{a.fps:g}fps.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(compliance=result['compliance'],targets=targets),ensure_ascii=False),flush=True)
