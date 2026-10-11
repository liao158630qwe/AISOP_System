"""Verify the frozen V2.2 selection on previously reported test subjects and export a replay."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
import torch

from common import file_hash, save_json
from evidence_filter import EvidenceFilter
from scene_diagnostics import event_diagnostics, summarize_events
from scene_runtime import check_sop
from scene_v2 import DensePredictor, StageFilter
from select_scene_v22 import apply_filter, source_hashes, step_delay
from verify_scene_flow import read, score

BASE = Path('work/simple-sop-2026-10-10')
ROOT = BASE/'v2.2'
SOURCE = Path('experiments/public_training')


def synthetic_checks(kind, params, hz):
    cases = {'ordered': [1, 2, 3, 4], 'skip': [1, 3, 4], 'swap': [1, 3, 2, 4],
             'repeat': [1, 2, 1, 3, 4], 'missing_end': [1, 2, 3]}
    results = {}
    for name, sequence in cases.items():
        p = np.eye(5)[np.repeat(sequence, 100)]
        stages = apply_filter(kind, params, p, np.ones(len(p)))
        sop = check_sop(stages, [1, 2, 3, 4], hz)
        assert sop['sequence'] == sequence
        assert sop['observed_in_order'] == (name == 'ordered')
        results[name] = sop
    p = np.eye(5)[[1]*100+[3]*2+[1]*100]
    assert check_sop(apply_filter(kind, params, p, np.ones(len(p))), [1, 2, 3, 4], hz)['sequence'] == [1]
    results['two_frame_spike_suppressed'] = True
    results['clean_step_response_delay_seconds'] = step_delay(params, kind, hz)
    # Characterize short genuine steps as well as spurious spikes with the same rule.
    results['short_stage_probe'] = []
    for confidence in [1., .7, .55]:
        for duration in [2, 5, 10, 20]:
            p = np.eye(5)[[1]*100+[2]*duration+[3]*100]
            p[100:100+duration] = 0
            p[100:100+duration, 1] = 1-confidence
            p[100:100+duration, 2] = confidence
            stages = apply_filter(kind, params, p, np.ones(len(p)))
            observed = np.flatnonzero(stages[100:] == 2)
            results['short_stage_probe'].append({'confidence': confidence, 'duration_seconds': duration/hz,
                'detected': bool(len(observed)),
                'onset_offset_seconds': int(observed[0])/hz if len(observed) else None})
    return results


def paired_subject_interval(names, truths, before, after):
    subjects = sorted({name[3:6] for name in names})
    totals = []
    for subject in subjects:
        indices = [i for i, name in enumerate(names) if name[3:6] == subject]
        count = gain = exact_gain = 0
        for i in indices:
            truth, a, b = np.asarray(truths[i]), np.asarray(before[i]), np.asarray(after[i])
            keep = truth >= 0
            count += int(keep.sum())
            gain += int((b[keep] == truth[keep]).sum())-int((a[keep] == truth[keep]).sum())
            sequence = check_sop(truth, [1, 2, 3, 4])['sequence']
            exact_gain += int(check_sop(b, [1, 2, 3, 4])['sequence'] == sequence)
            exact_gain -= int(check_sop(a, [1, 2, 3, 4])['sequence'] == sequence)
        totals.append([count, gain, len(indices), exact_gain])
    totals = np.asarray(totals)
    rng = np.random.default_rng(20261010)
    sampled = totals[rng.integers(0, len(subjects), size=(3000, len(subjects)))].sum(axis=1)
    return {'subjects': len(subjects), 'samples': 3000,
        'accuracy_gain_95pct': np.quantile(sampled[:, 1]/sampled[:, 0], [.025, .975]).tolist(),
        'exact_sequence_rate_gain_95pct': np.quantile(sampled[:, 3]/sampled[:, 2], [.025, .975]).tolist(),
        'meaning': 'Descriptive paired subject bootstrap; includes neither selection nor cross-scene uncertainty.'}


def main():
    torch.set_num_threads(4)
    selection, protocol = read(ROOT/'selection.json'), read(ROOT/'protocol.json')
    assert selection['protocol_sha256'] == file_hash(ROOT/'protocol.json')
    assert protocol['source_sha256'] == source_hashes(), 'Code changed after selection; re-freeze on validation'
    for key, path in [('dataset_sha256', BASE/'data/dataset.json'),
                      ('baseline_selection_sha256', BASE/'v2.1/selection.json'),
                      ('model_selection_sha256', BASE/'v2/selection.json'),
                      ('workflow_sha256', BASE/'v2.1/export/workflow.json')]:
        assert protocol[key] == file_hash(path), path
    for path, expected in selection['input_sha256'].items():
        assert file_hash(path) == expected, path
    selected = selection['selected']
    assert selected['eligible']
    kind, params = selected['filter_kind'], selected['parameters']
    dataset = read(BASE/'data/dataset.json')
    workflow = read(BASE/'v2.1/export/workflow.json')
    workflow.update(filter_kind=kind, filter_parameters=params, checkpoint_file='stage-ensemble.pt')
    export = ROOT/'export'
    export.mkdir(parents=True, exist_ok=True)
    members = [torch.load(BASE/'v2/models'/name/'best.pt', map_location='cpu', weights_only=True)
               for name in selected['model']['members']]
    bundle = {'format': 'dense-causal-ensemble-v2', 'members': members, 'input_dim': 74, 'classes': 5,
              'feature_spec': dataset['feature_spec'], 'labels': dataset['labels']}
    torch.save(bundle, export/'stage-ensemble.pt')
    save_json(export/'workflow.json', workflow)
    for name, expected in protocol['source_sha256'].items():
        destination = ROOT/'source'/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE/name, destination)
        assert file_hash(destination) == expected

    started = time.perf_counter()
    checkpoint = torch.load(export/'stage-ensemble.pt', map_location='cpu', weights_only=True)
    predictor = DensePredictor(checkpoint['members'])
    load_seconds = time.perf_counter()-started
    baseline_replays = {r['name']: r for r in read(BASE/'v2.1/replays.json')}
    same_members = selected['model']['members'] == read(BASE/'v2/selection.json')['model']['members']
    truths, predictions, raw_predictions, old_predictions, replay, checks = [], [], [], [], [], []
    before_diagnostics, after_diagnostics = [], []
    for name in dataset['splits']['test']:
        path = BASE/'data/prepared'/f'{name}.npz'
        assert file_hash(path) == next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        with np.load(path, allow_pickle=False) as data:
            x, valid, truth = data['x'], data['input_valid'], data['y']
        predictor.reset()
        started = time.perf_counter()
        probability = predictor.push(x)
        batch_seconds = time.perf_counter()-started
        predictor.reset()
        post = EvidenceFilter(**params) if kind == 'confidence-evidence-v1' else StageFilter(**params)
        probabilities, stages, chunk_times = [], [], []
        start, chunk_index = 0, 0
        while start < len(x):
            end = min(len(x), start+[1, 7, 31, 128, 257][chunk_index % 5])
            started = time.perf_counter()
            chunk = predictor.push(x[start:end])
            stages.extend(post.push(chunk, valid[start:end]).tolist())
            chunk_times.append(time.perf_counter()-started)
            probabilities.append(chunk)
            start, chunk_index = end, chunk_index+1
        streamed = np.concatenate(probabilities)
        error = float(np.abs(streamed-probability).max())
        assert np.allclose(streamed, probability, atol=1e-6)
        assert np.array_equal(stages, apply_filter(kind, params, probability, valid)), name
        if same_members:
            assert probability.argmax(1).tolist() == baseline_replays[name]['raw']
        predicted_sop = check_sop(stages, workflow['steps'], workflow['sample_hz'])
        reference = check_sop(truth, workflow['steps'], workflow['sample_hz'])
        input_path, output_path = ROOT/'runtime-inputs'/f'{name}.npz', ROOT/'runtime-outputs'/f'{name}.json'
        input_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(input_path, x=x, input_valid=valid)
        # Execute the exported source snapshot in a clean process, without y/annotation input.
        subprocess.run([sys.executable, str(ROOT/'source/scene_runtime.py'), '--bundle', str(export),
                        '--input', str(input_path), '--output', str(output_path), '--chunk-size', '128'],
                       check=True, capture_output=True, text=True)
        standalone = read(output_path)
        assert standalone['stages'] == stages and standalone['sop'] == predicted_sop
        assert standalone['raw'] == probability.argmax(1).tolist()
        assert np.allclose(standalone['confidence'], probability.max(1), atol=1e-6)
        old_prediction = baseline_replays[name]['prediction']
        assert baseline_replays[name]['truth'] == truth.tolist()
        before = event_diagnostics(truth, old_prediction, workflow['steps'], workflow['sample_hz'])
        after = event_diagnostics(truth, stages, workflow['steps'], workflow['sample_hz'])
        before_diagnostics.append(before)
        after_diagnostics.append(after)
        truths.append(truth)
        predictions.append(stages)
        raw_predictions.append(probability.argmax(1))
        old_predictions.append(old_prediction)
        replay.append({'name': name, 'truth': truth.tolist(), 'prediction': stages,
            'raw': probability.argmax(1).tolist(), 'confidence': probability.max(1).round(3).tolist(),
            'joints': x[:, :68].reshape(-1, 17, 4).round(3).tolist(), 'valid': valid.tolist(), 'sop': predicted_sop})
        checks.append({'recording': name, 'frames': len(x), 'stream_max_probability_error': error,
            'batch_stream_stage_and_sop_match': True, 'standalone_cli_matches': True,
            'raw_matches_v21': True if same_members else None, 'batch_seconds': batch_seconds,
            'stream_seconds': sum(chunk_times), 'max_chunk_seconds': max(chunk_times),
            'predicted_sequence': predicted_sop['sequence'], 'reference_sequence': reference['sequence'],
            'baseline_sequence': baseline_replays[name]['sop']['sequence'],
            'exact_stage_sequence_match': predicted_sop['sequence'] == reference['sequence'],
            'baseline_diagnostics': before, 'diagnostics': after})
        print(f'{name}: exported CLI/stream/replay match; sequence {predicted_sop["sequence"]}', flush=True)
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    raw, old, final = [score(truths, p, *args) for p in [raw_predictions, old_predictions, predictions]]
    metrics = {'version': 'v2.2', 'raw_test': raw, 'stabilized_test': final, 'v21_stabilized_test': old,
        'exact_stage_sequence_matches': final['exact_stage_sequence_matches'],
        'filter_kind': kind, 'parameters': params, 'selected_model': selected['model'],
        'selection_scope': protocol['scope'], 'limits': protocol['limitations'],
        'baseline_events': summarize_events(before_diagnostics), 'events': summarize_events(after_diagnostics),
        'paired_subject_bootstrap': paired_subject_interval(dataset['splits']['test'], truths, old_predictions, predictions),
        'recordings': checks}
    baseline_config = read(BASE/'v2.1/export/workflow.json')
    verification = {'weights_load_seconds': load_seconds, 'recordings': checks,
        'synthetic_cases': synthetic_checks(kind, params, workflow['sample_hz']),
        'baseline_synthetic_cases': synthetic_checks(baseline_config['filter_kind'], baseline_config['filter_parameters'], workflow['sample_hz']),
        'source_sha256': source_hashes(), 'selection_sha256': file_hash(ROOT/'selection.json')}
    save_json(ROOT/'evaluation.json', metrics)
    save_json(ROOT/'verification.json', verification)
    save_json(ROOT/'replays.json', replay)
    save_json(export/'manifest.json', {'weights_sha256': file_hash(export/'stage-ensemble.pt'),
        'workflow_sha256': file_hash(export/'workflow.json'), 'selection_sha256': file_hash(ROOT/'selection.json'),
        'source_sha256': source_hashes(), 'version': 'v2.2', 'members': selected['model']['members'],
        'baseline_replays_sha256': file_hash(BASE/'v2.1/replays.json'),
        'evaluation_sha256': file_hash(ROOT/'evaluation.json'), 'verification_sha256': file_hash(ROOT/'verification.json'),
        'replays_sha256': file_hash(ROOT/'replays.json')})
    template = (SOURCE/'simple_scene_demo.html').read_text(encoding='utf-8')
    template = template.replace('桌面装配收尾：看见阶段，检查顺序', '桌面装配收尾 · V2.2 置信度累积')
    template = template.replace('因果 TCN 使用过去 6.4 秒骨架观测；',
        '三个因果模型识别阶段，根据累积置信度确认切换；')
    comparison = (f'<section><strong>V2.1 → V2.2 · 相同测试人员</strong><p>'
        f'完整序列匹配：{old["exact_stage_sequence_matches"]}/14 → {final["exact_stage_sequence_matches"]}/14'
        f'　｜　稳定帧准确率：{old["accuracy"]:.2%} → {final["accuracy"]:.2%}<br>'
        f'分段 F1@0.5：{old["segment_f1_at_0.5"]:.2%} → {final["segment_f1_at_0.5"]:.2%}。'
        f'理想切换响应 {verification["synthetic_cases"]["clean_step_response_delay_seconds"]:.1f} 秒；'
        f'真实匹配事件起点绝对偏移中位数 {metrics["events"]["absolute_onset_offset_median_seconds"]:.2f} 秒。'
        '短步骤或证据模糊的步骤仍可能漏检。</p></section>')
    template = template.replace('<section><select id="record"', comparison+'<section><select id="record"')
    html = template.replace('/*PAYLOAD*/', 'const DATA = '+json.dumps(
        {'workflow': workflow, 'metrics': metrics, 'records': replay}, ensure_ascii=False)+';')
    (ROOT/'demo.html').write_text(html, encoding='utf-8')
    write_report(selection, metrics, verification)
    print(json.dumps({'stable_accuracy': final['accuracy'], 'stable_macro_f1': final['macro_f1_all_classes'],
        'stable_segment_f1': final['segment_f1_at_0.5'], 'exact_sequences': final['exact_stage_sequence_matches'],
        'baseline_events': metrics['baseline_events'], 'events': metrics['events'],
        'bootstrap': metrics['paired_subject_bootstrap'], 'cli_recordings_passed': len(checks)}, indent=2), flush=True)


def write_report(selection, metrics, verification):
    old, new = metrics['v21_stabilized_test'], metrics['stabilized_test']
    previous, val = selection['validation_baseline'], selection['selected']['validation']
    before, after = metrics['baseline_events'], metrics['events']
    interval = metrics['paired_subject_bootstrap']['accuracy_gain_95pct']
    text = f'''# 装配 SOP V2.2：置信度累积与事件误差验证

日期：2026-10-10。实际运行验证搜索、导出权重推理与逐段 CLI 回归。

## 验证集选择

比较 {selection['candidates_evaluated']} 个模型／过滤组合，包含 8 个已有单模型、两个集成，以及持续确认和置信度累积过滤。训练权重未重训；所有候选只读取验证集预测。要求准确率、宏 F1、完整序列匹配均不低于 V2.1，理想切换响应不超过 1.3 秒；按完整序列、分段 F1、宏 F1、准确率、响应时间和成员数排序。协议、输入和源码 SHA256 在测试前冻结。

选中 {selection['selected']['model']['name']}，过滤器 `{metrics['filter_kind']}`，参数 `{json.dumps(metrics['parameters'])}`。原始预测来自同样的三个成员。过滤器累计第一、第二候选的概率差，减去 margin 后达到 budget 才切换；新候选变化或回到当前阶段时清空待确认量。无效骨架输出未知并重置。所有方向应用同一规则，可保留跳步、逆序和重复；不会把证据回填到过去。

|验证指标|V2.1|V2.2|
|---|---:|---:|
|完整阶段序列匹配|{previous['exact_stage_sequence_matches']}/14|{val['exact_stage_sequence_matches']}/14|
|稳定帧准确率|{previous['accuracy']:.2%}|{val['accuracy']:.2%}|
|稳定宏 F1|{previous['macro_f1_all_classes']:.2%}|{val['macro_f1_all_classes']:.2%}|
|分段 F1@0.5|{previous['segment_f1_at_0.5']:.2%}|{val['segment_f1_at_0.5']:.2%}|

验证分段 F1 有所下降；这次排序优先完整序列，不能称所有指标均改善。

## 原测试集回归

|指标|V2.1|V2.2|
|---|---:|---:|
|完整阶段序列匹配|{old['exact_stage_sequence_matches']}/14|{new['exact_stage_sequence_matches']}/14|
|稳定帧准确率|{old['accuracy']:.2%}|{new['accuracy']:.2%}|
|稳定宏 F1|{old['macro_f1_all_classes']:.2%}|{new['macro_f1_all_classes']:.2%}|
|分段 F1@0.5|{old['segment_f1_at_0.5']:.2%}|{new['segment_f1_at_0.5']:.2%}|
|阶段事件总数|{old['stage_events']}|{new['stage_events']}|
|序列对齐后多余事件|{before['extra_events']}|{after['extra_events']}|
|序列对齐后缺失事件|{before['missing_events']}|{after['missing_events']}|
|匹配事件数／标注事件数|{before['matched_events']}/{before['reference_events']}|{after['matched_events']}/{after['reference_events']}|
|起点绝对偏移中位数（秒）|{before['absolute_onset_offset_median_seconds']:.2f}|{after['absolute_onset_offset_median_seconds']:.2f}|
|起点绝对偏移 P90（秒）|{before['absolute_onset_offset_p90_seconds']:.2f}|{after['absolute_onset_offset_p90_seconds']:.2f}|

原始帧准确率为 {metrics['raw_test']['accuracy']:.2%}。事件以同类别、保序最大匹配对齐，同等匹配数量时最小化起点绝对偏移。负偏移表示提前识别；未匹配事件不参与时间统计，另计缺失和多余。两版本匹配事件集合可能不同。该诊断不是异常检测精确率／召回率，也不是摄像头到显示端的端到端延迟。

按 7 名人员进行 3000 次配对 bootstrap，稳定帧准确率增量的描述性 95% 区间为 [{interval[0]*100:.2f}, {interval[1]*100:.2f}] 个百分点。区间不包含模型选择与跨场景不确定性；这仍是之前已使用的测试人员，不是新外部验证。

## 响应与实现验证

理想 one-hot 阶段切换响应：{verification['baseline_synthetic_cases']['clean_step_response_delay_seconds']:.1f} → {verification['synthetic_cases']['clean_step_response_delay_seconds']:.1f} 秒。合成检查保留正确、跳步、逆序、重复与结尾缺步，两帧尖峰被抑制。`verification.json` 同时记录 0.2／0.5／1／2 秒、不同置信度的真实短步骤探针，展示漏检代价。

14 段均重新加载导出权重推理，整段与不规则连续分块阶段完全一致；最大概率误差 {max(r['stream_max_probability_error'] for r in verification['recordings']):.3g}。每段单独启动导出源码 CLI，只输入 x 和 input_valid，阶段、SOP、原始类别及置信度与回放一致。原始预测逐帧与 V2.1 一致。

CPU 加载 {verification['weights_load_seconds']:.3f} 秒；{sum(r['frames'] for r in verification['recordings'])} 帧整段推理合计 {sum(r['batch_seconds'] for r in verification['recordings']):.3f} 秒，不规则分块合计 {sum(r['stream_seconds'] for r in verification['recordings']):.3f} 秒，不含采样等待。固定 128 帧 CLI 分块用于离线回归，在线推理器可逐帧 push。

选中阈值为 0，确认后的阶段在竞争证据不足时可能保持较久；小于 margin 的持续优势不会完成切换。短阶段、模糊步骤仍可能被压掉。当前数据只能评价 Kinect 骨架动作阶段；不能证明螺丝数量、扭矩或作业质量，也不能直接接入使用不同特征的 YOLO 生产管线。

## 逐段结果

|录制|V2.1 序列|V2.2 序列|V2.2 完全匹配|
|---|---|---|---|
'''
    for record in metrics['recordings']:
        text += f"|{record['recording']}|{record['baseline_sequence']}|{record['predicted_sequence']}|{'是' if record['exact_stage_sequence_match'] else '否'}|\n"
    text += '''
## 复现

```powershell
node tools/python.cjs experiments/public_training/select_scene_v22.py
node tools/python.cjs experiments/public_training/evaluate_scene_v22.py
npm run test:scene
npm run test:scene-ui
```

V2.2 回放与导出结果在 `work/simple-sop-2026-10-10/v2.2/`。`source/` 包含已核对哈希的源码快照，`export/` 包含权重、工作流和清单，`runtime-inputs/` 不含 y 标注。旧版结果保留在 v2、v2.1 目录中。

数据来源：[HA4M / Cicirelli 等 / Scientific Data 2022 / CC BY 4.0](https://www.nature.com/articles/s41597-022-01843-z)。
'''
    Path('docs/simple-sop-v22-results-2026-10-10.md').write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
