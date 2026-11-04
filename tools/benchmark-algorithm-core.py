"""Measure cache and feature reuse in isolation, not end-to-end camera FPS."""
import argparse
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from algorithms.asset_snapshot import AssetSnapshotCache
from algorithms import screws, stickers
from algorithms.sticker_actions import action_features, foreground_mask, foreground_fraction
from algorithms.training import write_json


def measure(function, count):
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000)
    return dict(samples=count, p50_ms=float(np.percentile(samples, 50)),
                p95_ms=float(np.percentile(samples, 95)), raw_ms=samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=8)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 2:
        parser.error('iterations must be >= 2')
    result = dict(scope='isolated warm filesystem snapshot/feature microbenchmark; not model accuracy or camera FPS')
    for module in (screws, stickers):
        validate = getattr(module, '_validate_config', None)
        def uncached():
            return AssetSnapshotCache().get(module.CONFIG, module.ROOT, module._asset_names, validate=validate)
        frozen = uncached()
        assert frozen == module.snapshot()
        result[module.__name__] = dict(
            asset_bytes=sum((module.ROOT / name).stat().st_size for name in frozen['assets']),
            uncached=measure(uncached, args.iterations),
            cached=measure(module.snapshot, args.iterations * 10))
    rng = np.random.default_rng(42)
    image = rng.integers(0, 256, (180, 180, 3), dtype=np.uint8)
    reference = rng.integers(0, 256, (180, 180, 3), dtype=np.uint8)
    hog = cv2.HOGDescriptor((48, 48), (16, 16), (8, 8), (8, 8), 9)
    def old_features():
        return action_features(image, reference), foreground_fraction(image, reference)
    def reused_features():
        mask = foreground_mask(image, reference)
        return action_features(image, reference, mask=mask, hog=hog), float(mask.mean())
    np.testing.assert_array_equal(old_features()[0], reused_features()[0])
    assert old_features()[1] == reused_features()[1]
    result['action_features'] = dict(input='synthetic 180x180 crop; exact feature equivalence checked',
        uncached=measure(old_features, args.iterations * 10),
        reused=measure(reused_features, args.iterations * 10))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(json.dumps({k: {name: {metric: value for metric, value in item.items() if metric != 'raw_ms'}
                         if isinstance(item, dict) else item for name, item in entry.items()}
                      if isinstance(entry, dict) else entry for k, entry in result.items()}, indent=2))


if __name__ == '__main__':
    main()
