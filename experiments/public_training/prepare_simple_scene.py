"""Freeze a five-class assembly finishing task without changing subject splits."""
import copy
import json
from pathlib import Path
import numpy as np
from common import file_hash, save_json

ROOT = Path('work/simple-sop-2026-10-10')
SOURCE = Path('work/public-training-2026-10-10/ha4m')


def main():
    dataset = copy.deepcopy(json.loads((SOURCE/'dataset.json').read_text(encoding='utf-8')))
    dataset['source_dataset_sha256'] = file_hash(SOURCE/'dataset.json')
    dataset['labels'] = ['other_work', 'join_blocks', 'cover', 'screws', 'allen_key_turn_return']
    dataset['label_mapping'] = {'0..7': 0, '8': 1, '9': 2, '10': 3, '11': 4,
                                '-100': 'preserved; never relabeled as background'}
    dataset['feature_spec']['not_deployment_compatible'] = 'Kinect-derived five-stage research task; not YOLO runtime labels'
    audit = {}
    for record in dataset['records']:
        name = record['recording']
        source = SOURCE/'prepared'/f'{name}.npz'
        assert file_hash(source) == record['prepared_sha256']
        with np.load(source, allow_pickle=False) as data:
            values = {k: data[k] for k in data.files}
        original = values['y']
        values['y'] = np.where(original < 0, -100, np.maximum(original-7, 0))
        target = ROOT/'data'/'prepared'/f'{name}.npz'
        target.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(target, **values)
        record['prepared_sha256'] = file_hash(target)
        record['class_counts'] = np.bincount(values['y'][values['y'] >= 0], minlength=5).tolist()
        # Collapse transitions, ignoring non-task intervals, only for descriptive audit.
        selected = values['y'][values['y'] > 0]
        sequence = selected[np.r_[True, selected[1:] != selected[:-1]]].tolist() if len(selected) else []
        audit[name] = {'stage_sequence': sequence, 'contains_all_four': set(sequence) == {1, 2, 3, 4}}
    save_json(ROOT/'data'/'dataset.json', dataset)
    save_json(ROOT/'data'/'sequence-audit.json', audit)
    workflow = {'id': 'desktop-assembly-finishing', 'name': '桌面装配收尾',
        'labels_zh': ['其他作业', '合拢组件', '放置盖板', '放置螺丝', '使用工具操作并归位'],
        'steps': [1, 2, 3, 4], 'sample_hz': 10,
        'threshold_candidates': [0.45, 0.6, 0.75], 'hold_candidates': [3, 5, 10],
        'semantics': 'Stage presence and order only; cannot establish screw count, torque, or assembly quality.'}
    save_json(ROOT/'workflow.json', workflow)
    print(json.dumps({'recordings': len(audit), 'all_four': sum(a['contains_all_four'] for a in audit.values())}))


if __name__ == '__main__':
    main()
