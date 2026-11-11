# 灯具模型训练与验证集诊断

日期：2026-10-14。目的：定位既有三轮合成试训为何没有形成有效操作事件，为下一阶段研究安排优先级。

本次重新加载已有拼接 TCN 和关系＋短长 TCN，使用 `load_training_data()` 读取 train/val 并做只读前向。未打开测试 NPZ，未更新权重、重新训练或调整解码参数。下列新指标不是现场精度，也不是新的测试集成绩。

## 1. 数据与计数单位

数据来自 synthetic-data/manifest.json（历史引用，当前工作区缺少文件：`../../outputs/lamp-research-2026-10-14/synthetic-data/manifest.json`），六个虚拟操作者、30 个周期，按操作者隔离为 20/5/5 个 train/val/test 周期。每周期 48 个时间点、两个位置。

| 监督类别 | train 动作帧位置样本 | val 动作帧位置样本 |
|---|---:|---:|
| 背景 | 1,476 | 369 |
| 接近 | 96 | 24 |
| 操作候选 | 188 | 47 |
| 撤离 | 96 | 24 |
| 合计 | 1,856 | 464 |

动作单位是“时间点×目标位置”；位置单位是时间点；边界单位是“时间点×目标位置×起／止”。未知动作监督被掩码排除。不能将 47 个操作采样点解释为 47 次操作事件。

验证集只预测背景的准确率为 `369/464 = 79.53%`，应作为最基本的多数类参照。

## 2. 动作头：尚未正确识别操作

| 模型 | 分区 | 动作准确率 | 四类宏 F1 | 正确操作采样点 |
|---|---|---:|---:|---:|
| 拼接 TCN | train | 78.45% | 21.98% | 0/188 |
| 拼接 TCN | val | 78.45% | 21.98% | 0/47 |
| 关系＋短长 TCN | train | 79.20% | 22.67% | 0/188 |
| 关系＋短长 TCN | val | 79.09% | 22.65% | 0/47 |

验证集预测类别数量：

| 模型 | 背景 | 接近 | 操作 | 撤离 |
|---|---:|---:|---:|---:|
| 拼接 TCN | 459 | 0 | 5 | 0 |
| 关系＋短长 TCN | 441 | 0 | 0 | 23 |

验证集混淆矩阵如下；行是真实类别，列是预测类别，顺序均为背景、接近、操作、撤离。

```text
拼接 TCN
[[364, 0, 5, 0],
 [ 24, 0, 0, 0],
 [ 47, 0, 0, 0],
 [ 24, 0, 0, 0]]

关系＋短长 TCN
[[367, 0, 0,  2],
 [ 24, 0, 0,  0],
 [ 26, 0, 0, 21],
 [ 24, 0, 0,  0]]
```

这说明主要问题在动作学习阶段已经出现。拼接模型的五个操作预测均落在真实背景上；关系模型不预测操作。当前不能将无事件输出主要归因于边界发布或质量拒判。

## 3. 位置头：关系模型还没有分清 A/B

位置类别顺序为 A、B、无目标、位置未知。验证集支持分别为 55、40、137、8 个时间点。

| 模型 | 位置准确率 | 四类位置宏 F1 | A 正确 | B 正确 |
|---|---:|---:|---:|---:|
| 拼接 TCN | 71.25% | 65.25% | 19/55 | 11/40 |
| 关系＋短长 TCN | 58.33% | 43.44% | 0/55 | 0/40 |

```text
拼接 TCN
[[19, 15,  21, 0],
 [18, 11,  11, 0],
 [ 4,  0, 133, 0],
 [ 0,  0,   0, 8]]

关系＋短长 TCN
[[0, 0,  55, 0],
 [6, 0,  34, 0],
 [0, 5, 132, 0],
 [0, 0,   0, 8]]
```

训练集也呈相同问题：关系模型 A 为 0/220、B 为 0/160。不能解释成只在新人验证集泛化失败。

未知类的正确输出包含模型全缺失时的硬回退机制，不应独立当作学习到的位置识别能力。

## 4. 发布漏斗与反事实检查

在验证集中，按现有事件导出使用的条件，统计通过门槛的操作帧位置样本：

| 条件 | 拼接 TCN 候选数 | 关系模型候选数 |
|---|---:|---:|
| 动作预测为操作 | 5 | 0 |
| 再要求位置关联正确 | 0 | 0 |
| 再要求质量可用 | 0 | 0 |
| 再要求当前证据新鲜有效 | 0 | 0 |

两者在第一层保留的正确操作样本已经为零。降低后续门槛不能凭空生成正确操作预测。

为定位另一个瓶颈，本次用真实动作替换动作预测，仅作诊断：经过现有模型位置、质量和当前证据门控后，拼接模型保留真实操作样本 14/47，关系模型 0/47。即使动作理想，现有位置头仍会阻止多数／全部正确操作发布。

用真实位置替换位置预测，两者仍保留零个正确操作样本。反事实检查使用标注，绝不能作为无标注推理结果、算法增益或正式评测成绩。

## 5. 质量与边界头不能只看表面准确率

两模型在验证集 480 个质量监督样本上均输出正确，其中 464 个可用、16 个不可用。结合合成生成逻辑与模型硬回退，这个任务较容易；当前结果不能证明对部分遮挡、误定位和多模态冲突的质量判断可靠。

