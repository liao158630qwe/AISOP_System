"""Train causal SOP error classifiers on real Assembly101 annotated event sequences.

Input is ground-truth verb/object events available after action completion, not video.
No mistake label, remark, subject, toy ID, absolute time or future event is an input.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import re
import time
import zipfile

import numpy as np
import torch
from torch import nn

from common import classification, file_hash, plot_confusion, save_json, seed_all

NAMES = ['correct', 'mistake', 'correction']


def prepare(path, out):
    sequences, exclusions = [], []
    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.namelist()):
            if not member.endswith('.csv'):
                continue
            name = Path(member).stem
            actor = re.search(r'action_both_(\d+)-', name).group(1)
            events = []
            for line, row in enumerate(csv.reader(io.StringIO(archive.read(member).decode('utf-8-sig'))), 1):
                if len(row) == 6:
                    row += ['']
                if len(row) != 7:
                    raise ValueError('Unexpected columns in '+member)
                start, end = int(row[0]), int(row[1])
                if end < start or row[5] not in NAMES:
                    raise ValueError('Invalid boundary or label')
                target = NAMES.index(row[5])
                verb = row[2].strip()
                if verb not in {'attach', 'detach', 'position'}:
                    exclusions.append({'sequence': name, 'line': line, 'reason': 'unverified verb '+verb,
                                       'handling': 'unknown verb input; loss/evaluation target masked, no guessed correction'})
                    verb, target = '<UNK>', -100
                events.append({'start': start, 'end': end, 'verb': verb, 'this': row[3].strip(),
                               'that': row[4].strip(), 'duration': float(np.log1p(end-start)), 'target': target})
            # Duration is known only at completion: expose events in completion order.
            events.sort(key=lambda row: (row['end'], row['start']))
            sequences.append({'id': name, 'actor': actor, 'events': events})
    actors = sorted({s['actor'] for s in sequences})
    shuffled = np.random.default_rng(20261010).permutation(actors).tolist()
    n_train, n_val = int(len(actors)*.7), max(1, int(len(actors)*.15))
    groups = {'train': sorted(shuffled[:n_train]), 'val': sorted(shuffled[n_train:n_train+n_val]),
              'test': sorted(shuffled[n_train+n_val:])}
    splits = {part: [s for s in sequences if s['actor'] in selected] for part, selected in groups.items()}
    assert all(set(groups[a]).isdisjoint(groups[b]) for a, b in [('train','val'),('train','test'),('val','test')])
    verbs = sorted({e['verb'] for s in splits['train'] for e in s['events']} - {'<UNK>'})
    objects = sorted({e[k] for s in splits['train'] for e in s['events'] for k in ('this', 'that')})
    vocabulary = {'verbs': {'<PAD>': 0, '<UNK>': 1, **{v: i+2 for i, v in enumerate(verbs)}},
                  'objects': {'<PAD>': 0, '<UNK>': 1, **{v: i+2 for i, v in enumerate(objects)}}}
    duration = [e['duration'] for s in splits['train'] for e in s['events']]
    mean, std = float(np.mean(duration)), max(float(np.std(duration)), .1)
    for part in splits:
        for seq in splits[part]:
            seq['tokens'] = np.array([[vocabulary['verbs'].get(e['verb'], 1),
                vocabulary['objects'].get(e['this'], 1), vocabulary['objects'].get(e['that'], 1)] for e in seq['events']], np.int64)
            seq['numeric'] = np.array([[(e['duration']-mean)/std] for e in seq['events']], np.float32)
            seq['targets'] = np.array([e['target'] for e in seq['events']], np.int64)
    spec = {'dataset': 'Assembly101 mistake detection annotations', 'source_archive_sha256': file_hash(path),
            'split_seed': 20261010, 'actors': groups, 'splits': {p: [s['id'] for s in seqs] for p, seqs in splits.items()},
            'labels': NAMES, 'vocabulary': vocabulary, 'duration_mean': mean, 'duration_std': std,
            'input': 'current/past ground-truth verb, two working objects, log1p duration in native annotation units',
            'excluded_inputs': ['mistake labels', 'remarks', 'subject ID', 'toy ID', 'absolute timestamps', 'future events'],
            'task': 'post-event three-class error classification from annotated events; NOT end-to-end video SOP recognition',
            'source_exclusions': exclusions, 'license': 'CC-BY-NC-4.0',
            'split_statistics': {p: {'sequences': len(ss), 'actors': len(groups[p]),
                'event_counts': np.bincount(np.concatenate([s['targets'][s['targets'] >= 0] for s in ss]), minlength=3).tolist()}
                for p, ss in splits.items()}}
    save_json(out/'dataset.json', spec)
    return splits, spec


class SOPModel(nn.Module):
    def __init__(self, kind, verbs, objects):
        super().__init__()
        self.kind = kind
        self.verb = nn.Embedding(verbs, 8, padding_idx=0)
        self.object = nn.Embedding(objects, 16, padding_idx=0)
        self.project = nn.Sequential(nn.Linear(41, 64), nn.GELU())
        if kind == 'gru':
            self.core = nn.GRU(64, 64, num_layers=1, batch_first=True)
        else:
            self.core = nn.Sequential(nn.Linear(64, 64), nn.GELU(), nn.Dropout(.1))
        self.head = nn.Linear(64, 3)

    def forward(self, tokens, numeric):
        x = self.project(torch.cat((self.verb(tokens[..., 0]), self.object(tokens[..., 1]),
                                   self.object(tokens[..., 2]), numeric), dim=-1))
        x = self.core(x)[0] if self.kind == 'gru' else self.core(x)
        return self.head(x)


def batch(sequences, device):
    length = max(len(s['targets']) for s in sequences)
    tokens = np.zeros((len(sequences), length, 3), np.int64)
    numeric = np.zeros((len(sequences), length, 1), np.float32)
    targets = np.full((len(sequences), length), -100, np.int64)
    for i, sequence in enumerate(sequences):
        n = len(sequence['targets'])
        tokens[i, :n], numeric[i, :n], targets[i, :n] = sequence['tokens'], sequence['numeric'], sequence['targets']
    return [torch.as_tensor(value, device=device) for value in (tokens, numeric, targets)]


@torch.inference_mode()
def evaluate(model, sequences, device):
    model.eval()
    tokens, numeric, targets = batch(sequences, device)
    probabilities = model(tokens, numeric).softmax(-1)
    keep = targets >= 0
    y, prob = targets[keep].cpu().numpy(), probabilities[keep].cpu().numpy()
    pred = prob.argmax(-1)
    return classification(y, pred, NAMES, prob), {'y': y, 'prediction': pred, 'probability': prob}


def train_one(kind, seed, splits, spec, args):
    seed_all(seed)
    out = args.out/f'{kind}-seed{seed}'
    out.mkdir(parents=True, exist_ok=True)
    vocab = spec['vocabulary']
    model = SOPModel(kind, len(vocab['verbs']), len(vocab['objects'])).to(args.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=.002, weight_decay=.001)
    counts = np.asarray(spec['split_statistics']['train']['event_counts'])
    weight = np.sqrt(counts.sum()/np.maximum(counts, 1))
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(weight/weight.mean(), device=args.device, dtype=torch.float32))
    best, best_epoch, history = -1, 0, []
    rng = np.random.default_rng(seed)
    started = time.monotonic()
    for epoch in range(1, args.epochs+1):
        model.train()
        order = rng.permutation(len(splits['train']))
        losses = []
        for first in range(0, len(order), 32):
            sequences = [splits['train'][i] for i in order[first:first+32]]
            tokens, numeric, targets = batch(sequences, args.device)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(tokens, numeric).reshape(-1, 3), targets.reshape(-1))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            losses.append(loss.item())
        validation, _ = evaluate(model, splits['val'], args.device)
        score = validation['macro_f1_all_classes']
        history.append({'epoch': epoch, 'loss': float(np.mean(losses)), 'val_macro_f1': score})
        if score > best:
            best, best_epoch = score, epoch
            torch.save({'state_dict': {k: v.detach().cpu() for k, v in model.state_dict().items()},
                        'kind': kind, 'seed': seed, 'best_epoch': epoch, 'spec': spec,
                        'dataset_sha256': file_hash(args.out/'dataset.json')}, out/'best.pt')
        if epoch % 10 == 0:
            print(f'SOP {kind} seed={seed} epoch={epoch} val_macro_f1={score:.4f}', flush=True)
        if epoch-best_epoch >= 20:
            break
    state = torch.load(out/'best.pt', map_location=args.device, weights_only=True)
    model.load_state_dict(state['state_dict'])
    validation, _ = evaluate(model, splits['val'], args.device)
    testing, predictions = evaluate(model, splits['test'], args.device)
    with torch.inference_mode():
        tokens, numeric, _ = batch(splits['val'][:2], args.device)
        before = model(tokens, numeric)
        altered_tokens, altered_numeric = tokens.clone(), numeric.clone()
        altered_tokens[:, 4:] = 1
        altered_numeric[:, 4:] = 5
        leakage = (before[:, :4]-model(altered_tokens, altered_numeric)[:, :4]).abs().max().item()
        assert leakage < 1e-5
    report = {'model': kind, 'seed': seed, 'parameters': sum(p.numel() for p in model.parameters()),
        'best_epoch': best_epoch, 'epochs_run': len(history), 'seconds': time.monotonic()-started,
        'validation': validation, 'test': testing, 'future_perturbation_max_error': leakage,
        'weights_sha256': file_hash(out/'best.pt'), 'scope': spec['task']}
    save_json(out/'metrics.json', report)
    save_json(out/'history.json', history)
    np.savez_compressed(out/'test-predictions.npz', **predictions)
    plot_confusion(out/'test-confusion.png', testing['confusion_matrix'], NAMES, f'Assembly101 annotated events: {kind}, seed {seed}')
    print('SOP TEST '+json.dumps({'model': kind, 'seed': seed, 'macro_f1': testing['macro_f1_all_classes'],
                                'mistake_AP': testing['average_precision_per_class']['mistake']}), flush=True)
    return report


def main(args):
    args.out.mkdir(parents=True, exist_ok=True)
    splits, spec = prepare(args.archive, args.out)
    print(json.dumps(spec['split_statistics']), flush=True)
    save_json(args.out/'protocol.json', {'script_sha256': file_hash(__file__), 'seeds': args.seeds,
        'max_epochs': args.epochs, 'early_stop_patience': 20, 'selection': 'validation macro F1 only',
        'torch': torch.__version__, 'device': args.device, 'data_sha256': file_hash(args.out/'dataset.json')})
    reports = []
    for kind in ['event_mlp', 'gru']:
        for seed in ([args.seeds[0]] if kind == 'event_mlp' else args.seeds):
            reports.append(train_one(kind, seed, splits, spec, args))
            save_json(args.out/'results.json', reports)
    summary = {}
    for kind in ['event_mlp', 'gru']:
        runs = [r for r in reports if r['model'] == kind]
        summary[kind] = {'runs': len(runs), 'scope': spec['task']}
        for metric in ['accuracy', 'macro_f1_all_classes']:
            values = [r['test'][metric] for r in runs]
            summary[kind][metric] = {'mean': float(np.mean(values)), 'std_across_seeds': float(np.std(values))}
        summary[kind]['mistake_average_precision'] = {'mean': float(np.mean([r['test']['average_precision_per_class']['mistake'] for r in runs]))}
    save_json(args.out/'summary.json', summary)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=Path('work/public-datasets-2026-10-10/annotations/assembly101_mistakes.zip'))
    parser.add_argument('--out', type=Path, default=Path('work/public-training-2026-10-10/sop-models'))
    parser.add_argument('--seeds', nargs='+', type=int, default=[2026, 2027, 2028])
    parser.add_argument('--epochs', type=int, default=150)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    main(parser.parse_args())
