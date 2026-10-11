"""Confidence-weighted causal stage confirmation, independent of SOP order."""
import numpy as np


class EvidenceFilter:
    """Integrate the winning class's margin before committing a stage change.

    Strong evidence confirms sooner than ambiguous evidence. A candidate must stay
    the winner; returning to the active class or changing candidate clears its evidence.
    Unavailable/low-confidence observations emit unknown and clear pending confirmation.
    All classes and transition directions use the same rule. No boundary is backdated.
    """

    def __init__(self, alpha=.3, budget=2., threshold=.4, margin=0.):
        values = np.asarray([alpha, budget, threshold, margin], dtype=float)
        if (not np.isfinite(values).all() or not 0 < alpha <= 1 or budget <= 0
                or not 0 <= threshold <= 1 or not 0 <= margin < 1):
            raise ValueError('Invalid evidence filter parameters')
        self.alpha, self.budget, self.threshold, self.margin = alpha, budget, threshold, margin
        self.reset()

    def reset(self):
        self.average, self.active, self.candidate, self.evidence = None, -1, -1, 0.

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
                self.active, self.candidate, self.evidence = -1, -1, 0.
                result.append(-1)
                continue
            if label == self.active:
                self.candidate, self.evidence = -1, 0.
            else:
                if label != self.candidate:
                    self.candidate, self.evidence = label, 0.
                # Runner-up margin also applies at startup; no one-frame startup spike.
                other = max(float(self.average[i]) for i in range(5) if i != label)
                self.evidence += max(0., float(self.average[label])-other-self.margin)
                if self.evidence >= self.budget:
                    self.active = label
                    self.candidate, self.evidence = -1, 0.
            result.append(self.active)
        return np.asarray(result, dtype=np.int64)
