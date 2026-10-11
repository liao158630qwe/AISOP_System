# 公共数据下载、训练与测试实测报告

日期：2026-10-10。用途：研究与模型验证。本次已实际下载 HA4M 骨架及人工标签，训练 TCN、Transformer 与逐帧基线；用 Assembly101 真实动作事件训练 SOP 错误分类 GRU 与当前事件基线。下列数字均来自本机实际运行。

## 数据与切分

HA4M 选择官方公开目录中 41 名参与者各前两次完整录制，共 **82 段**。这是训练前按编号确定的首轮子集，不是完整 217 段基准，也没有按模型表现挑选视频。所选骨架 ZIP 与标签合计 **1.287 GB**；初始下载的额外录制保留在 raw 目录但未进入本轮训练／测试。所有选用文件有 SHA-256 清单和 ZIP CRC 检查。

源标签按 30 fps 的帧编号对齐，每 3 帧取一个已有观测，得到 10 Hz。保留原帧号与设备时间戳，不插入未来观测。0 类 dont-care、骨架缺失／多人歧义、根部无效及明显陈旧观测被掩码；91,454 个已标动作采样点中，88,459 个满足输入条件，覆盖率 **96.73%**。识别指标条件于这些可用且已标注的采样点，并非包含检测失败的端到端产线准确率。

| 分区 | 人数 | 完整录制数 | 10 Hz 采样点 | 可用动作采样点 |
|---|---:|---:|---:|---:|
| train | 27 | 54 | 64,512 | 60,331 |
| val | 7 | 14 | 13,763 | 12,861 |
| test | 7 | 14 | 15,876 | 15,267 |

划分种子 20261010，人员不跨分区，所有窗口在完整录制内部生成。人员列表：`{"train": [4, 5, 6, 7, 8, 10, 11, 12, 15, 16, 17, 18, 19, 20, 21, 22, 24, 26, 28, 29, 32, 34, 35, 36, 37, 40, 41], "val": [1, 3, 14, 31, 33, 38, 39], "test": [2, 9, 13, 23, 25, 27, 30]}`。这是自定义跨人员协议；不能直接与论文的其他切分成绩比较。

Assembly101 使用已下载的 328 条真实序列、3,964 条动作记录。按 48 名操作者划分，train／val／test 为 33／7／8 人，216／51／61 条序列。一条动词为 interior、语义未核实的记录保留未知输入并掩码目标，实际监督记录为 3,963 条。类别 correct／mistake／correction 的分区计数见 `sop-models/dataset.json`。

## 动作时序结果

输入为 **74 维**：Kinect 的二维投影按 COCO17 关节顺序映射，包含身体归一化坐标、置信度、有效掩码、位移、尺度及观测年龄。窗口 64 点。标签是 HA4M 的 12 类装配动作，**不是当前产品界面的五类人体动作**。Kinect 置信度和投影与 YOLO 输出分布不同，不能直接替换现有在线权重。

训练使用 AdamW、最大 30 epoch、验证集宏 F1 早停，类别权重和归一化仅从训练分区计算。TCN／Transformer 分别运行 2026、2027、2028 三个种子；逐帧 MLP 为单种子参照。TCN 的感受野为 61 点，Transformer 显式禁止读取未来帧。模型选择只使用验证集。

| 模型 | 次数 | 测试帧准确率 | 测试宏 F1（均值±种子标准差） | 分段 F1@0.5 | Edit |
|---|---:|---:|---:|---:|---:|
| frame_mlp | 1 | 47.19% | 38.28%（单次） | 3.27% | 8.01% |
| tcn | 3 | 65.60% | 63.53% ± 2.68 个百分点 | 19.90% | 23.26% |
| transformer | 3 | 67.58% | 66.93% ± 0.98 个百分点 | 18.75% | 22.71% |

分段指标在同一 10 Hz 时间基准下计算。人工 dont-care 及无效输入区间在真值与预测两侧同时掩码；不同类或断开的区间不会按时长平均伪造正确段落。种子标准差不是置信区间；这只是一套固定跨人切分的首轮结果。

时序模型明显优于本轮逐帧基线，但分段 F1@0.5 仍只有约 19%–20%，连续步骤的稳定性不足。分类分数不能代替整个动作区间的识别质量；尚未做后处理参数调优或现场置信度校准。

