"""Causal online HMM forward filtering of five-stage probabilities (v3).

The filter tracks the posterior over stages under a sticky transition prior and
softened emissions, then confirms the posterior argmax only after `hold`
consecutive frames. No future frames, no SOP order constraint, and no boundary
backdating: output at time t depends only on observations up to t.
"""
import numpy as np


class HMMFilter:
    """Online forward filtering: posterior_t propto emission_t * T^T posterior_{t-1}.

    T keeps the active stage with probability `p_self` and switches to any single
    other stage with (1-p_self)/4. Emissions mix model probabilities with a uniform
    floor (`epsilon`), bounding the evidence of one frame so a short spike cannot
    outvote the prior. A posterior argmax replaces the active stage only after
    `hold` consecutive frames; posterior below `threshold` emits unknown and clears
    pending confirmation while keeping the posterior. Invalid inputs reset all state.
    All stages and transition directions use the same rule.
    """

    def __init__(self, p_self=.8, epsilon=.4, hold=3, threshold=0.):
        values = np.asarray([p_self, epsilon, threshold], dtype=float)
        if (not np.isfinite(values).all() or not 0 < p_self < 1 or not 0 <= epsilon < 1
                or isinstance(hold, (bool, np.bool_)) or not isinstance(hold, (int, np.integer)) or hold < 1
                or not 0 <= threshold <= 1):
            raise ValueError('Invalid HMM filter parameters')
        self.p_self, self.epsilon = float(p_self), float(epsilon)
        self.hold, self.threshold = int(hold), float(threshold)
        self.reset()

    def reset(self):
        self.posterior = None
        self.active, self.candidate, self.count = -1, -1, 0

    def push(self, probabilities, valid):
        probabilities, valid = np.asarray(probabilities), np.asarray(valid)
        if (probabilities.ndim != 2 or probabilities.shape[1] != 5
                or valid.shape != (len(probabilities),) or not np.isin(valid, [0, 1]).all()
                or not np.isfinite(probabilities).all() or np.any(probabilities < 0)
                or not np.allclose(probabilities.sum(axis=1), 1., atol=1e-5)):
            raise ValueError('Expected normalized N x 5 probabilities and matching binary validity mask')
        switch = (1.-self.p_self)/4
        transition = np.full((5, 5), switch)
        np.fill_diagonal(transition, self.p_self)
        result = []
        for p, available in zip(probabilities, valid):
            if not available:
                self.reset()
                result.append(-1)
                continue
            emission = (1.-self.epsilon)*p + self.epsilon/5
            prior = np.full(5, 1/5) if self.posterior is None else transition.T @ self.posterior
            posterior = prior*emission
            total = float(posterior.sum())
            posterior = posterior/total if total > 0 else np.full(5, 1/5)
            self.posterior = posterior
            label = int(posterior.argmax())
            if posterior[label] < self.threshold:
                self.active, self.candidate, self.count = -1, -1, 0
                result.append(-1)
                continue
            self.count = self.count+1 if label == self.candidate else 1
            self.candidate = label
            if self.count >= self.hold:
                self.active = label
            result.append(self.active)
        return np.asarray(result, dtype=np.int64)


def filter_stages_v3(probabilities, valid, **parameters):
    """Convenience wrapper equivalent to HMMFilter(**parameters).push."""
    return HMMFilter(**parameters).push(probabilities, valid)


class EvidenceDecayFilter:
    """Margin evidence that decays instead of resetting when the winner flips.

    Like confidence-evidence-v1, but partial evidence for a competing stage
    survives short interruptions (multiplicative decay per frame) so genuine
    low-margin stages are not delayed by alternating argmax. A switch commits
    when the winning class's accumulated evidence reaches `budget`, then all
    evidence clears. Invalid input resets everything; low confidence emits
    unknown and clears pending evidence. No future frames, no SOP order
    constraint, no backdating; every class and direction uses the same rule.
    """

    def __init__(self, alpha=.3, budget=2., threshold=0., margin=.1, decay=.9):
        values = np.asarray([alpha, budget, threshold, margin, decay], dtype=float)
        if (not np.isfinite(values).all() or not 0 < alpha <= 1 or budget <= 0
                or not 0 <= threshold <= 1 or not 0 <= margin < 1 or not 0 < decay < 1):
            raise ValueError('Invalid evidence decay filter parameters')
        self.alpha, self.budget, self.threshold = float(alpha), float(budget), float(threshold)
        self.margin, self.decay = float(margin), float(decay)
        self.reset()

    def reset(self):
        self.average, self.active, self.evidence = None, -1, np.zeros(5)

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
                self.active, self.evidence = -1, np.zeros(5)
                result.append(-1)
                continue
            self.evidence *= self.decay
            if label != self.active:
                other = max(float(self.average[i]) for i in range(5) if i != label)
                self.evidence[label] += max(0., float(self.average[label])-other-self.margin)
                if self.evidence[label] >= self.budget:
                    self.active = label
                    self.evidence = np.zeros(5)
            result.append(self.active)
        return np.asarray(result, dtype=np.int64)
