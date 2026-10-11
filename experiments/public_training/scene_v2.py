"""Dense causal sequence models and evidence-driven temporal filtering for HA4M."""
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F


class Block(nn.Module):
    def __init__(self, width, dilation):
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.conv = nn.Conv1d(width, width, 3, dilation=dilation)
        self.mix = nn.Conv1d(width, width, 1)
        self.dropout = nn.Dropout(.2)
        self.padding = 2*dilation

    def forward(self, x):
        h = self.norm(x.transpose(1, 2)).transpose(1, 2)
        h = self.conv(F.pad(h, (self.padding, 0)))
        return x + self.dropout(self.mix(F.gelu(h)))

    def stream(self, x, history):
        """Cache normalized activations, including the initial convolution padding."""
        h = self.norm(x.transpose(1, 2)).transpose(1, 2)
        h = F.pad(h, (self.padding, 0)) if history is None else torch.cat((history, h), dim=2)
        history = h[:, :, -self.padding:].clone()
        if x.device.type == 'cpu' and x.shape[2] <= 128:
            # Short streams pay more for the dilated-convolution dispatch than
            # the arithmetic. Apply the same kernel to its three sampled taps.
            taps = h.unfold(2, self.padding+1, 1)[..., ::self.conv.dilation[0]]
            taps = taps.permute(0, 2, 1, 3).reshape(x.shape[0], x.shape[2], -1)
            h = F.linear(taps, self.conv.weight.flatten(1), self.conv.bias)
            h = F.linear(F.gelu(h), self.mix.weight[:, :, 0], self.mix.bias).transpose(1, 2)
        else:
            h = self.mix(F.gelu(self.conv(h)))
        return x + self.dropout(h), history


class DenseModel(nn.Module):
    def __init__(self, kind='tcn', depth=7, width=96):
        super().__init__()
        self.kind = kind
        self.project = nn.Linear(74, width)
        if kind == 'tcn':
            self.core = nn.Sequential(*[Block(width, 2**i) for i in range(depth)])
        elif kind == 'gru':
            self.core = nn.GRU(width, width, 2, batch_first=True, dropout=.2)
        else:
            raise ValueError(kind)
        self.output = nn.Linear(width, 5)

    def forward(self, x):
        h = F.gelu(self.project(x))
        if self.kind == 'tcn':
            h = self.core(h.transpose(1, 2)).transpose(1, 2)
        else:
            h, _ = self.core(h)
        return self.output(h)


@torch.inference_mode()
def predict_members(members, features, device='cpu'):
    """Full recording inference, equivalent to prefix-only inference; no future data."""
    return DensePredictor(members, device).push(features)


class DensePredictor:
    """Load weights once, retain per-layer TCN activations / GRU hidden state.

    Call reset() between recordings. Chunks must be contiguous and in time order.
    No annotation, absolute timestamp, or SOP ordering is used as model input.
    """

    def __init__(self, members, device='cpu'):
        if not members:
            raise ValueError('At least one model member is required')
        self.device = device
        self.members = []
        for checkpoint in members:
            model = DenseModel(**checkpoint['architecture']).to(device).eval()
            model.load_state_dict(checkpoint['state_dict'])
            mean, std = np.asarray(checkpoint['mean']), np.asarray(checkpoint['std'])
            if (mean.shape != (74,) or std.shape != (74,) or not np.isfinite(mean).all()
                    or not np.isfinite(std).all() or np.any(std <= 0)):
                raise ValueError('Expected finite 74-D normalization and positive standard deviations')
            self.members.append((model, mean, std))
        self.reset()

    def reset(self):
        self.states = [None] * len(self.members)

    @torch.inference_mode()
    def push(self, features):
        features = np.asarray(features)
        if features.ndim != 2 or features.shape[1] != 74 or not np.isfinite(features).all():
            raise ValueError('Expected finite N x 74 HA4M features')
        if not len(features):
            return np.empty((0, 5), dtype=np.float32)
        probabilities = []
        for i, (model, mean, std) in enumerate(self.members):
            x = np.clip((features-mean)/std, -10, 10)
            x = torch.tensor(x, dtype=torch.float32, device=self.device)[None]
            if model.kind == 'tcn':
                h = F.gelu(model.project(x)).transpose(1, 2)
                previous = self.states[i] if self.states[i] is not None else [None] * len(model.core)
                states = []
                for block, history in zip(model.core, previous):
                    h, history = block.stream(h, history)
                    states.append(history)
                logits = model.output(h.transpose(1, 2))
                self.states[i] = states
            else:
                h, state = model.core(F.gelu(model.project(x)), self.states[i])
                logits = model.output(h)
                self.states[i] = state
            probabilities.append(logits[0].softmax(-1).cpu().numpy())
        return np.mean(probabilities, axis=0)


class StageFilter:
    """Causal EMA/persistence state shared by batch and streaming inference."""

    def __init__(self, alpha=1., hold=1, threshold=0.):
        if (not np.isfinite(alpha) or not 0 < alpha <= 1
                or isinstance(hold, (bool, np.bool_)) or not isinstance(hold, (int, np.integer)) or hold < 1
                or not np.isfinite(threshold) or not 0 <= threshold <= 1):
            raise ValueError('Invalid filter parameters')
        self.alpha, self.hold, self.threshold = alpha, hold, threshold
        self.reset()

    def reset(self):
        self.average, self.active, self.candidate, self.count = None, -1, -1, 0

    def push(self, probabilities, valid):
        probabilities, valid = np.asarray(probabilities), np.asarray(valid)
        if (probabilities.ndim != 2 or probabilities.shape[1] != 5
                or valid.shape != (len(probabilities),) or not np.isin(valid, [0, 1]).all()
                or not np.isfinite(probabilities).all() or np.any(probabilities < 0)
                or not np.allclose(probabilities.sum(axis=1), 1., atol=1e-5)):
            raise ValueError('Expected normalized N x 5 probabilities and matching binary validity mask')
        result = []
        for p, available in zip(probabilities, valid):
            if not available:
                self.reset()
                result.append(-1)
                continue
            self.average = p.copy() if self.average is None else self.alpha*p+(1-self.alpha)*self.average
            label = int(self.average.argmax())
            if self.average[label] < self.threshold:
                self.active, self.candidate, self.count = -1, -1, 0
                result.append(-1)
                continue
            self.count = self.count+1 if label == self.candidate else 1
            self.candidate = label
            if self.count >= self.hold:
                self.active = label
            result.append(self.active)
        return np.array(result, dtype=np.int64)


def filter_stages(probabilities, valid, alpha=1., hold=1, threshold=0.):
    """EMA and persistence without any SOP order constraint or boundary backfilling.

    Invalid inputs reset evidence. Low confidence emits unknown. A persistent
    competitor replaces the active state; arbitrary backwards transitions remain possible.
    """
    return StageFilter(alpha, hold, threshold).push(probabilities, valid)
