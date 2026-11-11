"""Scene-independent training contracts and auditable, isolated datasets.

Adapters own model-specific preprocessing. Training never selects on test data
and never changes a production scene's configuration or weights.
"""
from collections import Counter
import copy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import shutil
import time

from .video import file_hash


SPLITS = ('train', 'val', 'test')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _path(root, value):
    return (root / value).resolve()


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _labels(record, task, names, root, dataset):
    """Validate the labels consumed by the supported trainers, including negatives."""
    if task in ('classify', 'feature-classify'):
        label = record.get('label')
        if not _integer(label) or label >= len(names):
            raise ValueError(f'{record["id"]}: label must be a class index')
        return [label]
    path = _path(root, record['label'])
    if not path.is_file():
        raise ValueError(f'Missing label: {path}; negative images need an explicit empty label file')
    found = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        fields = [float(v) for v in line.split()]
        cls, coords = fields[0], fields[1:]
        if not all(math.isfinite(v) for v in fields) or not cls.is_integer() or not 0 <= cls < len(names):
            raise ValueError(f'{path}: invalid class or nonfinite coordinates')
        if task == 'pose':
            shape = dataset.get('kpt_shape', [])
            if len(shape) != 2 or not _integer(shape[0], 1) or shape[1] not in (2, 3):
                raise ValueError('pose requires kpt_shape=[keypoint_count, 2 or 3]')
            if len(coords) != 4 + shape[0] * shape[1]:
                raise ValueError(f'{path}: incorrect pose label shape')
            normalized = coords[:4] + [v for i, v in enumerate(coords[4:]) if i % shape[1] < 2]
            if shape[1] == 3 and any(v not in (0, 1, 2) for v in coords[6::3]):
                raise ValueError(f'{path}: invalid keypoint visibility')
        else:
            normalized = coords
            valid = ((task == 'detect' and len(coords) == 4) or
                     (task == 'segment' and len(coords) >= 6 and len(coords) % 2 == 0) or
                     (task == 'obb' and len(coords) == 8))
            if not valid:
                raise ValueError(f'{path}: label does not match task {task}')
        if any(not 0 <= v <= 1 for v in normalized):
            raise ValueError(f'{path}: coordinates must be normalized to [0,1]')
        if task in ('detect', 'pose') and (coords[2] <= 0 or coords[3] <= 0):
            raise ValueError(f'{path}: box size must be positive')
        found.append(int(cls))
    return found


def audit_dataset(manifest_path, task):
    """Hash and validate provenance; this does not infer on or tune against test."""
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    dataset = json.loads(manifest_path.read_text(encoding='utf-8'))
    names, records = dataset.get('names'), dataset.get('samples')
    if dataset.get('schema_version') != 1 or not isinstance(names, list) or not names:
        raise ValueError('dataset requires schema_version=1 and nonempty names')
    if any(not isinstance(n, str) or not n.strip() for n in names) or len(set(names)) != len(names):
        raise ValueError('class names must be unique, nonempty strings')
    if not isinstance(records, list) or not records:
        raise ValueError('dataset requires samples')
    extra_keys = dataset.get('isolation_keys', [])
    if not isinstance(extra_keys, list) or any(not isinstance(k, str) or not k for k in extra_keys):
        raise ValueError('isolation_keys must be a list of field names')
    isolation = list(dict.fromkeys(['source', 'group'] + extra_keys))
    ownership, digests, paths, identities = {}, {}, {}, set()
    counts = {split: Counter() for split in SPLITS}
    split_counts, audited = Counter(), []
    for record in records:
        identity, split = record.get('id'), record.get('split')
        if not isinstance(identity, str) or not identity or identity in identities or split not in SPLITS:
            raise ValueError('sample ids must be unique and split must be train/val/test')
        identities.add(identity)
        for key in isolation:
            value = record.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{identity}: required isolation key {key}')
            old = ownership.setdefault((key, value), split)
            if old != split:
                raise ValueError(f'Data leakage: {key}={value} crosses {old}/{split}')
        if record.get('synthetic') and split != 'train':
            raise ValueError(f'{identity}: synthetic samples are train-only')
        sample_path = _path(root, record['file'])
        digest = file_hash(sample_path)
        old = paths.setdefault(str(sample_path), split)
        if old != split:
            raise ValueError(f'Data leakage: identical content path crosses {old}/{split}: {identity}')
        # Different independent frames may legitimately produce the exact same
        # low-dimensional feature vector. Its hash proves integrity, not leakage.
        if task != 'feature-classify':
            old = digests.setdefault(digest, split)
            if old != split:
                raise ValueError(f'Data leakage: identical content crosses {old}/{split}: {identity}')
        labels = _labels(record, task, names, root, dataset)
        counts[split].update(set(labels))
        split_counts[split] += 1
        item = dict(record, sha256=digest)
        if task not in ('classify', 'feature-classify'):
            item['label_sha256'] = file_hash(_path(root, record['label']))
        audited.append(item)
    if any(split_counts[s] == 0 for s in SPLITS):
        raise ValueError('train, val and independent test must all be nonempty')
    # A global accuracy cannot validate a class that was never represented.
    missing = {s: [names[i] for i in range(len(names)) if counts[s][i] == 0] for s in SPLITS}
    if any(missing.values()):
        raise ValueError(f'Every class needs real val/test and training support: {missing}')
    by_id = {record['id']: record for record in records}
    for record in records:
        parents = record.get('parent_ids', [])
        if not isinstance(parents, list) or (record.get('synthetic') and not parents):
            raise ValueError(f'{record["id"]}: derived samples require parent_ids')
        for parent in parents:
            if parent == record['id'] or parent not in by_id or by_id[parent]['split'] != record['split']:
                raise ValueError(f'Data leakage or invalid parent: {record["id"]} -> {parent}')
    canonical = dict(dataset, samples=audited)
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return dict(dataset=canonical, root=str(root), sha256=digest, counts=dict(split_counts),
                class_samples={s: dict(counts[s]) for s in SPLITS}, isolation_keys=isolation)


