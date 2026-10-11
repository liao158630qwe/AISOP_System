"""Shared reporting helpers for local, reproducible public-data experiments."""
import hashlib
import json
from pathlib import Path
import random

import numpy as np
import torch
from sklearn.metrics import accuracy_score, average_precision_score, confusion_matrix, precision_recall_fscore_support


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.set_num_threads(4)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def save_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def classification(y, pred, names, probabilities=None):
    y, pred = np.asarray(y), np.asarray(pred)
    keep = y >= 0
    y, pred = y[keep], pred[keep]
    p, r, f, n = precision_recall_fscore_support(y, pred, labels=list(range(len(names))), zero_division=0)
    output = {'samples': len(y), 'accuracy': float(accuracy_score(y, pred)),
              'macro_f1_all_classes': float(f.mean()), 'balanced_accuracy_all_classes': float(r.mean()),
              'per_class': {name: {'precision': float(p[i]), 'recall': float(r[i]), 'f1': float(f[i]), 'support': int(n[i])}
                            for i, name in enumerate(names)},
              'confusion_matrix': confusion_matrix(y, pred, labels=list(range(len(names)))).tolist()}
    if probabilities is not None:
        probability = np.asarray(probabilities)[keep]
        output['average_precision_per_class'] = {
            name: float(average_precision_score(y == i, probability[:, i])) if np.any(y == i) else None
            for i, name in enumerate(names)}
    return output


def segments(values):
    values = np.asarray(values)
    if not len(values):
        return []
    boundaries = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1, len(values)]
    return [(int(values[a]), int(a), int(b)) for a, b in zip(boundaries[:-1], boundaries[1:]) if values[a] >= 0]


def edit_score(a, b):
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (left != right)))
        previous = current
    return 1 - previous[-1] / max(len(a), len(b), 1)


def temporal_metrics(truths, predictions):
    totals = {threshold: [0, 0, 0] for threshold in (0.1, 0.25, 0.5)}
    edits = []
    for truth, prediction in zip(truths, predictions):
        prediction = np.asarray(prediction).copy()
        prediction[np.asarray(truth) < 0] = -100
        gt, proposed = segments(truth), segments(prediction)
        if not gt:
            continue
        edits.append(edit_score([s[0] for s in gt], [s[0] for s in proposed]))
        for threshold in totals:
            used = set()
            tp = 0
            for label, start, end in proposed:
                candidates = [(max(0, min(end, b)-max(start, a))/(max(end, b)-min(start, a)), j)
                              for j, (other, a, b) in enumerate(gt) if other == label and j not in used]
                score, index = max(candidates, default=(0, -1))
                if score >= threshold:
                    used.add(index)
                    tp += 1
            totals[threshold][0] += tp
            totals[threshold][1] += len(proposed)-tp
            totals[threshold][2] += len(gt)-tp
    result = {'edit_mean': float(np.mean(edits)) if edits else None,
              'scope': '10 Hz frames; ground-truth dont-care and unavailable-input frames excluded from both streams; per-recording edit average'}
    for threshold, (tp, fp, fn) in totals.items():
        result[f'segment_f1_at_{threshold}'] = 2*tp/max(2*tp+fp+fn, 1)
    return result


def plot_confusion(path, matrix, names, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    matrix = np.asarray(matrix)
    figure, axis = plt.subplots(figsize=(10, 8))
    normalized = matrix / np.maximum(matrix.sum(axis=1, keepdims=True), 1)
    visual = axis.imshow(normalized, vmin=0, vmax=1, cmap='Blues')
    axis.set(xticks=range(len(names)), yticks=range(len(names)), xticklabels=names, yticklabels=names,
             xlabel='Predicted', ylabel='Ground truth', title=title)
    plt.setp(axis.get_xticklabels(), rotation=55, ha='right', fontsize=8)
    axis.tick_params(axis='y', labelsize=8)
    for i in range(len(names)):
        for j in range(len(names)):
            axis.text(j, i, str(matrix[i, j]), ha='center', va='center', fontsize=7,
                      color='white' if normalized[i, j] > .5 else '#334155')
    figure.colorbar(visual, ax=axis, label='Fraction of true class')
    figure.tight_layout()
    figure.savefig(path, dpi=180)
    plt.close(figure)
