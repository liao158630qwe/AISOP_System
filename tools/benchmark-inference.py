"""Compare cached TCN and batched ROI inference with saved pre-change implementations.

Uses existing trained weights and local inputs. Measures latency and output parity,
not industrial detection accuracy. Does not change checkpoints or historic reports.
"""
import argparse
import hashlib
import importlib.util
import json
import platform
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'experiments/public_training'))
from scene_v2 import DensePredictor
from scene_runtime import run_recording
from algorithms.inspection import Inspector


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measure(call, iterations, sync=lambda: None):
    samples = []
    for _ in range(iterations):
        sync()
        started = time.perf_counter()
        call()
        sync()
        samples.append((time.perf_counter()-started)*1000)
    return {'p50_ms': float(np.median(samples)), 'p95_ms': float(np.percentile(samples, 95)),
            'samples_ms': samples}


def state_bytes(state):
    if isinstance(state, torch.Tensor):
        return state.nelement()*state.element_size()
    return sum(state_bytes(item) for item in state) if isinstance(state, (list, tuple)) else 0


def temporal(args):
    old = load_source('baseline_scene_v2', args.baseline/'scene_v2.py')
    checkpoint_path = args.bundle/'export/stage-ensemble.pt'
    checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
    workflow = json.loads((args.bundle/'export/workflow.json').read_text(encoding='utf-8'))
    parity = []
    first = None
    for path in sorted((args.bundle/'runtime-inputs').glob('*.npz')):
        with np.load(path, allow_pickle=False) as data:
            x, valid = data['x'], data['input_valid']
        if first is None:
            first = x
        expected = old.predict_members(checkpoint['members'], x)
        stream = DensePredictor(checkpoint['members'])
        chunks, position, size_index = [], 0, 0
        sizes = [1, 7, 32, 3, 128, 19]
        while position < len(x):
            size = sizes[size_index % len(sizes)]
            chunks.append(stream.push(x[position:position+size]))
            position += size
            size_index += 1
        actual = np.concatenate(chunks)
        error = float(np.max(np.abs(expected-actual)))
        if not np.allclose(actual, expected, atol=1e-6):
            raise AssertionError(f'{path.stem}: probability mismatch {error}')
        result = run_recording(checkpoint, x, valid, workflow)
        saved = json.loads((args.bundle/'runtime-outputs'/f'{path.stem}.json').read_text(encoding='utf-8'))
        for key in ['raw', 'stages', 'sop']:
            if result[key] != saved[key]:
                raise AssertionError(f'{path.stem}: historic {key} mismatch')
        parity.append({'recording': path.stem, 'frames': len(x), 'max_probability_error': error,
                       'raw_stages_sop_match_historic': True, 'input_sha256': sha(path)})
    if first is None:
        raise ValueError('No temporal inputs found')
    timing = []
    for size in [1, 8, 128]:
        record = {'chunk_frames': size}
        for name, constructor in [('baseline', old.DensePredictor), ('optimized', DensePredictor)]:
            predictor = constructor(checkpoint['members'])
            predictor.push(first[:512])
            chunk = first[512:512+size]
            for _ in range(10):
                predictor.push(chunk)
            record[name] = measure(lambda: predictor.push(chunk), args.iterations)
            record[name]['state_bytes'] = state_bytes(predictor.states)
        record['speedup_p50'] = record['baseline']['p50_ms']/record['optimized']['p50_ms']
        timing.append(record)
    return {'checkpoint_sha256': sha(checkpoint_path), 'device': 'cpu', 'dtype': 'float32',
            'scope': 'trained three-member HA4M ensemble; normalization, forward, CPU probabilities included; excludes video/HTTP',
            'warmup': '512-frame prefill then 10 chunks', 'recording_parity': parity, 'latency': timing}


