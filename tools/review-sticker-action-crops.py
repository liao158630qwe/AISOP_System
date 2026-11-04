import json
from pathlib import Path
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
cfg=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf8'))
files=sorted((OUT/'aligned').glob('*.jpg'))
for target in cfg['targets']:
    tiles=[];x,y=map(round,target['point'])
    for file in files:
        im=cv2.imread(str(file));crop=im[y-90:y+90,x-90:x+90]
        tile=cv2.resize(crop,(120,120));cv2.putText(tile,file.stem,(3,17),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,0,255),1);tiles.append(tile)
    tiles += [np.zeros_like(tiles[0])]*((-len(tiles))%10)
    cv2.imwrite(str(OUT/('action-crops-'+target['id']+'.jpg')),np.vstack([np.hstack(tiles[i:i+10]) for i in range(0,len(tiles),10)]))
