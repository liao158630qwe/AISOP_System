"""Evaluate a frozen validation-selected model once and publish a versioned replay."""
import json
from pathlib import Path
import shutil
import time
import numpy as np
import torch
from common import classification, temporal_metrics, file_hash, save_json
from scene_v2 import predict_members, filter_stages, DenseModel
from scene_runtime import check_sop

BASE = Path('work/simple-sop-2026-10-10')
ROOT = BASE/'v2'


def main():
    torch.set_num_threads(4)
    selection = json.loads((ROOT/'selection.json').read_text(encoding='utf-8'))
    dataset = json.loads((BASE/'data/dataset.json').read_text(encoding='utf-8'))
    old = json.loads((BASE/'evaluation.json').read_text(encoding='utf-8'))
    workflow = json.loads((BASE/'workflow.json').read_text(encoding='utf-8'))
    members = [torch.load(ROOT/'models'/run/'best.pt', map_location='cpu', weights_only=True)
               for run in selection['model']['members']]
    params = selection['filter']['parameters']
    export = ROOT/'export'
    export.mkdir(parents=True, exist_ok=True)
    bundle = {'format': 'dense-causal-ensemble-v2', 'members': members, 'input_dim': 74, 'classes': 5,
              'feature_spec': dataset['feature_spec'], 'labels': dataset['labels']}
    torch.save(bundle, export/'stage-ensemble.pt')
    workflow.update({'checkpoint_file': 'stage-ensemble.pt', 'filter_kind': 'ema-persistence-v2',
                     'filter_parameters': params})
    save_json(export/'workflow.json', workflow)
    manifest = {'selection_sha256': file_hash(ROOT/'selection.json'),
        'dataset_sha256': file_hash(BASE/'data/dataset.json'), 'weights_sha256': file_hash(export/'stage-ensemble.pt'),
        'members': selection['model']['members'], 'filter': params, 'selection': 'validation only',
        'feature_spec': dataset['feature_spec']}
    save_json(export/'manifest.json', manifest)
    truths, predictions, raw_predictions, replay, sop = [], [], [], [], []
    for name in dataset['splits']['test']:
        path = BASE/'data/prepared'/f'{name}.npz'
        assert file_hash(path) == next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        with np.load(path, allow_pickle=False) as data:
            values = {key: data[key] for key in data.files}
        p = predict_members(members, values['x'])
        stages = filter_stages(p, values['input_valid'], **params)
        truth = values['y']
        predicted_sop, reference = check_sop(stages, workflow['steps']), check_sop(truth, workflow['steps'])
        truths.append(truth)
        predictions.append(stages)
        raw_predictions.append(p.argmax(-1))
        sop.append({'recording': name, 'predicted': predicted_sop, 'annotation_reference': reference,
                    'exact_stage_sequence_match': predicted_sop['sequence'] == reference['sequence']})
        replay.append({'name': name, 'truth': truth.tolist(), 'prediction': stages.tolist(),
            'raw': p.argmax(-1).tolist(), 'confidence': p.max(-1).round(3).tolist(),
            'joints': values['x'][:, :68].reshape(-1, 17, 4).round(3).tolist(),
            'valid': values['input_valid'].tolist(), 'sop': predicted_sop})
    metrics = {}
    for key, predicted in [('raw_test', raw_predictions), ('stabilized_test', predictions)]:
        y, pred = np.concatenate(truths), np.concatenate(predicted)
        report = classification(y, pred, dataset['labels'])
        report.update(temporal_metrics(truths, predicted))
        report['unknown_predictions_on_scored_frames'] = int(((y >= 0) & (pred < 0)).sum())
        report['confusion_unknown_column'] = [int(((y == i) & (pred < 0)).sum()) for i in range(5)]
        metrics[key] = report
    metrics.update({'version': 'v2', 'selected_model': selection['model']['name'], 'members': manifest['members'],
        'parameters': params, 'sop_recordings': sop, 'test_recordings': len(sop),
        'exact_stage_sequence_matches': sum(s['exact_stage_sequence_match'] for s in sop),
        'selection_sha256': manifest['selection_sha256'],
        'limits': 'Same previously reported held-out subjects, not a new untouched test set; no anomaly ground truth or cross-scene validation.'})
    # Paired subject bootstrap gives sampling uncertainty without treating correlated frames as independent.
    rng = np.random.default_rng(20261010)
    old_replays = json.loads((BASE/'replays.json').read_text(encoding='utf-8'))
    subjects = dataset['subjects']['test']
    groups = {subject: [i for i, name in enumerate(dataset['splits']['test']) if int(name[3:6]) == subject] for subject in subjects}
    deltas = []
    for _ in range(3000):
        indices = [i for subject in rng.choice(subjects, len(subjects), replace=True) for i in groups[subject]]
        truth = np.concatenate([truths[i] for i in indices])
        before = np.concatenate([old_replays[i]['raw'] for i in indices])
        after = np.concatenate([raw_predictions[i] for i in indices])
        valid = truth >= 0
        deltas.append(float(np.mean(after[valid] == truth[valid])-np.mean(before[valid] == truth[valid])))
    metrics['raw_accuracy_gain_subject_bootstrap_95pct'] = np.quantile(deltas, [.025, .975]).tolist()
    verification = {'member_causality': [], 'standalone_reload': None}
    for checkpoint in members:
        model = DenseModel(**checkpoint['architecture']).eval()
        model.load_state_dict(checkpoint['state_dict'])
        torch.manual_seed(37)
        x = torch.randn(1, 620, 74)
        altered = x.clone()
        altered[:, 310:] += 25
        with torch.inference_mode():
            original = model(x)[:, :310]
            future_error = (original-model(altered)[:, :310]).abs().max().item()
            prefix_error = (original-model(x[:, :310])).abs().max().item()
        assert future_error < 1e-5 and prefix_error < 1e-5
        verification['member_causality'].append({'architecture': checkpoint['architecture'],
            'future_perturbation_max_error': future_error, 'prefix_max_error': prefix_error})
    save_json(ROOT/'evaluation.json', metrics)
    save_json(ROOT/'verification.json', verification)
    save_json(ROOT/'replays.json', replay)
    template = Path('experiments/public_training/simple_scene_demo.html').read_text(encoding='utf-8')
    template = template.replace('桌面装配收尾：看见阶段，检查顺序', '桌面装配收尾 · V2 准确率优化')
    architectures = [m['architecture'] for m in members]
    contexts = [2**(a['depth']+1)-1 for a in architectures if a['kind'] == 'tcn']
    description = f'V2 对 {len(members)} 个因果模型的预测取平均，逐帧学习动作阶段；'
    if contexts:
        description += f'TCN 最长覆盖 {max(contexts)/10:.1f} 秒历史。'
    template = template.replace('因果 TCN 使用过去 6.4 秒骨架观测；', description)
    comparison = f'<section><strong>与首版比较 · 相同测试人员</strong><p>帧准确率（原始）：{old["raw_test"]["accuracy"]:.2%} → {metrics["raw_test"]["accuracy"]:.2%}　｜　宏 F1（原始）：{old["raw_test"]["macro_f1_all_classes"]:.2%} → {metrics["raw_test"]["macro_f1_all_classes"]:.2%}<br>回放输出分段 F1@0.5：{old["stabilized_test"]["segment_f1_at_0.5"]:.2%} → {metrics["stabilized_test"]["segment_f1_at_0.5"]:.2%}</p></section>'
    template = template.replace('<section><select id="record"', comparison+'<section><select id="record"')
    payload = json.dumps({'workflow': workflow, 'metrics': metrics, 'records': replay}, ensure_ascii=False)
    html = template.replace('/*PAYLOAD*/', 'const DATA = '+payload+';')
    (ROOT/'demo.html').write_text(html, encoding='utf-8')
    if not (BASE/'demo-v1.html').exists():
        shutil.copy2(BASE/'demo.html', BASE/'demo-v1.html')
    (BASE/'demo.html').write_text(html, encoding='utf-8')
    raw, previous, stabilized = metrics['raw_test'], old['raw_test'], metrics['stabilized_test']
    interval = metrics['raw_accuracy_gain_subject_bootstrap_95pct']
    report = f'''# 四步装配时序模型 V2 优化结果

日期：2026-10-10。实测训练与推理，保留 V1 权重和回放。目标仍为其他作业、合拢组件、放置盖板、放置螺丝、工具操作并归位。

## 结果

|指标|V1|V2|
|---|---:|---:|
|原始帧准确率|{previous['accuracy']:.2%}|{raw['accuracy']:.2%}|
|原始宏 F1|{previous['macro_f1_all_classes']:.2%}|{raw['macro_f1_all_classes']:.2%}|
|原始分段 F1@0.5|{previous['segment_f1_at_0.5']:.2%}|{raw['segment_f1_at_0.5']:.2%}|
|最终回放输出帧准确率|{old['stabilized_test']['accuracy']:.2%}|{stabilized['accuracy']:.2%}|
|最终回放输出分段 F1@0.5|{old['stabilized_test']['segment_f1_at_0.5']:.2%}|{stabilized['segment_f1_at_0.5']:.2%}|
|完整阶段序列完全匹配|{old['exact_stage_sequence_matches']}/14|{metrics['exact_stage_sequence_matches']}/14|

原始帧准确率提升 {(raw['accuracy']-previous['accuracy'])*100:.2f} 个百分点。以 7 名测试人员为抽样单位、3000 次配对 bootstrap，提升的描述性 95% 区间为 [{interval[0]*100:.2f}, {interval[1]*100:.2f}] 个百分点；人员少，区间不反映模型选择或跨场景不确定性。不是按帧独立抽样。

## 改动与选择依据

从 V1 64 点窗口的末帧监督，改成完整录制上的逐帧监督。TCN 使用 7/8 层膨胀因果卷积（感受野 255/511 点）；另训练双层单向 GRU。使用逐帧 LayerNorm、训练集归一化和类别权重、0.03 标签平滑、同阶段相邻帧平滑损失，以及 0.75–1.25 倍速度和小幅特征噪声增强。没有双向网络、未来特征、真实步骤标签输入、绝对录制时间输入或强制 SOP 顺序解码。

固定候选为 6 个 TCN 和 2 个 GRU，最多 90 epoch，验证宏 F1 早停 18 轮；另比较验证前三名和全部模型的概率平均。最终选择 **{selection['model']['name']}**，成员：{', '.join(manifest['members'])}。验证宏 F1 {selection['model']['validation']['macro_f1_all_classes']:.2%}、准确率 {selection['model']['validation']['accuracy']:.2%}。EMA/持续确认网格仅用验证集选择，参数 {json.dumps(params)}；alpha=1、hold=1、threshold=0 表示直接输出有效骨架上的模型预测。

优化程序只加载 train/val 数组；`selection.json` 写出后才由独立脚本读取 test。沿用 HA4M 41 人、82 段和原来的人员隔离切分。这是已在 V1 报告过的测试集，不是新采集或从未查看的独立外部验证集。训练代码、协议、验证候选表、逐 epoch 曲线及权重均留存。每个模型的未来扰动和前缀一致性检查通过，SOP 检查仍允许并报告逆序与重复。

## 每类效果（原始预测）

|阶段|V1 F1|V2 F1|
|---|---:|---:|
'''
    for name, zh in zip(dataset['labels'], workflow['labels_zh']):
        report += f'|{zh}|{previous["per_class"][name]["f1"]:.2%}|{raw["per_class"][name]["f1"]:.2%}|\n'
    report += '''
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
'''
    Path('docs/simple-sop-v2-results-2026-10-10.md').write_text(report, encoding='utf-8')
    print(json.dumps({'raw_accuracy': raw['accuracy'], 'raw_macro_f1': raw['macro_f1_all_classes'],
        'segment_f1': stabilized['segment_f1_at_0.5'], 'exact_sequences': metrics['exact_stage_sequence_matches'],
        'accuracy_gain_95pct': interval, 'members': manifest['members']}, indent=2))


if __name__ == '__main__':
    main()
