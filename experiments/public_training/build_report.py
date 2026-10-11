"""Export validation-selected research checkpoints and a measured experiment report."""
import json
from pathlib import Path
import shutil

import numpy as np

from common import file_hash, save_json

ROOT = Path('work/public-training-2026-10-10')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def percent(value):
    return f'{value*100:.2f}%'


def mean_sd(summary):
    value = summary['macro_f1_all_classes']
    if summary['runs'] == 1:
        return percent(value['mean']) + '（单次）'
    return percent(value['mean']) + f' ± {value["std_across_seeds"]*100:.2f} 个百分点'


def main():
    temporal = read(ROOT/'temporal-models/results.json')
    temporal_summary = read(ROOT/'temporal-models/summary.json')
    sop = read(ROOT/'sop-models/results.json')
    sop_summary = read(ROOT/'sop-models/summary.json')
    data = read(ROOT/'ha4m/dataset.json')
    sop_data = read(ROOT/'sop-models/dataset.json')
    download = read(ROOT/'ha4m/download-manifest.json')
    export = ROOT/'export'
    export.mkdir(exist_ok=True)
    entries = []
    for kind, runs, directory, name, scope in [
        ('tcn', temporal, 'temporal-models', 'ha4m-tcn.pt', 'HA4M 12-class assembly actions from Kinect-derived 74-D features'),
        ('transformer', temporal, 'temporal-models', 'ha4m-transformer.pt', 'HA4M 12-class assembly actions from Kinect-derived 74-D features'),
        ('gru', sop, 'sop-models', 'assembly101-sop-gru.pt', '3-class error classification from ground-truth completed action events')]:
        selected = max([r for r in runs if r['model'] == kind], key=lambda r: r['validation']['macro_f1_all_classes'])
        source = ROOT/directory/f'{kind}-seed{selected["seed"]}'/'best.pt'
        target = export/name
        assert file_hash(source) == selected['weights_sha256']
        shutil.copy2(source, target)
        entries.append({'file': name, 'sha256': file_hash(target), 'source_run': str(source.parent),
            'selection': 'highest validation macro F1 within this architecture; never test score',
            'seed': selected['seed'], 'best_epoch': selected['best_epoch'], 'scope': scope,
            'validation_macro_f1': selected['validation']['macro_f1_all_classes'],
            'test_macro_f1': selected['test']['macro_f1_all_classes'], 'research_only': True})
    save_json(export/'manifest.json', {'models': entries, 'not_installed_in_production': True,
        'ha4m_license': 'CC-BY-4.0', 'assembly101_license': 'CC-BY-NC-4.0',
        'ha4m_dataset_sha256': file_hash(ROOT/'ha4m/dataset.json'),
        'sop_dataset_sha256': file_hash(ROOT/'sop-models/dataset.json')})
    save_json(ROOT/'file-verification.json', {'export_weights_match_training_records': True,
        'causality_all_runs': [{'model': r['model'], 'seed': r['seed'], 'max_error': r['future_perturbation_max_error']} for r in temporal+sop],
        'temporal_checkpoint_reload_max_errors': [r['checkpoint_reload_max_error'] for r in temporal],
        'unit_checks': '4 tests passed: recording-boundary isolation, temporal metric behavior, action causality, SOP causality'})

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10})
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for axis, summary, keys, title in [
        (axes[0], temporal_summary, ['frame_mlp','tcn','transformer'], 'HA4M: held-out-person action recognition'),
        (axes[1], sop_summary, ['event_mlp','gru'], 'Assembly101: annotated-event error classification')]:
        means = [summary[k]['macro_f1_all_classes']['mean']*100 for k in keys]
        errors = [summary[k]['macro_f1_all_classes']['std_across_seeds']*100 for k in keys]
        bars = axis.bar(keys, means, yerr=errors, capsize=5, color=['#64748b','#0d9488','#2563eb'][:len(keys)])
        axis.set(title=title, ylabel='Test macro F1 (%)', ylim=(0, 100))
        axis.spines[['top','right']].set_visible(False)
        axis.grid(axis='y', alpha=.15)
        axis.set_axisbelow(True)
        for bar, value in zip(bars, means):
            axis.text(bar.get_x()+bar.get_width()/2, value+2, f'{value:.1f}', ha='center')
    figure.text(.5, .01, 'Temporal/GRU: 3 seeds; frame/event MLP: 1 seed. Error bars are seed SD, not confidence intervals. Tasks use different data and labels.', ha='center', fontsize=8)
    figure.tight_layout(rect=(0,.045,1,1))
    figure.savefig(ROOT/'measured-results.png', dpi=180)
    plt.close(figure)

    total_bytes = sum(f['bytes'] for row in download['recordings'] for f in row['files'])
    annotated = sum(r['annotated_action_samples'] for r in data['records'])
    usable = sum(r['usable_action_samples'] for r in data['records'])
    ha_rows = []
    for kind in ['frame_mlp','tcn','transformer']:
        r = temporal_summary[kind]
        ha_rows.append(f'| {kind} | {r["runs"]} | {percent(r["accuracy"]["mean"])} | '
            f'{mean_sd(r)} | '
            f'{percent(r["segment_f1_at_0.5"]["mean"])} | {percent(r["edit_mean"]["mean"])} |')
    sop_rows = []
    for kind in ['event_mlp','gru']:
        r = sop_summary[kind]
        sop_rows.append(f'| {kind} | {r["runs"]} | {percent(r["accuracy"]["mean"])} | '
            f'{mean_sd(r)} | '
            f'{r["mistake_average_precision"]["mean"]:.4f} |')
    split_rows = []
    for part in ('train','val','test'):
        recs = [r for r in data['records'] if r['recording'] in data['splits'][part]]
        split_rows.append(f'| {part} | {len(data["subjects"][part])} | {len(recs)} | '
            f'{sum(r["samples_10hz"] for r in recs):,} | {sum(r["usable_action_samples"] for r in recs):,} |')
    export_rows = [f'| `{e["file"]}` | {e["seed"]} | {e["best_epoch"]} | {percent(e["validation_macro_f1"])} | {percent(e["test_macro_f1"])} |' for e in entries]
    selected_sop = next(e for e in entries if 'sop' in e['file'])
    sop_run = next(r for r in sop if r['model']=='gru' and r['seed']==selected_sop['seed'])
    mistake = sop_run['test']['per_class']['mistake']
    text = f'''# 公共数据下载、训练与测试实测报告

日期：2026-10-10。用途：研究与模型验证。本次已实际下载 HA4M 骨架及人工标签，训练 TCN、Transformer 与逐帧基线；用 Assembly101 真实动作事件训练 SOP 错误分类 GRU 与当前事件基线。下列数字均来自本机实际运行。

## 数据与切分

HA4M 选择官方公开目录中 41 名参与者各前两次完整录制，共 **{len(data['records'])} 段**。这是训练前按编号确定的首轮子集，不是完整 217 段基准，也没有按模型表现挑选视频。所选骨架 ZIP 与标签合计 **{total_bytes/1e9:.3f} GB**；初始下载的额外录制保留在 raw 目录但未进入本轮训练／测试。所有选用文件有 SHA-256 清单和 ZIP CRC 检查。

源标签按 30 fps 的帧编号对齐，每 3 帧取一个已有观测，得到 10 Hz。保留原帧号与设备时间戳，不插入未来观测。0 类 dont-care、骨架缺失／多人歧义、根部无效及明显陈旧观测被掩码；{annotated:,} 个已标动作采样点中，{usable:,} 个满足输入条件，覆盖率 **{percent(usable/annotated)}**。识别指标条件于这些可用且已标注的采样点，并非包含检测失败的端到端产线准确率。

| 分区 | 人数 | 完整录制数 | 10 Hz 采样点 | 可用动作采样点 |
|---|---:|---:|---:|---:|
{chr(10).join(split_rows)}

划分种子 20261010，人员不跨分区，所有窗口在完整录制内部生成。人员列表：`{json.dumps(data['subjects'])}`。这是自定义跨人员协议；不能直接与论文的其他切分成绩比较。

Assembly101 使用已下载的 328 条真实序列、3,964 条动作记录。按 48 名操作者划分，train／val／test 为 33／7／8 人，216／51／61 条序列。一条动词为 interior、语义未核实的记录保留未知输入并掩码目标，实际监督记录为 3,963 条。类别 correct／mistake／correction 的分区计数见 `sop-models/dataset.json`。

## 动作时序结果

输入为 **74 维**：Kinect 的二维投影按 COCO17 关节顺序映射，包含身体归一化坐标、置信度、有效掩码、位移、尺度及观测年龄。窗口 64 点。标签是 HA4M 的 12 类装配动作，**不是当前产品界面的五类人体动作**。Kinect 置信度和投影与 YOLO 输出分布不同，不能直接替换现有在线权重。

训练使用 AdamW、最大 30 epoch、验证集宏 F1 早停，类别权重和归一化仅从训练分区计算。TCN／Transformer 分别运行 2026、2027、2028 三个种子；逐帧 MLP 为单种子参照。TCN 的感受野为 61 点，Transformer 显式禁止读取未来帧。模型选择只使用验证集。

| 模型 | 次数 | 测试帧准确率 | 测试宏 F1（均值±种子标准差） | 分段 F1@0.5 | Edit |
|---|---:|---:|---:|---:|---:|
{chr(10).join(ha_rows)}

分段指标在同一 10 Hz 时间基准下计算。人工 dont-care 及无效输入区间在真值与预测两侧同时掩码；不同类或断开的区间不会按时长平均伪造正确段落。种子标准差不是置信区间；这只是一套固定跨人切分的首轮结果。

时序模型明显优于本轮逐帧基线，但分段 F1@0.5 仍只有约 19%–20%，连续步骤的稳定性不足。分类分数不能代替整个动作区间的识别质量；尚未做后处理参数调优或现场置信度校准。

## SOP 序列错误结果

输入仅含当前及过去的真实动作动词、两个操作对象，以及当前动作结束后已知的持续时间。**没有输入错误标签、错误备注、操作者、玩具编号、绝对时间或未来事件。** 任务是动作事件已知条件下的三分类，当前记录按起点排序与完成时间排序一致；不是从视频直接判断工序合格。

| 模型 | 次数 | 测试准确率 | 测试宏 F1（均值±种子标准差） | mistake AP |
|---|---:|---:|---:|---:|
{chr(10).join(sop_rows)}

GRU 相对当前事件 MLP 的宏 F1 改善有限，不能据此宣称流程错误检测已可靠。按验证集选择的 GRU 种子 {selected_sop['seed']}，测试集 mistake 精确率 **{percent(mistake['precision'])}**、召回率 **{percent(mistake['recall'])}**；这项错误召回是当前明显的改进点。实际视频识别的上游错误还会进一步影响结果。

## 权重与复现

导出目录：`work/public-training-2026-10-10/export/`。每种架构取验证宏 F1 最高的种子，绝不按测试成绩挑权重。

| 权重 | 种子 | 最佳 epoch | 验证宏 F1 | 对应单次测试宏 F1 |
|---|---:|---:|---:|---:|
{chr(10).join(export_rows)}

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
'''
    Path('docs/public-training-results-2026-10-10.md').write_text(text, encoding='utf-8')
    print('Exported 3 validation-selected checkpoints and measured report.', flush=True)


if __name__ == '__main__':
    main()
