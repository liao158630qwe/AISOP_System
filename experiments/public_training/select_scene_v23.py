"""Freeze a V2.3 ensemble and causal filter using validation subjects only."""
import itertools
import json
from pathlib import Path

import numpy as np

from common import file_hash, save_json
from scene_v2 import filter_stages
from evidence_filter import EvidenceFilter
from scene_filter_v3 import EvidenceDecayFilter, HMMFilter
from verify_scene_flow import read, score

BASE = Path('work/simple-sop-2026-10-10')
ROOT = BASE/'v2.3'
SOURCE = Path('experiments/public_training')


def source_hashes():
    return {name: file_hash(SOURCE/name) for name in
            ['common.py', 'train_temporal.py', 'scene_v2.py', 'scene_runtime.py', 'scene_filter_v3.py',
             'scene_runtime_v23.py', 'optimize_scene_v23.py', 'select_scene_v23.py', 'verify_scene_flow.py',
             'evidence_filter.py', 'scene_diagnostics.py', 'evaluate_scene_v23.py', 'simple_scene_demo.html']}


def apply_filter(kind, parameters, probabilities, valid):
    if kind == 'confidence-evidence-v1':
        return EvidenceFilter(**parameters).push(probabilities, valid)
    if kind == 'ema-persistence-v2':
        return filter_stages(probabilities, valid, **parameters)
    if kind == 'hmm-forward-v3':
        return HMMFilter(**parameters).push(probabilities, valid)
    if kind == 'evidence-decay-v3':
        return EvidenceDecayFilter(**parameters).push(probabilities, valid)
    raise ValueError(f'Unknown filter kind: {kind}')


def step_delay(parameters, kind='ema-persistence-v2', hz=10):
    p = np.eye(5)[[1]*100+[2]*100]
    stages = apply_filter(kind, parameters, p, np.ones(len(p)))
    found = np.flatnonzero(stages[100:] == 2)
    return int(found[0])/hz if len(found) else float('inf')


def spike_suppressed(kind, parameters):
    """A two-frame one-hot spike must not be committed as a stage."""
    from scene_runtime import check_sop
    p = np.eye(5)[[1]*100+[3]*2+[1]*100]
    return check_sop(apply_filter(kind, parameters, p, np.ones(len(p))), [1, 2, 3, 4])['sequence'] == [1]


def filter_grid(protocol):
    configs = []
    for alpha, budget, threshold, margin in itertools.product(*protocol['grid']['evidence'].values()):
        configs.append(('confidence-evidence-v1',
                        {'alpha': alpha, 'budget': budget, 'threshold': threshold, 'margin': margin}))
    for alpha, hold, threshold in itertools.product(*protocol['grid']['ema'].values()):
        configs.append(('ema-persistence-v2', {'alpha': alpha, 'hold': hold, 'threshold': threshold}))
    for alpha, budget, margin, decay in itertools.product(*protocol['grid']['decay'].values()):
        configs.append(('evidence-decay-v3',
                        {'alpha': alpha, 'budget': budget, 'threshold': 0., 'margin': margin, 'decay': decay}))
    for p_self, epsilon, hold in itertools.product(*protocol['grid']['hmm'].values()):
        configs.append(('hmm-forward-v3',
                        {'p_self': p_self, 'epsilon': epsilon, 'hold': hold, 'threshold': 0.}))
    eligible = []
    for kind, parameters in configs:
        delay = step_delay(parameters, kind, protocol['sample_hz'])
        if delay <= protocol['guard']['clean_step_delay_max_seconds'] and spike_suppressed(kind, parameters):
            eligible.append((kind, parameters, delay))
    return configs, eligible


def tuple_of(metrics):
    return (metrics['exact_stage_sequence_matches'], metrics['segment_f1_at_0.5'],
            metrics['macro_f1_all_classes'], metrics['accuracy'])


