"""Select on validation, export a runnable bundle and all held-out skeleton replays."""
import json
from pathlib import Path
import shutil
import numpy as np
import torch
from common import classification, temporal_metrics, file_hash, save_json
from scene_runtime import infer, stabilize, check_sop

ROOT = Path('work/simple-sop-2026-10-10')


def main():
    torch.set_num_threads(4)
    dataset = json.loads((ROOT/'data/dataset.json').read_text(encoding='utf-8'))
    workflow = json.loads((ROOT/'workflow.json').read_text(encoding='utf-8'))
    reports = [json.loads(p.read_text()) for p in (ROOT/'models').glob('tcn-*/metrics.json')]
    chosen = max(reports, key=lambda r: r['validation']['macro_f1_all_classes'])
    source = ROOT/'models'/f'tcn-seed{chosen["seed"]}'/'best.pt'
    checkpoint = torch.load(source, map_location='cpu', weights_only=True)
    cache = {}
    for part in ('val', 'test'):
        for name in dataset['splits'][part]:
            path = ROOT/'data/prepared'/f'{name}.npz'
            record = next(r for r in dataset['records'] if r['recording'] == name)
            assert file_hash(path) == record['prepared_sha256']
            with np.load(path, allow_pickle=False) as data:
                cache[name] = {k: data[k] for k in data.files}
            cache[name]['prob'] = infer(checkpoint, cache[name]['x'])
    truth = [cache[n]['y'] for n in dataset['splits']['val']]
    candidates = []
    for threshold in workflow['threshold_candidates']:
        for hold in workflow['hold_candidates']:
            predicted = [stabilize(cache[n]['prob'], cache[n]['input_valid'], threshold, hold)
                         for n in dataset['splits']['val']]
            score = temporal_metrics(truth, predicted)['segment_f1_at_0.5']
            candidates.append({'threshold': threshold, 'hold': hold, 'validation_segment_f1_at_0.5': score})
    parameters = max(candidates, key=lambda p: p['validation_segment_f1_at_0.5'])
    workflow.update(parameters)
    workflow['confirmation_seconds'] = workflow['hold']/workflow['sample_hz']
    export = ROOT/'export'
    export.mkdir(exist_ok=True)
    shutil.copy2(source, export/'stage-tcn.pt')
    save_json(export/'workflow.json', workflow)
    save_json(export/'manifest.json', {'weights_sha256': file_hash(export/'stage-tcn.pt'),
        'dataset_sha256': file_hash(ROOT/'data/dataset.json'), 'seed': chosen['seed'],
        'checkpoint_selection': 'maximum validation macro F1 among three seeds',
        'postprocessing_selection': 'maximum validation segment F1@0.5 over declared 9 candidates',
        'postprocessing_candidates': candidates, 'feature_spec': checkpoint['feature_spec']})
    predicted, truth, replay, sop = [], [], [], []
    for name in dataset['splits']['test']:
        data = cache[name]
        stages = stabilize(data['prob'], data['input_valid'], workflow['threshold'], workflow['hold'])
        predicted.append(stages)
        truth.append(data['y'])
        observed = check_sop(stages, workflow['steps'])
        reference = check_sop(data['y'], workflow['steps'])
        sop.append({'recording': name, 'predicted': observed, 'annotation_reference': reference,
                    'exact_stage_sequence_match': observed['sequence'] == reference['sequence']})
        replay.append({'name': name, 'truth': data['y'].tolist(), 'prediction': stages.tolist(),
            'raw': data['prob'].argmax(1).tolist(),
            'confidence': data['prob'].max(1).round(3).tolist(),
            'joints': data['x'][:, :68].reshape(-1, 17, 4).round(3).tolist(),
            'valid': data['input_valid'].tolist(), 'sop': observed})
    y, pred = np.concatenate(truth), np.concatenate(predicted)
    stable = classification(y, pred, dataset['labels'])
    # Include abstentions explicitly; the shared five-class confusion matrix omits them.
    stable['unknown_predictions_on_scored_frames'] = int(((y >= 0) & (pred < 0)).sum())
    stable['confusion_unknown_column'] = [int(((y == i) & (pred < 0)).sum()) for i in range(5)]
    stable.update(temporal_metrics(truth, predicted))
    metrics = {'selected_seed': chosen['seed'], 'raw_test': chosen['test'], 'stabilized_test': stable,
        'parameters': parameters, 'sop_recordings': sop,
        'exact_stage_sequence_matches': sum(r['exact_stage_sequence_match'] for r in sop),
        'test_recordings': len(sop),
        'limits': 'No real anomaly ground truth. Rule cases are synthetic tests, not measured anomaly accuracy. Unknowns count as classification misses.'}
    save_json(ROOT/'evaluation.json', metrics)
    save_json(ROOT/'replays.json', replay)
    template = Path('experiments/public_training/simple_scene_demo.html').read_text(encoding='utf-8')
    payload = json.dumps({'workflow': workflow, 'metrics': metrics, 'records': replay}, ensure_ascii=False)
    (ROOT/'demo.html').write_text(template.replace('/*PAYLOAD*/', 'const DATA = '+payload+';'), encoding='utf-8')
    raw, smoothed = chosen['test'], stable
    report = f'''# 桌面装配收尾：四步 SOP 与时序识别

日期：2026-10-10。已实际训练并导出因果 TCN。流程：合拢组件 → 放置盖板 → 放置螺丝 → 使用工具操作并归位；前八类装配动作映射为“其他作业”，未知标签仍掩码。

## 数据与协议

HA4M 82 段录制，41 人；沿用按人员隔离的 27/7/7 人、54/14/14 段 train/val/test。保留全部录制，81 段在有效标注中包含四个目标阶段；未剔除不完整序列。使用真实 Kinect 骨架，10 Hz、74 维、64 点历史窗口。归一化和类别权重仅来自训练集。不是原始 RGB 视频模型。

TCN 三个种子 2026/2027/2028，最多 25 epoch、验证宏 F1 早停 7 轮。按验证宏 F1 选择种子 {chosen['seed']}（epoch {chosen['best_epoch']}）；逐帧 MLP 单种子作为基线。稳定化在预先固定的 9 组参数中按验证分段 F1@0.5 选择，不按测试表现选择。模型原始帧准确率三种子平均 75.73%，宏 F1 平均 68.02%（种子标准差 2.17 个百分点）；MLP 宏 F1 55.78%。

## 导出模型的独立测试结果

|指标|原始 TCN|因果稳定化|
|---|---:|---:|
|帧准确率|{raw['accuracy']:.2%}|{smoothed['accuracy']:.2%}|
|宏 F1|{raw['macro_f1_all_classes']:.2%}|{smoothed['macro_f1_all_classes']:.2%}|
|分段 F1@0.5|{raw['segment_f1_at_0.5']:.2%}|{smoothed['segment_f1_at_0.5']:.2%}|
|Edit|{raw['edit_mean']:.2%}|{smoothed['edit_mean']:.2%}|

稳定化：置信度 ≥ {workflow['threshold']}、连续 {workflow['hold']} 点才确认新阶段，不回填过去。无效或低置信度输入输出未知；未知在有真值帧上按漏识别计分。SOP 不强制修正模型顺序，也不输入真值。忽略其他作业与未知间隔，合并连续同阶段证据，检查先后顺序、跨阶段后重复和结束时未观察到的步骤。它不测量单次动作次数。

测试录制中，识别出的阶段序列与标注阶段序列完全一致为 **{metrics['exact_stage_sequence_matches']}/{len(sop)}**。这是上游识别加规则的序列一致率，不是实际异常检测准确率。此数据没有独立的漏装、错装或质量缺陷标签；规则的漏步、换序、重复、未完成测试来自明确标为人工构造的测试用例。

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
'''
    Path('docs/simple-sop-results-2026-10-10.md').write_text(report, encoding='utf-8')
    print(json.dumps({'seed': chosen['seed'], 'parameters': parameters, 'accuracy': stable['accuracy'],
                      'macro_f1': stable['macro_f1_all_classes'], 'segment_f1': stable['segment_f1_at_0.5'],
                      'exact_sequences': metrics['exact_stage_sequence_matches']}, indent=2))


if __name__ == '__main__':
    main()