def validate_recipe(recipe, base):
    recipe = copy.deepcopy(recipe)
    base = Path(base).resolve()
    if not isinstance(recipe.get('train', {}), dict):
        raise ValueError('train must be an object of hyperparameters')
    adapter = ADAPTERS.get(recipe.get('backend'))
    if adapter is None or recipe.get('task') not in adapter.tasks:
        raise ValueError('Unsupported backend/task; register an explicit training adapter')
    recipe['dataset'] = str(_path(base, recipe['dataset']))
    if recipe.get('pretrained'):
        recipe['pretrained'] = str(_path(base, recipe['pretrained']))
        if not Path(recipe['pretrained']).is_file():
            raise ValueError('pretrained must be an existing local model; no implicit downloads')
    recipe.setdefault('seed', 42)
    if not _integer(recipe['seed']):
        raise ValueError('seed must be a nonnegative integer')
    adapter.validate(recipe)
    return recipe, adapter


def load_recipe(path):
    path = Path(path).resolve()
    return validate_recipe(json.loads(path.read_text(encoding='utf-8')), path.parent)


def _checked_copy(source, destination, expected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if file_hash(destination) != expected:
        raise ValueError(f'Dataset changed after audit: {source}')


def prepare_yolo(audit, output, task, splits):
    """Materialize only explicitly selected splits; training YAML has no test key."""
    import yaml
    dataset, root = audit['dataset'], Path(audit['root'])
    output = Path(output).resolve()
    for split in splits:
        if task == 'classify':
            for i in range(len(dataset['names'])):
                (output / split / f'{i:06d}').mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(dataset['samples']):
        split = record['split']
        if split not in splits:
            continue
        source = _path(root, record['file'])
        stem = f'{index:09d}'
        if task == 'classify':
            destination = output / split / f'{record["label"]:06d}' / (stem + source.suffix)
        else:
            destination = output / 'images' / split / (stem + source.suffix)
            _checked_copy(_path(root, record['label']), output / 'labels' / split / (stem + '.txt'), record['label_sha256'])
        _checked_copy(source, destination, record['sha256'])
    if task == 'classify':
        return str(output)
    config = dict(path=output.as_posix(), names=dataset['names'])
    config.update({s: f'images/{s}' for s in splits})
    for key in ('kpt_shape', 'flip_idx'):
        if key in dataset:
            config[key] = dataset[key]
    path = output / 'data.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return str(path)


def _metrics(metrics):
    return {k: float(v) for k, v in metrics.results_dict.items()}


class UltralyticsAdapter:
    tasks = {'detect', 'segment', 'pose', 'obb', 'classify'}
    # Dataset paths, output location, test split and resume are owned by this runner.
    allowed = {'epochs', 'imgsz', 'batch', 'device', 'workers', 'optimizer', 'lr0', 'lrf',
               'patience', 'cos_lr', 'freeze', 'weight_decay', 'warmup_epochs', 'amp',
               'degrees', 'translate', 'scale', 'shear', 'perspective', 'fliplr', 'flipud',
               'mosaic', 'mixup', 'copy_paste', 'hsv_h', 'hsv_s', 'hsv_v', 'close_mosaic',
               'auto_augment', 'erasing'}

    def validate(self, recipe):
        from .gpu import cuda_index
        cuda_index(recipe.get('train', {}).get('device'))
        if not recipe.get('pretrained'):
            raise ValueError('ultralytics requires pretrained local weights or a local model YAML')
        if Path(recipe['pretrained']).suffix.lower() not in ('.pt', '.yaml', '.yml'):
            raise ValueError('Training requires .pt weights or model .yaml; inference exports cannot be fine-tuned')
        unknown = set(recipe.get('train', {})) - self.allowed
        if unknown:
            raise ValueError(f'Unsupported or runner-owned training parameters: {sorted(unknown)}')
        evaluation = recipe.get('evaluation', {})
        if not isinstance(evaluation, dict) or set(evaluation) - {'confidence'}:
            raise ValueError('evaluation only supports confidence')
        confidence = evaluation.get('confidence', .6)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not .05 <= confidence <= .99:
            raise ValueError('evaluation confidence must be in [0.05, 0.99]')

    def train(self, recipe, audit, output, progress=None):
        from ultralytics import YOLO
        data = prepare_yolo(audit, output / 'dataset', recipe['task'], ('train', 'val'))
        model = YOLO(recipe['pretrained'], task=recipe['task'])
        if progress:
            model.add_callback('on_train_epoch_end', lambda trainer: progress(
                (trainer.epoch + 1) / trainer.epochs, f'已完成 {trainer.epoch + 1}/{trainer.epochs} 轮'))
        if model.task != recipe['task']:
            raise ValueError('pretrained model task does not match recipe')
        parameters = dict(epochs=100, imgsz=640, batch=8, workers=0, patience=20, amp=False,
                          fliplr=0.0, flipud=0.0, mosaic=0.0, mixup=0.0)
        if recipe['task'] == 'classify':
            parameters.update(auto_augment=None, erasing=0.0)
        parameters.update(recipe.get('train', {}))
        from .gpu import training_device
        parameters['device'] = training_device(parameters.get('device'))
        model.train(data=data, project=str(output), name='fit', exist_ok=False,
                    seed=recipe['seed'], deterministic=True, val=True, plots=False, **parameters)
        best = Path(model.trainer.best)
        if not best.is_file():
            raise ValueError('Trainer did not produce best.pt')
        model = YOLO(str(best))
        metrics = model.val(data=data, split='val', imgsz=parameters['imgsz'],
                            batch=parameters['batch'], device=parameters.get('device'), workers=0,
                            project=str(output), name='validation', plots=False, verbose=False)
        return dict(model=str(best.resolve()), validation=_metrics(metrics),
                    training_parameters=parameters,
                    class_index_to_name=audit['dataset']['names'])

    def evaluate(self, recipe, audit, output, model_path):
        from ultralytics import YOLO
        # The library's dataset checker requires train/val keys. Point both at
        # the isolated test material ONLY for this explicit evaluation process.
        data = prepare_yolo(audit, output / 'dataset', recipe['task'], ('test',))
        if recipe['task'] != 'classify':
            import yaml
            path = Path(data)
            value = yaml.safe_load(path.read_text(encoding='utf-8'))
            value.update(train='images/test', val='images/test')
            path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')
        else:
            # Classification checker requires nonempty train folders even for
            # val(split='test'). Duplicate ONLY this evaluation's test material;
            # this process never calls fit/train and never sees training images.
            shutil.copytree(Path(data) / 'test', Path(data) / 'train')
        parameters = recipe.get('train', {})
        from .gpu import training_device
        confidence = recipe.get('evaluation', {}).get('confidence', .6)
        result = YOLO(str(model_path)).val(data=data, split='test',
            imgsz=parameters.get('imgsz', 640), batch=parameters.get('batch', 8),
            device=training_device(parameters.get('device')), workers=0, project=str(output), name='metrics',
            plots=True, verbose=False, conf=confidence)
        return dict(metrics=_metrics(result), speed_ms_per_image=dict(result.speed),
                    per_class=confusion_metrics(result.confusion_matrix.matrix, audit['dataset']['names']),
                    operating_point=dict(confidence=confidence if recipe['task'] != 'classify' else None,
                                         match_iou=.45 if recipe['task'] != 'classify' else None,
                                         scope='top1' if recipe['task'] == 'classify' else 'bounding-box'),
                    timing_scope='library validation throughput; not batch=1 p95 or end-to-end latency')


def confusion_metrics(matrix, names):
    """Rows are predictions, columns are truth; last detection row/column is background."""
    import numpy as np
    matrix = np.asarray(matrix)
    n = len(names)
    if matrix.shape not in ((n, n), (n + 1, n + 1)) or not np.isfinite(matrix).all() or (matrix < 0).any():
        raise ValueError('Invalid confusion matrix')
    rows = []
    for i, name in enumerate(names):
        tp, predicted, support = float(matrix[i, i]), float(matrix[i, :].sum()), float(matrix[:, i].sum())
        rows.append(dict(id=i, name=name, precision=tp / predicted if predicted else 0.0,
                         recall=tp / support if support else 0.0, support=int(support),
                         tp=int(tp), fp=int(predicted - tp), fn=int(support - tp)))
    return rows


def _features(audit, split):
    import numpy as np
    rows, labels = [], []
    for sample in audit['dataset']['samples']:
        if sample['split'] != split:
            continue
        path = _path(Path(audit['root']), sample['file'])
        if file_hash(path) != sample['sha256']:
            raise ValueError(f'Dataset changed after audit: {path}')
        value = np.load(path, allow_pickle=False)
        if value.ndim != 1 or not np.isfinite(value).all():
            raise ValueError('feature-classify expects finite 1D .npy arrays of equal length')
        rows.append(value)
        labels.append(sample['label'])
    return np.stack(rows), np.array(labels)


def _classification_metrics(model, x, y, names):
    from sklearn.metrics import accuracy_score, classification_report
    predicted = model.predict(x)
    report = classification_report(y, predicted, labels=list(range(len(names))),
        output_dict=True, zero_division=0)
    return dict(accuracy=float(accuracy_score(y, predicted)), macro_f1=report['macro avg']['f1-score'],
                per_class=[dict(id=i, name=name, **report[str(i)]) for i, name in enumerate(names)])


class FeatureClassifierAdapter:
    tasks = {'feature-classify'}

    def validate(self, recipe):
        parameters = recipe.get('train', {})
        if set(parameters) - {'C', 'max_iter', 'device'}:
            raise ValueError('feature-classify supports C, max_iter and CUDA device')
        from .gpu import cuda_index
        cuda_index(parameters.get('device'))
        if not math.isfinite(parameters.get('C', 1.0)) or parameters.get('C', 1.0) <= 0:
            raise ValueError('C must be finite and positive')
        if not _integer(parameters.get('max_iter', 1000), 1):
            raise ValueError('max_iter must be positive')
        if not recipe.get('feature_schema'):
            raise ValueError('feature_schema must identify the extractor version and feature order')

    def train(self, recipe, audit, output, progress=None):
        import joblib
        from .gpu import training_device
        from .gpu_classifier import CudaLogisticClassifier
        x, y = _features(audit, 'train')
        vx, vy = _features(audit, 'val')
        # No hidden random inner CV or SVC probability calibration across videos.
        model = CudaLogisticClassifier(C=recipe.get('train', {}).get('C', 1.0),
            max_iter=recipe.get('train', {}).get('max_iter', 1000),
            device=training_device(recipe.get('train', {}).get('device')))
        model.fit(x, y)
        path = output / 'model.joblib'
        joblib.dump(dict(model=model, names=audit['dataset']['names'],
                         feature_schema=recipe['feature_schema'], feature_dim=x.shape[1]), path)
        return dict(model=str(path), execution=dict(device=model.device, backend='torch', cpuFallback=False),
                    validation=_classification_metrics(model, vx, vy, audit['dataset']['names']),
                    feature_dim=x.shape[1], feature_schema=recipe['feature_schema'])

    def evaluate(self, recipe, audit, output, model_path):
        import joblib
        import numpy as np
        bundle = joblib.load(model_path)
        x, y = _features(audit, 'test')
        model = bundle['model']
        from .gpu_classifier import CudaLogisticClassifier
        if not isinstance(model, CudaLogisticClassifier):
            raise ValueError('Legacy CPU feature model requires GPU retraining; CPU evaluation is forbidden')
        if bundle['feature_schema'] != recipe['feature_schema'] or x.shape[1] != bundle['feature_dim']:
            raise ValueError('Feature contract mismatch')
        for _ in range(5):
            model.predict(x[:1])
        timings = []
        for row in x:
            started = time.perf_counter()
            model.predict(row[None, :])
            timings.append((time.perf_counter() - started) * 1000)
        return dict(metrics=_classification_metrics(model, x, y, audit['dataset']['names']),
            latency=dict(p50_ms=float(np.percentile(timings, 50)), p95_ms=float(np.percentile(timings, 95)),
                         samples=len(timings), batch=1, warmup=5),
            timing_scope='CUDA feature classifier including result transfer; excludes feature extraction, capture and network')


# The legacy recipe key is retained for stored jobs; both aliases run CUDA.
ADAPTERS = {'ultralytics': UltralyticsAdapter(), 'torch': FeatureClassifierAdapter(), 'sklearn': FeatureClassifierAdapter()}


def train_model(recipe_path, output, progress=None):
    recipe, adapter = load_recipe(recipe_path)
    audit = audit_dataset(recipe['dataset'], recipe['task'])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'recipe.json', recipe)
    write_json(output / 'dataset-audit.json', audit)
    started = time.perf_counter()
    initial_hash = file_hash(recipe['pretrained']) if recipe.get('pretrained') else None
    result = adapter.train(recipe, audit, output, **({'progress': progress} if progress else {}))
    if initial_hash is not None and file_hash(recipe['pretrained']) != initial_hash:
        raise ValueError('Pretrained weights changed during training')
    dependencies = {}
    for package in ('ultralytics', 'torch', 'scikit-learn', 'numpy', 'opencv-python'):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            continue
    result.update(schema_version=1, backend=recipe['backend'], task=recipe['task'],
        dataset_sha256=audit['sha256'], model_sha256=file_hash(result['model']),
        recipe_sha256=file_hash(output / 'recipe.json'), dependencies=dependencies,
        pretrained_sha256=initial_hash, elapsed_seconds=time.perf_counter()-started,
        python=platform.python_version(), platform=platform.platform(),
        status='candidate', test_evaluated=False,
        scope='validation-selected candidate; no production activation or accuracy guarantee')
    write_json(output / 'model-card.json', result)
    return result


