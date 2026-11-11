"""Compare 4/10 FPS evidence and paced 10 FPS processing using existing development videos.

Runs an isolated API/database. Does not train, tune, or alter production settings.
Full sampling preserves video timestamps even when processing slower than playback.
Paced sampling drops old input frames when busy, so it cannot fake realtime throughput.
"""
import argparse
import ast
from bisect import bisect_right
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
import torch
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.config import Settings
from backend.db import initialize
from algorithms.screws import snapshot as screw_snapshot
from algorithms.stickers import snapshot as sticker_snapshot


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def constant(path, name):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    # FOCUS has one finite dict comprehension for the 12 known final positions.
    if name == 'FOCUS':
        return eval(compile(ast.Expression(node), str(path), 'eval'), {'__builtins__': {}, 'range': range})
    return ast.literal_eval(node)


def stats(values):
    if not values:
        return {'samples': 0}
    s = sorted(values)
    return {'samples': len(s), 'meanMs': round(statistics.mean(s), 2),
            'medianMs': round(statistics.median(s), 2),
            'p95Ms': round(s[math.ceil(.95 * len(s)) - 1], 2),
            'p99Ms': round(s[math.ceil(.99 * len(s)) - 1], 2), 'maxMs': round(s[-1], 2)}


def encode(frame, kind):
    width, height = (1080, 1920) if kind == 'sticker' else (1920, 1080)
    frame = cv2.resize(frame, (width, height))
    quality = 94 if kind == 'sticker' else 82
    ok, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    if len(jpg) > 1024 * 1024:
        ok, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
    assert ok and len(jpg) <= 1024 * 1024
    return jpg.tobytes()


