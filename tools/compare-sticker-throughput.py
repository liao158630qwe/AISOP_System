"""Alternate original/candidate CUDA sticker inference on identical video JPEGs.

This measures the detector, not browser/HTTP throughput or camera performance.
Each version keeps an independent action sequence. Outputs must match exactly;
timing fields are excluded, but action timestamps and signatures are compared.
"""
import argparse
from contextlib import contextmanager
import copy
import hashlib
import importlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_BASELINE = ROOT / 'test-results/throughput-optimization-20261107'
DEFAULT_VIDEO = Path('C:/Users/Administrator/Downloads/lv_0_20261017162449.mp4')
TIMING_KEYS = {'timingsMs', 'inferenceMs', 'processingMs', 'totalProcessingMs', 'serviceTimingsMs'}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def file_hash(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def without_timings(value):
    if isinstance(value, dict):
        return {key: without_timings(item) for key, item in value.items() if key not in TIMING_KEYS}
    if isinstance(value, (list, tuple)):
        return [without_timings(item) for item in value]
    return value


def difference_paths(left, right, path='$', limit=40):
    """Concise mismatch locations; complete values remain in frames.jsonl."""
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                result.append(path + '.' + key)
            else:
                result += difference_paths(left[key], right[key], path + '.' + key, limit-len(result))
            if len(result) >= limit:
                break
        return result[:limit]
    if isinstance(left, list) and isinstance(right, list):
        result = [path + '.length'] if len(left) != len(right) else []
        for index, (a, b) in enumerate(zip(left, right)):
            result += difference_paths(a, b, f'{path}[{index}]', limit-len(result))
            if len(result) >= limit:
                break
        return result[:limit]
    return [path] if limit else []


def load_backup(name, path):
    if not path.is_file():
        raise FileNotFoundError('Missing original module: ' + str(path))
    qualified = 'algorithms._throughput_original_' + name
    spec = importlib.util.spec_from_file_location(qualified, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def module_version(modules):
    """Switch dynamic relative imports without changing candidate module objects."""
    package = importlib.import_module('algorithms')
    missing = object()
    saved = {}
    for name, module in modules.items():
        qualified = 'algorithms.' + name
        saved[name] = (sys.modules.get(qualified, missing), getattr(package, name, missing))
        sys.modules[qualified] = module
        setattr(package, name, module)
    try:
        yield
    finally:
        for name, (entry, attribute) in saved.items():
            qualified = 'algorithms.' + name
            if entry is missing:
                sys.modules.pop(qualified, None)
            else:
                sys.modules[qualified] = entry
            if attribute is missing:
                delattr(package, name)
            else:
                setattr(package, name, attribute)


def versions(baseline):
    candidate = {name: importlib.import_module('algorithms.' + name)
                 for name in ('cuda_vision', 'cuda_actions', 'gpu_classifier', 'sticker_actions')}
    paths = {
        'cuda_vision': baseline / 'before-vision/cuda_vision.py',
        **{name: baseline / 'before-gpu' / (name + '.py')
           for name in ('cuda_actions', 'gpu_classifier', 'sticker_actions')},
    }
    original = {name: load_backup(name, path) for name, path in paths.items()}
    original['cuda_vision'].ROOT = ROOT
    original['cuda_vision'].FEATURE_WEIGHTS = ROOT / original['cuda_vision'].FEATURE_ASSET
    original['cuda_actions'].v = original['cuda_vision']
    original['sticker_actions'].ROOT = ROOT
    original['sticker_actions'].vision = original['cuda_vision']
    original['sticker_actions'].cuda_actions = original['cuda_actions']
    source_paths = {'original': paths,
                    'candidate': {name: Path(module.__file__).resolve() for name, module in candidate.items()}}
    return {'original': original, 'candidate': candidate}, source_paths


def encode_sample(frame, cv2):
    height, width = frame.shape[:2]
    scale = min(1., 1920/max(width, height), math.sqrt(1920*1080/(width*height)))
    size = max(1, math.floor(width*scale)), max(1, math.floor(height*scale))
    if size != (width, height):
        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise ValueError('JPEG encoding failed')
    if encoded.size > 1024*1024:
        raise ValueError('JPEG82 exceeds the 1 MiB station contract; no quality fallback in this comparison')
    return encoded.tobytes(), size


def summarize_times(values):
    if not values:
        return dict(count=0, medianMs=None, p95Ms=None, meanMs=None)
    ordered = sorted(values)
    return dict(count=len(values), medianMs=round(statistics.median(values), 3),
                p95Ms=round(ordered[math.ceil(.95*len(ordered))-1], 3),
                meanMs=round(statistics.mean(values), 3))


def compare(args):
    import cv2
    import torch
    from algorithms.gpu import require_cuda
    from algorithms.stickers import snapshot, verify
    from backend.inference_runtime import configure_inference_runtime, configure_torch_runtime

    video = args.video.resolve()
    if not video.is_file():
        raise FileNotFoundError('Video not found: ' + str(video))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    device = require_cuda()
    configure_inference_runtime(args.opencv_threads)
    runtime = configure_torch_runtime(args.torch_threads)
    frozen = snapshot()
    verify(frozen)
    if frozen['config'].get('mode') != 'action':
        raise ValueError('The configured sticker model must use action mode')
    modules, source_paths = versions(args.baseline.resolve())
    provenance = {version: {name: dict(path=str(path), sha256=file_hash(path))
                            for name, path in paths.items()} for version, paths in source_paths.items()}
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError('Cannot decode source video')
    native_fps, frame_count = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not math.isfinite(native_fps) or native_fps <= 0 or args.sample_fps > native_fps:
        raise ValueError('Sample FPS must not exceed the positive native video frame rate')
    manifest = dict(video=dict(path=str(video), sha256=file_hash(video), nativeFps=native_fps,
        frameCount=frame_count, width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), durationSeconds=frame_count/native_fps),
        sampleFps=args.sample_fps, maxFrames=args.max_frames, jpegQuality=82,
        maxLongEdge=1920, maxPixels=1920*1080, modules=provenance,
        gpu=torch.cuda.get_device_name(device), runtime=runtime, opencvThreads=args.opencv_threads,
        timing='CUDA synchronized wall time, alternating original/candidate order; excludes input video decode, resize, JPEG encoding, sequence update, HTTP and UI',
        percentile='nearest-rank', comparison='exact equality, timing fields excluded; action timestamps retained')
    write_json(output/'manifest.json', manifest)
    write_json(output/'frozen-model.json', frozen)
    detectors, sequences = {}, {}
    warmup = []
    reference = cv2.imread(str(ROOT/frozen['config']['reference']))
    if reference is None:
        raise ValueError('Reference image is unreadable')
    warm_payload, _ = encode_sample(reference, cv2)

    def inspect(version, payload):
        with module_version(modules[version]):
            torch.cuda.synchronize(device)
            started = time.perf_counter()
            image = modules[version]['cuda_vision'].decode(payload)
            torch.cuda.synchronize(device)
            decoded = time.perf_counter()
            result = detectors[version].inspect(image)
            torch.cuda.synchronize(device)
            ended = time.perf_counter()
        return result, dict(decodeMs=(decoded-started)*1000, inspectMs=(ended-decoded)*1000,
                            decodeInspectMs=(ended-started)*1000)

    counts = {key: 0 for key in ('registration', 'targets', 'displayTargets', 'inspection', 'sequence', 'aggregate', 'compliance')}
    timings = {version: {field: [] for field in ('decodeMs', 'inspectMs', 'decodeInspectMs')}
               for version in modules}
    registered_timings = {version: [] for version in modules}
    matched_frames, samples, source_index = 0, 0, -1
    mismatching_frames = []
    try:
        for version, dependency_set in modules.items():
            with module_version(dependency_set):
                started = time.perf_counter()
                detectors[version] = dependency_set['sticker_actions'].StickerActionDetector(copy.deepcopy(frozen))
                sequences[version] = dependency_set['sticker_actions'].StickerActionSequence(
                    frozen['config']['targets'], frozen['config']['action'])
                torch.cuda.synchronize(device)
                warmup.append(dict(version=version, phase='load', milliseconds=(time.perf_counter()-started)*1000))
        for turn in range(args.warmup):
            for version in (('original', 'candidate') if turn % 2 == 0 else ('candidate', 'original')):
                result, timing = inspect(version, warm_payload)
                warmup.append(dict(version=version, phase='reference', registered=result['registered'], **timing))
        write_json(output/'warmup.json', warmup)
        with (output/'frames.jsonl').open('w', encoding='utf-8') as records:
            while not args.max_frames or samples < args.max_frames:
                ok, frame = cap.read()
                if not ok:
                    break
                source_index += 1
                target_index = round(samples*native_fps/args.sample_fps)
                if source_index < target_index:
                    continue
                stamp = source_index/native_fps
                payload, size = encode_sample(frame, cv2)
                order = ['original', 'candidate'] if samples % 2 == 0 else ['candidate', 'original']
                row = dict(sample=samples, sourceFrame=source_index, frameTime=stamp,
                           requestedSampleTime=samples/args.sample_fps, order=order,
                           jpeg=dict(bytes=len(payload), width=size[0], height=size[1], sha256=hashlib.sha256(payload).hexdigest()))
                for version in order:
                    inspection, timing = inspect(version, payload)
                    for field, value in timing.items():
                        timings[version][field].append(value)
                    if inspection['registered']:
                        registered_timings[version].append(timing['inspectMs'])
                    # update() deliberately consumes signatures. Preserve the
                    # raw observations first so metadata regressions are visible.
                    raw = without_timings(copy.deepcopy(inspection))
                    sequence = sequences[version].update(stamp, inspection)
                    aggregate = sequences[version].aggregate()
                    compliance = modules[version]['sticker_actions'].action_compliance(aggregate, sequence)
                    row[version] = dict(timings={key: round(value, 3) for key, value in timing.items()},
                        detectorTimingsMs=inspection.get('timingsMs', {}), inspection=raw,
                        sequence=sequence, aggregate=aggregate, compliance=compliance)
                a, b = row['original'], row['candidate']
                checks = dict(registration=all(a['inspection'].get(key) == b['inspection'].get(key)
                                              for key in ('registered', 'inliers', 'reason')),
                    targets=a['inspection']['targets'] == b['inspection']['targets'],
                    displayTargets=a['inspection']['displayTargets'] == b['inspection']['displayTargets'],
                    inspection=a['inspection'] == b['inspection'], sequence=a['sequence'] == b['sequence'],
                    aggregate=a['aggregate'] == b['aggregate'], compliance=a['compliance'] == b['compliance'])
                row['matches'] = checks
                row['differencePaths'] = difference_paths(
                    {key: a[key] for key in ('inspection', 'sequence', 'aggregate', 'compliance')},
                    {key: b[key] for key in ('inspection', 'sequence', 'aggregate', 'compliance')})
                for key, equal in checks.items():
                    counts[key] += not equal
                matched_frames += all(checks.values())
                if not all(checks.values()):
                    mismatching_frames.append(samples)
                records.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
                records.flush()
                samples += 1
                if samples % 25 == 0:
                    print(f'Compared {samples} frames; mismatches {len(mismatching_frames)}', flush=True)
        if not samples:
            raise ValueError('No sampled video frames were decoded')
        final = {}
        for version in modules:
            sequence = sequences[version].snapshot()
            aggregate = sequences[version].aggregate(final=True)
            final[version] = dict(sequence=sequence, aggregate=aggregate,
                compliance=modules[version]['sticker_actions'].action_compliance(aggregate, sequence, final=True))
        verify(frozen)
        ending = snapshot()
        assets_unchanged = ending['config_hash'] == frozen['config_hash'] and ending['assets'] == frozen['assets']
        code_unchanged = all(file_hash(path) == provenance[version][name]['sha256']
            for version, paths in source_paths.items() for name, path in paths.items())
        stats = {version: {key: summarize_times(values) for key, values in measures.items()}
                 for version, measures in timings.items()}
        for version in stats:
            stats[version]['registeredInspect'] = summarize_times(registered_timings[version])
        original_median = stats['original']['decodeInspectMs']['medianMs']
        candidate_median = stats['candidate']['decodeInspectMs']['medianMs']
        summary = dict(samples=samples, matchedFrames=matched_frames, mismatchingFrames=mismatching_frames,
            mismatchCounts=counts, finalMatches=final['original'] == final['candidate'], final=final,
            assetsUnchanged=assets_unchanged, codeUnchanged=code_unchanged, timing=stats,
            medianReductionPercent=round((1-candidate_median/original_median)*100, 3) if original_median else None,
            scope='sampled development video output equivalence and isolated detector timings; not independent accuracy or end-to-end FPS validation')
        summary['passed'] = matched_frames == samples and summary['finalMatches'] and assets_unchanged and code_unchanged
        write_json(output/'report.json', summary)
        print(json.dumps({key: summary[key] for key in ('passed', 'samples', 'matchedFrames', 'mismatchCounts', 'finalMatches', 'timing', 'medianReductionPercent')}, ensure_ascii=False, indent=2))
        print('Artifacts: ' + str(output), flush=True)
        return 0 if summary['passed'] else 1
    except Exception as error:
        write_json(output/'failure.json', dict(error=type(error).__name__, message=str(error), completedSamples=samples))
        raise
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, default=DEFAULT_VIDEO)
    parser.add_argument('--baseline', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--sample-fps', type=float, default=4)
    parser.add_argument('--max-frames', type=int, default=0, help='0 compares the full video')
    parser.add_argument('--warmup', type=int, default=3, help='Reference passes per version, excluded from timings')
    parser.add_argument('--opencv-threads', type=int, default=4)
    parser.add_argument('--torch-threads', type=int, default=4)
    parser.add_argument('--output', type=Path, required=True, help='New output directory, never overwritten')
    args = parser.parse_args()
    if not math.isfinite(args.sample_fps) or not .5 <= args.sample_fps <= 30 or args.max_frames < 0 or args.warmup < 1:
        parser.error('sample-fps must be 0.5–30, max-frames >= 0, and warmup >= 1')
    if not 1 <= args.opencv_threads <= 16 or not 1 <= args.torch_threads <= 16:
        parser.error('Thread counts must be 1–16')
    return compare(args)


if __name__ == '__main__':
    raise SystemExit(main())
