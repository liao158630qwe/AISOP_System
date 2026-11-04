"""Generic evidence/cache/training contracts; synthetic data is not field accuracy."""
import copy
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pytest

from algorithms.asset_snapshot import AssetSnapshotCache
from algorithms.evidence import EvidenceWindow, event_precedence
from algorithms.screws import ScrewObservationHistory
from algorithms.sticker_actions import (StickerActionSequence, action_compliance,
                                        action_features, foreground_mask)
from algorithms.training import (audit_dataset, evaluate_model, load_recipe,
                                  prepare_yolo, select_candidate, train_model, write_json)
from algorithms.video import AnalysisError


def test_cache_tracks_nested_assets_deletions_and_return_isolation(tmp_path, monkeypatch):
    import algorithms.asset_snapshot as module
    root = tmp_path
    config = root / 'scene.json'
    write_json(config, dict(assets=['base.pt', 'view.jpg', 'special.pt']))
    for name in ('base.pt', 'view.jpg', 'special.pt'):
        (root / name).write_bytes(name.encode())
    original, calls = module.file_hash, []
    monkeypatch.setattr(module, 'file_hash', lambda p: calls.append(p) or original(p))
    cache = AssetSnapshotCache()
    def get():
        return cache.get(config, root, lambda c: c['assets'])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: get(), range(8)))
    assert len(calls) == 3
    results[0]['config']['assets'].clear()
    results[0]['assets'].clear()
    assert len(get()['assets']) == 3
    (root / 'special.pt').write_bytes(b'new-model')
    assert get()['assets']['special.pt'] != results[1]['assets']['special.pt']
    assert len(calls) == 6
    write_json(config, dict(assets=['base.pt']))
    assert list(get()['assets']) == ['base.pt']
    (root / 'base.pt').unlink()
    with pytest.raises(AnalysisError):
        get()


def test_snapshot_never_caches_files_that_change_during_hash(tmp_path, monkeypatch):
    import algorithms.asset_snapshot as module
    config, asset = tmp_path / 'scene.json', tmp_path / 'model.pt'
    write_json(config, dict(model='model.pt'))
    asset.write_bytes(b'old')
    original = module.file_hash
    def change_once(path):
        digest = original(path)
        if path.read_bytes() == b'old':
            path.write_bytes(b'new model')
        return digest
    monkeypatch.setattr(module, 'file_hash', change_once)
    result = AssetSnapshotCache().get(config, tmp_path, lambda c: [c['model']])
    assert result['assets']['model.pt'] == original(asset)


def test_evidence_expiration_and_short_gap_are_independent_of_class_names():
    votes = EvidenceWindow(frames=3, max_gap=1, max_window=1.5)
    assert votes.observe(0, 'connector_locked', .95) is None
    assert votes.observe(.5) is None
    assert votes.observe(.75, 'connector_locked', .9) is None
    result = votes.observe(1, 'connector_locked', .92)
    assert (result.value, result.first, result.last, result.score) == ('connector_locked', 0, 1, .9)
    votes.observe(2, 'red', .9)
    assert votes.observe(10, 'red', .9) is None
    assert votes.observe(10.8, 'red', .9) is None
    assert votes.observe(11.6, 'red', .9) is None  # total window exceeded
    with pytest.raises(ValueError):
        votes.observe(11.6, 'red')
    with pytest.raises(ValueError):
        votes.observe(float('nan'), 'red')


def test_history_expires_pending_but_retains_confirmed_result():
    target = dict(id='arbitrary-component', point=[10, 10])
    history = ScrewObservationHistory([target], max_gap=1, max_window=2)
    def update(t, state, registered=True):
        return history.update(t, dict(registered=registered, targets=[dict(target, state=state, score=.95)]))[0]
    update(0, 'PRESENT')
    update(.25, 'UNKNOWN')
    update(30, 'UNKNOWN', False)
    assert update(60, 'PRESENT')['state'] == 'UNKNOWN'
    assert update(60.25, 'PRESENT')['state'] == 'PRESENT'
    assert update(100, 'UNKNOWN')['state'] == 'PRESENT'


def test_interval_order_distinguishes_confirm_delay_from_true_overlap():
    predecessor = dict(time=1, lastContactAt=1.5, releasedAt=2, confirmedAt=3)
    assert event_precedence(predecessor, dict(time=2.5, startAfter=2)) == 'before'
    assert event_precedence(predecessor, dict(time=1.5, startAfter=1)) == 'violation'
    assert event_precedence(predecessor, dict(time=2, startAfter=1.5)) == 'uncertain'


TARGETS = [dict(id='T1', expected='triangle'), dict(id='T2', expected='triangle'),
           dict(id='R', expected='rectangle')]


