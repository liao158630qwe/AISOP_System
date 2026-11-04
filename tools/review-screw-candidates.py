import json
from pathlib import Path
import cv2
import numpy as np

BASE=Path('outputs/screw-inspection-2026-10-17')
source=json.loads((BASE/'source.json').read_text())
records=json.loads((BASE/'refinement/gate-diagnosis.json').read_text(encoding='utf-8'))
tiles=[]
for record in records:
    entry=min(source['samples'],key=lambda e:abs(e['time']-record['time']))
    image=cv2.imread(str(BASE/'frames'/entry['file']))
    for target in record['candidate']:
        if target['state']!='PRESENT':continue
        a,b,c,d=target['detectedBbox'];x,y=round((a+c)*960),round((b+d)*540)
        crop=image[max(0,y-32):y+32,max(0,x-32):x+32]
        if not crop.size:continue
        tile=np.full((185,160,3),245,np.uint8)
        tile[25:185]=cv2.resize(crop,(160,160))
        cv2.putText(tile,f'{record["time"]:.1f}s {target["id"]} {target["score"]:.2f}',(3,17),0,.38,(40,50,150),1)
        tiles.append(tile)
tiles+=[np.full_like(tiles[0],245)]*((-len(tiles))%8)
cv2.imwrite(str(BASE/'refinement/candidate-review.jpg'),np.vstack([np.hstack(tiles[i:i+8]) for i in range(0,len(tiles),8)]))
