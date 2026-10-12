"""骨骼拓扑框架：统一定义、渲染接入与前后端一致性。"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from algorithms import skeleton
from algorithms.skeleton import metric_keypoints, topology, visible_edges, visible_mask


def test_body_topology_is_complete_and_consistent():
    assert skeleton.JOINT_COUNT == 17 and len(set(skeleton.JOINT_NAMES)) == 17
    assert (set(skeleton.LEFT_JOINTS) | set(skeleton.RIGHT_JOINTS)
            | set(skeleton.MIDLINE_JOINTS)) == set(range(17))
    assert not set(skeleton.LEFT_JOINTS) & set(skeleton.RIGHT_JOINTS)
    assert set(skeleton.FACE_EDGES) == {(0, 1), (0, 2), (1, 3), (2, 4)}
    # LIMB_EDGES 必须与旧版 pose.EDGES 完全一致，保证既有算法与前端兜底不漂移。
    assert skeleton.LIMB_EDGES == ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
                                   (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))
    assert skeleton.BODY_EDGES == skeleton.LIMB_EDGES + skeleton.FACE_EDGES
    assert len(set(skeleton.BODY_EDGES)) == len(skeleton.BODY_EDGES)
    assert all(a != b and 0 <= a < 17 and 0 <= b < 17 for a, b in skeleton.BODY_EDGES)
    from algorithms.pose import EDGES
    assert tuple(EDGES) == skeleton.LIMB_EDGES


def test_hand_topology_covers_all_joints_and_legacy_consumers():
    assert skeleton.HAND_COUNT == 21 and len(set(skeleton.HAND_JOINT_NAMES)) == 21
    assert len(set(skeleton.HAND_BONES)) == len(skeleton.HAND_BONES)
    assert all(a != b and 0 <= a < 21 and 0 <= b < 21 for a, b in skeleton.HAND_BONES)
    bones = {(min(a, b), max(a, b)) for a, b in skeleton.HAND_BONES}
    drawn = {(min(a, b), max(a, b)) for chain in skeleton.HAND_CHAINS for a, b in zip(chain, chain[1:])}
    assert drawn <= bones, '绘制链必须是真实骨骼连接'
    assert {i for chain in skeleton.HAND_CHAINS for i in chain} == set(range(21))
    assert all(0 <= m < 21 and 0 <= p < 21 and 0 <= t < 21 for m, p, t in skeleton.FINGER_CURL_JOINTS)
    # 与旧版内联定义一致：lab.py 的弯曲角三元组、server-ui.js 的绘制链。
    assert tuple(skeleton.FINGER_CURL_JOINTS) == ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20))
    assert tuple(map(tuple, skeleton.HAND_CHAINS)) == ((0, 1, 2, 3, 4), (0, 5, 6, 7, 8), (5, 9, 10, 11, 12),
                                                       (9, 13, 14, 15, 16), (13, 17, 18, 19, 20), (0, 17))


def test_visible_mask_and_edges_gate_on_confidence():
    points = np.tile([.5, .5, .9], (17, 1))
    points[0, 2] = .1
    mask = visible_mask(points)
    assert not mask[0] and mask[5]
    edges = visible_edges(points)
    assert (0, 1) not in edges and (0, 2) not in edges and (5, 6) in edges
    assert len(edges) == len(skeleton.BODY_EDGES) - 2
    assert len(visible_edges(points, threshold=.05)) == len(skeleton.BODY_EDGES)
    with pytest.raises(ValueError):
        visible_mask(np.zeros((3, 2)))


def test_metric_keypoints_scales_x_without_mutating_input():
    points = np.array([[.2, .4, .9], [.5, .5, .8]])
    result = metric_keypoints(points, 2.0)
    assert result[0, 0] == pytest.approx(.4) and result[1, 0] == pytest.approx(1.0)
    assert np.array_equal(result[:, 1:], points[:, 1:])
    assert np.array_equal(points, np.array([[.2, .4, .9], [.5, .5, .8]]))
    with pytest.raises(ValueError):
        metric_keypoints(points, 0)
    with pytest.raises(ValueError):
        metric_keypoints(points, float('nan'))


def test_topology_payload_is_json_serializable():
    data = topology()
    assert json.loads(json.dumps(data)) == data
    assert data['jointCount'] == 17 and len(data['jointNames']) == 17
    assert data['edges'] == [list(edge) for edge in skeleton.BODY_EDGES]
    assert data['limbEdges'] == [list(edge) for edge in skeleton.LIMB_EDGES]
    assert data['faceEdges'] == [list(edge) for edge in skeleton.FACE_EDGES]
    assert data['handChains'] == [list(chain) for chain in skeleton.HAND_CHAINS]
    assert data['handBones'] == [list(bone) for bone in skeleton.HAND_BONES]
    assert data['visibilityThreshold'] == skeleton.VISIBILITY_THRESHOLD
    assert data['reliableVisibility'] == skeleton.RELIABLE_VISIBILITY


def test_annotate_draws_complete_skeleton_including_face_bones():
    from algorithms.pipeline import annotate
    keypoints = np.tile([.5, .5, .9], (17, 1))
    keypoints[0] = [.50, .40, .9]   # nose
    keypoints[1] = [.46, .36, .9]   # left_eye
    keypoints[2] = [.54, .36, .9]   # right_eye
    keypoints[3] = [.42, .36, .9]   # left_ear
    keypoints[4] = [.58, .36, .9]   # right_ear
    person = dict(trackId=1, bbox=[.2, .2, .8, .9], keypoints=keypoints.tolist())
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    annotate(frame, [person])
    nose_eye_midpoint = ((.50 + .46) / 2 * 200, (.40 + .36) / 2 * 200)
    assert frame[int(nose_eye_midpoint[1]), int(nose_eye_midpoint[0])].any(), '头部连线应被画出'
    shoulder_line = frame[100, 100]
    assert shoulder_line.any(), '躯干连线仍应画出'


def test_analysis_payload_carries_skeleton_topology(tmp_path, video_bytes):
    from algorithms.pipeline import analyze
    class EmptyDetector:
        def predict(self, *args, **kwargs):
            return [SimpleNamespace(keypoints=None, speed={'inference': .1})]
    video = tmp_path / 'empty.avi'
    video.write_bytes(video_bytes)
    model = tmp_path / 'fixture.pt'
    model.write_bytes(b'test stub')
    out = tmp_path / 'skeleton-result'
    data = analyze(video, model, out, run_id='skeleton-test', detector=EmptyDetector())
    assert data['skeleton'] == topology()
    written = json.loads((out / 'analysis.json').read_text(encoding='utf-8'))
    assert written['skeleton']['jointCount'] == 17
    assert written['skeleton']['edges'] == [list(edge) for edge in skeleton.BODY_EDGES]


def test_frontend_fallback_matches_server_topology():
    """app.js 的兜底骨架只允许是 topology() 的副本，防止前后端定义漂移。"""
    source = (Path(__file__).resolve().parents[1] / 'app.js').read_text(encoding='utf-8')
    match = re.search(r'const fallbackSkeleton=(\{.*?\});', source, re.S)
    assert match, 'app.js 中未找到 fallbackSkeleton 常量'
    fallback = json.loads(match.group(1))
    data = topology()
    for key in ('jointCount', 'edges', 'leftJoints', 'rightJoints', 'handChains', 'visibilityThreshold'):
        assert fallback[key] == data[key], f'app.js 兜底骨架的 {key} 与服务端 topology() 不一致'
