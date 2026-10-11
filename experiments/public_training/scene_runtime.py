"""Causal five-stage inference and a configurable, evidence-only SOP checker."""
import argparse
import json
from pathlib import Path
import numpy as np
import torch
from common import save_json
from train_temporal import TemporalModel, Windows


@torch.inference_mode()
def infer(checkpoint, features):
    if checkpoint.get('format') == 'dense-causal-ensemble-v2':
        from scene_v2 import predict_members
        return predict_members(checkpoint['members'], features)
    model = TemporalModel(checkpoint['kind'], checkpoint['input_dim'], checkpoint['classes']).eval()
    model.load_state_dict(checkpoint['state_dict'])
    data = Windows(['input'], {'input': (features, np.zeros(len(features), dtype=np.int64))},
                   np.array(checkpoint['mean']), np.array(checkpoint['std']), 'cpu')
    output = []
    for start in range(0, len(features), 512):
        x, _ = data.batch(data.ends[start:start+512])
        output.append(model(x)[:, -1].softmax(-1).numpy())
    return np.concatenate(output)


def stabilize(probabilities, valid, threshold, hold):
    """Confirm after N consecutive confident observations; never backdate boundaries.

    Uncertain/invalid observations are unknown, not fabricated stage completion.
    """
    result = []
    candidate, count, active = -1, 0, -1
    for probability, available in zip(probabilities, valid):
        proposed = int(np.argmax(probability))
        if not available or probability[proposed] < threshold:
            candidate, count, active = -1, 0, -1
            result.append(-1)
            continue
        count = count+1 if proposed == candidate else 1
        candidate = proposed
        if count >= hold:
            active = proposed
        result.append(active)
    return np.array(result, dtype=np.int64)


def check_sop(stages, steps, hz=10):
    if (not steps or any(isinstance(s, (bool, np.bool_)) or not isinstance(s, (int, np.integer)) or s <= 0 for s in steps)
            or len(set(steps)) != len(steps)):
        raise ValueError('Workflow must contain unique positive stage IDs')
    if not np.isfinite(hz) or hz <= 0:
        raise ValueError('Sample rate must be finite and positive')
    stages = np.asarray(stages)
    if stages.ndim != 1 or not np.isfinite(stages).all() or np.any(stages != np.floor(stages)):
        raise ValueError('Stages must be a one-dimensional sequence of finite integer IDs')
    events, seen, sequence = [], set(), []
    for frame, stage in enumerate(stages):
        stage = int(stage)
        if stage <= 0 or (sequence and sequence[-1] == stage):
            continue
        expected = next((s for s in steps if s not in seen), None)
        status = ('unexpected' if stage not in steps else 'repeat' if stage in seen
                  else 'in_order' if stage == expected else 'out_of_order')
        events.append({'frame': frame, 'seconds': frame/hz, 'stage': stage,
                       'expected': expected, 'status': status})
        sequence.append(stage)
        seen.add(stage)
    missing = [s for s in steps if s not in seen]
    return {'events': events, 'sequence': sequence, 'missing_at_end': missing,
            'observed_in_order': sequence == steps,
            'meaning': 'Recognized stage order; not a quality or physical completion certificate'}


def run_recording(checkpoint, features, valid, config, chunk_size=128):
    """Standalone runtime used by the CLI and replay evaluation; labels are not inputs."""
    features, valid = np.asarray(features), np.asarray(valid)
    if (features.ndim != 2 or features.shape[1] != checkpoint['input_dim'] or not len(features)
            or not np.isfinite(features).all() or valid.shape != (len(features),)
            or not np.isin(valid, [0, 1]).all()):
        raise ValueError('Expected finite nonempty HA4M 74-D features and matching binary validity mask')
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError('Chunk size must be a positive integer')
    check_sop([], config['steps'], config['sample_hz'])
    if checkpoint.get('format') == 'dense-causal-ensemble-v2':
        from scene_v2 import DensePredictor
        predictor = DensePredictor(checkpoint['members'])
        probability = np.concatenate([predictor.push(features[start:start+chunk_size])
                                      for start in range(0, len(features), chunk_size)])
    else:
        probability = infer(checkpoint, features)
    if config.get('filter_kind') in ('ema-persistence-v2', 'confidence-evidence-v1'):
        if config['filter_kind'] == 'confidence-evidence-v1':
            from evidence_filter import EvidenceFilter
            post = EvidenceFilter(**config['filter_parameters'])
        else:
            from scene_v2 import StageFilter
            post = StageFilter(**config['filter_parameters'])
        stages = np.concatenate([post.push(probability[start:start+chunk_size], valid[start:start+chunk_size])
                                 for start in range(0, len(features), chunk_size)])
    else:
        stages = stabilize(probability, valid, config['threshold'], config['hold'])
    return {'stages': stages.tolist(), 'raw': probability.argmax(1).tolist(),
            'confidence': probability.max(1).tolist(),
            'sop': check_sop(stages, config['steps'], config['sample_hz'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path('work/simple-sop-2026-10-10/export'))
    parser.add_argument('--input', type=Path, required=True, help='Prepared HA4M NPZ with x and input_valid')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chunk-size', type=int, default=128, help='Contiguous streaming chunk length')
    args = parser.parse_args()
    torch.set_num_threads(4)
    config = json.loads((args.bundle/'workflow.json').read_text(encoding='utf-8'))
    checkpoint = torch.load(args.bundle/config.get('checkpoint_file', 'stage-tcn.pt'), map_location='cpu', weights_only=True)
    with np.load(args.input, allow_pickle=False) as content:
        x, valid = content['x'], content['input_valid']
    save_json(args.output, run_recording(checkpoint, x, valid, config, args.chunk_size))


if __name__ == '__main__':
    main()
