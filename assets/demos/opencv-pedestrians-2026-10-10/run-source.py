"""Replay a downloaded public clip through the local realtime API and export its measured output."""
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

import cv2
import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from algorithms.pipeline import annotate

DIRECTORY = ROOT/'assets/demos/opencv-pedestrians-2026-10-10'
SERVER = 'http://127.0.0.1:8000'


def main():
    source = json.loads((DIRECTORY/'source.json').read_text(encoding='utf-8'))
    path = DIRECTORY/source['filename']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']
    credentials = json.loads((ROOT/'.runtime/local-settings.json').read_text(encoding='utf-8'))
    session = requests.Session()
    session.trust_env = False
    login = session.post(SERVER+'/api/v1/login', json=credentials, timeout=15)
    login.raise_for_status()
    session.headers['X-CSRF-Token'] = login.json()['csrf']
    cap = cv2.VideoCapture(str(path))
    source_hz = cap.get(cv2.CAP_PROP_FPS)
    hz, duration, width, height = 5, 20, 480, 360
    raw_writer = cv2.VideoWriter(str(DIRECTORY/'original.webm'), cv2.VideoWriter_fourcc(*'VP80'), hz, (width, height))
    result_writer = cv2.VideoWriter(str(DIRECTORY/'detection.webm'), cv2.VideoWriter_fourcc(*'VP80'), hz, (width, height))
    assert raw_writer.isOpened() and result_writer.isOpened()
    stream_id = str(uuid.uuid4())
    frames, start, cold_ms, last_frame_index = [], None, None, -1
    try:
        for i in range(hz*duration):
            target_index = round(i*source_hz/hz)
            while last_frame_index < target_index:
                ok, source_frame = cap.read()
                assert ok, f'Decode stopped at frame {last_frame_index+1}'
                last_frame_index += 1
            frame = cv2.resize(source_frame, (width, height))
            ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            assert ok
            payload = jpeg.tobytes()
            if start is None:
                # Warm-up is measured separately; it is not disguised as steady-state latency.
                began = time.perf_counter()
                warm = session.post(SERVER+'/api/v1/realtime/frame', data=payload,
                                    headers={'Content-Type': 'image/jpeg'}, timeout=90)
                warm.raise_for_status()
                cold_ms = (time.perf_counter()-began)*1000
                start = time.perf_counter()
            target = start+i/hz
            if target > time.perf_counter():
                time.sleep(target-time.perf_counter())
            began = time.perf_counter()
            response = session.post(SERVER+'/api/v1/realtime/frame', data=payload,
                headers={'Content-Type': 'image/jpeg', 'X-Stream-Id': stream_id, 'X-Frame-Time': str(i/hz)}, timeout=60)
            elapsed_ms = (time.perf_counter()-began)*1000
            response.raise_for_status()
            data = response.json()
            assert data['tracking'] == 'stream-scoped' and data['frameTime'] == i/hz
            assert data['width'] == width and data['height'] == height
            assert elapsed_ms >= 0 and data['verdict'] == 'REVIEW'
            data.update(time=i/hz, requestMs=round(elapsed_ms, 2), scheduleLagMs=round((began-target)*1000, 2))
            frames.append(data)
            # Draw on the JPEG decoded by the server, keeping evidence and image identical.
            decoded = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
            marked = annotate(decoded.copy(), data['people'])
            raw_writer.write(decoded)
            result_writer.write(marked)
            if i == 0:
                cv2.imwrite(str(DIRECTORY/'detection-poster.jpg'), marked)
            if i % 25 == 0:
                print(f'{i/hz:.1f}s: {len(data["people"])} people, request {elapsed_ms:.1f} ms', flush=True)
        wall_seconds = time.perf_counter()-start
    finally:
        cap.release()
        raw_writer.release()
        result_writer.release()
        try:
            released = session.delete(SERVER+'/api/v1/realtime/streams/'+stream_id, timeout=10)
            released.raise_for_status()
        finally:
            session.post(SERVER+'/api/v1/logout', timeout=10).raise_for_status()
            session.close()
    assert len(frames) == hz*duration
    for filename in ['original.webm', 'detection.webm']:
        clip = cv2.VideoCapture(str(DIRECTORY/filename))
        count, ok = 0, True
        while ok:
            ok, _ = clip.read()
            count += int(ok)
        clip.release()
        assert count == len(frames), (filename, count)
    summary = {
        'mode': 'Recorded public video replayed through the actual realtime HTTP API, paced at 5 Hz; this page replays measured responses.',
        'duration_seconds': duration, 'sample_hz': hz, 'frames': len(frames),
        'frames_with_people': sum(bool(f['people']) for f in frames),
        'max_people': max(len(f['people']) for f in frames),
        'request_mean_ms': float(np.mean([f['requestMs'] for f in frames])),
        'request_p95_ms': float(np.quantile([f['requestMs'] for f in frames], .95)),
        'server_mean_ms': float(np.mean([f['processingMs'] for f in frames])),
        'warmup_request_ms': cold_ms, 'paced_wall_seconds': wall_seconds,
        'request_deadline_misses': sum(f['requestMs'] > 1000/hz for f in frames),
        'tracking': 'stream-scoped', 'model': 'YOLO11s-pose',
        'model_sha256': hashlib.sha256((ROOT/'models/yolo11s-pose.pt').read_bytes()).hexdigest(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'stream_released': True, 'session_logged_out': True,
        'limitations': 'No annotated accuracy measurement. This is human pose detection, not the Kinect V2.2 four-stage assembly model. Track IDs are not identities.',
    }
    result = {'source': source, 'summary': summary, 'frames': frames}
    (DIRECTORY/'realtime-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    template = (ROOT/'tools/demo-video-stream.html').read_text(encoding='utf-8')
    html = template.replace('/*PAYLOAD*/', 'const DATA = '+json.dumps(result, ensure_ascii=False).replace('<', '\\u003c')+';')
    (DIRECTORY/'demo.html').write_text(html, encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