def evaluate_model(run, output):
    run, output = Path(run).resolve(), Path(output).resolve()
    recipe = json.loads((run / 'recipe.json').read_text(encoding='utf-8'))
    card = json.loads((run / 'model-card.json').read_text(encoding='utf-8'))
    if file_hash(run / 'recipe.json') != card['recipe_sha256']:
        raise ValueError('Frozen recipe changed; create a new run')
    audit = audit_dataset(recipe['dataset'], recipe['task'])
    if audit['sha256'] != card['dataset_sha256'] or file_hash(card['model']) != card['model_sha256']:
        raise ValueError('Frozen dataset or trained model changed; create a new run')
    output.mkdir(parents=True, exist_ok=False)
    result = ADAPTERS[recipe['backend']].evaluate(recipe, audit, output, card['model'])
    result.update(split='test', class_names=audit['dataset']['names'],
                  model_sha256=card['model_sha256'], dataset_sha256=audit['sha256'])
    write_json(output / 'test-evaluation.json', result)
    return result


def select_candidate(candidates, *, min_recall, max_false_accept, max_p95_ms):
    """Enforce accuracy AND latency before ranking. Missing evidence fails closed.

    All candidates must use the same frozen evaluation and timing protocol.
    The metrics here are scene-level defect recall / product false acceptance,
    not detector mAP or classifier frame accuracy.
    """
    if not 0 <= min_recall <= 1 or not 0 <= max_false_accept <= 1 or not math.isfinite(max_p95_ms) or max_p95_ms <= 0:
        raise ValueError('Invalid acceptance constraints')
    eligible, rejected = [], []
    protocols = {c.get('protocol_id') for c in candidates}
    if len(protocols) != 1 or not next(iter(protocols), None):
        raise ValueError('Candidates require the same nonempty protocol_id (dataset/hardware/concurrency/timing scope)')
    for candidate in candidates:
        reasons = []
        for key, lower, upper in [('defect_recall', min_recall, 1),
                                  ('false_accept_rate', 0, max_false_accept),
                                  ('end_to_end_p95_ms', 0, max_p95_ms)]:
            value = candidate.get(key)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lower <= value <= upper:
                reasons.append(key)
        if reasons:
            rejected.append(dict(id=candidate.get('id'), failed=reasons))
        else:
            eligible.append(candidate)
    eligible.sort(key=lambda c: (-c['defect_recall'], c['false_accept_rate'], c['end_to_end_p95_ms']))
    return dict(selected=copy.deepcopy(eligible[0]) if eligible else None, rejected=rejected,
                status='eligible' if eligible else 'no_qualified_candidate')
