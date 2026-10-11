import pytest
import numpy as np
from algorithms.video import AnalysisError, probe, validate_options
from algorithms.pipeline import summarize


def test_pose_smoothing_reduces_jitter_without_filling_occlusion():
    from algorithms.pose import smooth_keypoints
    previous = np.tile([.5, .5, .9], (17, 1))
    current = previous.copy()
    current[:, 0] += .005
    filtered = smooth_keypoints(current, previous, 1/30, .6)
    assert np.all(filtered[:, 0] > previous[:, 0])
    assert np.all(filtered[:, 0] < current[:, 0])
    assert np.array_equal(filtered[:, 2], current[:, 2])
    current[9] = [0, 0, .1]
    assert np.array_equal(smooth_keypoints(current, previous, 1/30, .6)[9], current[9])
    assert np.array_equal(smooth_keypoints(current, previous, .5, .6), current)
    assert np.array_equal(smooth_keypoints(current, None, .1, .6), current)
    # A large genuine movement follows the current sample instead of trailing it.
    current[10, 0] += .2
    result = smooth_keypoints(current, previous, 1/30, .6)
    assert abs(result[10, 0]-current[10, 0]) < .025


def test_one_hidden_arm_cannot_prove_standing():
    from algorithms.pose import action_observation
    k = np.tile([.5, .5, .9], (17, 1))
    k[[5,6], 1] = .3
    k[[11,12], 1] = .6
    k[[9,10], 1] = .65
    box = np.array([.2, .1, .8, .9])
    scores, _ = action_observation(k, box, [], 0)
    assert scores['standing'] == 1
    k[9, 2] = .1
    scores, _ = action_observation(k, box, [], 0)
    assert scores['standing'] == 0
    k[10, 1] = .1
    scores, _ = action_observation(k, box, [], 0)
    assert scores['raised'] == 1  # The visible arm still supplies positive evidence.


def test_tracker_filters_per_person_and_keeps_raw_evidence():
    from types import SimpleNamespace
    from algorithms.tracking import PoseTracker
    class Tensor:
        def __init__(self, data): self.data = np.array(data, dtype=float)
        def cpu(self): return self
        def numpy(self): return self.data
    def detection(offset=0., hidden=False):
        points = np.tile([150., 180., .9], (2, 17, 1))
        points[:, [5,6], 1] = 100
        points[:, [11,12], 1] = 240
        points[0, :, 0] += offset
        points[1, :, 0] += 600
        if hidden: points[0, 9] = [0, 0, .1]
        return SimpleNamespace(
            boxes=SimpleNamespace(xyxy=Tensor([[80,50,230,400],[680,50,830,400]]), conf=Tensor([.9,.9])),
            keypoints=SimpleNamespace(data=Tensor(points)))
    tracker = PoseTracker()
    tracker.update(detection(), 1000, 500, 0)
    people = tracker.update(detection(5), 1000, 500, .04)
    assert [p['trackId'] for p in people] == [1, 2]
    assert .15 < people[0]['keypoints'][9][0] < .155
    assert people[0]['rawKeypoints'][9][0] == .155
    assert people[1]['keypoints'][9][0] == .75
    people = tracker.update(detection(5, hidden=True), 1000, 500, .08)
    assert people[0]['keypoints'][9] == [0., 0., .1]
    people = tracker.update(detection(10), 1000, 500, 1.)
    assert people[0]['keypoints'] == people[0]['rawKeypoints']
    # A missed detection breaks action evidence without discarding track identity.
    tracker.update(SimpleNamespace(keypoints=None, boxes=None), 1000, 500, 1.1)
    assert all(tr['decoder'].last is None and not tr['history'] for tr in tracker.active.values())
    people = tracker.update(detection(10), 1000, 500, 1.2)
    assert [p['trackId'] for p in people] == [1, 2]
    assert all(p['temporal']['candidateSeconds'] == 0 for p in people)
    assert all(p['action'] == 'unknown' for p in people)
    empty = SimpleNamespace(boxes=SimpleNamespace(xyxy=Tensor(np.empty((0,4))), conf=Tensor([])),
                            keypoints=SimpleNamespace(data=Tensor(np.empty((0,17,3)))))
    tracker.update(empty, 1000, 500, 1.3)
    assert all(tr['decoder'].last is None for tr in tracker.active.values())


