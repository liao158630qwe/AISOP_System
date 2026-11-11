"""Scene safety boundaries, shared weights, ROI cost and create retry regressions."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
import pytest
import torch
from fastapi import HTTPException

from algorithms.scene_runtime import SceneDetector, SceneEvidence
from algorithms.video import AnalysisError
from backend.scenes import Rule, SceneDefinition, rule_conflicts
from test_scene_management import registered, definition, publish, raw_checks, scene_cpu_lane


def rule(identity='people', **extra):
    return Rule(id=identity, name=identity, class_id=0, **extra).model_dump(mode='json')


@pytest.mark.parametrize('second', [
    rule('forbidden', kind='forbidden'), rule('quantity', min_count=3),
])
def test_conflicts_block_publication_but_allow_draft(registered, second):
    _, _, client, model = registered
    config = definition(model['id'], rules=[rule(max_count=2), second])
    assert client.post('/api/v1/admin/scenes', json=config).status_code == 422
    assert client.post('/api/v1/admin/scenes', json=dict(config, status='draft')).status_code == 201


def test_conflict_detection_respects_thresholds_regions_and_classification():
    required = Rule(**rule())
    assert rule_conflicts([required, Rule(**rule('no', kind='forbidden'))], 'detect')
    assert not rule_conflicts([required, Rule(**rule('no', kind='forbidden', threshold=.8))], 'detect')
    assert not rule_conflicts([required, Rule(**rule('no', kind='forbidden', roi=(.5,0,1,1)))], 'detect')
    other = required.model_copy(update={'id':'other', 'class_id':1})
    assert rule_conflicts([required, other], 'classify')
    assert not rule_conflicts([required, other], 'detect')


@pytest.mark.parametrize('field,value', [('min_count', True), ('max_count', 2.5), ('class_id', '0')])
def test_rule_integer_fields_are_strict(registered, field, value):
    _, _, client, model = registered
    invalid = dict(rule(), **{field:value})
    assert client.post('/api/v1/admin/scenes', json=definition(model['id'], rules=[invalid])).status_code == 422


def test_scene_create_retries_and_disabled_model_recovery(registered):
    _, _, client, model = registered
    config, headers = definition(model['id']), {'Idempotency-Key':str(uuid.uuid4())}
    first = client.post('/api/v1/admin/scenes', json=config, headers=headers)
    assert first.status_code == 201
    assert client.post('/api/v1/admin/scenes', json=config, headers=headers).json()['id'] == first.json()['id']
    changed = client.post('/api/v1/admin/scenes', json=dict(config, name='different'), headers=headers)
    assert changed.status_code == 409
    client.patch('/api/v1/admin/models/'+model['id'], json={'enabled':False})
    retry = client.post('/api/v1/admin/scenes', json=config, headers=headers)
    assert retry.status_code == 201 and retry.json()['id'] == first.json()['id'] and not retry.json()['ready']
    assert len(client.get('/api/v1/admin/scenes').json()['items']) == 1
    assert client.post('/api/v1/admin/scenes', json=config, headers={'Idempotency-Key':'bad'}).status_code == 400


def test_concurrent_scene_create_reconciles_one_row(registered):
    settings, app, _, model = registered
    body = SceneDefinition(**definition(model['id']))
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _:app.state.scenes.save(body, settings.username, request_key=key), range(8)))
    assert len({item['id'] for item in results}) == 1
    assert len(app.state.scenes.scenes(settings.username)) == 1


@pytest.mark.parametrize('malformation', ['missing','duplicate','unknown','kind','state','nan','none','shape'])
def test_invalid_checks_break_confirmation_and_never_certify_pass(malformation):
    evidence = SceneEvidence(definition(str(uuid.uuid4()), sample_fps=4, confirmation_frames=2))
    evidence.update(0, raw_checks())
    assert evidence.update(.25, raw_checks())['compliance']['status'] == 'PASS'
    invalid = raw_checks()
    if malformation == 'missing': invalid['checks'].pop()
    if malformation == 'duplicate': invalid['checks'].append(invalid['checks'][0])
    if malformation == 'unknown': invalid['checks'][0]['id'] = 'unexpected'
    if malformation == 'kind': invalid['checks'][0]['kind'] = 'forbidden'
    if malformation == 'state': invalid['checks'][0]['state'] = 'CLEAR'
    if malformation == 'nan': invalid['checks'][0]['score'] = float('nan')
    if malformation == 'none': invalid['checks'][0]['score'] = None
    if malformation == 'shape': invalid['checks'] = [None]
    assert evidence.update(.5, invalid)['compliance']['status'] == 'REVIEW'
    assert evidence.update(.75, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(1, raw_checks())['compliance']['status'] == 'PASS'


def test_forbidden_recovery_requires_consecutive_clear_frames():
    evidence = SceneEvidence(definition(str(uuid.uuid4()), sample_fps=4, confirmation_frames=2))
    assert evidence.update(0, raw_checks(bad='FAIL'))['compliance']['status'] == 'REVIEW'
    assert evidence.update(.25, raw_checks(bad='FAIL'))['compliance']['status'] == 'FAIL'
    assert evidence.update(.5, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(.75, raw_checks(bad='REVIEW'))['compliance']['status'] == 'REVIEW'
    assert evidence.update(1, raw_checks())['compliance']['status'] == 'REVIEW'
    assert evidence.update(1.25, raw_checks())['compliance']['status'] == 'PASS'
    assert evidence.update(5, raw_checks())['compliance']['status'] == 'REVIEW'


def test_classifier_checks_forbidden_probability_even_when_it_is_not_top_one(monkeypatch):
    import algorithms.gpu
    monkeypatch.setattr(algorithms.gpu, 'require_cuda', lambda device: 0)
    frozen = dict(model={'task': 'classify', 'classes': {'0': 'normal', '1': 'defect'}},
        config={'device': 'cuda', 'rules': [rule(threshold=.5),
            dict(rule('defect', kind='forbidden', threshold=.3), class_id=1)],
            'imgsz': 320, 'min_sharpness': 0})
    prediction = SimpleNamespace(probs=SimpleNamespace(top1=0, top1conf=.55,
                                                      data=torch.tensor([.55, .45])))
    model = SimpleNamespace(task='classify', predict=lambda crops, **kw: [prediction for _ in crops])
    result = SceneDetector(frozen, model).inspect(np.ones((100, 100, 3), np.uint8))
    assert [check['state'] for check in result['checks']] == ['PASS', 'FAIL']
    assert result['checks'][1]['score'] == pytest.approx(.45)


@pytest.mark.parametrize('values', [None, [float('nan'), .2], [.8], [.8, 1.2]])
def test_classifier_incomplete_probabilities_cannot_clear_forbidden_class(monkeypatch, values):
    import algorithms.gpu
    monkeypatch.setattr(algorithms.gpu, 'require_cuda', lambda device: 0)
    frozen = dict(model={'task': 'classify', 'classes': {'0': 'normal', '1': 'defect'}},
        config={'device': 'cuda', 'rules': [rule()], 'imgsz': 320, 'min_sharpness': 0})
    prediction = SimpleNamespace(probs=SimpleNamespace(top1=0, top1conf=.8,
        data=None if values is None else torch.tensor(values)))
    model = SimpleNamespace(task='classify', predict=lambda crops, **kw: [prediction for _ in crops])
    with pytest.raises(AnalysisError, match='概率'):
        SceneDetector(frozen, model).inspect(np.ones((100, 100, 3), np.uint8))


def prediction(count=1, score=.9):
    coordinates = [[float(i%20)*2, float(i//20)*2, float(i%20)*2+1, float(i//20)*2+1] for i in range(count)]
    return SimpleNamespace(boxes=SimpleNamespace(cls=torch.zeros(count), conf=torch.full((count,),score),
        xyxy=torch.tensor(coordinates).reshape(-1,4)))


def detector(monkeypatch, rules, output):
    import algorithms.gpu
    monkeypatch.setattr(algorithms.gpu, 'require_cuda', lambda device:0)
    calls = []
    def predict(crops, **kwargs):
        calls.append((len(crops), kwargs))
        return [output for _ in crops]
    model = SimpleNamespace(task='detect', predict=predict)
    frozen = dict(model={'task':'detect', 'classes':{'0':'person','1':'defect'}},
        config={'device':'cuda','rules':rules,'imgsz':320,'min_sharpness':0})
    return SceneDetector(frozen, model), calls


def test_pixel_equivalent_rois_share_inference_with_independent_thresholds(monkeypatch):
    runtime, calls = detector(monkeypatch, [rule(threshold=.6), rule('strict', threshold=.95, roi=(.0001,0,1,1))], prediction())
    result = runtime.inspect(np.ones((100,100,3),np.uint8))
    assert len(calls) == 1 and calls[0][0] == 1
    assert [check['state'] for check in result['checks']] == ['PASS','REVIEW']
    assert len(result['observations']) == 1


def test_display_cap_preserves_independent_regional_counts(monkeypatch):
    runtime, _ = detector(monkeypatch, [rule(roi=(0,0,.5,1)),rule('right',roi=(.5,0,1,1))], prediction(200))
    result = runtime.inspect(np.ones((100,100,3),np.uint8))
    assert len(result['observations']) == 300 and result['observationsTruncated']
    assert [check['count'] for check in result['checks']] == [200,200]
    assert [check['state'] for check in result['checks']] == ['PASS','PASS']


def test_saturated_detections_cannot_certify_pass_or_absence_but_can_fail(monkeypatch):
    rules = [rule(),dict(rule('absent',kind='forbidden'),class_id=1),rule('over',max_count=2)]
    runtime, _ = detector(monkeypatch, rules, prediction(300))
    result = runtime.inspect(np.ones((100,100,3),np.uint8))
    assert [check['state'] for check in result['checks']] == ['REVIEW','REVIEW','FAIL']


@pytest.mark.parametrize('fault', ['nan','score','class','fractional','infinite','empty','mismatch','missing'])
def test_invalid_detector_outputs_fail_closed(monkeypatch, fault):
    output = prediction()
    if fault == 'nan': output.boxes.conf[0] = float('nan')
    if fault == 'score': output.boxes.conf[0] = 1.2
    if fault == 'class': output.boxes.cls[0] = 99
    if fault == 'fractional': output.boxes.cls[0] = .5
    if fault == 'infinite': output.boxes.xyxy[0,0] = float('inf')
    if fault == 'empty': output.boxes.xyxy[0,2] = 0
    if fault == 'mismatch': output.boxes.conf = torch.tensor([])
    if fault == 'missing': output.boxes = None
    runtime, _ = detector(monkeypatch, [rule()], output)
    with pytest.raises(AnalysisError, match='模型'):
        runtime.inspect(np.ones((100,100,3),np.uint8))


def test_threshold_comparison_does_not_round_up_scores(monkeypatch):
    runtime, _ = detector(monkeypatch, [rule()], prediction(score=.599999))
    result = runtime.inspect(np.ones((100,100,3),np.uint8))
    assert result['checks'][0]['count'] == 0 and result['checks'][0]['state'] == 'REVIEW'


@pytest.mark.parametrize('modern', [False, True])
def test_fp32_option_supports_old_and_new_ultralytics(monkeypatch, modern):
    import ultralytics.cfg
    monkeypatch.setattr(ultralytics.cfg, 'DEFAULT_CFG_DICT', {'quantize':None} if modern else {'half':False})
    runtime, calls = detector(monkeypatch, [rule()], prediction())
    runtime.inspect(np.ones((100,100,3),np.uint8))
    kwargs = calls[0][1]
    assert kwargs.get('quantize' if modern else 'half') == (32 if modern else False)
    assert ('half' if modern else 'quantize') not in kwargs


def realtime_fixture(registered, monkeypatch):
    import backend.scene_realtime as module
    settings, app, _, _ = registered
    scene = publish(registered)
    loads, state = [], {'fail':False}
    class FakeDetector:
        def __init__(self, frozen, model=None): pass
        def inspect(self, frame):
            if state['fail']: raise AnalysisError('PREDICTION_INVALID', 'invalid output')
            return raw_checks()
    def load(frozen):
        loads.append(frozen['model']['id'])
        return object()
    monkeypatch.setattr(module, 'SceneDetector', FakeDetector)
    monkeypatch.setattr(module, 'load_model', load)
    service = module.RealtimeScenes(app.state.scenes)
    _, image = cv2.imencode('.jpg', np.ones((64,96,3),np.uint8)*100)
    def frame(timestamp, payload=None):
        return service.predict(image.tobytes() if payload is None else payload, scene_id=scene['id'],
            stream_key=('owner','one'), frame_time=timestamp, owner=settings.username)
    return service, frame, loads, state


@pytest.mark.parametrize('failure', ['inference','decode','snapshot','load'])
def test_failed_sample_invalidates_previous_pass_and_pending_votes(registered, monkeypatch, failure):
    import backend.scene_realtime as module
    service, frame, loads, state = realtime_fixture(registered, monkeypatch)
    frame(0)
    assert frame(.25)['compliance']['status'] == 'PASS'
    with monkeypatch.context() as scoped:
        payload = None
        if failure == 'inference': state['fail'] = True
        if failure == 'decode': payload = b'invalid jpeg'
        def fail(*args): raise RuntimeError('failed sample')
        if failure == 'snapshot': scoped.setattr(service.scenes, 'snapshot', fail)
        if failure == 'load':
            service.models.clear()
            scoped.setattr(module, 'load_model', fail)
        with pytest.raises((AnalysisError, HTTPException, RuntimeError)):
            frame(.5, payload)
    with pytest.raises(HTTPException) as missing:
        service.finish(('owner','one'))
    assert missing.value.status_code == 404
    state['fail'] = False
    assert frame(.75)['compliance']['status'] == 'REVIEW'
    assert frame(1)['compliance']['status'] == 'PASS'


def test_finished_or_replayed_stream_does_not_load_or_erase_result(registered, monkeypatch):
    service, frame, loads, _ = realtime_fixture(registered, monkeypatch)
    frame(0)
    frame(.25)
    with pytest.raises(HTTPException): frame(.25, b'invalid jpeg')
    assert service.finish(('owner','one'))['compliance']['status'] == 'PASS'
    service.models.clear()
    with pytest.raises(HTTPException) as finished: frame(.5)
    assert finished.value.status_code == 409 and len(loads) == 1
    assert service.finish(('owner','one'))['compliance']['status'] == 'PASS'


def test_shared_model_uses_each_scenes_config_and_reports_cache_hits(registered, monkeypatch):
    import backend.scene_realtime as module
    settings, app, client, model = registered
    runtime, calls = detector(monkeypatch, [rule()], prediction(2))
    monkeypatch.setattr(module, 'load_model', lambda frozen:runtime.model)
    first = publish(registered, rules=[rule()], confirmation_frames=1, imgsz=320)
    second = publish(registered, rules=[rule(max_count=1,threshold=.8,roi=(.5,0,1,1))], confirmation_frames=1)
    service = module.RealtimeScenes(app.state.scenes)
    _, jpeg = cv2.imencode('.jpg', np.ones((100,100,3),np.uint8))
    def frame(scene, stream, timestamp):
        return service.predict(jpeg.tobytes(), scene_id=scene['id'], stream_key=('owner',stream),
            frame_time=timestamp, owner=settings.username)
    assert frame(first,'a',0)['compliance']['status'] == 'PASS'
    assert frame(second,'b',0)['compliance']['status'] == 'FAIL'
    assert calls[0][1]['imgsz'] == 320 and calls[1][1]['imgsz'] == 640
    assert calls[0][1]['conf'] == .6 and calls[1][1]['conf'] == .8
    update = dict(first['rules'][0], threshold=.95)
    assert client.put('/api/v1/admin/scenes/'+first['id'],json=definition(model['id'],rules=[update],revision=1)).status_code == 200
    with pytest.raises(HTTPException) as stale: frame(first,'a',.25)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as no_old_pass: service.finish(('owner','a'))
    assert no_old_pass.value.status_code == 404
    assert frame(first,'fresh',0)['compliance']['status'] == 'REVIEW'
    assert service.cache_metrics() == dict(residentModels=1,capacity=1,modelLoads=1,cacheHits=2,evictions=0,
        concurrency=1,activeModels=0,loadingModels=0,busyRejections=0)
    assert 'sceneModelCache' in client.get('/api/v1/metrics').json()


def test_distinct_registered_weights_evict_lru_without_mixing_stream_state(registered, monkeypatch):
    import backend.scene_realtime as module
    settings, app, client, _ = registered
    first = publish(registered)
    other_model = client.post('/api/v1/admin/models/upload?name=second&filename=second.pt',content=b'second-weights').json()
    second = publish((settings, app, client, other_model))
    loads = []
    class FakeDetector:
        def __init__(self, frozen, model=None): self.frozen = frozen
        def inspect(self, frame): return raw_checks()
    def load(frozen):
        loads.append(frozen['model']['id'])
        return object()
    monkeypatch.setattr(module,'SceneDetector',FakeDetector)
    monkeypatch.setattr(module,'load_model',load)
    service = module.RealtimeScenes(app.state.scenes)
    _, jpeg = cv2.imencode('.jpg',np.ones((100,100,3),np.uint8))
    results = []
    for scene,key,timestamp in [(first,'a',0),(second,'b',0),(first,'a',.25)]:
        results.append(service.predict(jpeg.tobytes(),scene_id=scene['id'],stream_key=('owner',key),
            frame_time=timestamp,owner=settings.username))
    assert [r['compliance']['status'] for r in results] == ['REVIEW','REVIEW','PASS']
    assert loads == [first['model_id'],second['model_id'],first['model_id']]
    assert service.cache_metrics() == dict(residentModels=1,capacity=1,modelLoads=3,cacheHits=0,evictions=2,
        concurrency=1,activeModels=0,loadingModels=0,busyRejections=0)
