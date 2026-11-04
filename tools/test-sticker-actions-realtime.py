"""Exercise the active action-mode sticker task through the real frame API."""
import json
import hashlib
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
import requests

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/sticker-inspection-2026-10-17'
BASE='http://127.0.0.1:8000/api/v1'
VIDEO=Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4')


def login():
    client=requests.Session();client.trust_env=False
    response=client.post(BASE+'/login',json=json.loads((ROOT/'.runtime/local-settings.json').read_text(encoding='utf-8')),timeout=20)
    response.raise_for_status();client.headers['X-CSRF-Token']=response.json()['csrf'];return client


def jpeg(image,quality=94):
    image=cv2.resize(image,(1080,1920))
    ok,encoded=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,quality]);assert ok
    return encoded.tobytes()


def load_video():
    capture=cv2.VideoCapture(str(VIDEO));fps=capture.get(cv2.CAP_PROP_FPS)
    wanted={round(t,3):t for t in np.arange(14,50,.25)}
    frames={};next_sample=14.;last=None
    for index in range(round(50*fps)+1):
        time_s=index/fps
        if time_s+1e-6<next_sample:
            assert capture.grab()
            continue
        ok,image=capture.read();assert ok
        frames[round(next_sample,3)]=jpeg(image)
        last=image;next_sample+=.25
    capture.release()
    return frames,frames[48.0]


def post_frames(client,identity,payloads):
    latencies=[];last=None
    for index,payload in enumerate(payloads):
        started=time.perf_counter()
        response=client.post(BASE+'/realtime/frame?analysis_type=sticker',data=payload,
            headers={'Content-Type':'image/jpeg','X-Stream-Id':identity,'X-Frame-Time':str(index*.25)},timeout=60)
        response.raise_for_status();last=response.json()
        latencies.append(round((time.perf_counter()-started)*1000,2))
    return last,latencies


def finish(client,identity):
    response=client.post(BASE+f'/realtime/streams/{identity}/finish?analysis_type=sticker',timeout=30)
    response.raise_for_status();return response.json()


def run_case(client,name,payloads,expected,provenance):
    identity=str(uuid.uuid4());last,latencies=post_frames(client,identity,payloads)
    result=finish(client,identity)
    matched=result['compliance']['status']==expected
    client.delete(BASE+f'/realtime/streams/{identity}',timeout=15).raise_for_status()
    print(name,result['compliance']['status'],flush=True)
    return dict(case=name,provenance=provenance,expected=expected,matched=matched,result=result,
        final_frame=last,wall_ms=latencies)


def main():
    config=json.loads((ROOT/'config/sticker-inspection.json').read_text(encoding='utf-8'))
    if config.get('mode')!='action':raise SystemExit('active sticker config is not in action mode')
    frames,final_frame=load_video()
    correct=[frames[round(t,3)] for t in np.arange(14,49.5,.25) if round(t,3) in frames]
    still=[final_frame]*3
    blank=[jpeg(np.zeros((1920,1080,3),np.uint8))]*3
    wrong_order=[frames[16.0]]*3
    wrong_order+= [frames[round(t,3)] for t in np.arange(33.25,36.5,.25) if round(t,3) in frames]
    wrong_order+= [frames[round(t,3)] for t in np.arange(19.5,21.25,.25) if round(t,3) in frames]
    wrong_order+= [frames[round(t,3)] for t in np.arange(22.5,24.5,.25) if round(t,3) in frames]
    client=login();results=[];failures=[]
    cases=[('recorded-correct-order',correct,'PASS','real video sampled at 4FPS from 14s to 49.5s'),
        ('finished-product-without-actions',still,'REVIEW','recorded finished frame repeated; no action sequence'),
        ('blank',blank,'REVIEW','synthetic blank frame'),
        ('rectangle-action-before-triangles',wrong_order,'FAIL','real R01 action clip replayed before real triangle clips')]
    for name,payloads,expected,provenance in cases:
        record=run_case(client,name,payloads,expected,provenance)
        if name=='recorded-correct-order':
            sequence=record['result']['sequence']
            record['matched']=record['matched'] and record['final_frame'].get('inspectionMode')=='action'
            record['matched']=record['matched'] and len(sequence['events'])==8
            record['matched']=record['matched'] and sequence['trianglesCompleteAt']<sequence['firstRectangleAt']
            record['matched']=record['matched'] and all(t['state']=='ACTION_DONE' for t in record['result']['targets'])
        if name=='finished-product-without-actions':
            record['matched']=record['matched'] and record['result']['sequence']['events']==[]
        if name=='blank':
            record['matched']=record['matched'] and record['result']['sequence']['events']==[]
            record['matched']=record['matched'] and record['final_frame']['inspection']['displayTargets']==[]
        if name=='rectangle-action-before-triangles':
            record['matched']=record['matched'] and record['result']['sequence']['status']=='FAIL'
            record['matched']=record['matched'] and bool(record['result']['sequence']['violations'])
        if not record['matched']:failures.append(name)
        results.append(record)
    model_hash=hashlib.sha256((ROOT/config['model']).read_bytes()).hexdigest()
    for record in results:
        if record['final_frame']['modelHash']!=model_hash:raise SystemExit('active model changed during realtime test')
    report=dict(source=str(VIDEO),source_sha256=hashlib.sha256(VIDEO.read_bytes()).hexdigest(),
        model=config['model'],model_sha256=model_hash,cases=results,failures=failures,
        scope='real HTTP frame API with recorded video clips and synthetic blank; not physical camera or independent station validation')
    (OUT/'action-realtime-api-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if failures:raise SystemExit('Action realtime checks failed: '+', '.join(failures))


if __name__=='__main__':main()
