"""Train the v2.3 candidate pool on train subjects; select checkpoints on validation only."""
import json
from pathlib import Path
import time
import numpy as np
import torch
from torch.nn import functional as F
from common import classification, file_hash, save_json, seed_all
from scene_v2 import DenseModel

ROOT = Path('work/simple-sop-2026-10-10/v2.3')
DATA = ROOT.parent/'data'
CANDIDATES = (
    [{'kind': 'tcn', 'depth': 7, 'width': 96, 'seed': seed} for seed in [2026, 2027, 2028]]
    + [{'kind': 'tcn', 'depth': 8, 'width': 96, 'seed': seed} for seed in [2026, 2027, 2028]]
    + [{'kind': 'gru', 'depth': 0, 'width': 96, 'seed': seed} for seed in [2026, 2027, 2028]]
    + [{'kind': 'tcn', 'depth': 6, 'width': 96, 'seed': seed} for seed in [2026, 2027]]
    + [{'kind': 'tcn', 'depth': 7, 'width': 128, 'seed': seed} for seed in [2026, 2027]])


def padded(records, device, augment=False, rng=None):
    examples = []
    for x, y in records:
        if augment:
            # Subject-preserving temporal speed augmentation, with aligned targets.
            length = max(1, int(len(x)*rng.uniform(.75, 1.25)))
            index = np.minimum((np.arange(length)*len(x)/length).astype(int), len(x)-1)
            x, y = x[index].copy(), y[index]
            x += rng.normal(0, .025, x.shape).astype(np.float32)
        examples.append((x, y))
    length = max(len(x) for x, _ in examples)
    x = np.zeros((len(examples), length, 74), np.float32)
    y = np.full((len(examples), length), -100, np.int64)
    for i, (features, labels) in enumerate(examples):
        x[i, :len(features)], y[i, :len(labels)] = features, labels
    return torch.from_numpy(x).to(device), torch.from_numpy(y).to(device)


