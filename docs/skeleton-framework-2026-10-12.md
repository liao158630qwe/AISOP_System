# 骨骼框架统一（2026-10-12）

## 动机

骨骼拓扑此前分散在四处，改动一处容易与其余三处漂移：

- `algorithms/pose.py` 的 `EDGES`（四肢躯干 12 条连线，不含头部）；
- `app.js` 的 `poseEdges` 常量与绘制函数里硬编码的头部连线；
- `server-ui.js` 手部绘制内联的 6 条手指折线链；
- `algorithms/lab.py` 手指弯曲角度的内联三元组。

参考 `D:\Project\ARecognition\bend_demo\skeleton_defs.py` 的做法（骨架关键点定义由
媒体管道与 ST-GCN 共用，保持点名、顺序、骨骼连接一致），本次把骨骼拓扑收拢为
单一事实来源，并让证据渲染、服务接口和界面消费同一份定义。

## 改动

- 新增 `algorithms/skeleton.py`：
  - COCO-17 人体关键点：`JOINT_NAMES`（YOLO11-pose / RTMPose / RTMO 输出顺序）、
    `LEFT_JOINTS` / `RIGHT_JOINTS` / `MIDLINE_JOINTS` 侧别索引；
  - 骨骼连接：`LIMB_EDGES`（四肢躯干，与旧 `EDGES` 逐项一致）、`FACE_EDGES`
    （鼻-眼、眼-耳，仅用于绘制）、`BODY_EDGES = LIMB_EDGES + FACE_EDGES`；
    图结构类消费者应使用 `LIMB_EDGES`，避免把绘制连线算进骨骼图；
  - 可见度阈值：`VISIBILITY_THRESHOLD = 0.35`、`RELIABLE_VISIBILITY = 0.5`；
  - MediaPipe Hands 21 点：`HAND_JOINT_NAMES`、`HAND_BONES`、`HAND_CHAINS`
    （绘制链）、`FINGER_CURL_JOINTS`（抓握弯曲角三元组）；
  - 工具函数：`visible_mask` / `visible_edges` / `metric_keypoints`
    （宽高比校正副本，供跟踪与动作规则共用）/ `topology()`（JSON 可序列化拓扑）。
- 证据图：`algorithms/pipeline.py` 的 `annotate()` 改用 `BODY_EDGES`，补齐头部
  连线；此前证据图骨架缺少鼻-眼、眼-耳，与界面不一致。
- 数据下发：`analysis.json` 与实时接口响应新增 `skeleton` 字段（`topology()`），
  含点序、连接、侧别、手部链、阈值与版本号。
- 前端：`app.js` 内置兜底骨架（静态预览等无接口场景），收到服务端拓扑时优先
  采用；`server-ui.js` 手部绘制改用同一定义。`tests/test_skeleton.py` 会校验
  兜底常量与 `topology()` 完全一致，防止前后端漂移。
- 算法层：`pose.py` / `tracking.py` / `lab.py` 改用统一阈值与工具函数，判定
  逻辑与阈值数值均未改变。

## 测试

- `tests/test_skeleton.py`：拓扑不变量（点序、侧别、连接合法且无重复、旧
  `EDGES` 兼容、手部链覆盖全部 21 点）、可见度与宽高比工具函数、证据图头部
  连线渲染、`analysis.json` 携带拓扑、`app.js` 兜底与服务端一致。
- 全量 `pytest tests -q`：90 passed, 1 skipped；浏览器回归
  `node tools/preview-test.cjs` 通过。

## 边界

- 本次只统一骨骼拓扑与渲染/下发的接入方式，不改动模型权重、跟踪与动作规则
  阈值，因此不产生新的精度声明；现场视频仍按既有流程复验。
- `topology()` 的 `version` 字段用于未来扩展（例如接入手部 59 点或工具关键点），
  前端遇到不认识的版本仍按字段校验后使用。
