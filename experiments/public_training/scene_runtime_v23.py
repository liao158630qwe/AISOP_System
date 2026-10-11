"""Standalone causal inference CLI for the V2.3 export: stages, SOP events, replay fields.

Same contract as scene_runtime.py with the V2.3 filter kinds added. Labels are never
an input; only features and the input-validity mask are read.
"""
import argparse
import json
from pathlib import Path
import numpy as np
import torch
from common import save_json
from scene_runtime import check_sop, infer


def make_filter(kind, parameters):
    if kind == 'confidence-evidence-v1':
        from evidence_filter import EvidenceFilter
        return EvidenceFilter(**parameters)
    if kind == 'ema-persistence-v2':
        from scene_v2 import StageFilter
        return StageFilter(**parameters)
    if kind == 'hmm-forward-v3':
        from scene_filter_v3 import HMMFilter
        return HMMFilter(**parameters)
    if kind == 'evidence-decay-v3':
        from scene_filter_v3 import EvidenceDecayFilter
        return EvidenceDecayFilter(**parameters)
    raise ValueError(f'Unknown filter kind: {kind}')


def run_recording(checkpoint, features, valid, config, chunk_size=128):
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
    post = make_filter(config['filter_kind'], config['filter_parameters'])
    stages = np.concatenate([post.push(probability[start:start+chunk_size], valid[start:start+chunk_size])
                             for start in range(0, len(features), chunk_size)])
    return {'stages': stages.tolist(), 'raw': probability.argmax(1).tolist(),
            'confidence': probability.max(1).tolist(),
            'sop': check_sop(stages, config['steps'], config['sample_hz'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path('work/simple-sop-2026-10-10/v2.3/export'))
    parser.add_argument('--input', type=Path, required=True, help='Prepared HA4M NPZ with x and input_valid')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chunk-size', type=int, default=128, help='Contiguous streaming chunk length')
    args = parser.parse_args()
    torch.set_num_threads(4)
    config = json.loads((args.bundle/'workflow.json').read_text(encoding='utf-8'))
    checkpoint = torch.load(args.bundle/config.get('checkpoint_file', 'stage-ensemble.pt'),
                            map_location='cpu', weights_only=True)
    with np.load(args.input, allow_pickle=False) as content:
        x, valid = content['x'], content['input_valid']
    save_json(args.output, run_recording(checkpoint, x, valid, config, args.chunk_size))


if __name__ == '__main__':
    main()
