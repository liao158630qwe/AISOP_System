"""Evaluate saved analysis.json files against frame-level field annotations.

The tool intentionally does not train or tune thresholds. It reports presence
and action metrics by declared field conditions, so a new model can be compared
on an untouched test split with the same annotations.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def _f1(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _nearest_frame(frames, timestamp, tolerance):
    candidates = [frame for frame in frames if abs(float(frame.get('time', 0)) - timestamp) <= tolerance]
    return min(candidates, key=lambda frame: abs(float(frame.get('time', 0)) - timestamp), default=None)


def evaluate(dataset, split='test'):
    samples = [item for item in dataset.get('videos', []) if item.get('split', 'test') == split]
    if not samples:
        raise ValueError(f'没有 split={split} 的标注视频')
    action_counts = defaultdict(lambda: [0, 0, 0])
    condition_counts = defaultdict(lambda: [0, 0, 0])
    presence = [0, 0, 0]
    used = 0
    for item in samples:
        prediction = json.loads(Path(item['prediction']).read_text(encoding='utf-8'))
        fps = float(prediction.get('model', {}).get('sampleFps', item.get('sample_fps', 5)))
        tolerance = max(.05, .51 / max(.5, fps))
        frames = prediction.get('frames', [])
        conditions = item.get('conditions', {})
        for annotation in item.get('frames', []):
            frame = _nearest_frame(frames, float(annotation['time']), tolerance)
            if frame is None:
                continue
            used += 1
            expected = [str(person.get('action', 'unknown')) for person in annotation.get('people', [])]
            actual = [str(person.get('action', 'unknown')) for person in frame.get('people', [])]
            expected_set, actual_set = set(expected), set(actual)
            presence[0] += int(expected_set and actual_set)
            presence[1] += int(not expected_set and actual_set)
            presence[2] += int(expected_set and not actual_set)
            for action in sorted(expected_set | actual_set):
                action_counts[action][0] += int(action in expected_set and action in actual_set)
                action_counts[action][1] += int(action not in expected_set and action in actual_set)
                action_counts[action][2] += int(action in expected_set and action not in actual_set)
            for key, value in conditions.items():
                group = f'{key}={value}'
                condition_counts[group][0] += int(expected_set and actual_set)
                condition_counts[group][1] += int(not expected_set and actual_set)
                condition_counts[group][2] += int(expected_set and not actual_set)
    if not used:
        raise ValueError('没有找到能与预测采样帧匹配的标注时间点')
    pp, pr, pf = _f1(*presence)
    actions = {name: dict(zip(('precision', 'recall', 'f1'), _f1(*counts)))
               for name, counts in sorted(action_counts.items())}
    groups = {name: dict(zip(('precision', 'recall', 'f1'), _f1(*counts)))
              for name, counts in sorted(condition_counts.items())}
    macro_f1 = sum(item['f1'] for item in actions.values()) / len(actions) if actions else 0.0
    return {'datasetVersion': dataset.get('dataset_version'), 'split': split, 'videos': len(samples),
            'matchedFrames': used, 'presence': {'precision': pp, 'recall': pr, 'f1': pf},
            'actionMacroF1': macro_f1, 'actions': actions, 'conditions': groups,
            'provenance': {'predictionsAreSavedAnalysisJson': True, 'thresholdTuningPerformed': False}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--split', default='test', choices=['train', 'validation', 'test'])
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.dataset.read_text(encoding='utf-8')), args.split)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(output + '\n', encoding='utf-8')
    else:
        print(output)


if __name__ == '__main__':
    main()
