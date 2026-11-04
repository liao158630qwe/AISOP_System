import json
from pathlib import Path
import sys
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector
base=ROOT/'outputs/screw-inspection-2026-10-17'
d=ScrewDetector();a=cv2.resize(cv2.imread(str(base/'frame-51.jpg')),(1920,1080))
for radius in [8,12,16,20,40]:
    image=a.copy();x,y=759,252;hx,hy=1194,727
    image[y-radius:y+radius,x-radius:x+radius]=a[hy-radius:hy+radius,hx-radius:hx+radius]
    result=d.inspect(image)
    print(radius,result['registered'],[(t['id'],t['state'],t['score'],t.get('contextScore')) for t in result['targets'] if t['id']=='S01'])
