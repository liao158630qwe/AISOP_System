import numpy as np
import pytest

from algorithms.screw_refined import RefinedScrewDetector


class Model:
    def __init__(self):
        self.calls = []

    def predict(self, images, **kwargs):
        self.calls.append((images, kwargs))
        return [id(image) for image in images]


def test_specialized_crops_are_not_run_on_general_model_and_order_is_preserved(monkeypatch):
    monkeypatch.setattr('algorithms.screw_refined.predict_cuda',lambda model,images,**kwargs:model.predict(images,**kwargs))
    detector = object.__new__(RefinedScrewDetector)
    detector.config = {'target_models': {'S2': 'special', 'S4': 'special'}}
    detector.model, special = Model(), Model()
    detector.target_models, detector.device = {'special': special}, 0
    crops = [np.zeros((80, 80, 3), np.uint8) for _ in range(4)]
    targets = [{'id': 'S'+str(i+1)} for i in range(4)]
    results = detector.predict_crops(crops, targets, .45, {'S3': .3, 'S4': .35})
    assert results == [id(crop) for crop in crops]
    assert len(detector.model.calls) == len(special.calls) == 1
    images, options = detector.model.calls[0]
    assert [id(image) for image in images] == [id(crops[0]), id(crops[2])]
    assert options['conf'] == .3
    images, options = special.calls[0]
    assert [id(image) for image in images] == [id(crops[1]), id(crops[3])]
    assert options['conf'] == .35


def test_incomplete_model_batch_cannot_produce_partial_evidence(monkeypatch):
    monkeypatch.setattr('algorithms.screw_refined.predict_cuda',lambda model,images,**kwargs:model.predict(images,**kwargs))
    detector = object.__new__(RefinedScrewDetector)
    detector.config, detector.device = {}, 0
    detector.model = Model()
    detector.model.predict = lambda *args, **kwargs: []
    with pytest.raises(ValueError, match='数量'):
        detector.predict_crops([np.zeros((80, 80, 3), np.uint8)], [{'id': 'S1'}], .45, {})


def test_primary_weight_override_joins_primary_batch(monkeypatch):
    monkeypatch.setattr('algorithms.screw_refined.predict_cuda',lambda model,images,**kwargs:model.predict(images,**kwargs))
    detector = object.__new__(RefinedScrewDetector)
    detector.config = {'target_models': {'S2': 'same-weights'}}
    detector.model, detector.device = Model(), 0
    detector.target_models = {'same-weights': detector.model}
    crops = [np.zeros((80, 80, 3), np.uint8) for _ in range(3)]
    targets = [{'id': f'S{i+1}'} for i in range(3)]
    results = detector.predict_crops(crops, targets, .45, {'S2': .3})
    assert results == [id(crop) for crop in crops]
    assert len(detector.model.calls) == 1
    assert detector.model.calls[0][1]['conf'] == .3
