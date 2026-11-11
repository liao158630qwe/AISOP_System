"""CUDA inference for existing calibrated RBF SVCs and GPU feature training.

Legacy joblib files are read as weights only. No sklearn predict/fit method is
called. Scaling, kernels, pairwise decisions and probability coupling run on CUDA.
"""
import numpy as np
import torch

from .gpu import require_cuda
from .video import AnalysisError


class CudaSVC:
    def __init__(self, pipeline):
        self.device = f'cuda:{require_cuda()}'
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.svm import SVC
        if not isinstance(pipeline, Pipeline) or len(pipeline.steps) != 2:
            raise AnalysisError('GPU_MODEL_UNSUPPORTED', 'GPU 分类适配器要求 StandardScaler + RBF SVC')
        scaler, model = (step for _, step in pipeline.steps)
        if not isinstance(scaler, StandardScaler) or not isinstance(model, SVC) or model.kernel != 'rbf' or not model.probability:
            raise AnalysisError('GPU_MODEL_UNSUPPORTED', '仅支持已校准概率的 RBF SVC；禁止 CPU 分类回退')
        self.classes_ = model.classes_.copy()
        self.n_features_in_ = model.n_features_in_
        self.mean = self._tensor(scaler.mean_ if scaler.with_mean else np.zeros(self.n_features_in_))
        self.scale = self._tensor(scaler.scale_ if scaler.with_std else np.ones(self.n_features_in_))
        self.support = self._tensor(model.support_vectors_)
        self.support_norm = self.support.square().sum(1)
        self.gamma = float(model._gamma)
        count = len(self.classes_)
        starts = np.r_[0, np.cumsum(model.n_support_)]
        weights = np.zeros((count * (count - 1) // 2, len(model.support_vectors_)))
        pair = 0
        # Internal coefficients retain libsvm's binary decision orientation.
        for i in range(count):
            for j in range(i + 1, count):
                weights[pair, starts[i]:starts[i+1]] = model._dual_coef_[j-1, starts[i]:starts[i+1]]
                weights[pair, starts[j]:starts[j+1]] = model._dual_coef_[i, starts[j]:starts[j+1]]
                pair += 1
        self.weights = self._tensor(weights)
        self.intercept = self._tensor(model._intercept_)
        self.prob_a, self.prob_b = self._tensor(model.probA_), self._tensor(model.probB_)

    def _tensor(self, value):
        return torch.as_tensor(value, dtype=torch.float64, device=self.device)

    @torch.inference_mode()
    def _pairwise_probabilities(self, values, *, check_finite=True):
        # Preserve the trained scaler's float32 rounding before libsvm's float64
        # kernel, when the feature extractor supplied float32 (as it does live).
        on_device = isinstance(values, torch.Tensor)
        if not on_device:
            values = np.asarray(values)
        if values.ndim != 2 or values.shape[1] != self.n_features_in_:
            raise ValueError('Feature contract mismatch')
        # Individual predictions validate here. Batched predictions validate all
        # sites together before issuing kernels, with one CUDA wait per device.
        if check_finite:
            finite = torch.isfinite(values).all().item() if on_device else np.isfinite(values).all()
            if not finite:
                raise ValueError('Feature values must be finite')
        x = torch.as_tensor(values, device=self.device)
        dtype = x.dtype if x.is_floating_point() else torch.float64
        x = (x.to(torch.float64) - self.mean).to(dtype)
        x = (x.to(torch.float64) / self.scale).to(dtype).to(torch.float64)
        distance = (x.square().sum(1, keepdim=True) + self.support_norm - 2 * x @ self.support.T).clamp_min_(0)
        decision = torch.exp(-self.gamma * distance) @ self.weights.T + self.intercept
        return torch.sigmoid(-(decision * self.prob_a + self.prob_b)).clamp_(1e-7, 1 - 1e-7)

    @staticmethod
    def _couple(calibrated, k):
        n, device = len(calibrated), calibrated.device
        r = torch.zeros((n, k, k), dtype=torch.float64, device=device)
        a, b = torch.triu_indices(k, k, 1, device=device)
        r[:, a, b], r[:, b, a] = calibrated, 1 - calibrated
        q = -r * r.transpose(1, 2)
        diag = r.square().sum(1)
        q.diagonal(dim1=1, dim2=2).copy_(diag)
        p = torch.full((n, k), 1 / k, dtype=torch.float64, device=device)
        # Batched Wu-Lin-Weng coupling, with libsvm's convergence tolerance.
        # Rows freeze independently so batch size cannot change probabilities.
        active = torch.ones(n, dtype=torch.bool, device=device)
        for _ in range(max(100, k)):
            qp = torch.bmm(q, p.unsqueeze(2)).squeeze(2)
            pqp = (p * qp).sum(1)
            active &= (qp - pqp[:, None]).abs().amax(1) >= .005 / k
            if not active.any().item():
                break
            for t in range(k):
                diff = torch.where(active, (pqp - qp[:, t]) / diag[:, t], 0)
                p[:, t] += diff
                denominator = 1 + diff
                pqp = (pqp + diff * (diff * diag[:, t] + 2 * qp[:, t])) / denominator.square()
                qp = (qp + diff[:, None] * q[:, t, :]) / denominator[:, None]
                p /= denominator[:, None]
        return p

    @torch.inference_mode()
    def predict_proba(self, values):
        return self._couple(self._pairwise_probabilities(values), len(self.classes_)).cpu().numpy()

    @staticmethod
    @torch.inference_mode()
    def predict_many(models, values):
        """Couple independent site models together; preserve each row's stop rule.

        Kernels retain their own scaler/support vectors. Only the equal-class-
        count probability solves are combined, with one CPU transfer per group.
        """
        if len(models) != len(values):
            raise ValueError('Model/feature count mismatch')
        normalized = []
        device_checks = {}
        for model, features in zip(models, values):
            if isinstance(features, torch.Tensor):
                if features.ndim != 2 or features.shape[1] != model.n_features_in_:
                    raise ValueError('Feature contract mismatch')
                device_checks.setdefault(features.device, []).append(torch.isfinite(features).all())
            else:
                features = np.asarray(features)
                if features.ndim != 2 or features.shape[1] != model.n_features_in_:
                    raise ValueError('Feature contract mismatch')
                if not np.isfinite(features).all():
                    raise ValueError('Feature values must be finite')
            normalized.append(features)
        for checks in device_checks.values():
            if not torch.stack(checks).all().item():
                raise ValueError('Feature values must be finite')
        groups = {}
        for index, (model, features) in enumerate(zip(models, normalized)):
            calibrated = model._pairwise_probabilities(features, check_finite=False)
            groups.setdefault((model.device, len(model.classes_)), []).append((index, calibrated))
        results = [None] * len(models)
        for (_, classes), entries in groups.items():
            combined = CudaSVC._couple(torch.cat([item for _, item in entries]), classes).cpu().numpy()
            offset = 0
            for index, item in entries:
                results[index] = combined[offset:offset+len(item)]
                offset += len(item)
        return results


class CudaLogisticClassifier:
    """Balanced multinomial logistic regression, optimized entirely on CUDA."""
    def __init__(self, C=1., max_iter=1000, device=None):
        self.device = f'cuda:{require_cuda(device)}'
        self.C, self.max_iter = C, max_iter

    def fit(self, x, y):
        x = torch.as_tensor(x, dtype=torch.float64, device=self.device)
        labels = torch.as_tensor(y, dtype=torch.long, device=self.device)
        classes, target, counts = torch.unique(labels, sorted=True, return_inverse=True, return_counts=True)
        if len(classes) < 2:
            raise ValueError('Training needs at least two classes')
        self.classes_ = classes.cpu().numpy()
        self.mean = x.mean(0)
        self.scale = x.std(0, correction=0)
        self.scale = torch.where(self.scale > 1e-12, self.scale, 1.)
        x = (x - self.mean) / self.scale
        weights = torch.zeros((len(classes), x.shape[1]), device=self.device, dtype=x.dtype, requires_grad=True)
        bias = torch.zeros(len(classes), device=self.device, dtype=x.dtype, requires_grad=True)
        sample_weights = len(x) / (len(classes) * counts[target].to(x.dtype))
        optimizer = torch.optim.LBFGS([weights, bias], max_iter=self.max_iter, line_search_fn='strong_wolfe', tolerance_grad=1e-8)

        def closure():
            optimizer.zero_grad()
            loss = (torch.nn.functional.cross_entropy(x @ weights.T + bias, target, reduction='none') * sample_weights).mean()
            loss = loss + weights.square().sum() / (2 * self.C * len(x))
            loss.backward()
            return loss

        optimizer.step(closure)
        self.weights, self.bias = weights.detach(), bias.detach()
        return self

    @torch.inference_mode()
    def predict_proba(self, x):
        values = torch.as_tensor(x, dtype=torch.float64, device=self.device)
        return torch.softmax(((values - self.mean) / self.scale) @ self.weights.T + self.bias, dim=1).cpu().numpy()

    def predict(self, x):
        return self.classes_[self.predict_proba(x).argmax(1)]

    def __getstate__(self):
        state = dict(self.__dict__)
        # Portable storage only; inference restores every parameter to CUDA.
        for key in ('mean', 'scale', 'weights', 'bias'):
            state[key] = state[key].cpu().numpy()
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.device = f'cuda:{require_cuda(state.get("device"))}'
        for key in ('mean', 'scale', 'weights', 'bias'):
            setattr(self, key, torch.as_tensor(state[key], dtype=torch.float64, device=self.device))
