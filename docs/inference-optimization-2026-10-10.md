# 时序模型与目标检测推理优化

2026-10-10：优化 HA4M 三模型集成的流式推理，以及工位专用 YOLO 的多 ROI 推理。使用已有权重，没有重新训练。主要结果是减少推理时间；本轮没有测量精度提升。

## 本机测量

环境：Windows、PyTorch 2.8.0+cu128、Ultralytics 8.4.155、RTX 5070 Ti。时序使用 CPU、4 线程、FP32；目标检测使用 GPU。每种配置预热后采样 60 次。下表时间为一次调用的中位数，8/128 帧指整个分块，2/4/8 ROI 指整个画面的该组区域。

| 任务 | 优化前 | 优化后 | 中位耗时加速比 |
|---|---:|---:|---:|
| 时序：逐帧 | 3.584 ms | 1.176 ms | 3.05× |
| 时序：8 帧分块 | 3.143 ms | 1.632 ms | 1.93× |
| 时序：128 帧分块 | 7.488 ms | 6.268 ms | 1.19× |
| 目标检测：2 个 ROI | 18.176 ms | 12.897 ms | 1.41× |
| 目标检测：4 个 ROI | 26.232 ms | 12.378 ms | 2.12× |
| 目标检测：8 个 ROI | 54.648 ms | 18.710 ms | 2.92× |

逐帧时序 p95 为 4.264 → 1.725 ms；8 ROI 检测 p95 为 63.350 → 22.772 ms。所有原始计时、来源哈希、输入哈希、逐视频一致性结果保存在 `work/inference-optimization-2026-10-10/benchmark.json`。计时包含时序归一化、前向与概率回传，或检测区域裁切、清晰度检查、YOLO 前后处理及结果整理；不包含权重加载、视频解码、网络、姿态提取和整个业务流程。单机微基准不能换算为多路视频服务吞吐。

## 实现

`experiments/public_training/scene_v2.py` 的 `DensePredictor` 缓存每层 TCN 卷积实际需要的归一化历史特征，避免每次重跑整个历史窗口。CPU 的不超过 128 帧分块使用三个卷积采样位置与同一组权重做矩阵运算，减少短序列卷积的调度开销；更长分块与 CUDA 保留卷积算子。GRU 继续使用隐状态。`DenseModel.forward` 与检查点格式兼容，模型参数、归一化与阶段过滤参数保持原值。

缓存按层长度固定，录制结束调用 `reset()`。本次三成员集成的状态张量从 151,136 字节增至 195,840 字节，增加约 43.7 KiB；这是以少量有界状态换取减少重复计算。空分块和非法输入不会消耗历史。缓存模式用于推理，训练继续使用原有 `forward`。

`algorithms/inspection.py` 先检查各 ROI 的批准状态、尺寸和清晰度，再按模型、像素尺寸及检测阈值分组，每批最多 8 个 ROI。分组保留单图的矩形填充与置信度筛选方式，每个目标仍按自己的类别取分数。不同检查项的同名目标独立映射，汇总候选数在检测完成后计算。HSV 与模板检测路径继续使用现有处理。

该检测实现由实时服务和离线 Worker 共用。更新后重启相关 Python 进程即可加载。只有配置了同尺寸、同阈值的多个 YOLO ROI 时才会合批；未配置的灯具工位仍显示待复核，单独的通用目标测试及人体姿态推理没有此次 ROI 合批收益。

## 结果一致性与范围

- 使用 V2.2 已训练集成与全部 14 段既有 HA4M 输入，共 15,876 帧。以优化前完整序列输出对比新实现的不规则连续分块，最大概率差为 `2.6822e-7`。动作类别、稳定阶段及全部 SOP 事件与存档 CLI 输出一致。因此这批回放的原有指标保持不变；没有新增测试集或精度提升结论。
- 时序测试独立调用原模型完整前向作为参考，覆盖逐帧、跨缓存长度、混合分块、非零 LayerNorm 偏置、重置、无效输入、CPU/CUDA 及 CLI 输出。
- 目标检测采用 `assets/factory-workers.mp4` 首帧的实际图像区域和已有 YOLO11n COCO 权重。用于测试管线的临时工位配置将 `person` 类作为目标，仅存在于基准脚本内；没有写入工位配置，也不能评价螺丝或灯具零件识别精度。2/4/8 ROI 共三组的候选状态一致，四位小数输出分数最大差为 0.0006。批量浮点计算可能在阈值附近改变结果，正式专用权重仍需在现场标注验证集上复核。
- 检测回归覆盖分组阈值、不同尺寸、目标类别、位置顺序、跨检查项重复 ID、8 图批量上限、帧间隔离、未批准/模糊拒识及返回数量异常。

历史 V2.2 导出权重、源代码快照和验收记录保留原样。新运行时位于 `experiments/public_training`；冻结在 `work/simple-sop-2026-10-10/v2.2/source` 的历史脚本仍代表当时实现。HA4M Kinect 特征模型依然不能直接替代线上 YOLO 姿态规则解码器。

## 复测

最终回归：127 项通过，1 项 PostgreSQL 专用测试因未配置测试数据库而跳过。本机 CUDA 一致性测试已实际执行。

```powershell
npm run benchmark:inference -- --iterations 60
node tools/python.cjs -m pytest tests experiments/public_training/test_invariants.py experiments/public_training/test_simple_scene.py experiments/public_training/test_scene_v2.py experiments/public_training/test_evidence_filter.py experiments/public_training/test_scene_diagnostics.py -q
```

基准读取本次保存的原实现 `work/inference-optimization-2026-10-10/baseline`，不改动旧权重或回放。可用 `--section temporal` 或 `--section detection` 分别运行，并用 `--out` 保存到新文件。缺少模型、视频或 V2.2 输入时会明确报错，不能把未执行当作通过。