@torch.inference_mode()
def evaluate(model, records, names, labels, device):
    model.eval()
    probabilities = []
    for name in names:
        x = torch.from_numpy(records[name][0]).to(device)[None]
        probabilities.append(model(x)[0].softmax(-1).cpu().numpy())
    truth = np.concatenate([records[name][1] for name in names])
    report = classification(truth, np.concatenate(probabilities).argmax(-1), labels)
    return report, probabilities


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    dataset = json.loads((DATA/'dataset.json').read_text(encoding='utf-8'))
    protocol = {'dataset_sha256': file_hash(DATA/'dataset.json'), 'candidates': CANDIDATES,
        'epochs_max': 90, 'patience': 18, 'batch_recordings': 6, 'optimizer': 'AdamW lr=.001 weight_decay=.005',
        'training': 'dense per-frame cross entropy with sqrt class weights and .03 label smoothing; .15 within-stage temporal penalty; .75..1.25 speed, .025 normalized feature noise',
        'model_selection': 'validation macro F1 per run; ensemble and filter selection are separate (select_scene_v23.py)',
        'split_policy': 'Reuse existing subject split; no test arrays or metrics read during optimization. Previously reported test set is not a fresh benchmark.',
        'reproducibility_check': 'overlapping v2 configurations are retrained and compared to v2 validation macro F1',
        'normalization': 'training frames only', 'created_unix': time.time()}
    protocol_path = ROOT/'protocol.json'
    if not protocol_path.exists():
        save_json(protocol_path, protocol)
    else:
        old = json.loads(protocol_path.read_text())
        assert old['candidates'] == CANDIDATES and old['dataset_sha256'] == protocol['dataset_sha256']
    arrays, validity = {}, {}
    for part in ['train', 'val']:
        for name in dataset['splits'][part]:
            path = DATA/'prepared'/f'{name}.npz'
            expected = next(r['prepared_sha256'] for r in dataset['records'] if r['recording'] == name)
            assert file_hash(path) == expected
            with np.load(path, allow_pickle=False) as content:
                arrays[name] = (content['x'], content['y'])
                validity[name] = content['input_valid']
    train_names, val_names = dataset['splits']['train'], dataset['splits']['val']
    features = np.concatenate([arrays[n][0] for n in train_names])
    mean, std = features.mean(0), np.maximum(features.std(0), .05)
    records = {n: (np.clip((x-mean)/std, -10, 10).astype(np.float32), y) for n, (x, y) in arrays.items()}
    counts = np.bincount(np.concatenate([arrays[n][1][arrays[n][1] >= 0] for n in train_names]), minlength=5)
    weight = np.sqrt(counts.sum()/counts)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    weights = torch.tensor(weight/weight.mean(), dtype=torch.float32, device=device)
    summaries = []
    for config in CANDIDATES:
        run = f'{config["kind"]}-d{config["depth"]}-w{config["width"]}-s{config["seed"]}'
        directory = ROOT/'models'/run
        directory.mkdir(parents=True, exist_ok=True)
        seed_all(config['seed'])
        architecture = {'kind': config['kind'], 'depth': config['depth'], 'width': config['width']}
        model = DenseModel(**architecture).to(device)
        if not (directory/'metrics.json').exists():
            started = time.monotonic()
            optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.005)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=protocol['epochs_max'])
            rng = np.random.default_rng(config['seed'])
            best, best_epoch, history = -1., 0, []
            for epoch in range(1, protocol['epochs_max']+1):
                model.train()
                order = rng.permutation(len(train_names))
                losses = []
                for start in range(0, len(order), 6):
                    x, y = padded([records[train_names[i]] for i in order[start:start+6]], device, True, rng)
                    optimizer.zero_grad(set_to_none=True)
                    logits = model(x)
                    ce = F.cross_entropy(logits.transpose(1, 2), y, weight=weights, ignore_index=-100, label_smoothing=.03)
                    logp = logits.log_softmax(-1)
                    same = (y[:, 1:] == y[:, :-1]) & (y[:, 1:] >= 0)
                    penalty = (logp[:, 1:]-logp[:, :-1].detach()).square().clamp(max=16).mean(-1)
                    loss = ce + .15*penalty[same].mean()
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
                    optimizer.step()
                    losses.append(loss.item())
                scheduler.step()
                metrics, _ = evaluate(model, records, val_names, dataset['labels'], device)
                score = metrics['macro_f1_all_classes']
                history.append({'epoch': epoch, 'loss': float(np.mean(losses)), 'val_macro_f1': score, 'val_accuracy': metrics['accuracy']})
                if score > best:
                    best, best_epoch = score, epoch
                    torch.save({'format': 'dense-causal-scene-v2', 'architecture': architecture,
                        'state_dict': {k: v.detach().cpu().clone() for k, v in model.state_dict().items()},
                        'mean': mean.tolist(), 'std': std.tolist(), 'labels': dataset['labels'],
                        'feature_spec': dataset['feature_spec'], 'seed': config['seed'], 'epoch': epoch,
                        'validation_macro_f1': score, 'dataset_sha256': protocol['dataset_sha256']}, directory/'best.pt')
                if epoch % 5 == 0 or epoch == 1:
                    print(f'{run} epoch={epoch} val_f1={score:.4f} val_accuracy={metrics["accuracy"]:.4f} best={best:.4f}', flush=True)
                save_json(directory/'history.json', history)
                if epoch-best_epoch >= 18:
                    break
            save_json(directory/'metrics.json', {'run': run, 'best_epoch': best_epoch, 'validation_macro_f1': best,
                'seconds': time.monotonic()-started, 'epochs_run': len(history)})
        checkpoint = torch.load(directory/'best.pt', map_location=device, weights_only=True)
        model.load_state_dict(checkpoint['state_dict'])
        metrics, probability = evaluate(model, records, val_names, dataset['labels'], device)
        np.savez_compressed(directory/'validation-predictions.npz', **dict(zip(val_names, probability)))
        summary = {'name': run, 'members': [run], 'architecture': architecture, 'seed': config['seed'],
                   'best_epoch': checkpoint['epoch'], 'validation': metrics}
        summaries.append(summary)
        print(f'{run}: best_epoch={checkpoint["epoch"]} val_macro_f1={metrics["macro_f1_all_classes"]:.4f}', flush=True)
    v2 = json.loads((ROOT.parent/'v2/selection.json').read_text(encoding='utf-8'))
    previous = {c['name']: c['validation']['macro_f1_all_classes'] for c in v2['candidates'] if len(c['members']) == 1}
    differences = []
    for summary in summaries:
        legacy = f'{summary["architecture"]["kind"]}-d{summary["architecture"]["depth"]}-s{summary["seed"]}'
        if legacy in previous and summary['architecture']['width'] == 96:
            difference = summary['validation']['macro_f1_all_classes']-previous[legacy]
            differences.append({'run': summary['name'], 'v2_run': legacy, 'validation_macro_f1_delta': difference})
    save_json(ROOT/'candidates.json', {'candidates': summaries, 'reproducibility_vs_v2': differences,
        'model_selection_scope': 'validation macro F1 only; test subjects were never loaded'})
    print(json.dumps({'runs': len(summaries), 'max_delta_vs_v2': max((abs(d['validation_macro_f1_delta']) for d in differences), default=0.),
        'top': sorted(((s['validation']['macro_f1_all_classes'], s['name']) for s in summaries), reverse=True)[:5]}, indent=1), flush=True)


if __name__ == '__main__':
    main()
