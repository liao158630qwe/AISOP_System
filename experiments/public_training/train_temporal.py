"""Train and test causal 74-D HA4M action classifiers on a frozen subject split."""
import argparse
import copy
import json
from pathlib import Path
import time

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from common import classification, file_hash, plot_confusion, save_json, seed_all, temporal_metrics


class CausalBlock(nn.Module):
    def __init__(self, width, dilation):
        super().__init__()
        self.padding = 2*dilation
        self.layers = nn.ModuleList([nn.Conv1d(width, width, 3, dilation=dilation) for _ in range(2)])
        self.dropout = nn.Dropout(.1)

    def forward(self, x):
        residual = x
        for layer in self.layers:
            x = self.dropout(F.gelu(layer(F.pad(x, (self.padding, 0)))))
        return residual+x


class TemporalModel(nn.Module):
    def __init__(self, kind, input_dim=74, classes=12, width=64):
        super().__init__()
        self.kind = kind
        if kind == 'tcn':
            self.input = nn.Conv1d(input_dim, width, 1)
            self.core = nn.Sequential(*(CausalBlock(width, d) for d in (1, 2, 4, 8)))
            self.output = nn.Conv1d(width, classes, 1)
        elif kind == 'transformer':
            self.input = nn.Linear(input_dim, width)
            self.position = nn.Parameter(torch.randn(1, 64, width)*.02)
            layer = nn.TransformerEncoderLayer(width, 4, 128, dropout=.1, batch_first=True, activation='gelu')
            self.core = nn.TransformerEncoder(layer, 2, enable_nested_tensor=False)
            self.output = nn.Linear(width, classes)
        elif kind == 'frame_mlp':
            self.core = nn.Sequential(nn.Linear(input_dim, width), nn.GELU(), nn.Dropout(.1),
                                      nn.Linear(width, width), nn.GELU(), nn.Linear(width, classes))
        else:
            raise ValueError(kind)

    def forward(self, x):
        if self.kind == 'tcn':
            return self.output(self.core(self.input(x.transpose(1, 2)))).transpose(1, 2)
        if self.kind == 'transformer':
            length = x.shape[1]
            mask = torch.triu(torch.ones(length, length, device=x.device, dtype=torch.bool), diagonal=1)
            return self.output(self.core(self.input(x)+self.position[:, :length], mask=mask))
        return self.core(x)


class Windows:
    def __init__(self, recordings, arrays, mean, std, device, stride=1):
        features, targets, endpoints = [], [], []
        self.ranges = []
        offset = 0
        for name in recordings:
            x, y = arrays[name]
            normalized = np.clip((x-mean)/std, -10, 10).astype(np.float32)
            features.append(np.concatenate([np.zeros((63, x.shape[1]), np.float32), normalized]))
            targets.append(np.concatenate([np.full(63, -100), y]))
            indices = np.arange(offset+63, offset+63+len(x), dtype=np.int64)
            endpoints.extend(indices[::stride][y[::stride] >= 0].tolist())
            self.ranges.append((name, indices, y))
            offset += 63+len(x)
        self.x = torch.from_numpy(np.concatenate(features)).to(device)
        self.y = torch.from_numpy(np.concatenate(targets)).long().to(device)
        self.ends = np.asarray(endpoints, np.int64)
        self.offsets = torch.arange(63, -1, -1, device=device)
        self.device = device

    def batch(self, endpoints):
        ends = torch.as_tensor(endpoints, dtype=torch.long, device=self.device)
        return self.x[ends[:, None]-self.offsets], self.y[ends]


@torch.inference_mode()
def evaluate(model, data, batch_size, names, temporal=False):
    model.eval()
    all_truth, all_pred, all_prob, sequence_truth, sequence_pred = [], [], [], [], []
    for _, endpoints, truth in data.ranges:
        probabilities = []
        for start in range(0, len(endpoints), batch_size):
            x, _ = data.batch(endpoints[start:start+batch_size])
            logits = model(x)[:, -1]
            probabilities.append(logits.softmax(-1).cpu().numpy())
        probability = np.concatenate(probabilities)
        prediction = probability.argmax(-1)
        all_truth.append(truth)
        all_pred.append(prediction)
        all_prob.append(probability)
        sequence_truth.append(truth)
        sequence_pred.append(prediction)
    y, pred, prob = map(np.concatenate, (all_truth, all_pred, all_prob))
    report = classification(y, pred, names)
    if temporal:
        report.update(temporal_metrics(sequence_truth, sequence_pred))
    return report, {'y': y, 'prediction': pred, 'probability': prob,
                    'sequence_lengths': np.asarray([len(y) for y in all_truth])}


