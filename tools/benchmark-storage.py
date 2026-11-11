"""Reproducible isolated SQLite storage benchmark, no production data touched.

Run: node tools/python.cjs tools/benchmark-storage.py --rows 50000
The baseline uses the original complete-row/JSON history query and indexes;
the optimized query projects display fields and uses the new compound index.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
import sqlalchemy
from sqlalchemy import event, select, text
from backend.config import Settings
from backend.db import alert_evidence, initialize, make_engine, media, runs
from backend.evidence import EvidenceStore


def measured(function, repeats=40):
    function()
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = function()
        durations.append((time.perf_counter()-started)*1000)
    ordered = sorted(durations)
    return {'median_ms': round(statistics.median(ordered), 3),
            'p95_ms': round(ordered[math.ceil(.95*len(ordered))-1], 3),
            'iterations': repeats}, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--output', type=Path, default=ROOT/'test-results/storage-benchmark.json')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aisop-storage-') as temporary:
        root = Path(temporary)
        engine = make_engine('sqlite:///' + (root/'bench.db').as_posix())
        initialize(engine)
        option = {'analysis_type': 'screw', 'screw_config': {'config': {
            'model_name': 'benchmark-model', 'targets': [{'id': str(i), 'roi': [.1,.2,.3,.4],
                'description': 'synthetic representative frozen configuration'} for i in range(70)]}}}
        payload_bytes = len(json.dumps(option).encode())
        started = time.perf_counter()
        with engine.begin() as c:
            c.execute(media.insert().values(id='fixture', owner='admin', name='sample.mp4', suffix='.mp4',
                sha256='a'*64, size=1024, metadata={}, created_at=1))
            for start in range(0, args.rows, 1000):
                c.execute(runs.insert(), [dict(id=f'{i:036d}', owner='admin', media_id='fixture',
                    status='FAILED' if i%100 == 0 else 'SUCCEEDED', request_key=str(i), request_hash='a'*64,
                    options=option, model_hash='a'*64, progress=1, attempts=1, token=1,
                    created_at=float(i), updated_at=float(i)) for i in range(start, min(start+1000,args.rows))])
        seed_seconds = time.perf_counter()-started
        additions = [index for index in runs.indexes if index.name in (
            'ix_runs_owner_status_created', 'ix_runs_owner_type_created', 'ix_runs_owner_scene_created')]
        for index in additions:
            index.drop(engine)
        conditions = [runs.c.owner == 'admin', runs.c.status == 'FAILED']
        order = [runs.c.created_at.desc(), runs.c.id.desc()]
        source = runs.join(media, runs.c.media_id == media.c.id)
        old = select(runs, media.c.name).select_from(source).where(*conditions).order_by(*order).limit(100)
        def execute(query):
            with engine.connect() as c:
                return c.execute(query).mappings().all()
        def explain(query):
            sql = str(query.compile(engine, compile_kwargs={'literal_binds': True}))
            with engine.connect() as c:
                return [r[3] for r in c.execute(text('EXPLAIN QUERY PLAN '+sql))]
        old_plan = explain(old)
        baseline, old_rows = measured(lambda: execute(old))
        for index in additions:
            index.create(engine)
        with engine.begin() as c:
            c.execute(text('ANALYZE'))
        columns = [runs.c[k] for k in ('id','media_id','status','progress','attempts','error_code','error_message','created_at','updated_at')]
        new = select(*columns, media.c.name, runs.c.options['analysis_type'].as_string().label('analysis_type'),
            runs.c.options['screw_config']['config']['model_name'].as_string().label('model_name'))
        new = new.select_from(source).where(*conditions).order_by(*order).limit(100)
        optimized, new_rows = measured(lambda: execute(new))
        assert [r['id'] for r in old_rows] == [r['id'] for r in new_rows]
        def row_bytes(rows):
            return len(json.dumps([dict(r) for r in rows], ensure_ascii=False, separators=(',', ':')).encode())
        baseline['fetched_row_bytes'] = row_bytes(old_rows)
        optimized['fetched_row_bytes'] = row_bytes(new_rows)
        baseline['query_plan'], optimized['query_plan'] = old_plan, explain(new)
        def concurrent():
            with ThreadPoolExecutor(max_workers=8) as pool:
                return list(pool.map(lambda _: len(execute(new)), range(32)))
        concurrency, counts = measured(concurrent, repeats=5)
        assert all(count == 100 for count in counts)
        settings = Settings('sqlite:///'+(root/'bench.db').as_posix(),root/'data','admin','test-only-password',root/'model',
                            environment='test', secure_cookie=False)
        store = EvidenceStore(engine,settings)
        random = np.random.default_rng(42)
        frame = random.integers(0, 256, (360,640,3), dtype=np.uint8)
        ok, encoded = cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,75])
        assert ok
        jpeg = encoded.tobytes()
        verdict = {'compliance': {'status': 'PASS', 'reason': 'synthetic'}}
        stamp = [0]
        sql_count = [0]
        def sql_event(*_):
            sql_count[0] += 1
        event.listen(engine,'before_cursor_execute',sql_event)
        def sample():
            stamp[0] += .1
            store.observe('admin','session','stream','screw',stamp[0],jpeg,verdict)
        sampling, _ = measured(sample,repeats=1000)
        event.remove(engine,'before_cursor_execute',sql_event)
        sampling.update(database_statements=sql_count[0], jpeg_bytes=len(jpeg), buffer_bytes=store.buffer_bytes)
        started = time.perf_counter()
        receipt = store.observe('admin','session','stream','screw',stamp[0]+.1,jpeg,
            {'compliance': {'status': 'FAIL','reason':'synthetic benchmark'}})
        spool_ms = (time.perf_counter()-started)*1000
        started = time.perf_counter()
        store.process_one()
        encode_ms = (time.perf_counter()-started)*1000
        with engine.connect() as c:
            proof = c.execute(select(alert_evidence).where(alert_evidence.c.id==receipt['id'])).mappings().one()
        assert proof['status']=='READY', dict(proof)
        output = dict(measured_at_utc=datetime.now(timezone.utc).isoformat(), provenance='measured synthetic workload',
            engine='SQLite (local disk, WAL, synchronous=FULL)', python=platform.python_version(),
            platform=platform.platform(), sqlalchemy=sqlalchemy.__version__, rows=args.rows,
            config_json_bytes=payload_bytes, seed_seconds=round(seed_seconds,3), baseline=baseline,optimized=optimized,
            median_speedup=round(baseline['median_ms']/optimized['median_ms'],2),
            fetched_bytes_reduction_percent=round(100*(1-optimized['fetched_row_bytes']/baseline['fetched_row_bytes']),2),
            concurrent_32_reads_8_threads=concurrency, non_alert_sample=sampling,
            alert=dict(spool_ms=round(spool_ms,3), encode_and_commit_ms=round(encode_ms,3),
                frames=proof['frame_count'], video_bytes=proof['size_bytes'], format=proof['format'],
                retain_seconds=proof['retain_until']-proof['ready_at']),
            limitations=['Synthetic database and noise image workload, warm OS cache.',
                'No PostgreSQL server was used; not a production capacity claim.',
                'Encoding excludes neural network inference.'])
        args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(output,ensure_ascii=False,indent=2))
        engine.dispose()


if __name__ == '__main__':
    main()
