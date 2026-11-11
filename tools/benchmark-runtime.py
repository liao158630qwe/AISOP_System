"""Isolated real-model comparison and short mixed-load check; no production writes.

Run each thread budget in a fresh process. Timings include TestClient and API work,
but exclude a physical camera, network, browser rendering, and video encoding.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import secrets
import statistics
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
import psutil
import torch
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.config import Settings
from backend.db import initialize

DIAGNOSTIC_FIELDS = {'inferenceMs', 'processingMs', 'personProcessingMs', 'totalProcessingMs', 'timingsMs',
                     'serverReceivedAt', 'serverRespondedAt', 'serverProcessingMs', 'streamEpoch', 'gpuMemoryMB'}


def behavior(value):
    if isinstance(value, dict):
        return {key: behavior(item) for key, item in value.items() if key not in DIAGNOSTIC_FIELDS}
    if isinstance(value, list):
        return [behavior(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stats(values):
    ordered = sorted(values)
    if not ordered:
        return {'samples': 0}
    return dict(samples=len(values), medianMs=round(statistics.median(values), 2),
                p95Ms=round(ordered[math.ceil(.95*len(values))-1], 2),
                p99Ms=round(ordered[math.ceil(.99*len(values))-1], 2), maxMs=round(max(values), 2))


def compare_baseline(folder, result, outputs):
    baseline = json.loads((folder/'benchmark.json').read_text(encoding='utf-8'))
    previous = json.loads((folder/'responses.json').read_text(encoding='utf-8'))
    comparable = (baseline['fixtureHashes'] == result['fixtureHashes']
                  and baseline['cases'].keys() == result['cases'].keys()
                  and all(baseline['cases'][name]['fixtureHashes'] == case['fixtureHashes']
                          for name, case in result['cases'].items()))
    # Normalize older records too: elapsed times and stream creation clocks are
    # diagnostics, while scores, coordinates, action history and verdicts remain.
    matches = {name: name in previous and behavior(previous[name]) == behavior(value)
               for name, value in outputs.items()}
    return dict(path=str(folder.resolve()), fixturesMatched=comparable, outputsMatched=matches,
                passed=comparable and previous.keys() == outputs.keys() and all(matches.values()))


def jpeg(frame, quality=85):
    ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return data.tobytes()


def fixtures():
    cap = cv2.VideoCapture(str(ROOT/'assets/factory-workers.mp4'))
    people = []
    try:
        for _ in range(10):
            ok, frame = cap.read()
            assert ok
            people.append(jpeg(cv2.resize(frame, (640, 360))))
    finally:
        cap.release()
    result = {'pose': people}
    for kind, name in [('screw', 'screw-reference.jpg'), ('sticker', 'sticker-reference-v1.jpg')]:
        frame = cv2.imread(str(ROOT/'models'/name))
        assert frame is not None
        shifted = cv2.warpAffine(frame, np.float32([[1, 0, 3], [0, 1, -2]]), (frame.shape[1], frame.shape[0]))
        result[kind] = [jpeg(frame), jpeg(shifted)]
    return result


def sticker_cases(path):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    assert cap.isOpened() and fps > 0, str(path)
    frames, next_sample = {}, 14.
    try:
        for index in range(round(50*fps)+1):
            if index/fps + 1e-6 < next_sample:
                assert cap.grab()
                continue
            ok, frame = cap.read()
            assert ok
            frames[round(next_sample, 3)] = jpeg(cv2.resize(frame, (1080, 1920)), 94)
            next_sample += .25
    finally:
        cap.release()
    sample = lambda a, b: [frames[round(t, 3)] for t in np.arange(a, b, .25)]
    return [
        ('recorded-correct-order', sample(14, 49.5), 'PASS'),
        ('finished-product-without-actions', [frames[48.]]*3, 'REVIEW'),
        ('blank', [jpeg(np.zeros((1920, 1080, 3), np.uint8), 94)]*3, 'REVIEW'),
        ('reordered-recorded-actions', [frames[16.]]*3 + sample(33.25, 36.5) + sample(19.5, 21.25)
         + sample(22.5, 24.5), 'FAIL'),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opencv-threads', type=int, default=4)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--mixed-rounds', type=int, default=30)
    parser.add_argument('--sticker-video', type=Path)
    parser.add_argument('--baseline', type=Path, help='Compare all non-diagnostic outputs with this run directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 5 or args.mixed_rounds < 1:
        parser.error('samples >= 5 and mixed-rounds >= 1 are required')
    args.output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='isolated-', dir=args.output))
    settings = Settings('sqlite:///' + (folder/'test.db').as_posix(), folder/'data', 'benchmark',
                        secrets.token_urlsafe(24), ROOT/'models/yolo11s-pose.pt', environment='test',
                        secure_cookie=False, realtime_backend='yolo', pose_backend='yolo',
                        realtime_pool_size=1, realtime_warmup=False, opencv_threads=args.opencv_threads)
    corpus = fixtures()
    app = create_app(settings)
    initialize(app.state.engine)
    result = dict(scope='isolated in-process API, real models; short test, not server capacity certification',
                  startedAt=datetime.now(timezone.utc).isoformat(), logicalCPUs=psutil.cpu_count(),
                  device=torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu',
                  python=sys.version, opencvVersion=cv2.__version__, torchVersion=torch.__version__,
                  requestedThreads=args.opencv_threads, poolSize=1, tasks={}, cases={},
                  fixtureHashes={key: [hashlib.sha256(p).hexdigest() for p in value] for key, value in corpus.items()})
    outputs = {}
    process = psutil.Process()

    with TestClient(app) as client:
        login = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
        assert login.status_code == 200, login.text
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        result['effectiveThreads'] = cv2.getNumThreads()

        def post(kind, stream, index, payload):
            started = time.perf_counter()
            response = client.post('/api/v1/realtime/frame?analysis_type='+kind, content=payload,
                                   headers={'Content-Type': 'image/jpeg', 'X-Stream-Id': stream,
                                            'X-Frame-Time': str(index*.25), 'X-Frame-Id': f'{kind}-{index}'})
            return response, (time.perf_counter()-started)*1000

        def release(kind, stream):
            final = None
            if kind != 'pose':
                response = client.post(f'/api/v1/realtime/streams/{stream}/finish?analysis_type={kind}')
                assert response.status_code == 200, response.text
                final = behavior(response.json())
                assert final['final'] is True
            assert client.delete('/api/v1/realtime/streams/'+stream).status_code == 200
            return final

        for kind, frames in corpus.items():
            stream, timings, stage_values, records = str(uuid.uuid4()), [], {}, []
            for index in range(args.samples+3):
                response, elapsed = post(kind, stream, index, frames[index % len(frames)])
                assert response.status_code == 200, response.text
                data = response.json()
                assert data['frameId'] == f'{kind}-{index}'
                timings.append(elapsed)
                if index >= 3:
                    for key, value in data.get('timingsMs', {}).items():
                        stage_values.setdefault(key, []).append(value)
                records.append(behavior(data))
            final = release(kind, stream)
            outputs[kind] = {'frames': records, 'final': final}
            result['tasks'][kind] = dict(firstMs=round(timings[0], 2), warmupExcluded=3,
                steady=stats(timings[3:]), durationsMs=timings, stages={k: stats(v) for k, v in stage_values.items()},
                behaviorSha256=digest(outputs[kind]))
            print(kind, result['tasks'][kind]['steady'], flush=True)

        if args.sticker_video:
            result['stickerVideo'] = str(args.sticker_video.resolve())
            for name, frames, expected in sticker_cases(args.sticker_video):
                stream, timings, records = str(uuid.uuid4()), [], []
                for index, payload in enumerate(frames):
                    response, elapsed = post('sticker', stream, index, payload)
                    assert response.status_code == 200, response.text
                    timings.append(elapsed)
                    records.append(behavior(response.json()))
                final = release('sticker', stream)
                outputs[name] = {'frames': records, 'final': final}
                result['cases'][name] = dict(expected=expected, actual=final['compliance']['status'],
                    matched=final['compliance']['status'] == expected, latency=stats(timings),
                    fixtureHashes=[hashlib.sha256(p).hexdigest() for p in frames], behaviorSha256=digest(outputs[name]))
                print(name, result['cases'][name]['actual'], stats(timings), flush=True)

        # Rotate the first task and stagger arrivals so CPU inspection can overlap
        # the next task's pose inference. Avoid always giving pose the first slot.
        streams = {kind: str(uuid.uuid4()) for kind in corpus}
        mixed = {kind: {'200': 0, '429': 0} for kind in corpus}
        mixed_times = {kind: {'200': [], '429': []} for kind in corpus}
        health_times, rss, gpu_allocated, gpu_reserved = [], [], [], []
        rss.append(process.memory_info().rss/1024**2)
        started = time.perf_counter()
        cpu_started = process.cpu_times()
        def delayed_post(delay, kind, index):
            time.sleep(delay)
            frames = corpus[kind]
            return post(kind, streams[kind], index, frames[index % len(frames)])

        with ThreadPoolExecutor(max_workers=4) as pool:
            for index in range(args.mixed_rounds):
                kinds = list(corpus)
                offset = index % len(kinds)
                kinds = kinds[offset:] + kinds[:offset]
                pending = {kind: pool.submit(delayed_post, offset*.025, kind, index)
                           for offset, kind in enumerate(kinds)}
                health_started = time.perf_counter()
                health = client.get('/api/v1/health')
                health_times.append((time.perf_counter()-health_started)*1000)
                assert health.status_code == 200
                for kind, task in pending.items():
                    response, elapsed = task.result()
                    assert response.status_code in (200, 429), response.text
                    mixed[kind][str(response.status_code)] += 1
                    mixed_times[kind][str(response.status_code)].append(elapsed)
                    if response.status_code == 429:
                        assert response.headers.get('retry-after')
                rss.append(process.memory_info().rss/1024**2)
                if torch.cuda.is_available():
                    gpu_allocated.append(torch.cuda.memory_allocated()/1024**2)
                    gpu_reserved.append(torch.cuda.memory_reserved()/1024**2)
                if (index+1) % 100 == 0:
                    print('Mixed rounds:', index+1, 'RSS MB:', round(rss[-1], 2), flush=True)
        load_seconds = time.perf_counter()-started
        cpu_finished = process.cpu_times()
        # All task types must remain usable after contention.
        for kind, stream in streams.items():
            response, _ = post(kind, stream, args.mixed_rounds+1, corpus[kind][0])
            assert response.status_code == 200, response.text
            release(kind, stream)
        result['mixed'] = dict(rounds=args.mixed_rounds, durationSeconds=round(load_seconds, 2),
            arrivalStaggerMs=25, firstTaskRotates=True,
            latencies={kind: {status: stats(values) for status, values in times.items()}
                       for kind, times in mixed_times.items()},
            statusCounts=mixed, health=stats(health_times), rssMB=[round(v, 2) for v in rss],
            cpuSeconds=round(cpu_finished.user+cpu_finished.system-cpu_started.user-cpu_started.system, 2),
            gpuAllocatedMB=[round(v, 2) for v in gpu_allocated],
            gpuReservedMB=[round(v, 2) for v in gpu_reserved], recoveryPassed=True)
        assert all(counts['200'] > 0 for counts in mixed.values()), 'Each task must run under mixed load'
        result['metrics'] = client.get('/api/v1/metrics').json()
        assert result['metrics']['realtime']['active'] == 0
        assert all(service['active'] == 0 for service in result['metrics']['realtimeTransport']['services'].values())
        result['remainingStreams'] = {kind: len(registry.items) for kind, registry in {
            'pose': app.state.realtime.streams, 'screw': app.state.screw_realtime.registry,
            'sticker': app.state.sticker_realtime.registry, 'scene': app.state.scene_realtime.streams}.items()}
        assert not any(result['remainingStreams'].values())
        assert cv2.getNumThreads() == result['effectiveThreads']
        result['threadsPersistedAfterAllModels'] = True
    result['finishedAt'] = datetime.now(timezone.utc).isoformat()
    result['businessCasesPassed'] = all(case['matched'] for case in result['cases'].values()) if result['cases'] else None
    if args.baseline:
        result['baselineComparison'] = compare_baseline(args.baseline, result, outputs)
    (args.output/'responses.json').write_text(json.dumps(outputs, ensure_ascii=False), encoding='utf-8')
    (args.output/'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Output:', args.output, flush=True)
    if args.baseline:
        assert result['baselineComparison']['passed'], 'Baseline inputs or inference outputs differ'
    assert all(case['matched'] for case in result['cases'].values()), 'Expected sticker verdict mismatch; compare the baseline before calling this a regression'


if __name__ == '__main__':
    main()
