"""Industrial workflow contracts; fixture metrics are not field accuracy claims."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import uuid

import numpy as np
import pytest
from sqlalchemy import select, update

from algorithms.industrial_training import candidate_gate, industrial_audit
from algorithms.training import confusion_metrics, write_json
from backend.db import model_registry, training_jobs, training_runs
from backend.training_service import (Policy, claim_training, execute_training, execute_one,
                                      finish, recover_expired)


def dataset(root, task='feature-classify'):
    from PIL import Image
    root.mkdir(parents=True)
    rng, samples = np.random.default_rng(42), []
    for split in ('train', 'val', 'test'):
        for cls in range(2):
            for index in range(6):
                identity = f'{split}-{cls}-{index}'
                if task == 'feature-classify':
                    filename = identity + '.npy'
                    np.save(root / filename, np.eye(2)[cls] * 3 + rng.normal(0, .1, 2))
                    label = cls
                else:
                    filename, label = identity + '.png', identity + '.txt'
                    Image.fromarray(rng.integers(0, 256, (32, 32, 3), dtype=np.uint8)).save(root / filename)
                    (root / label).write_text(f'{cls} .5 .5 .2 .2')
                samples.append(dict(id=identity, split=split, source=f'{split}-source-{index % 2}',
                    group=f'{split}-group-{index % 2}', batch=f'{split}-batch-{index % 2}', cycle=identity,
                    station='station1', product='widget', camera='cam1', lighting='normal', occlusion='clear',
                    file=filename, label=label))
        if task == 'detect':
            for i in range(5):
                identity = f'{split}-negative-{i}'
                Image.fromarray(rng.integers(0, 256, (32, 32, 3), dtype=np.uint8)).save(root / (identity + '.png'))
                (root / (identity + '.txt')).write_text('')
                samples.append(dict(samples[-1], id=identity, cycle=identity, file=identity+'.png', label=identity+'.txt'))
    manifest = root / 'manifest.json'
    write_json(manifest, dict(schema_version=1, names=['component', 'defect'], samples=samples))
    return manifest


@pytest.fixture
def training(authenticated):
    settings, app, client = authenticated
    manifest = dataset(settings.data_dir / 'industrial-fixture')
    body = dict(name='工位1测试夹具', station='station1', recipe=dict(backend='sklearn', task='feature-classify',
        dataset=str(manifest), feature_schema='fixture-v1', seed=42), policy=dict(min_batches_per_split=2,
        min_class_samples=5, min_negative_samples=5, min_precision=.95, min_recall=.95))
    return settings, app, client, body


def submit(context, key=None, body=None):
    _, _, client, original = context
    result = client.post('/api/v1/admin/training-runs', json=body or original,
                         headers={'Idempotency-Key': key or str(uuid.uuid4())})
    assert result.status_code == 202, result.text
    return result.json()


def detail(context, identity):
    return context[2].get('/api/v1/admin/training-runs/' + identity).json()


def test_authentication_csrf_and_private_files(context):
    _, _, client = context
    assert client.get('/api/v1/admin/training-runs').status_code == 401
    assert client.get('/training/snapshot/manifest.json').status_code == 404
    client.post('/api/v1/login', json={'username': 'admin', 'password': 'test-password-12345'})
    body = dict(name='a', station='station1', recipe={})
    assert client.post('/api/v1/admin/training-runs', json=body).status_code == 403


def test_real_feature_pipeline_snapshot_and_candidate(training):
    settings, app, client, _ = training
    run = submit(training)
    job = claim_training(app.state.engine, 60)
    assert claim_training(app.state.engine, 60) is None
    execute_training(app.state.engine, settings, job)
    done = detail(training, run['id'])
    assert done['state'] == 'CANDIDATE', done
    assert done['evaluation']['gate']['passed'] and done['card']['test_evaluated']
    assert not done['evaluation']['gate']['production_qualified']
    assert done['model_id'] is None
    assert done['audit']['batches'] == dict(train=2, val=2, test=2)
    assert Path(done['card']['model']).is_file()
    assert client.get('/api/v1/admin/models').json()['items'] == []
    assert client.get('/api/v1/scenes').json()['items'] == []
    report = client.get(f'/api/v1/admin/training-runs/{run["id"]}/report')
    assert 'attachment' in report.headers['content-disposition']
    assert report.json()['evaluation'] == done['evaluation']
    assert client.get(f'/api/v1/admin/training-runs/{run["id"]}/log').status_code == 200


def test_actual_subprocess_supervisor(training):
    settings, app, _, _ = training
    run = submit(training)
    assert execute_one(app.state.engine, settings)
    assert detail(training, run['id'])['state'] == 'CANDIDATE'
    assert not execute_one(app.state.engine, settings)


def test_idempotency_is_immutable_even_after_source_changes(training):
    _, _, client, body = training
    key = str(uuid.uuid4())
    first = submit(training, key)
    Path(body['recipe']['dataset']).unlink()
    assert submit(training, key)['id'] == first['id']
    other = dict(body, name='changed')
    assert client.post('/api/v1/admin/training-runs', json=other, headers={'Idempotency-Key': key}).status_code == 409


def test_parallel_submission_and_claim_have_one_owner(training):
    settings, app, _, body = training
    from backend.training_service import CreateTraining, create_run
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create_run(app.state.engine, settings, 'admin', key,
                            CreateTraining.model_validate(body)), range(4)))
    assert len({r['id'] for r in results}) == 1
    submit(training)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: claim_training(app.state.engine, 60), range(4)))
    assert len([c for c in claims if c]) == 1


def test_changed_manifest_and_data_leakage_fail_closed(training):
    settings, app, _, body = training
    run = submit(training)
    path = Path(body['recipe']['dataset'])
    value = json.loads(path.read_text())
    value['samples'][-1]['batch'] = value['samples'][0]['batch']
    write_json(path, value)
    execute_training(app.state.engine, settings, claim_training(app.state.engine, 60))
    assert detail(training, run['id'])['state'] == 'FAILED'
    run2 = submit(training)
    execute_training(app.state.engine, settings, claim_training(app.state.engine, 60))
    assert '跨集合' in detail(training, run2['id'])['message']


def test_queued_cancel_and_expired_worker_cannot_publish(training):
    _, app, client, _ = training
    first = submit(training)
    assert client.post(f'/api/v1/admin/training-runs/{first["id"]}/cancel').json()['state'] == 'CANCELLED'
    assert claim_training(app.state.engine, 60) is None
    second = submit(training)
    job = claim_training(app.state.engine, 60)
    with app.state.engine.begin() as c:
        c.execute(update(training_jobs).where(training_jobs.c.id == job['id']).values(lease_until=0))
    assert not finish(app.state.engine, job, 'CANDIDATE', 'late')
    recover_expired(app.state.engine)
    assert detail(training, second['id'])['state'] == 'FAILED'
    assert not finish(app.state.engine, job, 'CANDIDATE', 'late')


def test_running_cancel_blocks_result_and_releases_slot(training):
    _, app, client, _ = training
    run = submit(training)
    job = claim_training(app.state.engine, 60)
    assert client.post(f'/api/v1/admin/training-runs/{run["id"]}/cancel').json()['state'] == 'CANCELLING'
    assert not finish(app.state.engine, job, 'CANDIDATE', 'late')
    assert finish(app.state.engine, job, 'CANCELLED', 'cancelled')
    assert detail(training, run['id'])['state'] == 'CANCELLED'


def test_history_is_bounded_filtered_and_does_not_load_reports(training):
    from sqlalchemy import event
    from backend.training_service import list_runs
    _, app, client, _ = training
    template = submit(training)
    with app.state.engine.begin() as c:
        original = dict(c.execute(select(training_runs)).mappings().one())
        c.execute(training_jobs.delete())
        c.execute(training_runs.delete())
        rows = [dict(original, id=str(uuid.uuid4()), request_key=str(uuid.uuid4()),
            name=f'历史训练 {index}', created_at=1000 + index // 3,
            station='station1' if index % 2 == 0 else 'station2',
            state='FAILED' if index < 30 else 'CANDIDATE',
            audit={'large': 'x' * 50000}, evaluation={'large': 'x' * 50000}) for index in range(125)]
        c.execute(training_runs.insert(), rows)
        c.execute(training_runs.insert().values(**dict(original, id=str(uuid.uuid4()), owner='another', request_key='other')))
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        result = list_runs(app.state.engine, 'admin')
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', record)
    assert len(statements) == 3
    assert not any(any(field in sql for field in ('training_runs.audit', 'training_runs.evaluation', 'training_runs.card')) for sql in statements)
    assert len(result['items']) == 25 and result['total'] == 125 and result['has_more']
    assert result['counts']['candidate'] == 95 and result['counts']['attention'] == 30
    assert not any(field in result['items'][0] for field in ('recipe', 'audit', 'evaluation', 'card', 'policy'))
    pages = [client.get('/api/v1/admin/training-runs', params={'offset': offset, 'limit': 25}).json() for offset in range(0, 125, 25)]
    identities = [row['id'] for page in pages for row in page['items']]
    assert len(set(identities)) == 125 and not pages[-1]['has_more']
    assert identities == [row['id'] for row in sorted(rows, key=lambda row: (row['created_at'], row['id']), reverse=True)]
    filtered = client.get('/api/v1/admin/training-runs?station=station1&status=attention').json()
    assert filtered['total'] == 15 and filtered['counts']['candidate'] == 95
    for query in ('%', '_', 'does-not-exist'):
        assert client.get('/api/v1/admin/training-runs', params={'search': query}).json()['total'] == 0
    for query in ('limit=0', 'limit=101', 'offset=-1', 'station=station7', 'status=invalid'):
        assert client.get('/api/v1/admin/training-runs?' + query).status_code == 422
    assert detail(training, rows[0]['id'])['audit']['large'] == 'x' * 50000


def test_training_reads_do_not_recover_or_write_jobs(training, monkeypatch):
    import backend.training_service as service
    _, app, client, _ = training
    run = submit(training)
    job = claim_training(app.state.engine, 60)
    with app.state.engine.begin() as c:
        c.execute(update(training_jobs).where(training_jobs.c.id == job['id']).values(lease_until=0))
    def forbidden(*args):
        raise AssertionError('Recovery belongs to the worker, not a browser read')
    monkeypatch.setattr(service, 'recover_expired', forbidden)
    assert client.get('/api/v1/admin/training-runs').status_code == 200
    assert detail(training, run['id'])['state'] == 'RUNNING'


@pytest.mark.parametrize('metric', [None, float('nan'), float('inf'), True, .4])
def test_gate_requires_complete_finite_per_class_evidence(metric):
    policy = Policy().model_dump()
    value = dict(class_names=['a'], metrics={'per_class': [dict(id=0, name='a', precision=metric, recall=.99, support=20)]})
    assert not candidate_gate(value, policy)['passed']
    assert not candidate_gate(dict(class_names=['a'], metrics={'accuracy': 1}), policy)['passed']


def test_fixed_confusion_metrics_include_empty_background_false_positives():
    matrix = [[8, 1, 5], [2, 9, 3], [0, 0, 0]]
    rows = confusion_metrics(matrix, ['a', 'b'])
    assert rows[0]['precision'] == pytest.approx(8/14)
    assert rows[0]['recall'] == .8
    assert rows[1]['support'] == 10


def test_visual_duplicate_pixels_path_escape_and_synthetic_validation(training):
    from PIL import Image
    settings, _, _, body = training
    manifest = dataset(settings.data_dir / 'visual', 'detect')
    recipe = dict(task='detect', dataset=str(manifest))
    policy = body['policy']
    assert industrial_audit(recipe, 'station1', policy)['negatives']['test'] == 5
    data = json.loads(manifest.read_text())
    train = data['samples'][0]
    test = next(v for v in data['samples'] if v['split'] == 'test')
    with Image.open(manifest.parent / train['file']) as image:
        image.save(manifest.parent / 'same.bmp')
    test['file'] = 'same.bmp'
    write_json(manifest, data)
    with pytest.raises(ValueError, match='解码后的相同'):
        industrial_audit(recipe, 'station1', policy)
    test['file'] = '../outside.png'
    write_json(manifest, data)
    with pytest.raises(ValueError, match='目录内'):
        industrial_audit(recipe, 'station1', policy)


def test_visual_registration_is_atomic_disabled_and_requires_scene_publication(training, monkeypatch):
    import backend.training_service as service
    settings, app, client, body = training
    manifest = dataset(settings.data_dir / 'visual-register', 'detect')
    weights = settings.data_dir / 'initial.pt'
    weights.write_bytes(b'trusted-test-stub')
    visual = dict(body, recipe=dict(backend='ultralytics', task='detect', dataset=str(manifest), pretrained=str(weights)))
    run = submit(training, body=visual)
    def fake_train(recipe_file, output, progress):
        from algorithms.training import audit_dataset
        from algorithms.video import file_hash
        output.mkdir()
        model = output / 'best.pt'
        model.write_bytes(b'candidate-test-stub')
        recipe = json.loads(recipe_file.read_text())
        return dict(model=str(model), model_sha256=file_hash(model), task='detect',
                    dataset_sha256=audit_dataset(recipe['dataset'], 'detect')['sha256'])
    monkeypatch.setattr(service, 'train_model', fake_train)
    monkeypatch.setattr(service, 'evaluate_model', lambda *a: dict(split='test', class_names=['component', 'defect'],
        per_class=[dict(id=i, name=n, precision=1, recall=1, support=6) for i,n in enumerate(['component', 'defect'])]))
    # The real evaluator owns this directory.
    real_eval = service.evaluate_model
    def evaluate(fit, out):
        out.mkdir()
        return real_eval(fit, out)
    monkeypatch.setattr(service, 'evaluate_model', evaluate)
    monkeypatch.setattr(service, 'inspect_model', lambda path: ('detect', {'0': 'component', '1': 'defect'}))
    execute_training(app.state.engine, settings, claim_training(app.state.engine, 60))
    done = detail(training, run['id'])
    assert done['state'] == 'CANDIDATE', done
    models = client.get('/api/v1/admin/models').json()['items']
    assert len(models) == 1 and not models[0]['enabled'] and models[0]['id'] == done['model_id']
    from test_scene_management import definition
    assert client.post('/api/v1/admin/scenes', json=definition(done['model_id'])).status_code == 422
    assert client.patch('/api/v1/admin/models/' + done['model_id'], json={'enabled': True}).status_code == 200
    assert client.post('/api/v1/admin/scenes', json=definition(done['model_id'])).status_code == 201
    assert len(client.get('/api/v1/scenes').json()['items']) == 1


def test_invalid_configuration_and_owner_isolation(training):
    _, app, client, body = training
    for bad in [dict(body, station='other'), dict(body, policy=dict(body['policy'], min_class_samples=0)),
                dict(body, recipe=dict(body['recipe'], train={'split': 'test'}))]:
        assert client.post('/api/v1/admin/training-runs', json=bad,
                           headers={'Idempotency-Key': str(uuid.uuid4())}).status_code == 422
    run = submit(training)
    with app.state.engine.begin() as c:
        from backend.db import sessions
        c.execute(update(sessions).values(owner='other'))
    assert client.get('/api/v1/admin/training-runs/'+run['id']).status_code == 404
    for endpoint in ('report', 'log'):
        assert client.get(f'/api/v1/admin/training-runs/{run["id"]}/{endpoint}').status_code == 404


def test_log_output_is_bounded_and_authenticated(training):
    settings, _, client, _ = training
    run = submit(training)
    path = settings.data_dir / 'training-logs' / (run['id'] + '.log')
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'x' * 70000 + b'end')
    response = client.get(f'/api/v1/admin/training-runs/{run["id"]}/log')
    assert response.json()['truncated']
    assert len(response.json()['text']) == 65536 and response.json()['text'].endswith('end')


def test_supervisor_stops_real_child_on_cancel(training):
    import time
    settings, app, client, _ = training
    run = submit(training)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_one, app.state.engine, settings)
        deadline = time.monotonic() + 10
        while detail(training, run['id'])['state'] == 'QUEUED' and time.monotonic() < deadline:
            time.sleep(.05)
        client.post(f'/api/v1/admin/training-runs/{run["id"]}/cancel')
        assert future.result(timeout=20)
    assert detail(training, run['id'])['state'] == 'CANCELLED'
    with app.state.engine.connect() as c:
        assert c.execute(select(training_jobs.c.id).where(training_jobs.c.active_slot.is_not(None))).first() is None
