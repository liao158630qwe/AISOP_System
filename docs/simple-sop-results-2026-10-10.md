# 桌面装配收尾：四步 SOP 与时序识别

日期：2026-10-10。已实际训练并导出因果 TCN。流程：合拢组件 → 放置盖板 → 放置螺丝 → 使用工具操作并归位；前八类装配动作映射为“其他作业”，未知标签仍掩码。

## 数据与协议

HA4M 82 段录制，41 人；沿用按人员隔离的 27/7/7 人、54/14/14 段 train/val/test。保留全部录制，81 段在有效标注中包含四个目标阶段；未剔除不完整序列。使用真实 Kinect 骨架，10 Hz、74 维、64 点历史窗口。归一化和类别权重仅来自训练集。不是原始 RGB 视频模型。

TCN 三个种子 2026/2027/2028，最多 25 epoch、验证宏 F1 早停 7 轮。按验证宏 F1 选择种子 2027（epoch 5）；逐帧 MLP 单种子作为基线。稳定化在预先固定的 9 组参数中按验证分段 F1@0.5 选择，不按测试表现选择。模型原始帧准确率三种子平均 75.73%，宏 F1 平均 68.02%（种子标准差 2.17 个百分点）；MLP 宏 F1 55.78%。

## 导出模型的独立测试结果

|指标|原始 TCN|因果稳定化|
|---|---:|---:|
|帧准确率|75.68%|71.00%|
|宏 F1|68.48%|65.90%|
|分段 F1@0.5|18.80%|44.83%|
|Edit|19.25%|51.70%|

稳定化：置信度 ≥ 0.45、连续 10 点才确认新阶段，不回填过去。无效或低置信度输入输出未知；未知在有真值帧上按漏识别计分。SOP 不强制修正模型顺序，也不输入真值。忽略其他作业与未知间隔，合并连续同阶段证据，检查先后顺序、跨阶段后重复和结束时未观察到的步骤。它不测量单次动作次数。

测试录制中，识别出的阶段序列与标注阶段序列完全一致为 **0/14**。这是上游识别加规则的序列一致率，不是实际异常检测准确率。此数据没有独立的漏装、错装或质量缺陷标签；规则的漏步、换序、重复、未完成测试来自明确标为人工构造的测试用例。

## 运行与回放

直接打开 `work/simple-sop-2026-10-10/demo.html`。全部 14 段测试录制可选择，默认第一段，不按效果挑样例；显示骨架、真实标签、原始/稳定预测、时序条和规则事件。骨架是身体归一化投影，无零件或手部细节。

```powershell
node tools/python.cjs experiments/public_training/prepare_simple_scene.py
node tools/python.cjs experiments/public_training/train_temporal.py --data work/simple-sop-2026-10-10/data --out work/simple-sop-2026-10-10/models --models frame_mlp tcn --seeds 2026 2027 2028 --epochs 25 --patience 7 --batch 512
node tools/python.cjs experiments/public_training/build_simple_scene.py
node tools/python.cjs experiments/public_training/scene_runtime.py --input work/simple-sop-2026-10-10/data/prepared/IDU002V001.npz --output work/simple-sop-2026-10-10/example-inference.json
node tools/python.cjs -m pytest experiments/public_training/test_invariants.py experiments/public_training/test_simple_scene.py -q
```

权重及步骤配置在 `work/simple-sop-2026-10-10/export/`，完整指标在 `evaluation.json`，原始训练记录在 `models/`。可替换步骤表与标签映射复用管线；新动作需要对应数据重新训练。推理入口只接受相同 HA4M 特征协议的 NPZ，维度相同不代表现场 YOLO 特征兼容。现有生产模型未替换。当前结果是简单场景研究原型，尚不能验证跨场景通用性、零件安装正确性、螺丝数量或扭矩。

来源：[HA4M, Cicirelli 等, Scientific Data 2022, CC BY 4.0](https://www.nature.com/articles/s41597-022-01843-z)。原始数据哈希与特征转换记录在 `work/public-training-2026-10-10/ha4m/`，本次映射数据的哈希在 `data/dataset.json`。
