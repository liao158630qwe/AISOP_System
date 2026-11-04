import numpy as np
import pytest
from algorithms.pose import OneEuroFilter, OneEuroPoseFilter


def test_one_euro_filter_reduces_jitter():
    filt = OneEuroFilter(min_cutoff=1.0, beta=0.05, d_cutoff=1.0)
    # Simulate stationary point with Gaussian jitter
    t = 0.0
    dt = 1.0 / 30.0
    true_val = 100.0
    np.random.seed(42)
    raw_vals = [true_val + np.random.normal(0, 1.0) for _ in range(60)]
    filtered_vals = []
    for raw in raw_vals:
        filtered_vals.append(filt.filter(raw, t))
        t += dt

    # Skip initial convergence frames
    raw_jitter = np.std(raw_vals[10:])
    filtered_jitter = np.std(filtered_vals[10:])
    assert filtered_jitter < raw_jitter * 0.5, "1€ filter should damp stationary jitter by at least 50%"


def test_one_euro_filter_adapts_to_fast_motion_with_low_lag():
    filt = OneEuroFilter(min_cutoff=1.0, beta=0.1, d_cutoff=1.0)
    t = 0.0
    dt = 1.0 / 30.0
    # Step response: sudden movement
    for _ in range(10):
        filt.filter(0.0, t)
        t += dt

    # Large sudden jump
    result = filt.filter(100.0, t)
    # Due to high speed, cutoff increases and allows rapid jump
    assert result > 50.0, "Filter should respond quickly to fast motion"


def test_one_euro_pose_filter_preserves_visibility():
    pose_filt = OneEuroPoseFilter(min_cutoff=1.5, beta=0.05)
    k = np.zeros((17, 3), dtype=float)
    k[:, :2] = 0.5
    k[:, 2] = 0.9
    k[9] = [0.0, 0.0, 0.1]  # Joint 9 is occluded

    res1 = pose_filt.filter(k, 0.0)
    assert res1[9, 2] == 0.1
    assert np.array_equal(res1[9], k[9])

    k_next = k.copy()
    k_next[:, :2] += 0.05
    k_next[9] = [0.0, 0.0, 0.05]
    res2 = pose_filt.filter(k_next, 0.033)
    assert res2[9, 2] == 0.05
    assert np.array_equal(res2[9], k_next[9])


def test_filter_reset_on_gap():
    filt = OneEuroFilter()
    filt.filter(10.0, 0.0)
    filt.filter(10.2, 0.033)
    # Time gap > 0.5s should reset history
    res = filt.filter(20.0, 1.0)
    assert res == 20.0
