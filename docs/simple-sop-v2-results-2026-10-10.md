# 四步装配时序模型 V2 优化结果

日期：2026-10-10。实测训练与推理，保留 V1 权重和回放。目标仍为其他作业、合拢组件、放置盖板、放置螺丝、工具操作并归位。

## 结果

|指标|V1|V2|
|---|---:|---:|
|原始帧准确率|75.68%|84.88%|
|原始宏 F1|68.48%|78.79%|
|原始分段 F1@0.5|18.80%|56.00%|
|最终回放输出帧准确率|71.00%|84.88%|
|最终回放输出分段 F1@0.5|44.83%|56.00%|
|完整阶段序列完全匹配|0/14|1/14|

原始帧准确率提升 9.20 个百分点。以 7 名测试人员为抽样单位、3000 次配对 bootstrap，提升的描述性 95% 区间为 [5.00, 14.00] 个百分点；人员少，区间不反映模型选择或跨场景不确定性。不是按帧独立抽样。

## 改动与选择依据

从 V1 64 点窗口的末帧监督，改成完整录制上的逐帧监督。TCN 使用 7/8 层膨胀因果卷积（感受野 255/511 点）；另训练双层单向 GRU。使用逐帧 LayerNorm、训练集归一化和类别权重、0.03 标签平滑、同阶段相邻帧平滑损失，以及 0.75–1.25 倍速度和小幅特征噪声增强。没有双向网络、未来特征、真实步骤标签输入、绝对录制时间输入或强制 SOP 顺序解码。

固定候选为 6 个 TCN 和 2 个 GRU，最多 90 epoch，验证宏 F1 早停 18 轮；另比较验证前三名和全部模型的概率平均。最终选择 **top3-ensemble**，成员：tcn-d7-s2027, tcn-d7-s2026, gru-d0-s2026。验证宏 F1 80.38%、准确率 86.13%。EMA/持续确认网格仅用验证集选择，参数 {"alpha": 1.0, "hold": 1, "threshold": 0.0}；alpha=1、hold=1、threshold=0 表示直接输出有效骨架上的模型预测。

优化程序只加载 train/val 数组；`selection.json` 写出后才由独立脚本读取 test。沿用 HA4M 41 人、82 段和原来的人员隔离切分。这是已在 V1 报告过的测试集，不是新采集或从未查看的独立外部验证集。训练代码、协议、验证候选表、逐 epoch 曲线及权重均留存。每个模型的未来扰动和前缀一致性检查通过，SOP 检查仍允许并报告逆序与重复。

## 每类效果（原始预测）

|阶段|V1 F1|V2 F1|
|---|---:|---:|
|其他作业|94.04%|98.61%|
|合拢组件|81.73%|89.75%|
|放置盖板|57.33%|70.08%|
|放置螺丝|48.39%|59.76%|
|使用工具操作并归位|60.93%|75.73%|

## 使用

原回放地址 `http://127.0.0.1:8766/demo.html` 已更新为 V2；旧版保留为 `demo-v1.html`。全部 14 段测试录制仍可切换，不选择性展示成功样本。

```powershell
node tools/python.cjs experiments/public_training/optimize_scene.py
node tools/python.cjs experiments/public_training/evaluate_scene_v2.py
node tools/python.cjs experiments/public_training/scene_runtime.py --bundle work/simple-sop-2026-10-10/v2/export --input work/simple-sop-2026-10-10/data/prepared/IDU002V001.npz --output work/simple-sop-2026-10-10/v2/example-inference.json
node tools/python.cjs -m pytest experiments/public_training/test_invariants.py experiments/public_training/test_simple_scene.py experiments/public_training/test_scene_v2.py -q
```

该模型仍只识别 HA4M Kinect 骨架动作阶段；相同维度的 YOLO 特征不能直接替换。完整序列匹配不等于异常检测准确率或装配质量合格；数据缺少独立错装、漏装、扭矩和跨场景标签。生产运行时未替换。

数据：[HA4M, Cicirelli 等, Scientific Data 2022, CC BY 4.0](https://www.nature.com/articles/s41597-022-01843-z)。
