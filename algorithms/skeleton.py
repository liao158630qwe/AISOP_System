"""COCO-17 人体骨架与 MediaPipe 手部骨架的统一定义。

与参考实现（ARecognition/bend_demo/skeleton_defs.py）相同，本模块是骨骼拓扑的
唯一事实来源，避免算法、证据渲染、服务接口和前端各写一份：

- 姿态后端：YOLO11-pose / RTMPose / RTMO 都输出 COCO-17，索引顺序与 JOINT_NAMES 一致；
- 算法层：algorithms.pose 的动作规则、algorithms.tracking 的骨架匹配；
- 证据渲染：algorithms.pipeline.annotate 使用 BODY_EDGES 画出完整骨架；
- 服务与前端：analysis.json 与实时接口携带 topology()，前端优先按服务端拓扑绘制；
- 手部：algorithms.lab 与界面共用 HAND_CHAINS / FINGER_CURL_JOINTS（MediaPipe Hands 21 点）。

坐标约定：[x, y, confidence]，x/y 为归一化画面坐标（y 向下），confidence 为关键点
置信度。本模块只描述连接关系与可见度阈值，不改变任何模型权重或判定规则。
"""
from __future__ import annotations

import numpy as np

# --- COCO-17 人体关键点 -----------------------------------------------------
JOINT_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)
JOINT_COUNT = len(JOINT_NAMES)

# 侧别索引遵循 COCO 约定：奇数点为左侧，偶数点为右侧，0 为中线。
LEFT_JOINTS = (1, 3, 5, 7, 9, 11, 13, 15)
RIGHT_JOINTS = (2, 4, 6, 8, 10, 12, 14, 16)
MIDLINE_JOINTS = (0,)

# 头部连线只用于绘制：脸部没有可动关节，动作规则与图结构（ST-GCN 类邻接矩阵）
# 都应使用 LIMB_EDGES，避免把纯绘制连线算进骨骼图。
FACE_EDGES = ((0, 1), (0, 2), (1, 3), (2, 4))
LIMB_EDGES = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12),
              (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))
# 完整可绘制骨架：四肢躯干在前、头部连线在后，便于按前缀截取旧版连线集合。
BODY_EDGES = LIMB_EDGES + FACE_EDGES

# --- 关键点可见度阈值 -------------------------------------------------------
VISIBILITY_THRESHOLD = .35   # 绘制、证据图与动作规则的“可见”下限
RELIABLE_VISIBILITY = .5     # 规则特征与界面强调使用的“可靠”下限

# --- MediaPipe Hands 21 点手部骨架（不含左右前缀） --------------------------
HAND_JOINT_NAMES = (
    "wrist",
    "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip",
    "index_mcp", "index_pip", "index_dip", "index_tip",
    "middle_mcp", "middle_pip", "middle_dip", "middle_tip",
    "ring_mcp", "ring_pip", "ring_dip", "ring_tip",
    "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip",
)
HAND_COUNT = len(HAND_JOINT_NAMES)
HAND_BONES = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)
# 绘制用折线链：四指 + 拇指各一条，掌根到小指掌指关节一条。
HAND_CHAINS = ((0, 1, 2, 3, 4), (0, 5, 6, 7, 8), (5, 9, 10, 11, 12),
               (9, 13, 14, 15, 16), (13, 17, 18, 19, 20), (0, 17))
# 手指弯曲角：(掌指关节, 近端指节, 指尖)，拇指不参与抓握判定。
FINGER_CURL_JOINTS = ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20))


def visible_mask(keypoints, threshold=VISIBILITY_THRESHOLD):
    """关键点可见性布尔掩码；输入形如 (N, 3) 的 [x, y, confidence]。"""
    points = np.asarray(keypoints, dtype=float)
    if points.ndim != 2 or points.shape[1] < 3:
        raise ValueError("关键点数组形状应为 (N, 3)")
    return points[:, 2] >= threshold


def visible_edges(keypoints, edges=BODY_EDGES, threshold=VISIBILITY_THRESHOLD):
    """两端都可见的骨骼连接，供绘制与统计使用。"""
    visible = visible_mask(keypoints, threshold)
    return [(a, b) for a, b in edges if visible[a] and visible[b]]


def metric_keypoints(keypoints, aspect):
    """转到各向同性坐标系的副本：x 乘以宽高比，消除画面拉伸对距离的影响。"""
    if not np.isfinite(aspect) or aspect <= 0:
        raise ValueError("宽高比必须是有限正数")
    result = np.array(keypoints, copy=True)
    result[:, 0] *= aspect
    return result


def topology():
    """JSON 可序列化的骨骼拓扑，随 analysis.json 与实时接口下发给前端。"""
    return {
        "version": 1,
        "jointCount": JOINT_COUNT,
        "jointNames": list(JOINT_NAMES),
        "edges": [list(edge) for edge in BODY_EDGES],
        "limbEdges": [list(edge) for edge in LIMB_EDGES],
        "faceEdges": [list(edge) for edge in FACE_EDGES],
        "leftJoints": list(LEFT_JOINTS),
        "rightJoints": list(RIGHT_JOINTS),
        "midlineJoints": list(MIDLINE_JOINTS),
        "handJointNames": list(HAND_JOINT_NAMES),
        "handBones": [list(bone) for bone in HAND_BONES],
        "handChains": [list(chain) for chain in HAND_CHAINS],
        "fingerCurlJoints": [list(triple) for triple in FINGER_CURL_JOINTS],
        "visibilityThreshold": VISIBILITY_THRESHOLD,
        "reliableVisibility": RELIABLE_VISIBILITY,
    }