def detection(args):
    from ultralytics import YOLO
    old = load_source('algorithms.baseline_inspection', args.baseline/'inspection.py')
    model_path = ROOT/'models/yolo11n.pt'
    model = YOLO(str(model_path))
    capture = cv2.VideoCapture(str(args.video))
    ok, frame = capture.read()
    capture.release()
    if not ok:
        raise ValueError('Cannot decode benchmark video')
    device = 0 if torch.cuda.is_available() else 'cpu'
    sync = torch.cuda.synchronize if device == 0 else lambda: None
    model.predict(frame, imgsz=640, conf=.25, device=device, verbose=False)
    timing = []
    for columns, rows in [(2, 1), (2, 2), (4, 2)]:
        targets = [{'id': f'{col}-{row}', 'category': 'screw',
                    'roi': [col/columns, row/rows, (col+1)/columns, (row+1)/rows],
                    'method': 'yolo', 'asset': 'models/yolo11n.pt', 'class_name': 'person', 'threshold': .25}
                   for row in range(rows) for col in range(columns)]
        config = {'assets': {}, 'sha256': 'benchmark-only', 'config': {
            'id': 'benchmark', 'name': 'benchmark', 'version': '1', 'approved': True,
            'fixed_fixture': True, 'min_sharpness': 0,
            'checks': [{'id': 'objects', 'name': 'objects', 'kind': 'presence',
                        'expected': {'screw': len(targets)}, 'targets': targets}]}}
        record = {'regions': len(targets)}
        outputs = {}
        for name, constructor in [('baseline', old.Inspector), ('optimized', Inspector)]:
            inspector = constructor(config)
            inspector.cache[('yolo', 'models/yolo11n.pt')] = model
            for _ in range(5):
                inspector.inspect(frame)
            outputs[name] = inspector.inspect(frame)
            record[name] = measure(lambda: inspector.inspect(frame), args.iterations, sync)
        before = outputs['baseline']['checks'][0]['targets']
        after = outputs['optimized']['checks'][0]['targets']
        error = max(abs(a['score']-b['score']) for a, b in zip(before, after))
        if any(a['state'] != b['state'] for a, b in zip(before, after)) or error > .001:
            raise AssertionError(f'Detection parity failed: {error}')
        record.update(candidate_states_match=True, max_score_error=error,
                      candidates=sum(t['state'] == 'CANDIDATE' for t in after),
                      speedup_p50=record['baseline']['p50_ms']/record['optimized']['p50_ms'])
        timing.append(record)
    return {'checkpoint_sha256': sha(model_path), 'video_sha256': sha(args.video), 'device': str(device),
            'frame_shape': list(frame.shape), 'scope': 'real first-frame ROIs, YOLO11n COCO person class; synthetic station configuration solely exercises inference; no screw/industrial accuracy claims',
            'warmup_per_case': 5, 'latency': timing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT/'work/inference-optimization-2026-10-10/baseline')
    parser.add_argument('--bundle', type=Path, default=ROOT/'work/simple-sop-2026-10-10/v2.2')
    parser.add_argument('--video', type=Path, default=ROOT/'assets/factory-workers.mp4')
    parser.add_argument('--out', type=Path, default=ROOT/'work/inference-optimization-2026-10-10/benchmark.json')
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--section', choices=['all', 'temporal', 'detection'], default='all')
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    torch.set_num_threads(4)
    report = {'torch': torch.__version__, 'platform': platform.platform(), 'cpu_threads': 4,
              'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
              'iterations': args.iterations,
              'source_sha256': {str(path.relative_to(ROOT)): sha(path) for path in [
                  args.baseline/'scene_v2.py', args.baseline/'inspection.py',
                  ROOT/'experiments/public_training/scene_v2.py', ROOT/'algorithms/inspection.py']}}
    for section, runner in [('temporal', temporal), ('detection', detection)]:
        if args.section in ('all', section):
            report[section] = runner(args)
            print(f'{section}: parity passed', flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for section in ['temporal', 'detection']:
        if section in report:
            for result in report[section]['latency']:
                print(section, json.dumps({k: v if k not in ['baseline', 'optimized'] else
                    {a: b for a, b in v.items() if a != 'samples_ms'} for k, v in result.items()}), flush=True)
    print(args.out, flush=True)


if __name__ == '__main__':
    main()
