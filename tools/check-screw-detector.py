"""Real inference checks plus explicitly synthetic image stress tests."""
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector


def main():
    out=ROOT/'outputs/screw-inspection-2026-10-17'
    original=cv2.resize(cv2.imread(str(out/'frame-51.jpg')),(1920,1080))
    detector=ScrewDetector()
    cases=[('source-final-frame',original,'real-source')]
    cases.append(('blank-frame',np.zeros_like(original),'synthetic-blank'))
    occluded=original.copy();occluded[170:1010,520:990]=100
    cases.append(('product-occluded',occluded,'synthetic-occlusion'))
    changed=original.copy()
    x,y=map(round,detector.config['targets'][0]['point'])
    # Real open-boss crop, relocated digitally; this is not a real defective
    # product. Use only to check that removing one head does not stay PRESENT.
    hx,hy=1194,727
    changed[y-40:y+40,x-40:x+40]=original[hy-40:hy+40,hx-40:hx+40]
    cases.append(('synthetic-head-to-hole',changed,'synthetic-transplant-of-real-empty-hole'))
    source=json.loads((out/'source.json').read_text(encoding='utf-8'))
    removed=next(e for e in source['samples'] if e['time']>=53)
    cases.append(('product-removed-source',cv2.imread(str(out/'frames'/removed['file'])),'real-source'))
    results=[];tiles=[]
    for name,image,provenance in cases:
        result=detector.inspect(image)
        states={t['id']:t['state'] for t in result['targets']}
        results.append(dict(name=name,provenance=provenance,registered=result['registered'],states=states,
                            present=sum(s=='PRESENT' for s in states.values())))
        tile=cv2.resize(image,(960,540))
        for target in result['targets']:
            if target['bbox'] is None:continue
            a,b,c,d=[round(v*s) for v,s in zip(target['bbox'],[960,540,960,540])]
            color=(110,215,30) if target['state']=='PRESENT' else (0,90,240) if target['state']=='EMPTY' else (190,190,190)
            cv2.rectangle(tile,(a-3,b-3),(c+3,d+3),color,2)
            cv2.putText(tile,target['id'],(a-5,b-7),0,.4,color,1)
        cv2.putText(tile,name,(20,35),0,.7,(20,30,240),2)
        tiles.append(tile)
    assert results[1]['present']==0
    assert results[2]['present']==0
    assert results[3]['states']['S01']!='PRESENT'
    assert results[4]['registered'] is False
    assert results[4]['present']==0
    tiles.append(np.zeros_like(tiles[0]))
    cv2.imwrite(str(out/'inference-checks.jpg'),np.vstack([np.hstack(tiles[i:i+2]) for i in range(0,len(tiles),2)]))
    (out/'inference-checks.json').write_text(json.dumps(dict(cases=results,
        warning='Synthetic stress tests do not measure real missing-screw sensitivity.'),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':main()
