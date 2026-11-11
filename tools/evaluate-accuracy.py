"""Evaluate saved analysis.json files against frame-level field annotations.

The tool intentionally does not train or tune thresholds. It reports presence
and action metrics by declared field conditions, so a new model can be compared
on an untouched test split with the same annotations.
"""
import argparse
from bisect import bisect_left
import json
import math
from collections import defaultdict
from pathlib import Path


def _f1(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _timestamp(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('采样时间必须有限且非负')
    return value


def _nearest_frame(frames, times, timestamp, tolerance):
    index = bisect_left(times, timestamp)
    candidates = [i for i in (index-1, index) if 0 <= i < len(times)]
    closest = min(candidates, key=lambda i: abs(times[i]-timestamp), default=None)
    return frames[closest] if closest is not None and abs(times[closest]-timestamp) <= tolerance else None


def evaluate(dataset, split='test'):
    videos = dataset.get('videos', [])
    identities = {}
    for item in videos:
        if item.get('split') not in ('train', 'validation', 'test'):
            raise ValueError('每个视频必须显式声明 train/validation/test split')
        # Reusing a video or prediction across splits makes test metrics misleading.
        keys = [('prediction', str(Path(item['prediction']).resolve()))]
        if item.get('id'):
            keys.append(('id', item['id']))
        for key in keys:
            if key in identities and identities[key] != item['split']:
                raise ValueError('同一视频或预测文件不能跨 split 重复使用')
            identities[key] = item['split']
    samples = [item for item in videos if item['split'] == split]
    if not samples:
        raise ValueError(f'没有 split={split} 的标注视频')
    action_counts = defaultdict(lambda: [0, 0, 0])
    condition_counts = defaultdict(lambda: [0, 0, 0])
    presence = [0, 0, 0]
    used = annotated = 0
    for item in samples:
        prediction = json.loads(Path(item['prediction']).read_text(encoding='utf-8'))
        fps = float(prediction.get('model', {}).get('sampleFps', item.get('sample_fps', 5)))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('采样帧率必须有限且大于零')
        tolerance = max(.05, .51 / max(.5, fps))
        frames = sorted(prediction.get('frames', []), key=lambda frame: _timestamp(frame['time']))
        times = [_timestamp(frame['time']) for frame in frames]
        if len(set(times)) != len(times):
            raise ValueError('预测采样时间不能重复')
        conditions = item.get('conditions', {})
        for annotation in item.get('frames', []):
            annotated += 1
            frame = _nearest_frame(frames, times, _timestamp(annotation['time']), tolerance)
            used += int(frame is not None)
            expected = [str(person.get('action', 'unknown')) for person in annotation.get('people', [])]
            actual = [str(person.get('action', 'unknown')) for person in (frame or {}).get('people', [])]
            expected_set, actual_set = set(expected), set(actual)
            counts = (int(bool(expected_set) and bool(actual_set)),
                      int(not expected_set and bool(actual_set)),
                      int(bool(expected_set) and not actual_set))
            for i, count in enumerate(counts):
                presence[i] += count
            for action in sorted(expected_set | actual_set):
                action_counts[action][0] += int(action in expected_set and action in actual_set)
                action_counts[action][1] += int(action not in expected_set and action in actual_set)
                action_counts[action][2] += int(action in expected_set and action not in actual_set)
            for key, value in conditions.items():
                group = f'{key}={value}'
                for i, count in enumerate(counts):
                    condition_counts[group][i] += count
    if not annotated:
        raise ValueError('没有标注时间点可供评估')
    pp, pr, pf = _f1(*presence)
    actions = {name: dict(zip(('precision', 'recall', 'f1'), _f1(*counts)))
               for name, counts in sorted(action_counts.items())}
    groups = {name: dict(zip(('precision', 'recall', 'f1'), _f1(*counts)))
              for name, counts in sorted(condition_counts.items())}
    macro_f1 = sum(item['f1'] for item in actions.values()) / len(actions) if actions else 0.0
    return {'datasetVersion': dataset.get('dataset_version'), 'split': split, 'videos': len(samples),
            'annotatedFrames': annotated, 'matchedFrames': used, 'unmatchedFrames': annotated-used,
            'coverage': used/annotated, 'presence': {'precision': pp, 'recall': pr, 'f1': pf},
            'actionMacroF1': macro_f1, 'actions': actions, 'conditions': groups,
            'provenance': {'predictionsAreSavedAnalysisJson': True, 'thresholdTuningPerformed': False,
                'unmatchedPolicy': 'count-as-empty-prediction',
                'actionMetricScope': 'per-frame-class-presence-not-person-identity'}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--split', default='test', choices=['train', 'validation', 'test'])
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding='utf-8'))
    for item in dataset.get('videos', []):
        path = Path(item['prediction'])
        if not path.is_absolute():
            item['prediction'] = str(args.dataset.resolve().parent / path)
    result = evaluate(dataset, args.split)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(output + '\n', encoding='utf-8')
    else:
        print(output)


if __name__ == '__main__':
    main()
