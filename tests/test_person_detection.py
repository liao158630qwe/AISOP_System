"""Person-only inference, primary selection and selectable task integration."""
from types import SimpleNamespace

import numpy as np
import torch

from algorithms.tracking import PoseTracker
from backend.worker import execute_one
from conftest import new_run


def detections():
    points = np.tile([320., 250., .9], (3, 17, 1))
    points[:, :7] = [[320, 120, .9], [306, 100, .9], [334, 100, .9],
                     [292, 100, .9], [348, 100, .9], [270, 185, .9], [370, 185, .9]]
    return SimpleNamespace(boxes=SimpleNamespace(
        xyxy=torch.tensor([[250., 80., 400., 470.], [10., 10., 630., 470.], [200., 70., 430., 475.]]),
        conf=torch.tensor([.9, .99, .85]), cls=torch.tensor([0., 2., 0.])),
        keypoints=SimpleNamespace(data=torch.tensor(points)), speed={'inference': 1.})


def test_person_filter_primary_and_no_ghost_skeletons():
    tracker = PoseTracker()
    people = tracker.update(detections(), 640, 480, 0)
    assert len(people) == 2 and all(p['className'] == 'person' and p['classId'] == 0 for p in people)
    assert [p['primary'] for p in people] == [False, True]
    assert all(p['skeletonOverlay']['head'] and p['skeletonOverlay']['neck'] for p in people)
    assert tracker.update(SimpleNamespace(keypoints=None, boxes=None), 640, 480, .25) == []


def test_pose_task_metadata_does_not_load_model(context, monkeypatch):
    _, app, client = context
    monkeypatch.setattr(app.state.realtime, '_ensure_model', lambda: (_ for _ in ()).throw(AssertionError('must be read-only')))
    response = client.get('/api/v1/pose-model')
    assert response.status_code == 200
    data = response.json()
    assert data['ready'] and data['detectionClasses'] == ['person'] and data['skeleton']['jointCount'] == 17
    assert data['offlineBackend'] == 'yolo'
    assert [item['id'] for item in client.get('/api/v1/inspection-tasks').json()['items']] == ['screw', 'sticker', 'pose']


def test_pose_upload_worker_outputs_person_and_skeleton(authenticated, video_bytes):
    from algorithms.pipeline import analyze
    settings, app, client = authenticated
    calls = []

    class Detector:
        def predict(self, image, **kwargs):
            calls.append(kwargs)
            result = detections()
            # Match the tiny fixture's pixel dimensions.
            result.boxes.xyxy *= torch.tensor([.1, .1, .1, .1])
            result.keypoints.data[:, :, :2] *= .1
            return [result]

    def analyzer(*args, **kwargs):
        return analyze(*args, detector=Detector(), **kwargs)

    job = new_run(client, video_bytes, options={'analysis_type': 'pose', 'pose_backend': 'yolo', 'sample_fps': 4})
    # Injected analyzer skips loading real model weights while exercising the
    # real worker, video decoding, tracker, evidence, report and API artifacts.
    assert execute_one(app.state.engine, settings, analyzer=analyzer)
    assert client.get('/api/v1/analysis-runs/' + job['id']).json()['status'] == 'SUCCEEDED'
    result = client.get('/api/v1/analysis-runs/' + job['id'] + '/artifacts/analysis.json').json()
    assert result['analysisType'] == 'pose' and result['detectionClasses'] == ['person']
    assert result['skeleton']['jointCount'] == 17
    assert calls and all(call['classes'] == [0] for call in calls)
    assert all(len(frame['people']) == 2 for frame in result['frames'])
    assert all(sum(p['primary'] for p in frame['people']) == 1 for frame in result['frames'])