def test_action_hysteresis_keeps_close_labels_but_allows_real_change():
    from algorithms.temporal import ActionDecoder
    decoder = ActionDecoder()
    for t in (0, .15, .3):
        result = decoder.update({'raised': .9, 'reaching': .8}, t)
    assert result['action'] == 'raised'
    for t in (.4, .5, .6, .7, .8):
        assert decoder.update({'raised': .84, 'reaching': .9}, t)['action'] == 'raised'
    for t in (.9, 1., 1.1, 1.2, 1.3, 1.4):
        result = decoder.update({'raised': .1, 'reaching': 1.}, t)
    assert result['action'] == 'reaching'
    assert decoder.update({}, 1.5, valid=False)['action'] == 'unknown'


def test_probe_and_range(tmp_path, video_bytes):
    path=tmp_path/'video.avi';path.write_bytes(video_bytes)
    info=probe(path)
    assert info['width']==64 and info['duration']==1
    with pytest.raises(AnalysisError):validate_options(info,1,1,5,None,640)
    with pytest.raises(AnalysisError):validate_options(info,0,1,5,[63,0,16,48],640)
    with pytest.raises(AnalysisError):probe(tmp_path/'missing.mp4')


def test_missing_observation_does_not_bridge_segments():
    p=dict(trackId=1,action='standing',label='站立',evidence='规则',confidence=.8)
    frames=[dict(time=0,end=.2,people=[p]),dict(time=.2,end=.4,people=[]),dict(time=.4,end=.6,people=[p])]
    segments, tracks, sums=summarize(frames)
    assert len(segments)==2
    assert tracks[0]['visibleSeconds']==pytest.approx(.4)
    assert sums['standing']==pytest.approx(.4)


def test_empty_detections_are_valid():
    assert summarize([dict(time=0,end=.2,people=[])])[:2]==([],[])


def test_no_person_pipeline_has_finite_outputs(tmp_path, video_bytes):
    import json
    from types import SimpleNamespace
    from algorithms.pipeline import analyze
    class EmptyDetector:
        def predict(self, *args, **kwargs):
            return [SimpleNamespace(keypoints=None, speed={'inference':0.1})]
    video=tmp_path/'empty.avi';video.write_bytes(video_bytes)
    model=tmp_path/'fixture.pt';model.write_bytes(b'test stub')
    out=tmp_path/'result'
    data=analyze(video,model,out,run_id='unit-test',detector=EmptyDetector())
    assert data['verdict']=='REVIEW' and data['summary']['maxPeople']==0
    assert data['summary']['actionCoverage']==0
    assert data['source']['duration']==1
    assert 'NaN' not in (out/'analysis.json').read_text(encoding='utf-8')
    assert set(json.loads((out/'manifest.json').read_text())['files']) >= {'analysis.json','report.html','segments.csv','video.webm'}


def test_pipeline_cancellation_never_publishes_manifest(tmp_path, video_bytes):
    from algorithms.pipeline import analyze
    from algorithms.video import Cancelled
    video=tmp_path/'input.avi';video.write_bytes(video_bytes)
    model=tmp_path/'fixture.pt';model.write_bytes(b'test stub')
    def cancel(_):raise Cancelled()
    with pytest.raises(Cancelled):analyze(video,model,tmp_path/'cancelled',run_id='cancel-test',progress=cancel,detector=object())
    assert not (tmp_path/'cancelled'/'manifest.json').exists()


def test_external_model_meta_overrides_yolo_descriptor(tmp_path, video_bytes):
    from types import SimpleNamespace
    from algorithms.pipeline import analyze
    class EmptyDetector:
        def predict(self, *args, **kwargs):
            return [SimpleNamespace(keypoints=None, speed={'inference':0.1})]
    video=tmp_path/'meta.avi';video.write_bytes(video_bytes)
    meta=dict(name='RTMPose-m + YOLOX-m（rtmlib 两阶段）', sha256='a'*64, device='ONNX Runtime CUDA / Test',
              backend='rtmlib', inputSize='检测 640x640 / 姿态 256x192',
              components=[dict(role='pose', name='x.onnx', sha256='b'*64, bytes=1)])
    data=analyze(video,tmp_path/'missing.pt',tmp_path/'meta-result',run_id='meta-test',
                 detector=EmptyDetector(),options={'model_meta':meta})
    assert data['model']['name']=='RTMPose-m + YOLOX-m（rtmlib 两阶段）'
    assert data['model']['sha256']=='a'*64 and data['model']['backend']=='rtmlib'
    assert data['model']['inputSize']=='检测 640x640 / 姿态 256x192'
    assert data['model']['components'][0]['role']=='pose'
