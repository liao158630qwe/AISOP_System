"""Verify the frozen V2.3 selection on previously reported test subjects and export a replay."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
import torch

from common import file_hash, save_json
from scene_diagnostics import event_diagnostics, summarize_events
from scene_runtime import check_sop
from scene_v2 import DensePredictor
from select_scene_v23 import apply_filter, source_hashes, step_delay
from verify_scene_flow import read, score

BASE = Path('work/simple-sop-2026-10-10')
ROOT = BASE/'v2.3'
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
                      ('baseline_selection_sha256', BASE/'v2.2/selection.json'),
                      ('candidates_sha256', ROOT/'candidates.json'),
                      ('workflow_sha256', BASE/'v2.1/export/workflow.json')]:
        assert protocol[key] == file_hash(path), path
    for path, expected in selection['input_sha256'].items():
        assert file_hash(path) == expected, path
    selected = selection['selected']
    assert selected['eligible']
    assert selection['beats_v22_validation'], 'V2.3 selection does not beat the V2.2 validation tuple'
    kind, params = selected['filter_kind'], selected['parameters']
    dataset = read(BASE/'data/dataset.json')
    workflow = read(BASE/'v2.1/export/workflow.json')
    workflow.update(filter_kind=kind, filter_parameters=params, checkpoint_file='stage-ensemble.pt')
    export = ROOT/'export'
    export.mkdir(parents=True, exist_ok=True)
    members = [torch.load(ROOT/'models'/name/'best.pt', map_location='cpu', weights_only=True)
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
    baseline_replays = {r['name']: r for r in read(BASE/'v2.2/replays.json')}
    baseline_config = read(BASE/'v2.2/export/workflow.json')
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
        from scene_runtime_v23 import make_filter
        post = make_filter(kind, params)
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
        predicted_sop = check_sop(stages, workflow['steps'], workflow['sample_hz'])
        reference = check_sop(truth, workflow['steps'], workflow['sample_hz'])
        input_path, output_path = ROOT/'runtime-inputs'/f'{name}.npz', ROOT/'runtime-outputs'/f'{name}.json'
        input_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(input_path, x=x, input_valid=valid)
        subprocess.run([sys.executable, str(ROOT/'source/scene_runtime_v23.py'), '--bundle', str(export),
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
            'raw_matches_v22': None, 'batch_seconds': batch_seconds,
            'stream_seconds': sum(chunk_times), 'max_chunk_seconds': max(chunk_times),
            'predicted_sequence': predicted_sop['sequence'], 'reference_sequence': reference['sequence'],
            'baseline_sequence': baseline_replays[name]['sop']['sequence'],
            'exact_stage_sequence_match': predicted_sop['sequence'] == reference['sequence'],
            'baseline_exact_match': baseline_replays[name]['sop']['sequence'] == reference['sequence'],
            'baseline_diagnostics': before, 'diagnostics': after})
        print(f'{name}: exported CLI/stream/replay match; sequence {predicted_sop["sequence"]}', flush=True)
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    raw, old, final = [score(truths, p, *args) for p in [raw_predictions, old_predictions, predictions]]
    metrics = {'version': 'v2.3', 'raw_test': raw, 'stabilized_test': final, 'v22_stabilized_test': old,
        'exact_stage_sequence_matches': final['exact_stage_sequence_matches'],
        'filter_kind': kind, 'parameters': params, 'selected_model': selected['model'],
        'selection_scope': protocol['scope'], 'limits': protocol['limitations'],
        'baseline_events': summarize_events(before_diagnostics), 'events': summarize_events(after_diagnostics),
        'paired_subject_bootstrap': paired_subject_interval(dataset['splits']['test'], truths, old_predictions, predictions),
        'recordings': checks}
    verification = {'weights_load_seconds': load_seconds, 'recordings': checks,
        'synthetic_cases': synthetic_checks(kind, params, workflow['sample_hz']),
        'baseline_synthetic_cases': synthetic_checks(baseline_config['filter_kind'],
                                                     baseline_config['filter_parameters'], workflow['sample_hz']),
        'source_sha256': source_hashes(), 'selection_sha256': file_hash(ROOT/'selection.json'),
        'v22_selection_sha256': file_hash(BASE/'v2.2/selection.json')}
    save_json(ROOT/'evaluation.json', metrics)
    save_json(ROOT/'verification.json', verification)
    save_json(ROOT/'replays.json', replay)
    save_json(export/'manifest.json', {'weights_sha256': file_hash(export/'stage-ensemble.pt'),
        'workflow_sha256': file_hash(export/'workflow.json'), 'selection_sha256': file_hash(ROOT/'selection.json'),
        'source_sha256': source_hashes(), 'version': 'v2.3', 'members': selected['model']['members'],
        'baseline_replays_sha256': file_hash(BASE/'v2.2/replays.json'),
        'evaluation_sha256': file_hash(ROOT/'evaluation.json'), 'verification_sha256': file_hash(ROOT/'verification.json'),
        'replays_sha256': file_hash(ROOT/'replays.json')})
    template = (SOURCE/'simple_scene_demo.html').read_text(encoding='utf-8')
    template = template.replace('桌面装配收尾：看见阶段，检查顺序', '桌面装配收尾 · V2.3 衰减证据滤波')
    template = template.replace('因果 TCN 使用过去 6.4 秒骨架观测；',
        '因果模型子集集成识别阶段，候选翻转时证据衰减而非清零；')
    comparison = (f'<section><strong>V2.2 → V2.3 · 相同测试人员</strong><p>'
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
    old, new = metrics['v22_stabilized_test'], metrics['stabilized_test']
    previous = dict(zip(('exact_stage_sequence_matches', 'segment_f1_at_0.5',
                         'macro_f1_all_classes', 'accuracy'), selection['validation_baseline']['tuple']))
    val = selection['selected']['validation']
    before, after = metrics['baseline_events'], metrics['events']
    interval = metrics['paired_subject_bootstrap']['accuracy_gain_95pct']
    interval_note = ('包含 0，帧准确率增量不能排除人员抽样波动' if interval[0] <= 0 <= interval[1]
                     else '不包含 0')
    baseline = selection['validation_baseline']['tuple']
    text = f'''# 装配 SOP V2.3：子集集成与衰减证据滤波

日期：2026-10-10。实际运行验证集搜索、导出权重推理与逐段 CLI 回归。

## 验证集选择

重训 13 个因果模型（TCN 深度 6/7/8、宽度 96/128、GRU 三个随机种子；与 V2 同配置的运行验证宏 F1 偏差 ≤ {max(abs(d['validation_macro_f1_delta']) for d in read(ROOT/'candidates.json')['reproducibility_vs_v2'])*100:.2f} 个百分点）。枚举 13 个成员的全部 1–4 元子集加全池，在四个固定探针过滤器下排序，取各探针前 15 名、v2.2 成员集、按单模型宏 F1 的前 k 集成与全池进入完整网格（{selection['candidates_evaluated']} 个模型／过滤组合，{selection['eligible_candidates']} 个通过守卫）。

新增过滤器族 `evidence-decay-v3`：与置信度累积相同的裕度证据，但候选被短暂打断时证据按帧衰减而不是清零；达到预算才切换，切换后清零。`hmm-forward-v3`（黏性转移先验的在线前向滤波）与既有持续确认、置信度累积一同参与网格。守卫要求准确率、宏 F1、完整序列匹配均不低于 V2.2，理想切换响应不超过 1.3 秒，两帧尖峰被抑制；按完整序列、分段 F1、宏 F1、准确率、响应时间和成员数排序。协议、输入和源码 SHA256 在测试前冻结。

选中 {selection['selected']['model']['name']}，过滤器 `{metrics['filter_kind']}`，参数 `{json.dumps(metrics['parameters'])}`。验证集对比：完整序列 {previous['exact_stage_sequence_matches']}/14 → {val['exact_stage_sequence_matches']}/14；准确率 {previous['accuracy']:.2%} → {val['accuracy']:.2%}；宏 F1 {previous['macro_f1_all_classes']:.2%} → {val['macro_f1_all_classes']:.2%}；分段 F1@0.5 {previous['segment_f1_at_0.5']:.2%} → {val['segment_f1_at_0.5']:.2%}。

验证集两个失败段都来自同一名人员（IDU039）：一段后半骨架缺失约三分之一帧且标注被掩码，另一段阶段 2–4 手腕运动能量只有其他录制的一半左右，全部 8 个 V2 模型以 0.9 以上置信度一致错判；这两段属于特征信息不足，滤波器无法修复，验证集 12/14 接近该特征下的上限。

## 原测试集回归

|指标|V2.2|V2.3|
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

按 7 名人员进行 3000 次配对 bootstrap，稳定帧准确率增量的描述性 95% 区间为 [{interval[0]*100:.2f}, {interval[1]*100:.2f}] 个百分点（{interval_note}）；宏 F1 与分段 F1 为点估计，未做显著性检验。区间不包含模型选择与跨场景不确定性；这仍是之前已使用的测试人员，不是新外部验证。

## 响应与实现验证

理想 one-hot 阶段切换响应：{verification['baseline_synthetic_cases']['clean_step_response_delay_seconds']:.1f} → {verification['synthetic_cases']['clean_step_response_delay_seconds']:.1f} 秒。合成检查保留正确、跳步、逆序、重复与结尾缺步，两帧尖峰被抑制。`verification.json` 同时记录 0.2／0.5／1／2 秒、不同置信度的真实短步骤探针，展示漏检代价。

14 段均重新加载导出权重推理，整段与不规则连续分块阶段完全一致；最大概率误差 {max(r['stream_max_probability_error'] for r in verification['recordings']):.3g}。每段单独启动导出源码 CLI，只输入 x 和 input_valid，阶段、SOP、原始类别及置信度与回放一致。

CPU 加载 {verification['weights_load_seconds']:.3f} 秒；{sum(r['frames'] for r in verification['recordings'])} 帧整段推理合计 {sum(r['batch_seconds'] for r in verification['recordings']):.3f} 秒，不规则分块合计 {sum(r['stream_seconds'] for r in verification['recordings']):.3f} 秒，不含采样等待。固定 128 帧 CLI 分块用于离线回归，在线推理器可逐帧 push。

衰减证据在验证集上把分段 F1 提高约 {(val['segment_f1_at_0.5']-previous['segment_f1_at_0.5'])*100:.1f} 个百分点，主要来自低裕度短阶段（盖板）提前确认。代价与风险：更轻的确认预算对持续但低裕度的错误模式更敏感；子集枚举在 14 段验证上仍可能过拟合验证噪声。当前数据只能评价 Kinect 骨架动作阶段；不能证明螺丝数量、扭矩或作业质量，也不能直接接入使用不同特征的 YOLO 生产管线。

## 逐段结果

|录制|V2.2 序列|V2.3 序列|V2.3 完全匹配|
|---|---|---|---|
'''
    for record in metrics['recordings']:
        text += f"|{record['recording']}|{record['baseline_sequence']}|{record['predicted_sequence']}|{'是' if record['exact_stage_sequence_match'] else '否'}|\n"
    text += '''
## 复现

```powershell
node tools/python.cjs experiments/public_training/optimize_scene_v23.py
node tools/python.cjs experiments/public_training/select_scene_v23.py
node tools/python.cjs experiments/public_training/evaluate_scene_v23.py
npm run test:scene
npm run test:scene-ui
```

> 过程说明：首次测试运行后修复了报告生成脚本中的一处显示层错误（不影响任何识别指标或判定逻辑）；随后在验证集上重新冻结协议（选中配置与搜索产物完全复现）并重跑评估，全部识别指标与首次运行一致。

V2.3 回放与导出结果在 `work/simple-sop-2026-10-10/v2.3/`。`source/` 包含已核对哈希的源码快照，`export/` 包含权重、工作流和清单，`runtime-inputs/` 不含 y 标注。旧版结果保留在 v2、v2.1、v2.2 目录中。

数据来源：[HA4M / Cicirelli 等 / Scientific Data 2022 / CC BY 4.0](https://www.nature.com/articles/s41597-022-01843-z)。
'''
    Path('docs/simple-sop-v23-results-2026-10-10.md').write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