def main():
    dataset = read(BASE/'data/dataset.json')
    baseline = read(BASE/'v2.2/selection.json')
    previous = baseline['selected']['validation']
    workflow = read(BASE/'v2.1/export/workflow.json')
    pool = read(ROOT/'candidates.json')['candidates']
    names = dataset['splits']['val']
    truths, validity = [], []
    for name in names:
        path = BASE/'data/prepared'/f'{name}.npz'
        expected = next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        assert file_hash(path) == expected
        with np.load(path, allow_pickle=False) as data:
            truths.append(data['y'])
            validity.append(data['input_valid'])
    cache, hashes = {}, {}
    for candidate in pool:
        path = ROOT/'models'/candidate['name']/'validation-predictions.npz'
        hashes[str(path)] = file_hash(path)
        hashes[str(path.with_name('best.pt'))] = file_hash(path.with_name('best.pt'))
        with np.load(path, allow_pickle=False) as data:
            cache[candidate['name']] = [data[name] for name in names]
    runs = [candidate['name'] for candidate in pool]
    protocol = {
        'scope': 'Validation subjects only; no test arrays, predictions or metrics loaded',
        'source_sha256': source_hashes(), 'dataset_sha256': file_hash(BASE/'data/dataset.json'),
        'candidates_sha256': file_hash(ROOT/'candidates.json'),
        'baseline_selection_sha256': file_hash(BASE/'v2.2/selection.json'),
        'workflow_sha256': file_hash(BASE/'v2.1/export/workflow.json'),
        'runs': runs, 'sample_hz': workflow['sample_hz'],
        'ensemble_policy': 'all subsets of size 1..4 plus the full pool; per-subset probe metrics under four fixed '
                           'filters; union of each probe top 15 plus the v2.2 member set, top-k by individual '
                           'validation macro F1 (k=1..5) and the full pool advance to the full grid',
        'probe_filters': [
            {'kind': 'confidence-evidence-v1', 'parameters': {'alpha': .3, 'budget': 2., 'threshold': 0., 'margin': .1}},
            {'kind': 'evidence-decay-v3', 'parameters': {'alpha': .3, 'budget': 1., 'threshold': 0., 'margin': .1, 'decay': .9}},
            {'kind': 'ema-persistence-v2', 'parameters': {'alpha': .05, 'hold': 1, 'threshold': .4}},
            {'kind': 'evidence-decay-v3', 'parameters': {'alpha': 1., 'budget': 2., 'threshold': 0., 'margin': .1, 'decay': .95}}],
        'grid': {'evidence': {'alpha': [1., .3, .1], 'budget': [1., 2., 3., 4.], 'threshold': [0., .4, .55], 'margin': [0., .1]},
                 'ema': {'alpha': [1., .3, .1, .05], 'hold': [1, 5, 10, 20], 'threshold': [0., .4, .55]},
                 'decay': {'alpha': [1., .3, .1], 'budget': [.5, 1., 1.5, 2., 3.], 'margin': [0., .1], 'decay': [.8, .9, .95]},
                 'hmm': {'p_self': [.6, .7, .8, .9], 'epsilon': [.2, .4, .6], 'hold': [1, 3, 5]}},
        'guard': {'stable_accuracy_min': previous['accuracy'], 'stable_macro_f1_min': previous['macro_f1_all_classes'],
                  'exact_sequences_min': previous['exact_stage_sequence_matches'],
                  'clean_step_delay_max_seconds': 1.3, 'two_frame_spike_suppressed': True},
        'ranking': 'exact sequences, segment F1@0.5, macro F1, accuracy, shorter synthetic delay, fewer members',
        'limitations': 'Previously reported test subjects, not a new external evaluation; finite validation set; '
                       'subset enumeration on 14 validation recordings can overfit validation noise',
    }
    save_json(ROOT/'protocol.json', protocol)
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    subsets = []
    for size in range(1, 5):
        subsets.extend(itertools.combinations(runs, size))
    subsets.append(tuple(runs))
    probes = [(item['kind'], item['parameters']) for item in protocol['probe_filters']]
    probe_scores = []
    for members in subsets:
        probability = [np.mean([cache[run][i] for run in members], axis=0) for i in range(len(names))]
        best = None
        for kind, parameters in probes:
            predicted = [apply_filter(kind, parameters, p, v) for p, v in zip(probability, validity)]
            key = tuple_of(score(truths, predicted, *args))
            if best is None or key > best[0]:
                best = (key, kind)
        probe_scores.append({'members': list(members), 'probe_tuple': list(best[0]), 'probe_filter': best[1]})
    finalists = set()
    for index in range(len(probes)):
        ranked = sorted(range(len(subsets)), key=lambda i: probe_scores[i]['probe_tuple'], reverse=True)[:15]
        finalists.update(subsets[i] for i in ranked)
    singles = sorted(runs, key=lambda run: -next(c['validation']['macro_f1_all_classes']
                                                 for c in pool if c['name'] == run))
    for k in range(1, 6):
        finalists.add(tuple(singles[:k]))
    finalists.add(tuple(runs))
    finalists.add(('tcn-d7-w96-s2027', 'tcn-d7-w96-s2026', 'gru-d0-w96-s2026'))
    finalists = sorted(finalists, key=lambda members: (len(members), members))
    configs, eligible = filter_grid(protocol)
    print(f'subsets={len(subsets)} finalists={len(finalists)} filters={len(configs)} eligible={len(eligible)}', flush=True)
    candidates = []
    for members in finalists:
        probability = [np.mean([cache[run][i] for run in members], axis=0) for i in range(len(names))]
        model = {'name': '+'.join(members), 'members': list(members)}
        for kind, parameters, delay in eligible:
            predicted = [apply_filter(kind, parameters, p, v) for p, v in zip(probability, validity)]
            metrics = score(truths, predicted, *args)
            guard = protocol['guard']
            record = {'model': model, 'filter_kind': kind, 'parameters': parameters,
                      'clean_step_delay_seconds': delay,
                      'validation': {key: metrics[key] for key in
                                     ['exact_stage_sequence_matches', 'segment_f1_at_0.5',
                                      'macro_f1_all_classes', 'accuracy']}}
            record['eligible'] = (metrics['accuracy'] >= guard['stable_accuracy_min']
                                  and metrics['macro_f1_all_classes'] >= guard['stable_macro_f1_min']
                                  and metrics['exact_stage_sequence_matches'] >= guard['exact_sequences_min'])
            candidates.append(record)
        print(f'{model["name"][:70]}: best {max((tuple(c["validation"].values()) for c in candidates[-len(eligible):]), default=None)}', flush=True)
    ranked = sorted(candidates, key=lambda c: (
        c['validation']['exact_stage_sequence_matches'], c['validation']['segment_f1_at_0.5'],
        c['validation']['macro_f1_all_classes'], c['validation']['accuracy'],
        -c['clean_step_delay_seconds'], -len(c['model']['members'])), reverse=True)
    eligible_candidates = [c for c in ranked if c['eligible']]
    baseline_tuple = tuple(previous[key] for key in
                           ['exact_stage_sequence_matches', 'segment_f1_at_0.5', 'macro_f1_all_classes', 'accuracy'])
    selected = eligible_candidates[0] if eligible_candidates else None
    selected_tuple = tuple(selected['validation'][key] for key in
                           ['exact_stage_sequence_matches', 'segment_f1_at_0.5', 'macro_f1_all_classes', 'accuracy']) \
        if selected else None
    improved = bool(selected and selected_tuple > baseline_tuple)
    save_json(ROOT/'ensemble-search.json', {'subsets_evaluated': len(subsets),
        'probe_scores': [s for s in probe_scores if s['probe_tuple'][0] >= 11],
        'finalists': [list(members) for members in finalists]})
    save_json(ROOT/'filter-search.json', {'evaluated': len(candidates),
        'eligible': len(eligible_candidates), 'records': ranked[:200],
        'eligible_records': eligible_candidates[:200]})
    save_json(ROOT/'selection.json', {'protocol_sha256': file_hash(ROOT/'protocol.json'),
        'input_sha256': hashes, 'selected': selected,
        'selected_tuple': list(selected_tuple) if selected_tuple else None,
        'validation_baseline': {'tuple': list(baseline_tuple), 'source': 'v2.2 selected validation'},
        'beats_v22_validation': improved, 'candidates_evaluated': len(candidates),
        'eligible_candidates': len(eligible_candidates), 'frozen_before_test': True})
    print('SELECTED', json.dumps(selected, ensure_ascii=False) if selected else 'none', flush=True)
    print('beats_v22_validation', improved, flush=True)


if __name__ == '__main__':
    main()
