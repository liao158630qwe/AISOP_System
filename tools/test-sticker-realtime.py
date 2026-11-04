"""Actual API with recorded stills and explicitly synthetic defect controls."""
import json
from pathlib import Path
import time
import uuid
import cv2
import numpy as np
import requests
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
BASE='http://127.0.0.1:8000/api/v1'

def login():
    client=requests.Session();client.trust_env=False
    r=client.post(BASE+'/login',json=json.loads((ROOT/'.runtime/local-settings.json').read_text(encoding='utf-8')),timeout=20);r.raise_for_status()
    client.headers['X-CSRF-Token']=r.json()['csrf'];return client

def main():
    active=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf-8'))
    if active.get('mode')=='action':
        raise SystemExit('当前贴条任务为动作模式，请运行 tools/test-sticker-actions-realtime.py')
    data=json.loads((OUT/'candidate-evaluation.json').read_text(encoding='utf-8'))
    cap=cv2.VideoCapture('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4');fps=cap.get(5)
    def frame(t):
        cap.set(cv2.CAP_PROP_POS_FRAMES,round(t*fps));ok,image=cap.read();assert ok
        return image
    bare=frame(16);final=frame(48);cap.set(cv2.CAP_PROP_POS_FRAMES,0)
    recording=[];next_sample=0
    for index in range(round(49.5*fps)+1):
        t=index/fps
        if t<14 or t+1e-6<next_sample:
            assert cap.grab()
            if t+1e-6>=next_sample:next_sample+=.25
            continue
        ok,image=cap.read();assert ok;next_sample+=.25
        image=cv2.resize(image,(1080,1920));ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94]);assert ok
        recording.append(jpeg.tobytes())
    cap.release()
    # Canonical, same-position transplant of actual bare/installed pixels.
    normalized_bare=cv2.imread(str(OUT/'aligned/016.0.jpg'));normalized_final=cv2.imread(str(OUT/'aligned/048.0.jpg'))
    cfg=data['model']['config'];site=next(t for t in cfg['targets'] if t['id']=='R03');a,b,c,d=site['reference_bbox']
    missing=normalized_final.copy();missing[b:d,a:c]=normalized_bare[b:d,a:c]
    wrong_order=normalized_bare.copy();wrong_order[b:d,a:c]=normalized_final[b:d,a:c]
    cases=[('recorded-correct-order',recording,'PASS','sequential real video samples at 4FPS, simulated camera requests'),
           ('finished-product-without-order',[final]*3,'REVIEW','recorded finished still; no before/after order'),
           ('blank',[np.zeros((1080,608,3),np.uint8)]*3,'REVIEW','synthetic blank'),
           ('synthetic-missing-R03',[bare]*3+[final]*3+[missing]*3,'FAIL','synthetic same-position transplant of real bare pixels'),
           ('synthetic-rectangle-before-triangles',[bare]*3+[wrong_order]*3,'FAIL','synthetic same-position transplant of real installed rectangle')]
    client=login();other=login();results=[];failures=[]
    for name,images,expected,provenance in cases:
        identity=str(uuid.uuid4());latencies=[];last=None;samples=[]
        for i,image in enumerate(images):
            if isinstance(image,bytes):payload=image
            else:
                image=cv2.resize(image,(1080,1920));ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,94]);assert ok;payload=jpeg.tobytes()
            start=time.perf_counter();r=client.post(BASE+'/realtime/frame?analysis_type=sticker',data=payload,
                headers={'Content-Type':'image/jpeg','X-Stream-Id':identity,'X-Frame-Time':str(i*.25)},timeout=60)
            r.raise_for_status();last=r.json();latencies.append(round((time.perf_counter()-start)*1000,2))
            assert last['modelHash']==data['model']['assets'][data['model']['config']['model']]
            samples.append(dict(time=i*.25,inspection=last['inspection']))
            assert all(t['positionMode']=='DETECTED' and t['bbox'] for t in last['inspection']['displayTargets'])
            if name=='blank':assert last['inspection']['displayTargets']==[]
        assert client.post(BASE+f'/realtime/streams/{identity}/finish',timeout=10).status_code==404
        assert other.post(BASE+f'/realtime/streams/{identity}/finish?analysis_type=sticker',timeout=10).status_code==404
        r=client.post(BASE+f'/realtime/streams/{identity}/finish?analysis_type=sticker',timeout=15);r.raise_for_status();result=r.json()
        matched=result['compliance']['status']==expected
        if name=='synthetic-rectangle-before-triangles':matched=matched and result['sequence']['status']=='FAIL'
        if not matched:failures.append(name)
        assert client.post(BASE+f'/realtime/streams/{identity}/finish?analysis_type=sticker',timeout=15).json()==result
        results.append(dict(case=name,provenance=provenance,result=result,expected=expected,matched=matched,wall_ms=latencies,samples=samples))
        client.delete(BASE+f'/realtime/streams/{identity}',timeout=15).raise_for_status()
        print(name,result['compliance']['status'],flush=True)
    (OUT/'realtime-api-test.json').write_text(json.dumps(dict(source_frames=dict(bare=16,recording=[14,49.5],final=48),model_assets=data['model']['assets'],cases=results,
        failures=failures,scope='recorded frame API simulation and synthetic controls, not physical camera/field accuracy'),ensure_ascii=False,indent=2),encoding='utf-8')
    if failures:raise SystemExit('Realtime checks failed: '+', '.join(failures))

if __name__=='__main__':main()
