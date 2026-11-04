"""Real model/API live-cycle checks; synthetic input is explicitly identified."""
import argparse
import json
import time
from pathlib import Path
import uuid
import cv2
import numpy as np
import requests

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/screw-inspection-2026-10-17'
BASE='http://127.0.0.1:8000/api/v1'


def session():
    client=requests.Session();client.trust_env=False
    response=client.post(BASE+'/login',json=json.loads((ROOT/'.runtime/local-settings.json').read_text()),timeout=20)
    response.raise_for_status();client.headers['X-CSRF-Token']=response.json()['csrf']
    return client


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=OUT)
    parser.add_argument('--include-new-view',action='store_true');args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    client=session();other=session()
    source=json.loads((OUT/'source.json').read_text())
    images=[]
    for t in [50.53,50.76,51.03]:
        entry=min(source['samples'],key=lambda e:abs(e['time']-t))
        images.append(cv2.imread(str(OUT/'frames'/entry['file'])))
    path=ROOT/'.runtime/screw-live-camera.y4m'
    with path.open('wb') as f:
        f.write(b'YUV4MPEG2 W1920 H1080 F4:1 Ip A1:1 C420jpeg\n')
        for image in images*4:
            f.write(b'FRAME\n');f.write(cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420).tobytes())
    synthetic=images[-1].copy()
    # Replace only the head-sized region, preserving component appearance.
    synthetic[240:264,747:771]=images[-1][715:739,1182:1206]
    cases=[('recorded-correct-source',images*2,'real-source'),
           ('blank',[np.zeros_like(images[0])]*2,'synthetic-blank'),
           ('empty-hole-transplant',[synthetic]*2,'synthetic-empty-hole-transplant')]
    expected={'recorded-correct-source':'PASS','blank':'REVIEW','empty-hole-transplant':'FAIL'}
    if args.include_new_view:
        folder=ROOT/'outputs/screw-adaptation-v4-2026-10-17'
        for name,t,verdict in [('new-view-correct-stills',76,'PASS'),('new-view-occluded-stills',78,'REVIEW'),
                              ('new-view-product-removed',84,'REVIEW'),('new-view-unfinished-process',22,'FAIL')]:
            image=cv2.resize(cv2.imread(str(folder/f'frame-{t}.jpg')),(1920,1080))
            cases.append((name,[image]*2,'real-recorded-source-stills-simulating-camera-samples'))
            expected[name]=verdict
        changed=cv2.resize(cv2.imread(str(folder/'frame-76.jpg')),(1920,1080))
        open_hole=cv2.resize(cv2.imread(str(folder/'frame-22.jpg')),(1920,1080))
        x,y=937,769
        changed[y-10:y+10,x-10:x+10]=open_hole[y-10:y+10,x-10:x+10]
        cases.append(('new-view-empty-hole-transplant',[changed]*2,'synthetic-transplant-of-real-new-view-empty-hole'))
        expected['new-view-empty-hole-transplant']='FAIL'
    results=[]
    for name,frames,provenance in cases:
        identity=str(uuid.uuid4())
        wall_ms=[]
        for i,image in enumerate(frames):
            ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94]);assert ok
            started=time.perf_counter()
            response=client.post(BASE+'/realtime/frame?analysis_type=screw',data=jpeg.tobytes(),
                headers={'Content-Type':'image/jpeg','X-Stream-Id':identity,'X-Frame-Time':str(i*.25)},timeout=60)
            response.raise_for_status();data=response.json()
            wall_ms.append(round((time.perf_counter()-started)*1000,2))
            assert len(data['inspection']['displayTargets'])<=12
            assert all(t['bbox'] is not None for t in data['inspection']['displayTargets'])
            assert all(t['positionMode']=='DETECTED' and t['state'] in ('PRESENT','EMPTY') for t in data['inspection']['displayTargets'])
            if name in ('blank','new-view-product-removed'):assert data['inspection']['displayTargets']==[]
        assert other.post(BASE+f'/realtime/streams/{identity}/finish',timeout=15).status_code==404
        response=client.post(BASE+f'/realtime/streams/{identity}/finish',timeout=15)
        response.raise_for_status();result=response.json()
        assert client.post(BASE+f'/realtime/streams/{identity}/finish',timeout=15).json()==result
        assert result['compliance']['status']==expected[name],result
        sequence=result.get('sequence')
        if name=='recorded-correct-source':
            assert sequence is not None,result
            assert sequence['configured'] is True
            assert sequence['verificationMode']=='visual-cumulative'
            assert sequence['status']=='PASS'
            assert len(sequence['completed'])==10,sequence
            assert sequence['failure'] is None
        if name.startswith('new-view-') and sequence is not None:
            assert sequence['configured'] is True,sequence
            assert sequence['verificationMode']=='visual-cumulative',sequence
        if name=='new-view-empty-hole-transplant':
            assert next(t for t in result['targets'] if t['id']=='S02')['state']=='EMPTY'
            assert all(t['state']=='PRESENT' for t in result['targets'] if t['id']!='S02')
        results.append({'case':name,'provenance':provenance,'request_wall_ms':wall_ms,'result':result})
        print(name,result['compliance']['status'],flush=True)
        client.delete(BASE+f'/realtime/streams/{identity}',timeout=15).raise_for_status()
    (args.out/'realtime-api-test.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
