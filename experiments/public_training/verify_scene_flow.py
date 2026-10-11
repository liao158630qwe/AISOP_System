"""Select SOP filtering on validation, then verify frozen weights through CLI and replay.

Run --select before --evaluate. Neither selection nor its candidate ranking reads test
arrays or test metrics. V2 artifacts are preserved; outputs are versioned as v2.1.
"""
import argparse
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
import torch

from common import classification, file_hash, save_json, temporal_metrics
from scene_runtime import check_sop
from scene_v2 import DensePredictor, StageFilter, filter_stages

BASE = Path('work/simple-sop-2026-10-10')
OLD = BASE/'v2'
ROOT = BASE/'v2.1'
SOURCE = Path('experiments/public_training')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def source_hashes():
    return {name: file_hash(SOURCE/name) for name in ['scene_v2.py', 'scene_runtime.py', 'verify_scene_flow.py']}


def score(truths, predictions, labels, steps, hz):
    result = classification(np.concatenate(truths), np.concatenate(predictions), labels)
    result.update(temporal_metrics(truths, predictions))
    pairs = [(check_sop(y, steps, hz), check_sop(p, steps, hz)) for y, p in zip(truths, predictions)]
    result['exact_stage_sequence_matches'] = sum(a['sequence'] == b['sequence'] for a, b in pairs)
    result['recordings'] = len(pairs)
    result['stage_events'] = sum(len(b['events']) for a, b in pairs)
    result['reference_stage_events'] = sum(len(a['events']) for a, b in pairs)
    y, p = np.concatenate(truths), np.concatenate(predictions)
    result['unknown_predictions_on_scored_frames'] = int(((y >= 0) & (p < 0)).sum())
    result['confusion_unknown_column'] = [int(((y == i) & (p < 0)).sum()) for i in range(len(labels))]
    return result


def select():
    dataset = read(BASE/'data/dataset.json')
    model = read(OLD/'selection.json')['model']
    workflow = read(OLD/'export/workflow.json')
    protocol = {
        'selection_scope': 'validation arrays and validation predictions only; no test metrics used',
        'model': model['name'], 'members': model['members'],
        'dataset_sha256': file_hash(BASE/'data/dataset.json'),
        'weights_sha256': file_hash(OLD/'export/stage-ensemble.pt'),
        'v2_selection_sha256': file_hash(OLD/'selection.json'),
        'source_sha256': source_hashes(),
        'grid': {'alpha': [1., .3, .1, .05], 'hold': [1, 5, 10, 20], 'threshold': [0., .4, .55]},
        'guard': 'validation accuracy and macro F1 must each be >= raw baseline minus 0.03',
        'ranking': 'exact stage sequences, then segment F1@0.5, then macro F1; no SOP-order constraint',
        'test_policy': 'Previously reported held-out subjects; final regression check, not a fresh benchmark',
    }
    save_json(ROOT/'protocol.json', protocol)
    names = dataset['splits']['val']
    probabilities, truths, validity = [], [], []
    inputs = {}
    for name in names:
        path = BASE/'data/prepared'/f'{name}.npz'
        expected = next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        assert file_hash(path) == expected
        with np.load(path, allow_pickle=False) as data:
            truths.append(data['y'])
            validity.append(data['input_valid'])
        members = []
        for member in model['members']:
            path = OLD/'models'/member/'validation-predictions.npz'
            inputs[str(path)] = file_hash(path)
            with np.load(path, allow_pickle=False) as data:
                members.append(data[name])
        probabilities.append(np.mean(members, axis=0))
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    baseline = score(truths, [np.where(v, p.argmax(1), -1) for p, v in zip(probabilities, validity)], *args)
    candidates = []
    for alpha, hold, threshold in itertools.product(*protocol['grid'].values()):
        parameters = {'alpha': alpha, 'hold': hold, 'threshold': threshold}
        predictions = [filter_stages(p, v, **parameters) for p, v in zip(probabilities, validity)]
        metrics = score(truths, predictions, *args)
        eligible = (metrics['accuracy'] >= baseline['accuracy']-.03
                    and metrics['macro_f1_all_classes'] >= baseline['macro_f1_all_classes']-.03)
        candidates.append({'parameters': parameters, 'validation': metrics, 'eligible': eligible})
    selected = max((c for c in candidates if c['eligible']), key=lambda c: (
        c['validation']['exact_stage_sequence_matches'], c['validation']['segment_f1_at_0.5'],
        c['validation']['macro_f1_all_classes']))
    save_json(ROOT/'filter-search.json', candidates)
    selection = {'protocol_sha256': file_hash(ROOT/'protocol.json'), 'validation_inputs_sha256': inputs,
                 'baseline': baseline, 'selected': selected, 'frozen_before_test': True}
    save_json(ROOT/'selection.json', selection)
    print(json.dumps({'parameters': selected['parameters'], 'validation_exact': selected['validation']['exact_stage_sequence_matches'],
                      'validation_accuracy': selected['validation']['accuracy'],
                      'validation_macro_f1': selected['validation']['macro_f1_all_classes']}, indent=2), flush=True)