## SOP 序列错误结果

输入仅含当前及过去的真实动作动词、两个操作对象，以及当前动作结束后已知的持续时间。**没有输入错误标签、错误备注、操作者、玩具编号、绝对时间或未来事件。** 任务是动作事件已知条件下的三分类，当前记录按起点排序与完成时间排序一致；不是从视频直接判断工序合格。

| 模型 | 次数 | 测试准确率 | 测试宏 F1（均值±种子标准差） | mistake AP |
|---|---:|---:|---:|---:|
| event_mlp | 1 | 81.76% | 65.19%（单次） | 0.5201 |
| gru | 3 | 80.60% | 66.06% ± 0.32 个百分点 | 0.5295 |

GRU 相对当前事件 MLP 的宏 F1 改善有限，不能据此宣称流程错误检测已可靠。按验证集选择的 GRU 种子 2027，测试集 mistake 精确率 **52.00%**、召回率 **39.39%**；这项错误召回是当前明显的改进点。实际视频识别的上游错误还会进一步影响结果。

## 权重与复现

导出目录：`work/public-training-2026-10-10/export/`。每种架构取验证宏 F1 最高的种子，绝不按测试成绩挑权重。

| 权重 | 种子 | 最佳 epoch | 验证宏 F1 | 对应单次测试宏 F1 |
|---|---:|---:|---:|---:|
| `ha4m-tcn.pt` | 2028 | 12 | 68.90% | 64.35% |
| `ha4m-transformer.pt` | 2026 | 11 | 67.67% | 67.97% |
| `assembly101-sop-gru.pt` | 2027 | 16 | 68.90% | 66.22% |

`manifest.json` 保存导出权重哈希与来源。所有原始种子的权重、训练曲线 JSON、混淆矩阵和逐条测试预测保存在 `temporal-models/` 与 `sop-models/`。环境版本见 `environment.json`。

```powershell
# 重复下载会核对已完成文件哈希，再断点续作未完成的录制
node tools/python.cjs tools/download-ha4m.py --first-per-subject 2 --workers 4
node tools/python.cjs experiments/public_training/prepare_ha4m.py
node tools/python.cjs experiments/public_training/train_temporal.py
node tools/python.cjs experiments/public_training/train_sop.py
node tools/python.cjs -m pytest experiments/public_training/test_invariants.py -q

# 使用事件 JSON 测试已保存的 SOP 权重
node tools/python.cjs experiments/public_training/predict.py --checkpoint work/public-training-2026-10-10/export/assembly101-sop-gru.pt --input work/public-training-2026-10-10/sop-example-input.json --output work/public-training-2026-10-10/sop-prediction.csv
```

HA4M 推理同样使用 `predict.py`，输入 `ha4m/prepared/某录制.npz`，配套 JSON 用于特征版本和哈希校验；结果含源帧号、秒数、动作、置信度与输入有效性。这是可复现的研究推理入口，没有改动 Web／Worker 当前模型配置。

四项专项检查通过：窗口不能跨录制、分段指标对错误／碎片化有惩罚、动作模型因果性、SOP 模型因果性。每个训练后的动作模型另做权重保存重载一致性、未来扰动与单窗口前向计时。具体证据见 `file-verification.json` 和各运行的 `metrics.json`。

## 后续用途与边界

本轮完成了真实公共数据下载、训练、测试和可加载权重导出。动作任务需要进一步增加 RGB／手部／零件特征，并迁移到现场标签；SOP 任务需要接上视频动作识别及正确完成证据，再评估错误传播。螺丝扭矩、端子锁止、电测及实际吹气没有在本次数据中被验证，不能据此输出灯具生产合格结论。

数据来源与署名：[HA4M，Cicirelli 等，Scientific Data 2022，CC BY 4.0](https://www.nature.com/articles/s41597-022-01843-z)；[HA4M 官方数据仓库](https://baltig.cnr.it/ISP/ha4m)；[Assembly101，Sener 等，CVPR 2022](https://assembly-101.github.io/)；[错误标注与 Every Mistake Counts in Assembly，CC BY-NC 4.0](https://github.com/assembly-101/assembly101-mistake-detection)。