def train_one(kind, seed, args, dataset, arrays, mean, std, windows):
    seed_all(seed)
    directory = args.out/f'{kind}-seed{seed}'
    directory.mkdir(parents=True, exist_ok=True)
    classes = len(dataset['labels'])
    model = TemporalModel(kind, classes=classes).to(args.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.0001)
    train, val, test = windows
    counts = np.bincount(np.concatenate([arrays[n][1][arrays[n][1] >= 0] for n in dataset['splits']['train']]), minlength=classes)
    weight = np.sqrt(counts.sum()/np.maximum(counts, 1))
    weight = torch.tensor(weight/weight.mean(), dtype=torch.float32, device=args.device)
    criterion = nn.CrossEntropyLoss(weight=weight)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    rng = np.random.default_rng(seed)
    best_score, best_epoch, history, best_state = -1, 0, [], None
    started = time.monotonic()
    for epoch in range(1, args.epochs+1):
        model.train()
        shuffled = rng.permutation(train.ends)
        loss_sum, count = 0., 0
        for start in range(0, len(shuffled), args.batch):
            endpoints = shuffled[start:start+args.batch]
            x, y = train.batch(endpoints)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(x)[:, -1], y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            loss_sum += loss.item()*len(endpoints)
            count += len(endpoints)
        scheduler.step()
        val_report, _ = evaluate(model, val, args.batch, dataset['labels'])
        score = val_report['macro_f1_all_classes']
        history.append({'epoch': epoch, 'training_loss': loss_sum/count,
                        'val_macro_f1': score, 'val_accuracy': val_report['accuracy']})
        print(f'{kind} seed={seed} epoch={epoch} loss={loss_sum/count:.4f} '
              f'val_macro_f1={score:.4f} val_accuracy={val_report["accuracy"]:.4f}', flush=True)
        if score > best_score:
            best_score, best_epoch = score, epoch
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            torch.save({'state_dict': best_state, 'kind': kind, 'input_dim': 74, 'classes': classes,
                        'labels': dataset['labels'], 'feature_spec': dataset['feature_spec'],
                        'mean': mean.tolist(), 'std': std.tolist(), 'seed': seed, 'epoch': epoch,
                        'validation_macro_f1': score, 'dataset_sha256': file_hash(args.data/'dataset.json'),
                        'research_only': True}, directory/'best.pt')
        save_json(directory/'history.json', history)
        if epoch-best_epoch >= args.patience:
            break
    model.load_state_dict(best_state)
    model.eval()
    x, _ = val.batch(val.ends[:2])
    with torch.inference_mode():
        original = model(x)
        changed = x.clone()
        changed[:, 32:] = torch.randn_like(changed[:, 32:])*5
        leakage = (original[:, :32]-model(changed)[:, :32]).abs().max().item()
        if leakage > 1e-4:
            raise AssertionError(f'Future leakage {leakage}')
        restored = TemporalModel(kind, classes=classes).to(args.device)
        restored.load_state_dict(torch.load(directory/'best.pt', map_location=args.device, weights_only=True)['state_dict'])
        restored.eval()
        reload_error = (original-restored(x)).abs().max().item()
        assert reload_error < 1e-6
        sample = x[:1]
        for _ in range(20):
            model(sample)
        latencies = []
        for _ in range(100):
            if args.device == 'cuda': torch.cuda.synchronize()
            before = time.perf_counter()
            model(sample)
            if args.device == 'cuda': torch.cuda.synchronize()
            latencies.append((time.perf_counter()-before)*1000)
    validation, _ = evaluate(model, val, args.batch, dataset['labels'], temporal=True)
    testing, predictions = evaluate(model, test, args.batch, dataset['labels'], temporal=True)
    np.savez_compressed(directory/'test-predictions.npz', **predictions)
    report = {'model': kind, 'seed': seed, 'parameters': sum(p.numel() for p in model.parameters()),
        'best_epoch': best_epoch, 'epochs_run': len(history), 'seconds': time.monotonic()-started,
        'validation': validation, 'test': testing, 'future_perturbation_max_error': leakage,
        'checkpoint_reload_max_error': reload_error, 'weights_sha256': file_hash(directory/'best.pt'),
        'forward_ms_single_window': {'p50': float(np.median(latencies)), 'p95': float(np.percentile(latencies, 95)),
                                    'scope': 'GPU synchronized forward only; excludes detection, features, HTTP and acquisition'},
        'not_production_model': True}
    save_json(directory/'metrics.json', report)
    plot_confusion(directory/'test-confusion.png', testing['confusion_matrix'], dataset['labels'], f'HA4M {kind}, seed {seed}, held-out subjects')
    print('TEST '+json.dumps({'model': kind, 'seed': seed, 'macro_f1': testing['macro_f1_all_classes'],
                             'accuracy': testing['accuracy'], 'F1@50': testing['segment_f1_at_0.5']}), flush=True)
    return report


