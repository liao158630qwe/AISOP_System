from __future__ import annotations

import math

import pytest

from experiments.lamp_assembly.evaluate import evaluate_cycles, match_events


def event(cycle: str, target: int, start: float, end: float, *, action: int = 2, **extra):
    return {"cycle_id": cycle, "target": target, "action": action, "start_s": start, "end_s": end, **extra}


def test_matching_maximizes_cardinality_before_iou_and_preserves_duplicates():
    # GT-1 can match either prediction, but GT-2 can match only pred-1 at this
    # threshold. Greedy highest-IoU pairing could lose a true event.
    ground_truth = [event("c1", 0, 0, 10), event("c1", 0, 0, 3)]
    predictions = [event("c1", 0, 0, 3), event("c1", 0, 4, 10)]
    result = match_events(ground_truth, predictions, tiou_threshold=0.3)
    assert (result["tp"], result["fp"], result["fn"]) == (2, 0, 0)

    duplicate = match_events([event("c1", 0, 1, 4)], [event("c1", 0, 1, 4), event("c1", 0, 1.1, 4.1)])
    assert (duplicate["tp"], duplicate["fp"], duplicate["fn"]) == (1, 1, 0)


def test_cycle_target_action_are_hard_match_constraints():
    ground_truth = [event("c1", 0, 1, 3)]
    predictions = [
        event("c2", 0, 1, 3),
        event("c1", 1, 1, 3),
        event("c1", 0, 1, 3, action=1),
    ]
    result = match_events(ground_truth, predictions)
    assert (result["tp"], result["fp"], result["fn"]) == (0, 3, 1)


def test_unobservable_regions_are_ambiguous_unless_candidate_matches_scoreable_truth():
    records = [{
        "cycle_id": "c1",
        "condition": "occlusion",
        "is_synthetic": True,
        "events": [
            event("c1", 0, 1.0, 4.0, scoreable=True),
            {"cycle_id": "c1", "target": None, "action": None, "start_s": 2.0, "end_s": 3.0, "scoreable": False},
        ],
        "unobservable_intervals": [{"start_s": 2.0, "end_s": 3.0}],
    }]
    predictions = {"c1": [
        event("c1", 0, 1.1, 3.9),  # overlaps unknown span but still matches known event
        event("c1", 1, 2.1, 2.8),  # no scoreable GT: ambiguous, not a definite FP
    ]}
    report = evaluate_cycles(records, predictions, tiou_threshold=0.5, is_synthetic=True)
    overall = report["overall"]
    assert (overall["tp"], overall["fp"], overall["fn"]) == (1, 0, 0)
    assert overall["unscoreable_ground_truth_events"] == 1
    assert overall["ambiguous_unmatched_prediction_count"] == 1
    assert overall["conservative_sensitivity_diagnostic"]["fp_if_all_ambiguous_predictions_are_counted_as_false_positives"] == 1
    assert "field accuracy" in report["source_note"]


def test_missing_predictions_remain_false_negatives_and_boundary_errors_are_not_latency():
    truth = [event("c1", 0, 1.0, 2.0), event("c1", 1, 3.0, 4.0, scoreable=False)]
    result = match_events(truth, [], tiou_threshold=0.25)
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 1)
    assert result["unscoreable_ground_truth_events"] == 1
    assert result["boundary_errors_matched_only"]["matched_count"] == 0
    assert result["unmatched_events"]["false_negative_count"] == 1
    assert not any("latency" in key for key in result)

    matched = match_events([event("c1", 0, 1, 3)], [event("c1", 0, 1.2, 3.4)])
    errors = matched["boundary_errors_matched_only"]
    assert errors["matched_count"] == 1
    assert math.isclose(errors["mean_start_abs_error_s"], 0.2)
    assert math.isclose(errors["mean_end_abs_error_s"], 0.4)


def test_zero_overlap_threshold_and_nonfinite_intervals_are_rejected():
    with pytest.raises(ValueError, match="tiou_threshold"):
        match_events([], [], tiou_threshold=0.0)
    with pytest.raises(ValueError, match="start_s"):
        match_events([event("c1", 0, math.nan, 2)], [])


def test_empty_prediction_map_is_explicitly_a_miss():
    record = {"cycle_id": "c1", "events": [event("c1", 1, 0.5, 1.0)], "condition": "short"}
    report = evaluate_cycles([record], {"c1": []}, tiou_threshold=0.5, is_synthetic=True)
    assert report["overall"]["fn"] == 1
    assert report["by_position"]["1"]["fn"] == 1
