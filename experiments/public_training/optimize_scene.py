"""Train v2 on train subjects, select exclusively on validation; never load test arrays."""
import json
from pathlib import Path
import time
import numpy as np
import torch
from torch.nn import functional as F
from common import classification, file_hash, save_json, seed_all, temporal_metrics
from scene_v2 import DenseModel, filter_stages

ROOT = Path('work/simple-sop-2026-10-10/v2')
DATA = ROOT.parent/'data'
CANDIDATES = ([{'kind': 'tcn', 'depth': depth, 'seed': seed} for depth in [7, 8] for seed in [2026, 2027, 2028]]
              + [{'kind': 'gru', 'depth': 0, 'seed': seed} for seed in [2026, 2027]])


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
        'model_selection': 'validation macro F1; eligible probability ensembles: top3 individual runs and all runs',
        'filter_grid': {'alpha': [1., .3, .1, .05], 'hold': [1, 5, 10, 20], 'threshold': [0., .4, .55]},
        'filter_selection': 'maximize validation macro F1 subject to validation accuracy >= raw accuracy - .005; tie break segment F1@0.5',
        'split_policy': 'Reuse existing subject split; no test arrays or metrics read during optimization. Previously reported test set is not a fresh benchmark.',
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
    summaries, run_probabilities = [], []
    for config in CANDIDATES:
        run = f'{config["kind"]}-d{config["depth"]}-s{config["seed"]}'
        directory = ROOT/'models'/run
        directory.mkdir(parents=True, exist_ok=True)
        seed_all(config['seed'])
        architecture = {'kind': config['kind'], 'depth': config['depth'], 'width': 96}
        model = DenseModel(**architecture).to(device)
        if not (directory/'metrics.json').exists():
            rng = np.random.default_rng(config['seed'])
            optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=.005)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=90, eta_min=.00005)
            best, best_epoch, history = -1., 0, []
            started = time.monotonic()
            for epoch in range(1, 91):
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
        summaries.append({'name': run, 'members': [run], 'validation': metrics})
        run_probabilities.append(probability)
    top3 = sorted(range(len(summaries)), key=lambda i: summaries[i]['validation']['macro_f1_all_classes'], reverse=True)[:3]
    for name, indices in [('top3-ensemble', top3), ('all-ensemble', list(range(len(CANDIDATES))))]:
        probability = [np.mean([run_probabilities[i][j] for i in indices], axis=0) for j in range(len(val_names))]
        metrics = classification(np.concatenate([records[n][1] for n in val_names]),
                                 np.concatenate(probability).argmax(-1), dataset['labels'])
        summaries.append({'name': name, 'members': [summaries[i]['name'] for i in indices], 'validation': metrics})
        run_probabilities.append(probability)
    best_index = max(range(len(summaries)), key=lambda i: summaries[i]['validation']['macro_f1_all_classes'])
    selected = summaries[best_index]
    probabilities = run_probabilities[best_index]
    truth = [records[n][1] for n in val_names]
    filters = []
    for alpha in protocol['filter_grid']['alpha']:
        for hold in protocol['filter_grid']['hold']:
            for threshold in protocol['filter_grid']['threshold']:
                params = {'alpha': alpha, 'hold': hold, 'threshold': threshold}
                predicted = [filter_stages(p, validity[n], **params) for p, n in zip(probabilities, val_names)]
                metrics = classification(np.concatenate(truth), np.concatenate(predicted), dataset['labels'])
                metrics.update(temporal_metrics(truth, predicted))
                filters.append({'parameters': params, 'validation': metrics})
    eligible = [f for f in filters if f['validation']['accuracy'] >= selected['validation']['accuracy']-.005]
    post = max(eligible, key=lambda f: (f['validation']['macro_f1_all_classes'], f['validation']['segment_f1_at_0.5']))
    selection = {'model': selected, 'filter': post, 'candidates': summaries,
                 'selection_scope': 'validation only; now frozen before test evaluation'}
    save_json(ROOT/'selection.json', selection)
    save_json(ROOT/'filter-search.json', filters)
    print('SELECTED '+json.dumps({'name': selected['name'], 'raw_val_f1': selected['validation']['macro_f1_all_classes'],
        'filter': post['parameters'], 'val_f1': post['validation']['macro_f1_all_classes'],
        'val_accuracy': post['validation']['accuracy'], 'val_segment_f1': post['validation']['segment_f1_at_0.5']}), flush=True)


if __name__ == '__main__':
    main()
