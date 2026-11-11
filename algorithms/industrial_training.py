"""Additional industrial dataset checks and explicitly scoped candidate gates."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageOps

from .training import audit_dataset, SPLITS


def industrial_audit(recipe, station, policy):
    manifest = Path(recipe['dataset']).resolve()
    dataset = json.loads(manifest.read_text(encoding='utf-8'))
    for sample in dataset.get('samples', []):
        keys = ('file',) if recipe['task'] in ('classify', 'feature-classify') else ('file', 'label')
        for key in keys:
            path = (manifest.parent / sample[key]).resolve()
            if not path.is_relative_to(manifest.parent):
                raise ValueError(f'{sample.get("id")}: 数据文件必须位于清单所在目录内')
    audit = audit_dataset(recipe['dataset'], recipe['task'])
    data, root = audit['dataset'], Path(audit['root'])
    fields = ('station', 'product', 'camera', 'batch', 'cycle', 'lighting', 'occlusion')
    ownership, pixels, dimensions = {}, {}, set()
    groups = {split: set() for split in SPLITS}
    conditions = {split: Counter() for split in SPLITS}
    negative = Counter()
    by_id = {sample['id']: sample for sample in data['samples']}
    for sample in data['samples']:
        identity, split = sample['id'], sample['split']
        for field in fields:
            if not isinstance(sample.get(field), str) or not sample[field].strip():
                raise ValueError(f'{identity}: 缺少工业采集信息 {field}')
        if sample['station'] != station:
            raise ValueError(f'{identity}: 样本工位与训练任务不一致')
        for key in ('batch', 'cycle'):
            old = ownership.setdefault((key, sample[key]), split)
            if old != split:
                raise ValueError(f'{identity}: {key} 跨集合，完整批次与工件周期必须隔离')
        groups[split].add(sample['batch'])
        conditions[split][sample['lighting'] + ' / ' + sample['occlusion']] += 1
        if split != 'train' and (sample.get('synthetic') or sample.get('parent_ids')):
            raise ValueError(f'{identity}: 验证与测试仅接受独立原始采集样本')
        seen, pending = set(), list(sample.get('parent_ids', []))
        while pending:
            parent = pending.pop()
            if parent == identity:
                raise ValueError(f'{identity}: 派生样本存在循环引用')
            if parent in seen:
                continue
            seen.add(parent)
            pending.extend(by_id[parent].get('parent_ids', []))
        path = (root / sample['file']).resolve()
        if recipe['task'] == 'feature-classify':
            import numpy as np
            value = np.load(path, allow_pickle=False)
            if value.ndim != 1 or not np.isfinite(value).all():
                raise ValueError(f'{identity}: 特征必须为有限的一维向量')
            dimensions.add(value.shape[0])
        else:
            with Image.open(path) as picture:
                rgb = ImageOps.exif_transpose(picture).convert('RGB')
                if min(rgb.size) < 32:
                    raise ValueError(f'{identity}: 图像边长不足32像素')
                digest = hashlib.sha256(str(rgb.size).encode() + rgb.tobytes()).hexdigest()
            old = pixels.setdefault(digest, split)
            if old != split:
                raise ValueError(f'{identity}: 解码后的相同图像跨集合，不能通过改文件格式绕过隔离')
        if recipe['task'] not in ('classify', 'feature-classify'):
            if not sample.get('synthetic') and not sample.get('parent_ids') and not (root / sample['label']).read_text(encoding='utf-8').strip():
                negative[split] += 1
    if len(dimensions) > 1:
        raise ValueError('特征维度不一致')
    for split in SPLITS:
        if len(groups[split]) < policy['min_batches_per_split']:
            raise ValueError(f'{split}: 独立批次不足，需要至少 {policy["min_batches_per_split"]} 个')
        counts = audit['class_samples'][split]
        for i, name in enumerate(data['names']):
            if counts.get(i, counts.get(str(i), 0)) < policy['min_class_samples']:
                raise ValueError(f'{split}: 类别 {name} 的样本不足，需要至少 {policy["min_class_samples"]} 个')
        if recipe['task'] not in ('classify', 'feature-classify') and negative[split] < policy['min_negative_samples']:
            raise ValueError(f'{split}: 缺少真实空背景/难负样本，需要至少 {policy["min_negative_samples"]} 个')
    return dict(sha256=audit['sha256'], counts=audit['counts'], names=data['names'],
                class_samples=audit['class_samples'], batches={s: len(v) for s, v in groups.items()},
                conditions={s: dict(v) for s, v in conditions.items()}, negatives=dict(negative),
                isolation_keys=audit['isolation_keys'] + ['batch', 'cycle'],
                scope='来源、批次、工件周期及解码内容隔离；不保证人为填写的采集信息真实')


def candidate_gate(evaluation, policy):
    """This is a model-level gate, never a product false-acceptance claim."""
    rows = evaluation.get('per_class') or evaluation.get('metrics', {}).get('per_class', [])
    expected = evaluation.get('class_names', [])
    failures = []
    if evaluation.get('split') != 'test':
        failures.append('准入只接受独立测试集结果')
    if not expected or len(rows) != len(expected) or {r.get('id') for r in rows} != set(range(len(expected))):
        failures.append('缺少完整的逐类别指标，不能用总体准确率代替')
    for item in rows:
        support = item.get('support')
        if isinstance(support, bool) or not isinstance(support, (int, float)) or not math.isfinite(support) or not support >= policy['min_class_samples']:
            failures.append(f'{item.get("name", item.get("id"))}: 独立测试支持数不足')
        for metric in ('precision', 'recall'):
            value = item.get(metric)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not policy['min_' + metric] <= value <= 1:
                failures.append(f'{item.get("name", item.get("id"))}: {metric} 未达到冻结门槛')
    return dict(passed=not failures, failures=failures, thresholds=policy,
                production_qualified=False,
                scope='仅模型独立测试集的类别精确率/召回率准入；工件误放行率、漏检率与现场端到端时延须另行验收')