def synthetic_checks(params, hz):
    cases = {'ordered': [1, 2, 3, 4], 'skip': [1, 3, 4], 'swap': [1, 3, 2, 4],
             'repeat': [1, 2, 1, 3, 4], 'missing_end': [1, 2, 3]}
    results = {}
    for name, sequence in cases.items():
        p = np.eye(5)[np.repeat(sequence, 100)]
        stages = filter_stages(p, np.ones(len(p)), **params)
        sop = check_sop(stages, [1, 2, 3, 4], hz)
        assert sop['sequence'] == sequence
        assert sop['observed_in_order'] == (name == 'ordered')
        results[name] = sop
    p = np.eye(5)[[1]*100+[3]*2+[1]*100]
    assert check_sop(filter_stages(p, np.ones(len(p)), **params), [1, 2, 3, 4])['sequence'] == [1]
    results['two_frame_spike_suppressed'] = True
    p = np.eye(5)[[1]*100+[2]*100]
    stages = filter_stages(p, np.ones(len(p)), **params)
    results['clean_step_response_delay_seconds'] = (int(np.flatnonzero(stages == 2)[0])-100)/hz
    return results


def evaluate():
    torch.set_num_threads(4)
    selection, protocol = read(ROOT/'selection.json'), read(ROOT/'protocol.json')
    assert selection['protocol_sha256'] == file_hash(ROOT/'protocol.json')
    assert protocol['source_sha256'] == source_hashes(), 'Code changed after selection; re-freeze on validation'
    assert protocol['dataset_sha256'] == file_hash(BASE/'data/dataset.json')
    assert protocol['weights_sha256'] == file_hash(OLD/'export/stage-ensemble.pt')
    dataset = read(BASE/'data/dataset.json')
    workflow = read(OLD/'export/workflow.json')
    params = selection['selected']['parameters']
    workflow['filter_parameters'] = params
    export = ROOT/'export'
    export.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OLD/'export/stage-ensemble.pt', export/'stage-ensemble.pt')
    save_json(export/'workflow.json', workflow)
    started = time.perf_counter()
    checkpoint = torch.load(export/'stage-ensemble.pt', map_location='cpu', weights_only=True)
    predictor = DensePredictor(checkpoint['members'])
    load_seconds = time.perf_counter()-started
    truths, predictions, raw_predictions, replay, checks = [], [], [], [], []
    old_replay = {r['name']: r for r in read(OLD/'replays.json')}
    old_predictions = []
    for name in dataset['splits']['test']:
        path = BASE/'data/prepared'/f'{name}.npz'
        expected = next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
        assert file_hash(path) == expected
        with np.load(path, allow_pickle=False) as data:
            values = {key: data[key] for key in data.files}
        x, valid, truth = values['x'], values['input_valid'], values['y']
        predictor.reset()
        started = time.perf_counter()
        probability = predictor.push(x)
        batch_seconds = time.perf_counter()-started
        predictor.reset()
        post = StageFilter(**params)
        probabilities, stages, chunk_times = [], [], []
        # Irregular chunks cross both stage changes and model receptive-field limits.
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
        batch_stages = filter_stages(probability, valid, **params)
        assert np.array_equal(stages, batch_stages), name
        assert probability.argmax(1).tolist() == old_replay[name]['raw'], 'Raw model behavior changed'
        predicted_sop = check_sop(stages, workflow['steps'], workflow['sample_hz'])
        reference = check_sop(truth, workflow['steps'], workflow['sample_hz'])
        # Run the actual CLI in a clean process for EVERY recording; no y/annotation input.
        input_path = ROOT/'runtime-inputs'/f'{name}.npz'
        output_path = ROOT/'runtime-outputs'/f'{name}.json'
        input_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(input_path, x=x, input_valid=valid)
        subprocess.run([sys.executable, str(SOURCE/'scene_runtime.py'), '--bundle', str(export),
                        '--input', str(input_path), '--output', str(output_path), '--chunk-size', '128'],
                       check=True, capture_output=True, text=True)
        standalone = read(output_path)
        assert standalone['stages'] == stages and standalone['sop'] == predicted_sop
        assert standalone['raw'] == probability.argmax(1).tolist()
        assert np.allclose(standalone['confidence'], probability.max(1), atol=1e-6)
        truths.append(truth)
        predictions.append(stages)
        raw_predictions.append(probability.argmax(1))
        old_predictions.append(old_replay[name]['prediction'])
        record = {'name': name, 'truth': truth.tolist(), 'prediction': stages,
            'raw': probability.argmax(1).tolist(), 'confidence': probability.max(1).round(3).tolist(),
            'joints': x[:, :68].reshape(-1, 17, 4).round(3).tolist(),
            'valid': valid.tolist(), 'sop': predicted_sop}
        replay.append(record)
        checks.append({'recording': name, 'frames': len(x), 'stream_max_probability_error': error,
            'batch_stream_stage_and_sop_match': True, 'standalone_cli_matches': True,
            'raw_matches_v2': True, 'batch_seconds': batch_seconds,
            'stream_seconds': sum(chunk_times), 'max_chunk_seconds': max(chunk_times),
            'predicted_sequence': predicted_sop['sequence'], 'reference_sequence': reference['sequence'],
            'exact_stage_sequence_match': predicted_sop['sequence'] == reference['sequence']})
        print(f'{name}: CLI/stream/replay match; sequence {predicted_sop["sequence"]}', flush=True)
    args = (dataset['labels'], workflow['steps'], workflow['sample_hz'])
    raw = score(truths, raw_predictions, *args)
    old = score(truths, old_predictions, *args)
    final = score(truths, predictions, *args)
    metrics = {'version': 'v2.1', 'raw_test': raw, 'stabilized_test': final, 'v2_stabilized_test': old,
               'exact_stage_sequence_matches': final['exact_stage_sequence_matches'],
               'parameters': params, 'selection_scope': protocol['selection_scope'],
               'limits': protocol['test_policy'], 'recordings': checks}
    verification = {'weights_load_seconds': load_seconds, 'recordings': checks,
                    'synthetic_cases': synthetic_checks(params, workflow['sample_hz']),
                    'source_sha256': source_hashes(), 'selection_sha256': file_hash(ROOT/'selection.json')}
    save_json(ROOT/'evaluation.json', metrics)
    save_json(ROOT/'verification.json', verification)
    save_json(ROOT/'replays.json', replay)
    save_json(export/'manifest.json', {'weights_sha256': file_hash(export/'stage-ensemble.pt'),
        'workflow_sha256': file_hash(export/'workflow.json'), 'selection_sha256': file_hash(ROOT/'selection.json'),
        'source_sha256': source_hashes(), 'version': 'v2.1', 'weights_unchanged_from_v2': True})
    template = (SOURCE/'simple_scene_demo.html').read_text(encoding='utf-8')
    template = template.replace('桌面装配收尾：看见阶段，检查顺序', '桌面装配收尾 · V2.1 全流程优化')
    template = template.replace('因果 TCN 使用过去 6.4 秒骨架观测；',
        '三个因果模型识别阶段，验证集选择 EMA 平滑参数；')
    comparison = (f'<section><strong>流程优化 · 相同测试人员</strong><p>'
        f'完整阶段序列匹配：{old["exact_stage_sequence_matches"]}/14 → {final["exact_stage_sequence_matches"]}/14'
        f'　｜　分段 F1@0.5：{old["segment_f1_at_0.5"]:.2%} → {final["segment_f1_at_0.5"]:.2%}<br>'
        f'原始帧准确率 {raw["accuracy"]:.2%}；稳定输出帧准确率 {final["accuracy"]:.2%}。'
        f'平滑参数 α={params["alpha"]}、连续确认 {params["hold"]} 帧、阈值 {params["threshold"]}。'
        f'理想单步切换延迟 {verification["synthetic_cases"]["clean_step_response_delay_seconds"]:.1f} 秒；短步骤可能被抑制。</p></section>')
    template = template.replace('<section><select id="record"', comparison+'<section><select id="record"')
    html = template.replace('/*PAYLOAD*/', 'const DATA = '+json.dumps(
        {'workflow': workflow, 'metrics': metrics, 'records': replay}, ensure_ascii=False)+';')
    (ROOT/'demo.html').write_text(html, encoding='utf-8')
    if not (BASE/'demo-v2.html').exists():
        shutil.copy2(OLD/'demo.html', BASE/'demo-v2.html')
    (BASE/'demo.html').write_text(html, encoding='utf-8')
    write_report(selection, metrics, verification)
    print(json.dumps({'raw_accuracy': raw['accuracy'], 'stable_accuracy': final['accuracy'],
        'stable_segment_f1': final['segment_f1_at_0.5'], 'exact_sequences': final['exact_stage_sequence_matches'],
        'cli_recordings_passed': len(checks)}, indent=2), flush=True)