边界监督共 928 个起／止条目，正例仅 16 个，占 1.72%。

| 验证边界诊断 | 拼接 TCN | 关系＋短长 TCN |
|---|---:|---:|
| 正例平均 sigmoid 分数 | 0.4336 | 0.4166 |
| 负例平均 sigmoid 分数 | 0.4104 | 0.4245 |
| 分数至少 0.5 的条目数 | 50 | 204 |

关系模型的正例平均分数还低于负例；仅以上均值不能完整评价边界质量，但已不支持“边界头训练有效”的结论。应进一步做起／止分开的 PR 曲线、边界误差和事件级验证。

质量 100% 和背景主导的约 79% 动作准确率不能抵消操作与位置的失败。

## 6. 优化建议与证据边界

建议依次检查：

1. 少量训练周期能否拟合非背景动作与 A/B 位置；检查输入、标签、梯度和输出头。
2. 动作／位置单任务与联合任务对照，辨别多任务竞争和训练不足。
3. 训练类频数加权、周期／事件采样与更充足训练预算；同时监控背景误报。
4. 用操作召回、位置混淆和验证事件结果辅助选型，避免只以综合损失奖励易任务。
5. 增加独立模态缺失、质量冲突、位置顺序与时长变化的合成诊断；随后用真实现场数据检验。

本次证据表明当前动作与位置学习不足，但不能区分唯一根因，也不能承诺加权损失、更多轮次或更大模型一定有效。先定位原因，再修改结构。

详细方法和消融设计见 [算法详解与优化研究路线](algorithm-explanation-and-research-2026-10-14.md)。

## 7. 复现方法

从项目根目录执行以下只读诊断。它使用训练分区拟合的归一化，读取 train/val，重载已有权重，打印类别支持、预测数量、混淆矩阵和发布漏斗。

```powershell
@'
from pathlib import Path
import torch
from experiments.lamp_assembly.models import load_checkpoint
from experiments.lamp_assembly.train import load_training_data, collate_cycles

torch.set_num_threads(4)
root = Path('outputs/lamp-research-2026-10-14')
_, _, samples = load_training_data(root/'synthetic-data/manifest.json')
for kind in ('concat', 'relation'):
    checkpoint = torch.load(root/('training-'+kind)/'best.pt',
                            map_location='cpu', weights_only=True)
    model = load_checkpoint(checkpoint).eval()
    for split in ('train', 'val'):
        batch = collate_cycles(samples[split])
        with torch.inference_mode():
            out = model(batch['features'], batch['targets'], batch['masks'],
                        batch['ages'], batch['quality'])
        print(kind, split)
        for head, label, mask in (
            ('action_logits', 'action', 'action_mask'),
            ('position_logits', 'position', 'position_mask'),
            ('quality_logits', 'quality_label', 'quality_mask')):
            valid = batch[mask] & (batch[label] >= 0)
            truth = batch[label][valid].long()
            pred = out[head].argmax(-1)[valid]
            classes = out[head].shape[-1]
            cm = torch.bincount(truth*classes+pred,
                 minlength=classes*classes).reshape(classes, classes)
            f1 = 2*cm.diag().float() / (cm.sum(0)+cm.sum(1)).clamp_min(1)
            print(head, 'truth', cm.sum(1).tolist(),
                  'pred', cm.sum(0).tolist(), 'confusion', cm.tolist(),
                  'accuracy', float((pred==truth).float().mean()),
                  'macro_f1', float(f1.mean()))
        valid = batch['action_mask'] & (batch['action'] >= 0)
        op = (out['action_logits'].argmax(-1)==2) & valid
        pos = out['position_logits'].argmax(-1)[..., None]
        assoc = pos == torch.arange(model.num_targets)
        quality = out['quality_logits'].argmax(-1)==1
        evidence = (batch['masks'] & (batch['quality'][..., 0]>0)
                    & (batch['quality'][..., 1]<1)
                    & (batch['ages']<=1)).any(-1)[..., None]
        gates = [op, op & assoc, op & assoc & quality,
                 op & assoc & quality & evidence]
        print('operation_funnel', [int(g.sum()) for g in gates])
'@ | & .venv\Scripts\python.exe -
```

## 8. 诊断对象的版本记录

| 对象 | SHA-256 |
|---|---|
| 拼接模型 `training-concat/best.pt` | `dd401e9b292fff500da8168e95ee545599ed3b34e14d6281e5fc849d865ce93f` |
| 关系模型 `training-relation/best.pt` | `8b39aafccb2db96c72eb33dc186036f0097992ab19588909f74f29e160bfaa25` |
| 合成数据 manifest | `ae89d6f21d6e811b58cd4d85db1b49ef4915951e56b9fd3c11e4ea35b2a6e213` |
| `experiments/lamp_assembly/models.py` | `c895733b6414b624cb0c85f67b6be87fc9d76925270c7c05a4f3ccaca95ce8cc` |
| `experiments/lamp_assembly/train.py` | `879cdff267236a4899cb22eed71c5efc535016e0ff940b1606feef0f433c616f` |
| `experiments/lamp_assembly/data.py` | `8430b8bfe28bc87ffafee14864a6fae8db126522096720374ab7166c590bab56` |

代码或权重变化后应重新运行并记录版本，不能沿用本文诊断数值解释新的训练结果。
