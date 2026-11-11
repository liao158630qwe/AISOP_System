"""Accuracy reports must count missed samples and empty detections honestly."""
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('field_accuracy', Path(__file__).resolve().parents[1] / 'tools/evaluate-accuracy.py')
accuracy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accuracy)


def dataset(tmp_path, expected, actual):
    prediction = tmp_path / 'analysis.json'
    prediction.write_text(json.dumps({'model': {'sampleFps': 4}, 'frames': actual}), encoding='utf-8')
    return {'dataset_version': 1, 'videos': [{'id': 'camera-1', 'split': 'test',
        'prediction': str(prediction), 'frames': expected, 'conditions': {'lighting': 'low'}}]}


def frame(t, *actions):
    return {'time': t, 'people': [{'action': action} for action in actions]}


def test_presence_handles_empty_and_nonempty_detections(tmp_path):
    report = accuracy.evaluate(dataset(tmp_path,
        [frame(0, 'raised'), frame(.25, 'raised'), frame(.5), frame(.75)],
        [frame(0, 'raised'), frame(.25), frame(.5, 'raised'), frame(.75)]))
    assert report['presence'] == {'precision': .5, 'recall': .5, 'f1': .5}
    assert report['conditions']['lighting=low'] == report['presence']
    assert report['matchedFrames'] == 4


def test_missing_predictions_reduce_recall_and_report_coverage(tmp_path):
    report = accuracy.evaluate(dataset(tmp_path,
        [frame(0, 'raised'), frame(2, 'raised')], [frame(0, 'raised')]))
    assert report['annotatedFrames'] == 2
    assert report['matchedFrames'] == report['unmatchedFrames'] == 1
    assert report['coverage'] == .5
    assert report['presence']['recall'] == .5
    assert report['actions']['raised']['recall'] == .5


def test_all_missing_predictions_are_zero_recall_not_an_aborted_report(tmp_path):
    report = accuracy.evaluate(dataset(tmp_path, [frame(0, 'raised')], []))
    assert report['coverage'] == 0
    assert report['presence']['recall'] == report['actionMacroF1'] == 0


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_invalid_prediction_timestamps_are_rejected(tmp_path, value):
    with pytest.raises(ValueError, match='时间'):
        accuracy.evaluate(dataset(tmp_path, [frame(0)], [frame(value)]))


def test_split_must_be_explicit(tmp_path):
    data = dataset(tmp_path, [frame(0)], [frame(0)])
    del data['videos'][0]['split']
    with pytest.raises(ValueError, match='split'):
        accuracy.evaluate(data)


def test_same_video_cannot_leak_between_training_and_test(tmp_path):
    data = dataset(tmp_path, [frame(0, 'raised')], [frame(0, 'raised')])
    data['videos'].append(dict(data['videos'][0], split='train'))
    with pytest.raises(ValueError, match='split'):
        accuracy.evaluate(data)


def test_nearest_matching_sorts_predictions_and_breaks_ties_toward_earlier(tmp_path):
    report = accuracy.evaluate(dataset(tmp_path, [frame(.1, 'raised')],
                                      [frame(.2, 'bending'), frame(0, 'raised')]))
    assert report['coverage'] == report['actions']['raised']['f1'] == 1
