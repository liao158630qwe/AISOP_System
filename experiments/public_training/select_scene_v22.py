"""Jointly select existing model ensembles and causal filters using validation only."""
import itertools
from pathlib import Path

import numpy as np

from common import file_hash, save_json
from scene_v2 import filter_stages
from evidence_filter import EvidenceFilter
from verify_scene_flow import read, score

BASE = Path('work/simple-sop-2026-10-10')
ROOT = BASE/'v2.2'


def source_hashes():
    return {name: file_hash(Path('experiments/public_training')/name) for name in
            ['common.py', 'train_temporal.py', 'scene_v2.py', 'scene_runtime.py', 'select_scene_v22.py',
             'verify_scene_flow.py', 'evidence_filter.py', 'scene_diagnostics.py', 'evaluate_scene_v22.py',
             'simple_scene_demo.html']}


def apply_filter(kind, parameters, probabilities, valid):
    if kind == 'confidence-evidence-v1':
        return EvidenceFilter(**parameters).push(probabilities, valid)
    if kind == 'ema-persistence-v2':
        return filter_stages(probabilities, valid, **parameters)
    raise ValueError(f'Unknown filter kind: {kind}')


def step_delay(parameters, kind='ema-persistence-v2', hz=10):
    p = np.eye(5)[[1]*100+[2]*100]
    stages = apply_filter(kind, parameters, p, np.ones(len(p)))
    found = np.flatnonzero(stages[100:] == 2)
    return int(found[0])/hz if len(found) else float('inf')


def main():
    dataset = read(BASE/'data/dataset.json')
    previous = read(BASE/'v2.1/selection.json')['selected']['validation']
    v2 = read(BASE/'v2/selection.json')
    models = v2['candidates']
    workflow = read(BASE/'v2.1/export/workflow.json')
    protocol = {
        'scope': 'Validation subjects only; no test arrays, predictions or metrics loaded',
        'source_sha256': source_hashes(), 'dataset_sha256': file_hash(BASE/'data/dataset.json'),
        'baseline_selection_sha256': file_hash(BASE/'v2.1/selection.json'),
        'model_selection_sha256': file_hash(BASE/'v2/selection.json'),
        'workflow_sha256': file_hash(BASE/'v2.1/export/workflow.json'),
        'models': [{'name': m['name'], 'members': m['members']} for m in models],
        'grid': {'alpha': [1., .3, .1, .05], 'hold': [1, 5, 10, 20], 'threshold': [0., .4, .55]},
        'evidence_grid': {'alpha': [1., .3, .1], 'budget': [1., 2., 3., 4.],
                          'threshold': [0., .4, .55], 'margin': [0., .1]},
        'evidence_models': ['top3-ensemble', 'all-ensemble'],
        'guard': {'stable_accuracy_min': previous['accuracy'], 'stable_macro_f1_min': previous['macro_f1_all_classes'],
                  'exact_sequences_min': previous['exact_stage_sequence_matches'], 'clean_step_delay_max_seconds': 1.3},
        'ranking': 'exact sequences, segment F1@0.5, macro F1, accuracy, shorter synthetic delay, fewer members',
        'limitations': 'Previously reported test subjects, not a new external evaluation; finite validation set',
    }
    save_json(ROOT/'protocol.json', protocol)
    names = dataset['splits']['val']
    truths, validity, cache, hashes = [], [], {}, {}
    for name in names:
        path = BASE/'data/prepared'/f'{name}.npz'
        expected = next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        assert file_hash(path) == expected
        with np.load(path, allow_pickle=False) as data:
            truths.append(data['y'])
            validity.append(data['input_valid'])
    for member in sorted({m for model in models for m in model['members']}):
        path = BASE/'v2/models'/member/'validation-predictions.npz'
        hashes[str(path)] = file_hash(path)
        hashes[str(path.with_name('best.pt'))] = file_hash(path.with_name('best.pt'))
        with np.load(path, allow_pickle=False) as data:
            cache[member] = [data[name] for name in names]
    grid = []
    for alpha, hold, threshold in itertools.product(*protocol['grid'].values()):
        params = {'alpha': alpha, 'hold': hold, 'threshold': threshold}
        delay = step_delay(params, hz=workflow['sample_hz'])
        if delay <= protocol['guard']['clean_step_delay_max_seconds']:
            grid.append(('ema-persistence-v2', params, delay))
    evidence_grid = []
    for alpha, budget, threshold, margin in itertools.product(*protocol['evidence_grid'].values()):
        params = {'alpha': alpha, 'budget': budget, 'threshold': threshold, 'margin': margin}
        delay = step_delay(params, 'confidence-evidence-v1', workflow['sample_hz'])
        if delay <= protocol['guard']['clean_step_delay_max_seconds']:
            evidence_grid.append(('confidence-evidence-v1', params, delay))
    candidates = []
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    for model in models:
        probabilities = [np.mean([cache[m][i] for m in model['members']], axis=0) for i in range(len(names))]
        model_grid = grid + (evidence_grid if model['name'] in protocol['evidence_models'] else [])
        for kind, params, delay in model_grid:
            predicted = [apply_filter(kind, params, p, v) for p, v in zip(probabilities, validity)]
            metrics = score(truths, predicted, *args)
            guard = protocol['guard']
            eligible = (metrics['accuracy'] >= guard['stable_accuracy_min']
                        and metrics['macro_f1_all_classes'] >= guard['stable_macro_f1_min']
                        and metrics['exact_stage_sequence_matches'] >= guard['exact_sequences_min'])
            candidates.append({'model': {'name': model['name'], 'members': model['members']},
                'filter_kind': kind, 'parameters': params, 'validation': metrics,
                'clean_step_delay_seconds': delay, 'eligible': eligible})
        top = max(candidates[-len(model_grid):], key=lambda c: (c['validation']['exact_stage_sequence_matches'],
                                                         c['validation']['segment_f1_at_0.5']))
        print(f'{model["name"]}: best exact={top["validation"]["exact_stage_sequence_matches"]}, '
              f'eligible={sum(c["eligible"] for c in candidates[-len(model_grid):])}', flush=True)
    eligible = [c for c in candidates if c['eligible']]
    selected = max(eligible, key=lambda c: (c['validation']['exact_stage_sequence_matches'],
        c['validation']['segment_f1_at_0.5'], c['validation']['macro_f1_all_classes'], c['validation']['accuracy'],
        -c['clean_step_delay_seconds'], -len(c['model']['members'])))
    save_json(ROOT/'filter-search.json', candidates)
    save_json(ROOT/'selection.json', {'protocol_sha256': file_hash(ROOT/'protocol.json'),
        'input_sha256': hashes, 'selected': selected, 'validation_baseline': previous,
        'candidates_evaluated': len(candidates), 'frozen_before_test': True})
    print('SELECTED', selected['model']['name'], selected['parameters'], flush=True)
    print({k: selected['validation'][k] for k in ['exact_stage_sequence_matches','accuracy','macro_f1_all_classes','segment_f1_at_0.5']}, flush=True)


if __name__ == '__main__':
    main()
