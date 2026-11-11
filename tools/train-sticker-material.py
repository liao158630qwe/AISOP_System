"""CPU training of material-only vetoes from reviewed actual sites."""
import json
from pathlib import Path
import sys
import time
import cv2
import joblib
import numpy as np
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from algorithms.sticker_material import features
from algorithms.video import file_hash

# This legacy script executes training at import time as well.
raise SystemExit('Legacy sklearn SVC training is disabled by the CUDA-only policy. Use tools/train-model.py with a torch feature-classify recipe or a GPU visual detector; validate the candidate before publishing.')

OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
config=json.loads((ROOT/'config/sticker-inspection-candidate.json').read_text(encoding='utf-8'))
provenance=json.loads((OUT/'dataset-provenance.json').read_text(encoding='utf-8'))
training_times={r['time'] for r in provenance['records'] if r['split']=='train'}
intervals=provenance['reviewed_intervals'];sites={};audit=[];started=time.time()
reference=cv2.imread(str(ROOT/config['reference']))
for site in config['targets']:
    if site['expected']!='rectangle':continue
    a,b,c,d=site['reference_bbox'];mask=cv2.erode(np.uint8(cv2.cvtColor(reference[b:d,a:c],cv2.COLOR_BGR2GRAY)<70),np.ones((5,5),np.uint8)).astype(bool)
    before,after=intervals[site['id']];x=[];y=[]
    for entry in json.loads((OUT/'aligned-samples.json').read_text()):
        t=entry['time']
        if t not in training_times:continue
        label=0 if t<=before else 1 if t>=after else None
        if label is None:continue
        image=cv2.imread(str(OUT/'aligned'/entry['file']))[b:d,a:c]
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
        if (gray[mask]>105).mean()>.1:continue
        for quality in [85,94,100]:
            _,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,quality]);pixels=cv2.imdecode(encoded,cv2.IMREAD_COLOR)
            x.append(features(pixels,mask));y.append(label)
        audit.append(dict(id=site['id'],time=t,label=label,source='reviewed real bare/installed material region; outside patch erased'))
    estimator=make_pipeline(StandardScaler(),SVC(C=4,kernel='rbf',gamma='scale',class_weight='balanced',probability=True,random_state=20261017))
    estimator.fit(np.array(x),y)
    sites[site['id']]=dict(estimator=estimator,counts={'bare':y.count(0),'installed':y.count(1)})
path=ROOT/'models/sticker-material-v1.joblib'
joblib.dump(dict(sites=sites,source_sha256=provenance['source_sha256'],version=1),path)
(OUT/'material-training-result.json').write_text(json.dumps(dict(path=path.relative_to(ROOT).as_posix(),sha256=file_hash(path),
    seconds=time.time()-started,counts={k:v['counts'] for k,v in sites.items()},records=audit,
    scope='known source site textures, training times only, no independent field material test'),ensure_ascii=False,indent=2),encoding='utf-8')
print({k:v['counts'] for k,v in sites.items()})