def _action(sequence, t, states):
    return sequence.update(t, dict(registered=True,
        targets=[dict(target, state=state, score=.95) for target, state in zip(TARGETS, states)]))


@pytest.mark.parametrize('rectangle_start,expected', [(.75, 'FAIL'), (1.0, 'REVIEW'), (1.25, 'PASS')])
def test_action_order_uses_release_interval(rectangle_start, expected):
    sequence = StickerActionSequence(TARGETS, dict(max_gap=2))
    for t in [0, .25]:
        _action(sequence, t, ['CLEAR'] * 3)
    _action(sequence, .5, ['CONTACT', 'CONTACT', 'CLEAR'])
    for t in [.75, 1.0, 1.25, 1.5, 1.75]:
        triangular = 'CONTACT' if t < 1.0 else 'CLEAR'
        rectangle = 'CONTACT' if rectangle_start <= t < 1.5 else 'CLEAR'
        result = _action(sequence, t, [triangular, triangular, rectangle])
    assert len(result['events']) == 3
    assert result['status'] == expected
    assert result['trianglesCompleteAt'] == 1.0
    assert action_compliance(sequence.aggregate(), result)['status'] == expected


def test_cached_feature_path_is_numerically_identical():
    random = np.random.default_rng(2)
    image = random.integers(0, 256, (180, 180, 3), dtype=np.uint8)
    reference = random.integers(0, 256, (180, 180, 3), dtype=np.uint8)
    baseline = action_features(image, reference)
    optimized = action_features(image, reference, mask=foreground_mask(image, reference),
        hog=cv2.HOGDescriptor((48, 48), (16, 16), (8, 8), (8, 8), 9))
    np.testing.assert_array_equal(baseline, optimized)


def _dataset(root, task='feature-classify'):
    records = []
    rng = np.random.default_rng(42)
    for split in ('train', 'val', 'test'):
        for cls in range(3):
            for index in range(3):
                identity = f'{split}-{cls}-{index}'
                path = root / (identity + ('.npy' if task == 'feature-classify' else '.png'))
                if task == 'feature-classify':
                    np.save(path, np.eye(3)[cls] * 3 + rng.normal(0, .1, 3))
                    label = cls
                else:
                    cv2.imwrite(str(path), rng.integers(0, 256, (32, 32, 3), dtype=np.uint8))
                    if task == 'classify':
                        label = cls
                    else:
                        label = identity + '.txt'
                        (root / label).write_text(f'{cls} .5 .5 .2 .2\n')
                records.append(dict(id=identity, split=split, source=f'video-{split}', group=f'day-{split}',
                                    file=path.name, label=label))
    manifest = root / 'manifest.json'
    write_json(manifest, dict(schema_version=1, names=['clip', 'terminal', 'unknown-part'], samples=records))
    return manifest


@pytest.mark.parametrize('key', ['source', 'group'])
def test_training_refuses_video_or_batch_leakage(tmp_path, key):
    manifest = _dataset(tmp_path)
    value = json.loads(manifest.read_text())
    value['samples'][-1][key] = value['samples'][0][key]
    write_json(manifest, value)
    with pytest.raises(ValueError, match='leakage'):
        audit_dataset(manifest, 'feature-classify')


def test_training_refuses_identical_files_and_synthetic_test(tmp_path):
    manifest = _dataset(tmp_path)
    value = json.loads(manifest.read_text())
    value['samples'][-1]['file'] = value['samples'][0]['file']
    write_json(manifest, value)
    with pytest.raises(ValueError, match='identical content'):
        audit_dataset(manifest, 'feature-classify')
    value['samples'][-1]['synthetic'] = True
    write_json(manifest, value)
    with pytest.raises(ValueError, match='train-only'):
        audit_dataset(manifest, 'feature-classify')


def test_independent_sources_may_have_identical_feature_vectors(tmp_path):
    manifest = _dataset(tmp_path)
    np.save(tmp_path / 'test-0-0.npy', np.load(tmp_path / 'train-0-0.npy'))
    assert audit_dataset(manifest, 'feature-classify')['counts']['test'] == 9


def test_yolo_training_materialization_excludes_test(tmp_path):
    import yaml
    manifest = _dataset(tmp_path, 'detect')
    audit = audit_dataset(manifest, 'detect')
    data = prepare_yolo(audit, tmp_path / 'prepared', 'detect', ('train', 'val'))
    assert 'test' not in yaml.safe_load(Path(data).read_text())
    assert not (tmp_path / 'prepared/images/test').exists()
    assert len(list((tmp_path / 'prepared/images/train').glob('*.png'))) == 9
    (tmp_path / 'train-0-0.txt').write_text('0 .5 .5 .5 .5')
    with pytest.raises(ValueError, match='changed after audit'):
        prepare_yolo(audit, tmp_path / 'changed', 'detect', ('train',))


