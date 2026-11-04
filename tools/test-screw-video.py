"""Upload and test a real source video through the running API + worker."""
import argparse
import json
from pathlib import Path
import time
import uuid

import requests

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('video',type=Path)
    parser.add_argument('--reference',action='store_true')
    parser.add_argument('--media-id',help='Reuse an already uploaded source, preserving its server hash')
    parser.add_argument('--analysis-type',choices=['screw','sticker'],default='screw')
    parser.add_argument('--out',type=Path,
                        help='Save this evaluation separately from previous videos')
    args=parser.parse_args()
    if args.reference and args.analysis_type!='screw':parser.error('--reference is only for screw inspection')
    session=requests.Session();session.trust_env=False
    base='http://127.0.0.1:8000/api/v1'
    credentials=json.loads((ROOT/'.runtime/local-settings.json').read_text(encoding='utf-8'))
    response=session.post(base+'/login',json=credentials,timeout=15)
    response.raise_for_status()
    session.headers['X-CSRF-Token']=response.json()['csrf']
    media_id=args.media_id
    if not media_id:
        print('Uploading source video...',flush=True)
        with args.video.open('rb') as f:
            response=session.post(base+'/media',params={'filename':args.video.name},data=f,
                headers={'Content-Type':'application/octet-stream'},timeout=300)
        response.raise_for_status()
        media_id=response.json()['id']
    response=session.post(base+'/analysis-runs',json={'media_id':media_id,
        'options':{'analysis_type':args.analysis_type,'sample_fps':4}},headers={'Idempotency-Key':str(uuid.uuid4())},timeout=20)
    response.raise_for_status()
    job=response.json();print('Run:',job['id'],flush=True)
    started=time.time()
    while job['status'] not in ['FAILED','CANCELLED','SUCCEEDED']:
        response=session.get(base+f'/analysis-runs/{job["id"]}/wait',
            params={'since':job['updated_at'],'timeout':25},timeout=35)
        response.raise_for_status();job=response.json()
        print(job['status'],round(job['progress']*100,1),flush=True)
        if time.time()-started>900: raise TimeoutError('Analysis exceeded 15 minutes')
    if job['status']!='SUCCEEDED': raise RuntimeError(str(job))
    response=session.get(base+f'/analysis-runs/{job["id"]}/artifacts/analysis.json',timeout=30)
    response.raise_for_status();data=response.json()
    out=args.out or ROOT/('outputs/screw-inspection-2026-10-17' if args.analysis_type=='screw' else 'outputs/sticker-inspection-2026-10-17/api-test')
    out.mkdir(parents=True,exist_ok=True)
    (out/'full-video-result.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'api-test.json').write_text(json.dumps(dict(run=job,summary=data['summary'],
        source_sha256=data['source']['sha256'],model_sha256=data['model']['sha256'],
        wall_seconds=time.time()-started),ensure_ascii=False,indent=2),encoding='utf-8')
    if args.reference:
        (ROOT/'.runtime/screw-reference-run.json').write_text(json.dumps({'id':job['id']}),encoding='utf-8')
    print(json.dumps(data['summary'],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
