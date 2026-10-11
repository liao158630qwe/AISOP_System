"""Check ROI batching against the original per-region detection contract."""
import copy
from types import SimpleNamespace

import numpy as np
import pytest

from algorithms.inspection import Inspector


class Detector:
    names = {0: 'screw', 1: 'hole'}

    def __init__(self):
        self.calls = []

    def predict(self, images, **kwargs):
        self.calls.append((images, kwargs))
        results = []
        for image in images:
            scores = np.array([image[0, 0, 0]/255, .9])
            visible = scores >= kwargs['conf']
            results.append(SimpleNamespace(boxes=SimpleNamespace(cls=np.array([0, 1])[visible], conf=scores[visible])))
        return results


def profile(count=4):
    return {'assets': {}, 'sha256': 'test', 'config': {
        'id': 'batch', 'name': 'batch', 'version': '1', 'approved': True,
        'fixed_fixture': True, 'min_sharpness': 0,
        'checks': [{'id': 'parts', 'name': 'parts', 'kind': 'presence', 'expected': {'screw': count},
                    'targets': [{'id': str(i), 'category': 'screw', 'roi': [i/count, 0, (i+1)/count, 1],
                                 'method': 'yolo', 'asset': 'parts.pt', 'class_name': 'screw', 'threshold': .5}
                                for i in range(count)]}]}}


def prepare(config):
    inspector = Inspector(config)
    detector = Detector()
    inspector.cache[('yolo', 'parts.pt')] = detector
    return inspector, detector


def test_batches_preserve_region_order_class_mapping_and_check_counts():
    config = profile()
    # IDs may repeat across checks, so results must map by observation, not ID.
    other = copy.deepcopy(config['config']['checks'][0])
    other['id'] = 'other'
    for target in other['targets']:
        target['class_name'] = 'hole'
    config['config']['checks'].append(other)
    inspector, detector = prepare(config)
    frame = np.zeros((64, 256, 3), np.uint8)
    for i in range(4):
        frame[:, i*64:(i+1)*64] = 150+i*20
    result = inspector.inspect(frame)
    assert len(detector.calls) == 1
    images, kwargs = detector.calls[0]
    assert len(images) == 8 and kwargs['rect'] is True and kwargs['conf'] == .5
    assert [t['score'] for t in result['checks'][0]['targets']] == [round((150+i*20)/255, 4) for i in range(4)]
    assert [t['score'] for t in result['checks'][1]['targets']] == [.9]*4
    assert all('4/4' in check['reason'] for check in result['checks'])
    assert result['verdict'] == 'REVIEW'


def test_different_thresholds_shapes_and_unknown_classes_are_isolated():
    config = profile()
    targets = config['config']['checks'][0]['targets']
    targets[1]['threshold'] = .8
    targets[2]['roi'][3] = .5
    targets[3]['class_name'] = 'missing'
    inspector, detector = prepare(config)
    result = inspector.inspect(np.full((64, 256, 3), 200, np.uint8))
    assert len(detector.calls) == 3
    assert [call[1]['conf'] for call in detector.calls] == [.5, .8, .5]
    assert [call[0][0].shape for call in detector.calls] == [(64, 64, 3), (64, 64, 3), (32, 64, 3)]
    assert result['checks'][0]['targets'][0]['state'] == 'CANDIDATE'
    assert result['checks'][0]['targets'][1]['state'] == 'UNKNOWN'
    assert result['checks'][0]['targets'][1]['score'] == 0
    assert result['checks'][0]['targets'][3]['state'] == 'UNKNOWN'
    assert '指定训练类别' in result['checks'][0]['targets'][3]['reason']


def test_batch_size_is_bounded_and_resets_between_frames():
    inspector, detector = prepare(profile(20))
    frame = np.full((64, 1280, 3), 200, np.uint8)
    inspector.inspect(frame)
    assert [len(call[0]) for call in detector.calls] == [8, 8, 4]
    detector.calls.clear()
    inspector.inspect(frame)
    assert [len(call[0]) for call in detector.calls] == [8, 8, 4]


@pytest.mark.parametrize('changes', [{'approved': False}, {'fixed_fixture': False}, {'min_sharpness': 1}])
def test_unapproved_or_blurry_regions_never_reach_detector(changes):
    config = profile()
    config['config'].update(changes)
    inspector, detector = prepare(config)
    result = inspector.inspect(np.zeros((64, 256, 3), np.uint8))
    assert not detector.calls
    assert all(t['state'] == 'UNKNOWN' for t in result['checks'][0]['targets'])


def test_incomplete_detector_results_do_not_silently_lose_regions():
    inspector, detector = prepare(profile())
    detector.predict = lambda images, **kwargs: []
    result = inspector.inspect(np.zeros((64, 256, 3), np.uint8))
    assert all(t['state'] == 'UNKNOWN' and '返回数量' in t['reason'] for t in result['checks'][0]['targets'])