def write_report(selection, metrics, verification):
    old, new = metrics['v2_stabilized_test'], metrics['stabilized_test']
    val = selection['selected']['validation']
    text = f'''# 装配 SOP 全流程测试与优化

日期：2026-10-10。V2.1 保留 V2 的三个模型权重，仅调整经验证集选择的阶段过滤，并补充持续推理、输入校验及回放交互。

## 实测结果

|指标|V2|V2.1|
|---|---:|---:|
|原始帧准确率|{metrics['raw_test']['accuracy']:.2%}|{metrics['raw_test']['accuracy']:.2%}|
|稳定输出帧准确率|{old['accuracy']:.2%}|{new['accuracy']:.2%}|
|稳定输出宏 F1|{old['macro_f1_all_classes']:.2%}|{new['macro_f1_all_classes']:.2%}|
|稳定输出分段 F1@0.5|{old['segment_f1_at_0.5']:.2%}|{new['segment_f1_at_0.5']:.2%}|
|完整阶段序列与标注一致|{old['exact_stage_sequence_matches']}/14|{new['exact_stage_sequence_matches']}/14|
|检测阶段事件总数|{old['stage_events']}|{new['stage_events']}|

标注共 {new['reference_stage_events']} 个阶段事件。事件数量下降不等于已证实异常误报率下降；数据没有独立异常标签。

## 根因与修复

1. V2 按帧级宏 F1 选过滤参数，最终 alpha=1、hold=1、threshold=0，逐帧抖动直接形成 SOP 乱序/重复。沿用原先 48 组候选，只读验证集；准确率和宏 F1 各最多下降 3 个百分点，再优先选完整序列匹配，其次分段 F1@0.5。冻结参数 {json.dumps(metrics['parameters'])}，验证序列匹配 {val['exact_stage_sequence_matches']}/14，验证准确率 {val['accuracy']:.2%}、宏 F1 {val['macro_f1_all_classes']:.2%}。不限制步骤前进方向。
2. 新增可复用推理器，权重只加载一次，TCN 缓存有限历史，GRU 保留隐藏状态；录制切换显式 reset。批量与不规则分块结果核对，过滤器也跨块保留 EMA 和确认状态。
3. 拒绝长度不匹配、二维有效性掩码、非二值掩码、非有限概率、非归一化概率、非整数 hold、非法采样率，避免 zip 静默截断。无效观测清空过滤证据并输出未知，低置信度不生成阶段完成。
4. 回放按配置采样率计算时间，拖动/重置/换录制会暂停并清空计时余量；事件仅显示当前时刻以前的证据；时序图按录制缓存，避免每帧重画全部样本。

## 验证与代价

14 段测试录制均从导出权重重新推理；每段在独立 Python CLI 进程加载无 y 标签的 NPZ，与整段推理、不规则分块和最终回放逐项核对，阶段和 SOP 完全一致。原始类别预测逐帧与 V2 一致。最大分块概率误差 {max(r['stream_max_probability_error'] for r in verification['recordings']):.3g}。

合成输入覆盖正确顺序、跳步、逆序、重复、结尾缺步，均保留预期序列；两帧尖峰不再生成虚假阶段。理想 one-hot 从阶段 1 切换到 2 的响应延迟为 {verification['synthetic_cases']['clean_step_response_delay_seconds']:.1f} 秒。这是过滤器的合成响应测试，不是实测模型端到端延迟。短暂真实步骤也可能被抑制，不能据此保证短时异常召回。

CPU 权重加载 {verification['weights_load_seconds']:.3f} 秒；14 段共 {sum(r['frames'] for r in verification['recordings'])} 帧，整段推理合计 {sum(r['batch_seconds'] for r in verification['recordings']):.3f} 秒，不规则分块合计 {sum(r['stream_seconds'] for r in verification['recordings']):.3f} 秒。分块耗时不包含等待采样数据的时间；CLI 128 帧仅用于离线回归，在线可逐帧 push。

这仍是此前已评估的 HA4M 人员隔离测试集，不是全新外部验证。输出只代表骨架动作阶段和观察顺序，不能验证螺丝数量、扭矩或装配质量。生产 YOLO 视频管线未被此 Kinect 特征模型替换。

## 复现

```powershell
node tools/python.cjs -m pytest experiments/public_training/test_invariants.py experiments/public_training/test_simple_scene.py experiments/public_training/test_scene_v2.py -q
node tools/python.cjs experiments/public_training/verify_scene_flow.py --select
node tools/python.cjs experiments/public_training/verify_scene_flow.py --evaluate
node tools/python.cjs experiments/public_training/scene_runtime.py --bundle work/simple-sop-2026-10-10/v2.1/export --input work/simple-sop-2026-10-10/v2.1/runtime-inputs/IDU002V001.npz --output work/simple-sop-2026-10-10/v2.1/example-inference.json
```

回放：`work/simple-sop-2026-10-10/demo.html`；旧版：`demo-v1.html`、`demo-v2.html`。候选明细、冻结选择、数据及源码哈希、每段实测记录和 CLI 输出保存在 `work/simple-sop-2026-10-10/v2.1/`。

## 每段检查

|录制|标注阶段序列|识别阶段序列|完全匹配|独立 CLI|
|---|---|---|---|---|
'''
    for record in verification['recordings']:
        text += f"|{record['recording']}|{record['reference_sequence']}|{record['predicted_sequence']}|{'是' if record['exact_stage_sequence_match'] else '否'}|通过|\n"
    Path('docs/simple-sop-flow-results-2026-10-10.md').write_text(text, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--select', action='store_true')
    mode.add_argument('--evaluate', action='store_true')
    args = parser.parse_args()
    select() if args.select else evaluate()
