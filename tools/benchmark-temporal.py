"""Untrained causal architecture microbenchmark; never measures accuracy."""
import argparse
import json
import platform
import statistics
import time
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F


class CausalBlock(nn.Module):
    def __init__(self, width, dilation):
        super().__init__()
        self.padding = 2*dilation
        self.layers = nn.ModuleList([nn.Conv1d(width, width, 3, dilation=dilation) for _ in range(2)])

    def forward(self, x):
        residual = x
        for layer in self.layers:
            x = F.relu(layer(F.pad(x, (self.padding, 0))))
        return x+residual


class TinyTCN(nn.Module):
    def __init__(self):
        super().__init__()
        self.input = nn.Conv1d(74, 64, 1)
        self.blocks = nn.Sequential(*(CausalBlock(64, d) for d in [1, 2, 4, 8]))
        self.output = nn.Conv1d(64, 5, 1)

    def forward(self, x):
        return self.output(self.blocks(self.input(x.transpose(1, 2)))).transpose(1, 2)


class TinyTransformer(nn.Module):
    def __init__(self):
        super().__init__()
        self.input = nn.Linear(74, 64)
        self.position = nn.Parameter(torch.zeros(1, 64, 64))
        layer = nn.TransformerEncoderLayer(64, 4, 128, dropout=0., batch_first=True)
        self.encoder = nn.TransformerEncoder(layer, 2, enable_nested_tensor=False)
        self.output = nn.Linear(64, 5)

    def forward(self, x):
        length = x.shape[1]
        mask = torch.triu(torch.ones(length, length, dtype=torch.bool, device=x.device), diagonal=1)
        return self.output(self.encoder(self.input(x)+self.position[:, :length], mask=mask))


def run(output):
    torch.manual_seed(20261010)
    torch.set_num_threads(4)
    devices = ['cpu']+(['cuda'] if torch.cuda.is_available() else [])
    report = {'provenance': 'untrained random weights and synthetic inputs; no accuracy measurement',
              'torch': torch.__version__, 'cpu': platform.processor(), 'cpu_threads': 4,
              'gpu': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
              'features': 74, 'window_samples': 64, 'output_labels': 5,
              'warmup': 10, 'iterations': 50, 'dtype': 'float32',
              'scope': 'full window forward only; excludes pose, tracking, preprocessing, transfer and HTTP',
              'results': []}
    with torch.inference_mode():
        for name, constructor in [('causal_tcn', TinyTCN), ('causal_transformer', TinyTransformer)]:
            for device in devices:
                model = constructor().to(device).eval()
                sample = torch.randn(1, 64, 74, device=device)
                before = model(sample)[:, :32]
                sample[:, 32:] = torch.randn_like(sample[:, 32:])*10
                error = (before-model(sample)[:, :32]).abs().max().item()
                if error > 1e-5:
                    raise AssertionError(f'{name}: future leakage {error}')
                for batch in [1, 8]:
                    x = torch.randn(batch, 64, 74, device=device)
                    for _ in range(10): model(x)
                    sync = torch.cuda.synchronize if device == 'cuda' else lambda: None
                    sync()
                    samples = []
                    for _ in range(50):
                        start = time.perf_counter()
                        model(x)
                        sync()
                        samples.append((time.perf_counter()-start)*1000)
                    report['results'].append({'model': name, 'device': device, 'batch': batch,
                        'parameters': sum(p.numel() for p in model.parameters()),
                        'p50_ms': round(statistics.median(samples), 3),
                        'p95_ms': round(sorted(samples)[47], 3),
                        'future_perturbation_max_error': error})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('work/temporal-model-evaluation/benchmark.json'))
    run(parser.parse_args().out)
