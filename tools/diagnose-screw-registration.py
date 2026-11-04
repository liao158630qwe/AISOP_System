"""Inspect appearance anchors at product removal, independently of head counts."""
import json
from pathlib import Path
import sys
import cv2
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.screws import ScrewDetector
d=ScrewDetector()
out=ROOT/'outputs/screw-inspection-2026-10-17'
source=json.loads((out/'source.json').read_text())
anchors=[(735,290,780,390),(730,730,790,810),(770,480,815,530)]
tiles=[]
for entry in source['samples']:
    if entry['time']<48: continue
    image=cv2.imread(str(out/'frames'/entry['file']))
    key,desc=d.sift.detectAndCompute(cv2.cvtColor(image,cv2.COLOR_BGR2GRAY),None)
    good=[m for m,n in d.matcher.knnMatch(d.descriptors,desc,k=2) if m.distance<.7*n.distance]
    H,inliers=cv2.findHomography(np.float32([d.keypoints[m.queryIdx].pt for m in good]),np.float32([key[m.trainIdx].pt for m in good]),cv2.RANSAC,3)
    aligned=cv2.warpPerspective(image,np.linalg.inv(H),(1920,1080))
    gray=cv2.cvtColor(aligned,cv2.COLOR_BGR2GRAY);ref=cv2.cvtColor(d.reference,cv2.COLOR_BGR2GRAY)
    scores=[]
    for x1,y1,x2,y2 in anchors:
        template=ref[y1:y2,x1:x2]
        scores.append(round(cv2.minMaxLoc(cv2.matchTemplate(gray[y1-12:y2+12,x1-12:x2+12],template,cv2.TM_CCOEFF_NORMED))[1],3))
    print(round(entry['time'],2),int(inliers.sum()),scores,flush=True)
    tile=cv2.resize(aligned[220:960,570:960],(260,493))
    cv2.putText(tile,f'{entry["time"]:.2f}s {scores}',(3,18),0,.36,(0,0,255),1)
    tiles.append(tile)
tiles+=[np.zeros_like(tiles[0])]*((-len(tiles))%6)
cv2.imwrite(str(out/'removal-registration.jpg'),np.vstack([np.hstack(tiles[i:i+6]) for i in range(0,len(tiles),6)]))
