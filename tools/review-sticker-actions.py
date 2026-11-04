"""Review actual glove/fingertip evidence; no runtime timestamp rules."""
import sys,json
from pathlib import Path
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.hands import HandLandmarker
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'

def main():
    detector=HandLandmarker(ROOT/'models/hand_landmarker.task');records=[];tiles=[]
    targets=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf8'))['targets']
    for path in sorted((OUT/'aligned').glob('*.jpg')):
        t=float(path.stem);im=cv2.imread(str(path));crop=im[620:1400,120:880].copy()
        hands=detector.detect(crop)
        for hand in hands:
            hand['points']=[[p[0]*760+120,p[1]*780+620,p[2]] for p in hand['points']]
            for i in [0,4,8,12,16,20]:
                x,y,_=hand['points'][i];cv2.circle(crop,(round(x-120),round(y-620)),5,(0,0,255),-1)
                cv2.putText(crop,str(i),(round(x-120),round(y-620)),cv2.FONT_HERSHEY_SIMPLEX,.4,(0,0,255),1)
        records.append(dict(time=t,hands=hands))
        if t%1==0:
            for target in targets:
                x,y=target['point'];cv2.circle(crop,(round(x-120),round(y-620)),25,(0,255,255),2)
                cv2.putText(crop,target['id'],(round(x-120),round(y-620)),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,0,0),2)
            tile=cv2.resize(crop,(304,312));cv2.putText(tile,f'{t}s / {len(hands)} hands',(5,20),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,0,255),2);tiles.append(tile)
    (OUT/'action-hand-review.json').write_text(json.dumps(records,indent=2),encoding='utf8')
    for page in range(0,len(tiles),16):
        chunk=tiles[page:page+16]
        chunk+= [np.zeros_like(chunk[0])]*(16-len(chunk))
        cv2.imwrite(str(OUT/f'action-hands-{page//16}.jpg'),np.vstack([np.hstack(chunk[i:i+4]) for i in range(0,16,4)]))
    print('frames',len(records),'with hands',sum(bool(r['hands']) for r in records),flush=True)

if __name__=='__main__':main()
