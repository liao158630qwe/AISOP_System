"""Run saved research checkpoints on HA4M prepared features or SOP event JSON."""
import argparse
import csv
import json
from pathlib import Path

import numpy as np
import torch

from common import file_hash, save_json
from train_sop import SOPModel
from train_temporal import TemporalModel


@torch.inference_mode()
def run(args):
    checkpoint = torch.load(args.checkpoint, map_location=args.device, weights_only=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if 'spec' in checkpoint:
        spec = checkpoint['spec']
        vocab = spec['vocabulary']
        model = SOPModel(checkpoint['kind'], len(vocab['verbs']), len(vocab['objects'])).to(args.device)
        model.load_state_dict(checkpoint['state_dict'])
        model.eval()
        source = json.loads(args.input.read_text(encoding='utf-8'))
        events = source['events']
        if not events:
            raise ValueError('Expected nonempty events in completion-time order')
        tokens = []
        durations = []
        for e in events:
            duration = float(e['duration_native'])
            if not np.isfinite(duration) or duration < 0:
                raise ValueError('duration_native must be a finite nonnegative value')
            tokens.append([vocab['verbs'].get(e['verb'], 1), vocab['objects'].get(e['this'], 1),
                           vocab['objects'].get(e['that'], 1)])
            durations.append([(np.log1p(duration)-spec['duration_mean'])/spec['duration_std']])
        probability = model(torch.tensor([tokens], device=args.device),
                            torch.tensor([durations], dtype=torch.float32, device=args.device))[0].softmax(-1).cpu().numpy()
        rows = [{'event_index': i, 'prediction': spec['labels'][int(p.argmax())],
                 **{f'p_{label}': float(p[j]) for j, label in enumerate(spec['labels'])}}
                for i, p in enumerate(probability)]
        scope = spec['task']
    else:
        metadata = json.loads(args.input.with_suffix('.json').read_text(encoding='utf-8'))
        if metadata['feature_version'] != checkpoint['feature_spec']['version']:
            raise ValueError('Feature version does not match checkpoint')
        if file_hash(args.input) != metadata['prepared_sha256']:
            raise ValueError('Prepared input hash does not match its manifest')
        with np.load(args.input, allow_pickle=False) as source:
            x = source['x']
            frames = source['source_frame']
            input_valid = source['input_valid']
        if x.ndim != 2 or x.shape[1] != checkpoint['input_dim'] or not np.isfinite(x).all():
            raise ValueError('Invalid feature shape or values')
        mean, std = np.asarray(checkpoint['mean'], dtype=np.float32), np.asarray(checkpoint['std'], dtype=np.float32)
        normalized = np.clip((x-mean)/std, -10, 10).astype(np.float32)
        padded = torch.from_numpy(np.concatenate((np.zeros((63, x.shape[1]), np.float32), normalized))).to(args.device)
        model = TemporalModel(checkpoint['kind'], checkpoint['input_dim'], checkpoint['classes']).to(args.device)
        model.load_state_dict(checkpoint['state_dict'])
        model.eval()
        values = []
        offsets = torch.arange(63, -1, -1, device=args.device)
        for first in range(0, len(x), 512):
            ends = torch.arange(first+63, min(first+512, len(x))+63, device=args.device)
            values.append(model(padded[ends[:, None]-offsets])[:, -1].softmax(-1).cpu().numpy())
        probability = np.concatenate(values)
        rows = [{'source_frame': int(frames[i]), 'time_s': float(frames[i]/30),
                 'prediction': checkpoint['labels'][int(p.argmax())] if input_valid[i] else 'unknown',
                 'confidence': float(p.max()) if input_valid[i] else 0., 'input_valid': bool(input_valid[i]),
                 'available_context_samples': min(i+1, 64)} for i, p in enumerate(probability)]
        scope = 'HA4M Kinect-derived 74-D feature experiment; 12 assembly action labels, not five runtime body actions'
    with args.output.open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    save_json(args.output.with_suffix('.metadata.json'), {'checkpoint': str(args.checkpoint.resolve()),
        'checkpoint_sha256': file_hash(args.checkpoint), 'input_sha256': file_hash(args.input),
        'rows': len(rows), 'scope': scope, 'research_only': True})
    print(f'Wrote {len(rows)} predictions to {args.output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    run(parser.parse_args())
