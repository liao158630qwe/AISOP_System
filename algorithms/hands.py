"""MediaPipe Hands 21 点检测与手势判定；算法测试与实时检测共用同一实现。

手势只按四指关节角度判定（拇指不参与），三元组来自 skeleton.FINGER_CURL_JOINTS，
保持手部拓扑的单一事实来源；本模块不确认是否真实持物或抓牢。
"""
import math

from .skeleton import FINGER_CURL_JOINTS

HAND_MODEL_FILENAME = 'hand_landmarker.task'
HAND_SHAPE_LABELS = {'CLOSED': '握拳／抓握候选', 'OPEN': '张手', 'PARTIAL': '部分弯曲', 'UNKNOWN': '手势不确定'}


def hand_shape(points):
    """按四指弯曲角判定手形；输入为 MediaPipe 世界坐标（等比例，非像素）。"""
    def angle(a, b, c):
        u = [a[i] - b[i] for i in range(3)]
        v = [c[i] - b[i] for i in range(3)]
        norm = math.sqrt(sum(x * x for x in u) * sum(x * x for x in v))
        if norm < 1e-8:
            return None
        return math.degrees(math.acos(max(-1, min(1, sum(x * y for x, y in zip(u, v)) / norm))))
    angles = [angle(points[m], points[p], points[t]) for m, p, t in FINGER_CURL_JOINTS]
    if any(a is None for a in angles):
        return 'UNKNOWN'
    folded = sum(a < 125 for a in angles)
    return 'CLOSED' if folded >= 3 else 'OPEN' if all(a > 155 for a in angles) else 'PARTIAL'


class HandLandmarker:
    """惰性加载 MediaPipe HandLandmarker；缺依赖抛 ImportError，缺模型抛 FileNotFoundError。"""

    def __init__(self, model_path):
        self.model_path = model_path
        self._landmarker = None

    def detect(self, frame):
        if not self.model_path.is_file():
            raise FileNotFoundError(self.model_path.name)
        import cv2
        import mediapipe as mp
        if self._landmarker is None:
            self._landmarker = mp.tasks.vision.HandLandmarker.create_from_options(mp.tasks.vision.HandLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(self.model_path)), num_hands=2,
                min_hand_detection_confidence=.5, min_hand_presence_confidence=.5))
        result = self._landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        hands = []
        for i, landmarks in enumerate(result.hand_landmarks):
            world = [[p.x, p.y, p.z] for p in result.hand_world_landmarks[i]]
            shape = hand_shape(world)
            hands.append({'points': [[p.x, p.y, p.z] for p in landmarks], 'shape': shape,
                          'label': HAND_SHAPE_LABELS[shape]})
        return hands