def prepare(path, kind, start, end):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    assert cap.isOpened() and fps > 0
    grids = {rate: [round(start + i / rate, 6) for i in range(round((end - start) * rate))]
             for rate in (4, 10)}
    wanted = set(sum(grids.values(), []))
    if kind == 'sticker':
        # Existing wrong-order regression clips, resampled at each candidate rate.
        for rate in (4, 10):
            for a, b in ((33.25, 36.5), (19.5, 21.25), (22.5, 24.5)):
                wanted.update(round(a + i / rate, 6) for i in range(math.ceil((b - a) * rate)))
        wanted.update((16., 48.))
    by_index = {}
    for t in wanted:
        # Match the established sampler's source_time + 1e-6 >= target_time.
        # The tolerance is in seconds; metadata FPS can differ from 30 by 3e-7.
        by_index.setdefault(math.ceil((t - 1e-6) * fps), []).append(t)
    payloads = {}
    try:
        for index in range(max(by_index) + 1):
            if index not in by_index:
                assert cap.grab(), (path, index)
                continue
            ok, frame = cap.read()
            assert ok, (path, index)
            payload = encode(frame, kind)
            for t in by_index[index]:
                payloads[t] = {'payload': payload, 'sourceTime': index / fps, 'requestedTime': t}
    finally:
        cap.release()
    return grids, payloads, {'path': str(path), 'sha256': sha(path), 'nativeFps': fps,
                             'clip': [start, end], 'uniqueEncodedFrames': len(by_index)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--downloads', type=Path, default=Path('C:/Users/Administrator/Downloads'))
    p.add_argument('--backend', choices=['rtmo', 'yolo'], default='rtmo')
    p.add_argument('--tasks', nargs='+', choices=['sticker', 'screw'], default=['sticker', 'screw'])
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    isolated = Path(tempfile.mkdtemp(prefix='isolated-', dir=args.output))
    frozen = {'screw': screw_snapshot(), 'sticker': sticker_snapshot()}
    focus = constant(ROOT / 'tools/evaluate-screw-refinement.py', 'FOCUS')
    contacts = constant(ROOT / 'tools/train-sticker-actions.py', 'CONTACT')
    settings = Settings('sqlite:///' + (isolated / 'test.db').as_posix(), isolated / 'data',
                        'fps-evaluation', secrets.token_urlsafe(24), ROOT / 'models/yolo11s-pose.pt',
                        environment='test', secure_cookie=False, realtime_backend=args.backend,
                        pose_backend=args.backend, realtime_pool_size=2, realtime_warmup=False,
                        opencv_threads=4)
    report = {'startedAt': datetime.now(timezone.utc).isoformat(),
              'scope': 'Existing same-source development evidence, not independent factory accuracy',
              'timingScope': 'Isolated in-process real API; excludes physical camera/network/browser and precomputed JPEG encoding',
              'hardware': torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu',
              'requestedBackend': args.backend, 'poolSize': 2, 'sources': {}, 'runs': [],
              'frozen': frozen, 'labels': {'screw': focus, 'stickerContactIntervals': contacts,
                  'limitation': 'Screw labels cover selected previously reviewed positions. Sticker intervals were training aids; neither is an independent held-out corpus.'},
              'preparation': []}
    save(args.output / 'protocol.json', report)
    app = create_app(settings)
    initialize(app.state.engine)
    with TestClient(app) as client:
        response = client.post('/api/v1/login', json={'username': settings.username, 'password': settings.password})
        assert response.status_code == 200, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf']

        def post(kind, stream, index, payload, timestamp):
            started = time.perf_counter()
            response = client.post('/api/v1/realtime/frame?analysis_type=' + kind, content=payload,
                headers={'Content-Type': 'image/jpeg', 'X-Stream-Id': stream,
                         'X-Frame-Time': str(timestamp), 'X-Frame-Id': f'{kind}-{index}'})
            elapsed = (time.perf_counter() - started) * 1000
            assert response.status_code == 200, response.text
            data = response.json()
            # Sticker exposes hashes in each response; screw freezes them in its service.
            service = app.state.sticker_realtime if kind == 'sticker' else app.state.screw_realtime
            signature = service.signature if kind == 'sticker' else (service.detector.frozen['config_hash'],
                tuple(service.detector.frozen['assets'].values()))
            assert signature == (frozen[kind]['config_hash'], tuple(frozen[kind]['assets'].values())), 'Model/config changed'
            if kind == 'sticker':
                assert data['configHash'] == frozen[kind]['config_hash']
                assert data['modelHash'] == frozen[kind]['assets'][frozen[kind]['config']['model']]
            return data, elapsed

        def run(kind, name, rate, frames, expected, mode='full', truth=None):
            stream = str(uuid.uuid4())
            records, elapsed_values, starts, selected = [], [], [], []
            duration = len(frames) / rate
            origin = time.perf_counter()
            next_start = origin
            index = 0
            try:
                while index < len(frames):
                    if mode == 'paced':
                        now = time.perf_counter()
                        if next_start > now:
                            time.sleep(next_start - now)
                        now = time.perf_counter()
                        index = int((now - origin) * rate)
                        if index >= len(frames):
                            break
                    item = frames[index]
                    started = time.perf_counter()
                    timestamp = item['sourceTime'] - frames[0]['sourceTime'] if mode != 'control' else index / rate
                    data, elapsed = post(kind, stream, index, item['payload'], timestamp)
                    selected.append(index)
                    starts.append(started - origin)
                    elapsed_values.append(elapsed)
                    record = {'sampleIndex': index, 'requestedTime': item['requestedTime'],
                              'sourceTime': item['sourceTime'], 'elapsedMs': round(elapsed, 3),
                              'startedWallSeconds': started - origin, 'response': data}
                    if mode == 'paced':
                        record['sourceToResultMs'] = (time.perf_counter() - origin - index / rate) * 1000
                    records.append(record)
                    if len(records) % 100 == 0:
                        print(f'{kind} {name} {rate}fps: {len(records)} processed, input {index + 1}/{len(frames)}', flush=True)
                    next_start = started + 1 / rate
                    index += 1
                processed_seconds = time.perf_counter() - origin
                response = client.post(f'/api/v1/realtime/streams/{stream}/finish?analysis_type={kind}')
                assert response.status_code == 200, response.text
                final = response.json()
            finally:
                assert client.delete('/api/v1/realtime/streams/' + stream).status_code == 200
            result = {'kind': kind, 'name': name, 'mode': mode, 'requestedFps': rate,
                      'inputFrames': len(frames), 'processedFrames': len(records),
                      'skippedInputFrames': len(frames) - len(records), 'sourceSeconds': duration,
                      'wallSeconds': round(processed_seconds, 3),
                      'processedPerWallSecond': round(len(records) / processed_seconds, 3),
                      'latency': stats(elapsed_values), 'expected': expected,
                      'actual': final['compliance']['status'], 'verdictMatched': final['compliance']['status'] == expected,
                      'final': final, 'stages': {}}
            for key in records[-1]['response'].get('timingsMs', {}):
                result['stages'][key] = stats([r['response']['timingsMs'][key] for r in records])
            if mode == 'paced':
                result['completedWithinClip'] = sum(r['startedWallSeconds'] + r['elapsedMs'] / 1000 <= duration for r in records)
                result['sustainedFps'] = round(result['completedWithinClip'] / duration, 3)
                result['captureGapMs'] = stats([(b - a) * 1000 for a, b in zip(starts, starts[1:])])
                result['sourceToResultMs'] = stats([r['sourceToResultMs'] for r in records])
                result['tenFpsReached'] = result['completedWithinClip'] >= .95 * len(frames)
            if kind == 'sticker':
                found = {e['id'] for e in final['sequence']['events']}
                result['confirmedIds'] = sorted(found)
                if name == 'correct-sequence':
                    expected_ids = {t['id'] for t in frozen[kind]['config']['targets']}
                    result['expectedIds'] = sorted(expected_ids)
                    result['actionCoverage'] = len(found & expected_ids) / len(expected_ids)
                    result['missingIds'] = sorted(expected_ids - found)
                    positive = hits = 0
                    contact_rows = []
                    for r in records:
                        t = r['sourceTime']
                        for target in r['response']['inspection']['targets']:
                            if any(a - .035 <= t <= b + .035 for a, b in contacts[target['id']]):
                                positive += 1
                                hits += target['state'] == 'CONTACT'
                        site = next(t for t in r['response']['inspection']['targets'] if t['id'] == 'R05')
                        if 45 <= t <= 48:
                            contact_rows.append({'time': t, 'state': site['state'], 'score': site.get('score'),
                                                 'scores': site.get('actionScores')})
                    result['developmentContactFrameRecall'] = {'hits': hits, 'positiveAnnotatedObservations': positive,
                                                               'ratio': hits / positive if positive else None}
                    result['r05Detail'] = contact_rows
            if kind == 'screw' and truth and mode == 'full':
                scored = []
                for t, values in truth.items():
                    row = min(records, key=lambda r: abs(r['sourceTime'] - t))
                    assert abs(row['sourceTime'] - t) < .04
                    predicted = {v['id']: v['state'] for v in row['response']['inspection']['targets']}
                    scored.extend({'time': t, 'id': identity, 'expected': value,
                                   'predicted': predicted.get(identity, 'UNKNOWN')} for identity, value in values.items())
                correct = sum(v['expected'] == v['predicted'] for v in scored)
                unknown = sum(v['predicted'] == 'UNKNOWN' for v in scored)
                result['reviewedPositions'] = {'total': len(scored), 'correct': correct, 'unknown': unknown,
                    'wrong': len(scored) - correct - unknown, 'accuracyCountingUnknownAsMiss': correct / len(scored),
                    'emptyCalledPresent': sum(v['expected'] == 'EMPTY' and v['predicted'] == 'PRESENT' for v in scored),
                    'rows': scored}
            key = f'{kind}-{name}-{rate}fps-{mode}'
            save(args.output / (key + '-frames.json'), records)
            save(args.output / (key + '.json'), result)
            report['runs'].append(result)
            save(args.output / 'report.json', report)
            print(json.dumps({k: result[k] for k in ('kind', 'name', 'mode', 'requestedFps', 'processedFrames',
                              'actual', 'verdictMatched', 'processedPerWallSecond')}), flush=True)
            return result

        specs = [('sticker', 'lv_0_20261017162449.mp4', 14., 49.5),
                 ('screw', 'VID_20261017_103101.mp4', 0., 52.5)]
        for kind, filename, start, end in specs:
            if kind not in args.tasks:
                continue
            print('Preparing', kind, filename, flush=True)
            grids, payloads, provenance = prepare(args.downloads / filename, kind, start, end)
            report['sources'][kind] = provenance
            # Separate disposable preparation stream; warm both alternating pose slots.
            stream = str(uuid.uuid4())
            times = []
            for i in range(4):
                _, elapsed = post(kind, stream, i, payloads[grids[10][0]]['payload'], i * .1)
                times.append(elapsed)
            client.delete('/api/v1/realtime/streams/' + stream).raise_for_status()
            report['preparation'].append({'kind': kind, 'latenciesMs': times})
            for rate in (4, 10):
                frames = [payloads[t] for t in grids[rate]]
                run(kind, 'correct-sequence', rate, frames, 'PASS', truth=focus if kind == 'screw' else None)
                if kind == 'sticker':
                    controls = [('finished-product', [payloads[48.]] * rate, 'REVIEW'),
                                ('blank', [{'payload': encode(np.zeros((1920, 1080, 3), np.uint8), kind),
                                            'sourceTime': 0., 'requestedTime': 0.}] * rate, 'REVIEW')]
                    reordered = [payloads[16.]] * math.ceil(.75 * rate)
                    for a, b in ((33.25, 36.5), (19.5, 21.25), (22.5, 24.5)):
                        reordered.extend(payloads[round(a + i / rate, 6)] for i in range(math.ceil((b - a) * rate)))
                    controls.append(('synthetic-reordered-clips', reordered, 'FAIL'))
                    for name, sequence, expected in controls:
                        run(kind, name, rate, sequence, expected, mode='control')
                else:
                    for name, t, expected in [('unfinished-product', 12., 'FAIL'), ('finished-product', 51., 'PASS')]:
                        run(kind, name, rate, [payloads[t]] * rate, expected, mode='control')
                    blank = {'payload': encode(np.zeros((1080, 1920, 3), np.uint8), kind),
                             'sourceTime': 0., 'requestedTime': 0.}
                    run(kind, 'blank', rate, [blank] * rate, 'REVIEW', mode='control')
            run(kind, 'correct-sequence', 10, [payloads[t] for t in grids[10]], 'PASS', mode='paced')
            del payloads
        report['effectiveOpenCVThreads'] = cv2.getNumThreads()
        report['poseStatus'] = app.state.realtime.status_snapshot()
        report['remainingStreams'] = {'screw': len(app.state.screw_realtime.registry.items),
                                      'sticker': len(app.state.sticker_realtime.registry.items)}
    report['unchangedAtEnd'] = screw_snapshot() == frozen['screw'] and sticker_snapshot() == frozen['sticker']
    report['finishedAt'] = datetime.now(timezone.utc).isoformat()
    save(args.output / 'report.json', report)
    print('Saved', args.output / 'report.json', flush=True)


if __name__ == '__main__':
    main()
