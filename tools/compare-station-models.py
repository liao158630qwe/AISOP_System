"""Paired real-weight station benchmark. Does not write business records.

The baseline directory must contain pre-change screw_refined.py,
screw_multiview.py and stickers.py. Each pair uses the same decoded JPEG;
execution order alternates, and each detector has independent process history.
This is output regression and latency evidence, not labeled accuracy evidence.
"""
import argparse
from collections import deque
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_baseline(name, directory):
    spec = importlib.util.spec_from_file_location('algorithms._station_baseline_' + name,
                                                  directory / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, 'ROOT'):
        module.ROOT = ROOT
    return module


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def states(value):
    """Discrete evidence, retaining event timestamps but ignoring numeric scores."""
    if isinstance(value, dict):
        return {k: states(v) for k, v in value.items() if k not in {
            'inferenceMs', 'score', 'contextScore', 'componentScores', 'bbox',
            'detectedBbox', 'searchRoi', 'materialBareScore'}}
    if isinstance(value, list):
        return [states(v) for v in value]
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--kind', choices=['screw', 'sticker'], required=True)
    p.add_argument('--video', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--sample-fps', type=float, default=4)
    p.add_argument('--max-frames', type=int, default=0)
    args = p.parse_args()
    import cv2
    import numpy as np
    import torch
    from algorithms import screws, stickers
    from algorithms.cuda_vision import decode
    from algorithms.video import file_hash
    from backend.inference_runtime import configure_inference_runtime, configure_torch_runtime
    configure_inference_runtime(4)
    configure_torch_runtime(4)
    if not args.video.is_file() or not np.isfinite(args.sample_fps) or args.sample_fps <= 0:
        p.error('A readable video and positive finite sample FPS are required')
    args.output.mkdir(parents=True, exist_ok=False)
    module = screws if args.kind == 'screw' else stickers
    frozen = module.snapshot()
    if args.kind == 'screw':
        old = load_baseline('screw_multiview', args.baseline)
        old.RefinedScrewDetector = load_baseline('screw_refined', args.baseline).RefinedScrewDetector
        detectors = {'baseline': old.MultiViewScrewDetector(frozen), 'optimized': screws.ScrewDetector(frozen)}
    else:
        old = load_baseline('stickers', args.baseline)
        detectors = {'baseline': old.AppearanceStickerDetector(frozen), 'optimized': stickers.StickerDetector(frozen)}
    histories = {k: screws.create_history(frozen['config']) if args.kind == 'screw'
                 else stickers.create_sequence(frozen['config']) for k in detectors}
    frames = {k: deque(maxlen=120) for k in detectors}
    timings = {k: [] for k in detectors}
    registered_timings = {k: [] for k in detectors}
    mismatches = []
    max_score_delta = max_box_delta = max_anchor_delta = 0.

    def encode(image):
        h, w = image.shape[:2]
        scale = min(1., 1920 / max(h, w), (1920 * 1080 / (h * w)) ** .5)
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, payload = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            raise ValueError('JPEG encoding failed')
        return payload.tobytes()

    for reference in [frozen['config']['reference']] + [v['reference'] for v in frozen['config'].get('calibrations', [])]:
        image = decode(encode(cv2.imread(str(ROOT / reference))))
        for detector in detectors.values():
            for _ in range(3):
                detector.inspect(image)
    sources = {name: {version: file_hash(directory / name) for version, directory in
                     [('baseline', args.baseline), ('optimized', ROOT / 'algorithms')]}
               for name in ['screw_refined.py', 'screw_multiview.py', 'stickers.py']}
    video_hash = file_hash(args.video)
    capture = cv2.VideoCapture(str(args.video))
    fps = capture.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or args.sample_fps > fps:
        raise ValueError('Sample FPS exceeds video FPS, or video cannot be decoded')
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    processed = 0
    next_sample = 0.
    try:
        with (args.output / 'frames.jsonl').open('w', encoding='utf-8') as log:
            for index in range(count):
                if not capture.grab():
                    raise ValueError('Video decode interrupted')
                timestamp = index / fps
                if timestamp + 1e-6 < next_sample:
                    continue
                next_sample += 1 / args.sample_fps
                ok, frame = capture.retrieve()
                if not ok:
                    raise ValueError('Video frame cannot be retrieved')
                payload = encode(frame)
                image = decode(payload)
                pair = {}
                for version in (['baseline', 'optimized'] if processed % 2 == 0 else ['optimized', 'baseline']):
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    result = detectors[version].inspect(image)
                    torch.cuda.synchronize()
                    elapsed = (time.perf_counter() - start) * 1000
                    timings[version].append(elapsed)
                    if result['registered']:
                        registered_timings[version].append(elapsed)
                    history = histories[version]
                    if args.kind == 'screw':
                        result = screws.current_display(result)
                        aggregate = history.update(timestamp, result)
                        sequence = history.sequence()
                    else:
                        sequence = history.update(timestamp, result)
                        frames[version].append(dict(time=timestamp, inspection=result))
                        aggregate, _ = stickers.summarize(list(frames[version]), frozen['config'], history)
                    pair[version] = dict(inspection=result, aggregate=aggregate, sequence=sequence,
                                         compliance=module.compliance(aggregate, sequence), elapsedMs=elapsed)
                before, after = pair['baseline'], pair['optimized']
                if states({k: v for k, v in before.items() if k != 'elapsedMs'}) != states({k: v for k, v in after.items() if k != 'elapsedMs'}):
                    mismatches.append(timestamp)
                for a, b in zip(before['inspection']['targets'], after['inspection']['targets']):
                    if a.get('score') is not None and b.get('score') is not None:
                        max_score_delta = max(max_score_delta, abs(a['score'] - b['score']))
                    if a.get('bbox') is not None and b.get('bbox') is not None:
                        max_box_delta = max(max_box_delta, max(abs(x - y) for x, y in zip(a['bbox'], b['bbox'])))
                for a, b in zip(before['inspection'].get('componentScores', []), after['inspection'].get('componentScores', [])):
                    max_anchor_delta = max(max_anchor_delta, abs(a - b))
                log.write(json.dumps(dict(time=timestamp, **pair), ensure_ascii=False) + '\n')
                processed += 1
                if processed % 50 == 0:
                    print(json.dumps(dict(frames=processed, seconds=round(timestamp, 2), mismatches=len(mismatches))), flush=True)
                if args.max_frames and processed >= args.max_frames:
                    break
    finally:
        capture.release()
    final = {}
    for version, history in histories.items():
        if args.kind == 'screw':
            aggregate, sequence = history.aggregate(final=True), history.sequence(final=True)
        else:
            aggregate, _ = stickers.summarize(list(frames[version]), frozen['config'], history, final=True)
            sequence = history.snapshot()
        final[version] = dict(targets=aggregate, sequence=sequence, compliance=module.compliance(aggregate, sequence, final=True))
    def stats(values):
        return dict(frames=len(values), medianMs=float(np.median(values)), p95Ms=float(np.percentile(values, 95))) if values else None
    module.verify(frozen)
    for name, hashes in sources.items():
        assert hashes['optimized'] == file_hash(ROOT / 'algorithms' / name), 'Code changed during benchmark'
        assert hashes['baseline'] == file_hash(args.baseline / name), 'Baseline changed during benchmark'
    report = dict(kind=args.kind, video=str(args.video), videoSha256=video_hash,
        gpu=torch.cuda.get_device_name(), sampleFps=args.sample_fps, frames=processed,
        timingScope='Synchronized detector wall time including CUDA-to-CPU transfers; excludes JPEG decode, original video decoding/encoding, rules, person detector, HTTP and UI',
        scope='Known-video output regression; not independent labeled accuracy validation',
        sources=sources, model=frozen, latency={k:stats(v) for k,v in timings.items()},
        registeredLatency={k:stats(v) for k,v in registered_timings.items()},
        discreteMismatchTimes=mismatches, maxScoreDelta=max_score_delta,
        maxNormalizedBoxDelta=max_box_delta, maxAnchorDelta=max_anchor_delta,
        final=final, finalDiscreteEqual=states(final['baseline']) == states(final['optimized']))
    write_json(args.output / 'report.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'model','sources','final'}}, ensure_ascii=False), flush=True)
    return 1 if mismatches or not report['finalDiscreteEqual'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
