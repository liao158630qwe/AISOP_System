# RTMPose / RTMO 姿态后端（rtmlib）接入记录（2026-10-10）

## 动机与范围

- 离线管线（`algorithms/pipeline.py`）原先固定使用 Ultralytics YOLO11s-pose 做人形检测与关键点。
- 本次新增可选姿态后端：**RTMPose**（两阶段：YOLOX 人体检测 + RTMPose 自顶向下关键点）与 **RTMO**（单阶段多人实时），基于 [rtmlib](https://github.com/Tau-J/rtmlib) + ONNX Runtime。
- 跟踪、姿态规则、报告与证据渲染未改动；结论仍为 REVIEW，不输出工序合格。服务器 Worker/Web 界面暂未接入，仅离线 CLI 支持。

## 为什么用 rtmlib 而不是 MMPose 官方栈

- mmcv/mmpose 对新版 PyTorch/CUDA（本机 torch 2.8+cu128、Blackwell GPU）适配滞后，Windows 上编译 mmcv 成本高、易失败。
- rtmlib 是 RTMPose 作者维护的轻量推理库，直接运行官方 ONNX 权重，只依赖 onnxruntime，无 mmcv。

## 安装（本机已验证）

```powershell
D:\Anaconda\python.exe -m pip install rtmlib --no-deps
D:\Anaconda\python.exe -m pip install onnxruntime-gpu==1.22.0
```

- 不要直接 `pip install rtmlib`：它会同时装 CPU 版 `onnxruntime`，同名文件覆盖 GPU 版导致 CUDA 不可用（本机已复现并修复）。
- onnxruntime-gpu 必须与本机 CUDA 匹配：本机 CUDA 12.8，1.31.0 需要 CUDA 13（加载失败回退 CPU），1.22.0 正常。适配器会把 PyTorch 自带的 cuBLAS/cuDNN DLL 目录加入搜索路径，无需系统级安装 CUDA 运行时。
- 权重首次使用时自动下载到 `~/.cache/rtmlib/hub/checkpoints/`（yolox-m 约 90MB、rtmpose-m 约 48MB、rtmo-m 约 55MB）；离线部署需预置该目录。

## 用法

```powershell
# 两阶段 RTMPose（默认 balanced = YOLOX-m + RTMPose-m）
node tools/python.cjs tools/analyze-workers.py --video <视频> --pose-backend rtmpose --out <目录>

# 单阶段 RTMO（balanced = RTMO-m）
node tools/python.cjs tools/analyze-workers.py --video <视频> --pose-backend rtmo --out <目录>
```

- `--pose-backend yolo`（默认）行为与之前完全一致。
- `--rtm-mode` 可选 `lightweight` / `balanced` / `performance`（s / m / l 档位）。
- 产物与 YOLO 后端一致（analysis.json、report.html、segments.csv、evidence-*.jpg、video.webm、manifest.json）；`analysis.json` 的 `model` 段新增 `backend` 与 `components`（各 ONNX 文件名、SHA-256、字节数），manifest 的模型哈希取主模型文件。

## 实现位置

- `algorithms/rtmlib_pose.py`：适配器。`predict()` 返回与 Ultralytics Results 兼容的对象（`boxes.xyxy/conf`、`keypoints.data`、`speed`）。rtmlib 上游在 human 模式丢弃检测分数、RTMO 丢弃人体框，适配器通过子类化保留两者。
- `algorithms/pipeline.py`：支持 `options['model_meta']` 外部模型描述（名称、哈希、设备、组件）；不传时行为不变。
- `tools/analyze-workers.py`：新增 `--pose-backend`、`--rtm-mode` 参数。
- `tests/test_algorithms.py`：新增外部模型描述用例；其余 72 项测试全部通过。

## 验证结果（test.mp4，1920x1080 / 30fps / 196.7s，5fps 采样 984 帧，RTX 5070 Ti）

| 后端 | 模型 | 推理中位 / P95 | 人员检出率 | 平均置信度 | 动作覆盖 | 与 YOLO 标签一致 |
| --- | --- | --- | --- | --- | --- | --- |
| yolo（原有） | YOLO11s-pose @640 | 6.0 / 9.2 ms | 95.8% | 0.925 | 66.0% | — |
| rtmpose | YOLOX-m @640 + RTMPose-m 256x192 | 21.9 / 25.9 ms | 95.7% | 0.936 | 66.0% | 86.6% |
| rtmo | RTMO-m @640 单阶段 | 10.1 / 12.3 ms | 95.7% | 0.958 | 67.8% | 86.8% |

- 推理耗时为首帧预热后的单帧值；RTMPose 首帧 379ms、RTMO 首帧 5.4s，属 ONNX Runtime CUDA 首次编译/PTX JIT，之后稳定。
- 三个后端整段总耗时相近（113–124s，主要为视频解码与预览编码，推理不是瓶颈）。
- 证据图人工核对：三个后端的骨架与人体框均贴合；后端间标签差异主要集中在“手臂伸出 / 抬手 / 站立”的边界。
- 该视频是健身拉伸内容，非工厂场景；以上仅证明工程可运行与相对性能，不代表现场识别精度，需用现场数据复验。

## 已知限制

- 离线 Worker 与实时摄像头都已支持 rtmlib 后端：实时默认 RTMO-m（`REALTIME_BACKEND` 切换），上传任务可在界面选择 RTMO-m / RTMPose-m / YOLO11s-pose（服务端默认 `POSE_BACKEND`，默认 RTMO-m）。
- onnxruntime-gpu 版本需与本机 CUDA 匹配；环境升级后可换新版，无需改代码。
- 多人遮挡、远景小目标未在本视频验证；RTMO 单阶段对小目标的现场表现需另行评估。
- 检测阈值仍为 0.35，与 YOLO 后端保持一致；未做阈值调优。

## 实时摄像头接入（同日）

- `backend/realtime.py`：默认后端改为 RTMO-m（`mode='balanced'`，rtmlib + ONNX Runtime CUDA）；rtmlib 或权重不可用时自动回退 Ultralytics YOLO，忙时仍立即 429、不排队。响应新增 `backend`、`model` 字段，界面状态行显示当前模型名。
- 启动预热：`create_app` 在后台线程加载模型并跑 2 帧空画面（本机 2.28 s），消除摄像头首帧等待 CUDA 图编译；`REALTIME_WARMUP=false` 可关闭，`REALTIME_BACKEND` 可选 `rtmo`/`rtmpose`/`yolo`。
- 前端 `server-ui.js`：检测目标帧率上限 30→60 fps，默认 15→30 fps；采样 JPEG 质量 0.80→0.75；复用 canvas 2D 上下文，避免每帧重复取上下文。
- 输入尺寸不可再降：RTMO-m 的 ONNX 输入固定为 640×640（仅 batch 动态），无法用降低分辨率换延迟。

本机测量（RTX 5070 Ti，API 与 Worker 同时运行，test.mp4 抽帧 640×360 JPEG，RTMO-m）：

| 指标 | 优化前（YOLO11s-pose，默认 15 fps） | 现在（RTMO-m，默认 30 fps，上限 60） |
| --- | ---: | ---: |
| HTTP 往返中位 / P95 | 11.2 / 13.6 ms | 14.5 / 16.4 ms |
| 服务端处理中位 / P95 | 7.9 / 10.1 ms | 11.0 / 12.3 ms |
| 单请求在途持续吞吐 | — | 67.4 fps（120 帧连续请求） |
| 摄像头首帧 | 需先导入依赖并加载模型（约 2 s 级；离线整段运行时曾观测 RTMO 首帧 5.4 s） | 55 ms（预热已在启动后台完成） |
| 主画面检测帧率 | 默认 15 fps，上限 30 | 默认 30 fps，上限 60 |

- RTMO-m 单帧推理比 YOLO11s-pose 慢约 3 ms（10.1 vs 6.1 ms），但服务端 11 ms/帧仍可支撑 60 fps 目标；主画面帧率提升来自默认值与上限调整，单帧延迟下降来自启动预热与更低的编码开销。
- 浏览器每帧还有一次 JPEG 编码（约数毫秒），实际检测帧率由摄像头、编码与推理共同决定；界面分别显示预览帧率、检测帧率与延迟。
- 本轮只改实时路径，离线 Worker/CLI 的默认后端保持 YOLO11s-pose 不变；`tests/test_realtime.py` 新增后端选择、RTMO 响应与预热失败用例，全套 76 项通过、1 项跳过。

### 上传视频的模型选择（同日）

- 界面“离线分析姿态模型”下拉框提交 `Options.pose_backend`（`yolo`/`rtmo`/`rtmpose`）；不传时取服务端 `POSE_BACKEND`（默认 `rtmo`），并写回任务选项，任务因此自描述。
- 任务创建时按所选后端解析权重并记录 `model_hash`：YOLO 用 `models/yolo11s-pose.pt`，rtmlib 用对应 ONNX 主模型（RTMO-m 或 RTMPose-m）的 SHA-256；Worker 执行前与 manifest 发布前各校验一次，模型变化时任务失败而不是发布旧结果。
- Worker 按 `model_hash` 缓存检测器；rtmlib 任务的 `analysis.json` 与报告写入 `backend=rtmlib`、ONNX 组件哈希与 ONNX Runtime CUDA 设备信息。
- 网页实测（12 秒 640×360 片段、5fps 采样 60 帧、API 与 Worker 同时运行）：RTMO-m 平均单帧 15.7 ms、人员出现率 95%、1 条轨迹；YOLO11s-pose 平均 6.7 ms、出现率 100%。离线采样率低，两者端到端总耗时接近，模型选择主要影响姿态细节与标签口径。
- `tests/test_worker.py` 新增“按所选后端记录模型哈希”与“默认后端回落到配置值”用例；全套 78 项通过、1 项跳过。