@pytest.mark.parametrize('task,label,extra', [
    ('segment', '0 .1 .1 .9 .1 .9 .9 .1 .9', {}),
    ('obb', '0 .1 .1 .9 .1 .9 .9 .1 .9', {}),
    ('pose', '0 .5 .5 .4 .4 .4 .4 2 .6 .6 1', {'kpt_shape': [2, 3]}),
])
def test_other_visual_tasks_keep_task_specific_label_contracts(tmp_path, task, label, extra):
    manifest = _dataset(tmp_path, 'detect')
    value = json.loads(manifest.read_text())
    value.update(extra)
    for record in value['samples']:
        cls = (tmp_path / record['label']).read_text().split()[0]
        (tmp_path / record['label']).write_text(cls + ' ' + label.split(' ', 1)[1])
    write_json(manifest, value)
    audit = audit_dataset(manifest, task)
    assert audit['counts'] == {'train': 9, 'val': 9, 'test': 9}
    (tmp_path / value['samples'][0]['label']).write_text('0 nan 1 1 1')
    with pytest.raises(ValueError, match='nonfinite'):
        audit_dataset(manifest, task)


def test_classification_materialization_matches_library_class_order(tmp_path):
    from ultralytics.data.utils import check_cls_dataset
    manifest = _dataset(tmp_path, 'classify')
    audit = audit_dataset(manifest, 'classify')
    data = prepare_yolo(audit, tmp_path / 'classification', 'classify', ('train', 'val'))
    actual = check_cls_dataset(data, split='val')
    assert list(actual['names'].values()) == ['000000', '000001', '000002']
    assert actual['test'] is None


def test_derived_sample_cannot_use_held_out_parent(tmp_path):
    manifest = _dataset(tmp_path)
    value = json.loads(manifest.read_text())
    value['samples'][0].update(synthetic=True, parent_ids=[value['samples'][-1]['id']])
    write_json(manifest, value)
    with pytest.raises(ValueError, match='parent'):
        audit_dataset(manifest, 'feature-classify')


def test_feature_training_and_explicit_test_end_to_end(tmp_path):
    _dataset(tmp_path)
    recipe = tmp_path / 'recipe.json'
    write_json(recipe, dict(backend='sklearn', task='feature-classify', dataset='manifest.json',
                           feature_schema='synthetic-3d-v1'))
    run = tmp_path / 'candidate'
    result = train_model(recipe, run)
    assert result['status'] == 'candidate' and result['test_evaluated'] is False
    assert result['validation']['macro_f1'] == 1.0
    evaluation = evaluate_model(run, tmp_path / 'test')
    assert evaluation['split'] == 'test' and evaluation['latency']['batch'] == 1
    np.save(tmp_path / 'test-0-0.npy', np.zeros(3))
    with pytest.raises(ValueError, match='changed'):
        evaluate_model(run, tmp_path / 'changed-test')
    with pytest.raises(FileExistsError):
        train_model(recipe, run)


def test_task_model_mismatch_and_runner_owned_flags_fail_early(tmp_path):
    model = tmp_path / 'model.pt'
    model.write_bytes(b'stub')
    recipe = tmp_path / 'recipe.json'
    write_json(recipe, dict(backend='ultralytics', task='detect', dataset='unused.json',
                           pretrained='model.pt', train=dict(split='test')))
    with pytest.raises(ValueError, match='runner-owned'):
        load_recipe(recipe)
    write_json(recipe, dict(backend='sklearn', task='detect', dataset='unused.json'))
    with pytest.raises(ValueError, match='Unsupported backend/task'):
        load_recipe(recipe)


def test_selection_never_trades_accuracy_below_floor_for_speed():
    candidates = [dict(id='fast', protocol_id='fixtureA-gpuX-1camera', defect_recall=.8,
                       false_accept_rate=.01, end_to_end_p95_ms=5),
                  dict(id='accurate', protocol_id='fixtureA-gpuX-1camera', defect_recall=.995,
                       false_accept_rate=.001, end_to_end_p95_ms=80),
                  dict(id='too-slow', protocol_id='fixtureA-gpuX-1camera', defect_recall=.999,
                       false_accept_rate=0, end_to_end_p95_ms=1000)]
    result = select_candidate(candidates, min_recall=.99, max_false_accept=.005, max_p95_ms=100)
    assert result['selected']['id'] == 'accurate'
    assert select_candidate(candidates[:1], min_recall=.99, max_false_accept=.005,
                            max_p95_ms=100)['selected'] is None
    candidates[0]['protocol_id'] = 'different-camera'
    with pytest.raises(ValueError, match='protocol_id'):
        select_candidate(candidates, min_recall=.99, max_false_accept=.005, max_p95_ms=100)