def main(args):
    args.out.mkdir(parents=True, exist_ok=True)
    dataset = json.loads((args.data/'dataset.json').read_text(encoding='utf-8'))
    arrays = {}
    for record in dataset['records']:
        path = args.data/'prepared'/(record['recording']+'.npz')
        assert file_hash(path) == record['prepared_sha256']
        with np.load(path, allow_pickle=False) as content:
            arrays[record['recording']] = (content['x'], content['y'])
    training_features = np.concatenate([arrays[name][0] for name in dataset['splits']['train']])
    mean = training_features.mean(0)
    std = np.maximum(training_features.std(0), .05)
    windows = [Windows(dataset['splits'][part], arrays, mean, std, args.device,
                       stride=3 if part == 'train' else 1) for part in ('train', 'val', 'test')]
    save_json(args.out/'protocol.json', {'dataset_sha256': file_hash(args.data/'dataset.json'),
        'script_sha256': file_hash(__file__), 'models': args.models, 'seeds': args.seeds,
        'epochs_max': args.epochs, 'early_stop_patience': args.patience, 'selection': 'validation macro F1 only',
        'training_endpoint_stride': 3, 'context_samples': 64, 'batch_size': args.batch,
        'torch': torch.__version__, 'device': args.device,
        'gpu': torch.cuda.get_device_name(0) if args.device == 'cuda' else None,
        'subjects': dataset['subjects'], 'normalization_fit': 'all training-partition feature frames only'})
    reports = []
    for kind in args.models:
        for seed in ([args.seeds[0]] if kind == 'frame_mlp' else args.seeds):
            metrics = args.out/f'{kind}-seed{seed}'/'metrics.json'
            if metrics.exists():
                reports.append(json.loads(metrics.read_text(encoding='utf-8')))
                continue
            reports.append(train_one(kind, seed, args, dataset, arrays, mean, std, windows))
            save_json(args.out/'results.json', reports)
    aggregate = {}
    for kind in args.models:
        selected = [r for r in reports if r['model'] == kind]
        aggregate[kind] = {'runs': len(selected)}
        for metric in ['accuracy', 'macro_f1_all_classes', 'segment_f1_at_0.5', 'edit_mean']:
            values = [r['test'][metric] for r in selected]
            aggregate[kind][metric] = {'mean': float(np.mean(values)), 'std_across_seeds': float(np.std(values))}
    save_json(args.out/'summary.json', aggregate)
    print(json.dumps(aggregate, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('work/public-training-2026-10-10/ha4m'))
    parser.add_argument('--out', type=Path, default=Path('work/public-training-2026-10-10/temporal-models'))
    parser.add_argument('--models', nargs='+', default=['frame_mlp', 'tcn', 'transformer'])
    parser.add_argument('--seeds', nargs='+', type=int, default=[2026, 2027, 2028])
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--patience', type=int, default=8)
    parser.add_argument('--batch', type=int, default=512)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    main(parser.parse_args())
