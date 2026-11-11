"""Occlusion filtering must preserve ROI ownership and fail closed on bad batches."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import torch

from algorithms.stickers import AppearanceStickerDetector


@pytest.fixture
def appearance(monkeypatch):
    detector = object.__new__(AppearanceStickerDetector)
    detector.height, detector.width = 300, 600
    detector.scale, detector.size, detector.device = 1., (600, 300), 0
    detector.reference = np.repeat(np.random.default_rng(15).integers(20, 60, (300, 600, 1), dtype=np.uint8), 3, axis=2)
    detector.reference_gray = cv2.cvtColor(detector.reference, cv2.COLOR_BGR2GRAY)
    detector.config = {'targets': [dict(id=identity, expected=expected, point=[x, 150],
                                      reference_bbox=[x-10, 140, x+10, 160])
                                  for identity, expected, x in [('T1','triangle',150), ('T2','triangle',300), ('R1','rectangle',450)]]}
    detector.material_models = {}
    detector.site_masks = {t['id']: np.ones((20,20), dtype=bool) for t in detector.config['targets']}
    detector.keys = [SimpleNamespace(pt=(i % 7 * 60., i // 7 * 100.)) for i in range(14)]
    detector.descriptors = np.ones((14, 128), dtype=np.float32)
    detector.sift = SimpleNamespace(detectAndCompute=lambda *a: (detector.keys, detector.descriptors))
    detector.matcher = SimpleNamespace(knnMatch=lambda *a, **k: [
        [SimpleNamespace(queryIdx=i, trainIdx=i, distance=0), SimpleNamespace(distance=1)] for i in range(14)])
    monkeypatch.setattr(cv2, 'findHomography', lambda *a: (np.eye(3), np.ones((14,1), dtype=np.uint8)))
    calls = []
    def predict(crops, **kwargs):
        calls.append(crops)
        # Return a rectangle only. A hidden preceding triangle must not shift
        # this output onto another target when the visible batch is compacted.
        return [SimpleNamespace(boxes=SimpleNamespace(data=torch.tensor([[70.,70.,90.,90.,.95,1.]]))) for _ in crops]
    detector.model = SimpleNamespace(predict=predict)
    return detector, calls


def test_occluded_sites_are_not_inferred_and_results_keep_original_owners(appearance):
    detector, calls = appearance
    image = detector.reference.copy()
    image[130:170, 130:170] = 240
    image[130:170, 280:320] = 240
    result = detector.inspect(image)
    assert [len(batch) for batch in calls] == [1]
    assert [t['state'] for t in result['targets']] == ['UNKNOWN','UNKNOWN','PRESENT']
    assert [t['id'] for t in result['displayTargets']] == ['R1']


def test_fully_occluded_frame_skips_model_and_emits_no_evidence(appearance):
    detector, calls = appearance
    result = detector.inspect(np.full_like(detector.reference, 240))
    assert calls == []
    assert not result['registered'] and result['displayTargets'] == []
    assert all(t['state'] == 'UNKNOWN' and t['score'] is None for t in result['targets'])


@pytest.mark.parametrize('count', [0, 2, 4])
def test_partial_or_extra_prediction_batches_fail_closed(appearance, count):
    detector, _ = appearance
    detector.model.predict = lambda *a, **k: [object()] * count
    with pytest.raises(ValueError, match='数量'):
        detector.inspect(detector.reference)


def test_local_anchor_warp_preserves_full_warp_acceptance(appearance, monkeypatch):
    detector, _ = appearance
    detector.config.update(component_anchors=[[40,40,80,80],[510,210,550,250]],
                           component_threshold=.5, anchor_search_radius=12)
    original = cv2.warpPerspective
    for transform in [np.eye(3), np.array([[1.,.01,8.],[-.01,1.,-4.],[.00002,-.00001,1.]])]:
        monkeypatch.setattr(cv2, 'findHomography', lambda *a: (transform, np.ones((14,1), dtype=np.uint8)))
        image = original(detector.reference, transform, detector.size)
        result = detector.inspect(image)
        aligned = original(image, np.linalg.inv(transform), detector.size)
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        expected = [float(cv2.matchTemplate(gray[b-12:d+12,a-12:c+12],
                        detector.reference_gray[b:d,a:c], cv2.TM_CCOEFF_NORMED).max())
                    for a,b,c,d in detector.config['component_anchors']]
        assert result['componentScores'] == pytest.approx(expected, abs=2e-4)
        assert all(score > .5 for score in result['componentScores'])
