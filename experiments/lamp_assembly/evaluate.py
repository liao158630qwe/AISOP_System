"""Position-aware, cycle-local event evaluation with scoreability accounting."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable

try:  # Package invocation: python -m experiments.lamp_assembly.evaluate
    from .data import DatasetValidationError, load_manifest
except ImportError:  # Direct script invocation remains supported.
    from data import DatasetValidationError, load_manifest


def temporal_iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Temporal IoU for half-open [start_s, end_s) event intervals."""
    a0, a1 = float(a["start_s"]), float(a["end_s"])
    b0, b1 = float(b["start_s"]), float(b["end_s"])
    intersection = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return intersection / union if union > 0 else 0.0


def _scipy_assignment(weights: list[list[float]]) -> list[tuple[int, int]] | None:
    try:
        from scipy.optimize import linear_sum_assignment
    except Exception:
        return None
    import numpy as np

    if not weights or not weights[0]:
        return []
    rows, cols = linear_sum_assignment(np.asarray(weights, dtype=np.float64), maximize=True)
    return list(zip(rows.tolist(), cols.tolist()))


def _maximum_cardinality_iou_matching(
    gt: list[dict[str, Any]],
    pred: list[dict[str, Any]],
    threshold: float,
) -> list[tuple[int, int, float]]:
    """Maximize eligible pair count first and total IoU second.

    The optional SciPy assignment uses a cardinality reward larger than the
    maximum possible sum-IoU difference. A deterministic augmenting-path
    fallback preserves maximum cardinality when SciPy is unavailable.
    """
    if not gt or not pred:
        return []
    ious = [[temporal_iou(g, p) for p in pred] for g in gt]
    eligible = [[ious[i][j] >= threshold for j in range(len(pred))] for i in range(len(gt))]
    size = len(gt) + len(pred)
    reward = min(len(gt), len(pred)) + 1.0
    weights = [[0.0 for _ in range(size)] for _ in range(size)]
    for i in range(len(gt)):
        for j in range(len(pred)):
            weights[i][j] = reward + ious[i][j] if eligible[i][j] else -reward
    assignment = _scipy_assignment(weights)
    if assignment is not None:
        matched = [(i, j, ious[i][j]) for i, j in assignment if i < len(gt) and j < len(pred) and eligible[i][j]]
        return sorted(matched)

    # Kuhn augmenting paths guarantee the maximum number of eligible pairs.
    pred_to_gt: dict[int, int] = {}

    def augment(gt_index: int, seen: set[int]) -> bool:
        candidates = sorted(
            (j for j in range(len(pred)) if eligible[gt_index][j]),
            key=lambda j: (-ious[gt_index][j], j),
        )
        for pred_index in candidates:
            if pred_index in seen:
                continue
            seen.add(pred_index)
            if pred_index not in pred_to_gt or augment(pred_to_gt[pred_index], seen):
                pred_to_gt[pred_index] = gt_index
                return True
        return False

    for gt_index in sorted(range(len(gt)), key=lambda i: (-max(ious[i], default=0.0), i)):
        augment(gt_index, set())
    return sorted((gt_index, pred_index, ious[gt_index][pred_index]) for pred_index, gt_index in pred_to_gt.items())


def match_events(
    ground_truth: Iterable[dict[str, Any]],
    predictions: Iterable[dict[str, Any]],
    *,
    tiou_threshold: float = 0.5,
    unobservable_intervals: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Match scoreable events only within identical cycle, target and action."""
    if not 0.0 < tiou_threshold <= 1.0:
        raise ValueError("tiou_threshold must be in (0,1]")
    gt_all = [dict(row) for row in ground_truth]
    pred_all = [dict(row) for row in predictions]
    for kind, rows in (("ground truth", gt_all), ("prediction", pred_all)):
        for index, event in enumerate(rows):
            if "cycle_id" not in event:
                raise ValueError(f"{kind} event {index} is missing cycle_id")
            start, end = event.get("start_s"), event.get("end_s")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or not start < end:
                raise ValueError(f"{kind} event {index} needs start_s < end_s")
    scoreable_gt = [row for row in gt_all if bool(row.get("scoreable", True))]
    unscoreable_gt = [row for row in gt_all if not bool(row.get("scoreable", True))]
    group_key = lambda event: (str(event["cycle_id"]), event.get("target"), event.get("action"))
    gt_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    pred_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in scoreable_gt:
        gt_groups.setdefault(group_key(event), []).append(event)
    for event in pred_all:
        pred_groups.setdefault(group_key(event), []).append(event)

    matched_pairs: list[dict[str, Any]] = []
    matched_gt_ids: set[int] = set()
    matched_pred_ids: set[int] = set()
    for key in sorted(set(gt_groups) | set(pred_groups), key=lambda x: tuple(str(v) for v in x)):
        g_events = sorted(gt_groups.get(key, []), key=lambda e: (float(e["start_s"]), str(e.get("event_id", ""))))
        p_events = sorted(pred_groups.get(key, []), key=lambda e: (float(e["start_s"]), str(e.get("event_id", ""))))
        pairs = _maximum_cardinality_iou_matching(g_events, p_events, tiou_threshold)
        for gi, pi, iou in pairs:
            g, p = g_events[gi], p_events[pi]
            matched_pairs.append({"ground_truth": g, "prediction": p, "tiou": float(iou)})
            matched_gt_ids.add(id(g))
            matched_pred_ids.add(id(p))
    # The sorted group lists contain new dict references, so derive unmatched
    # by identity after flattening those same grouped rows.
    gt_flat = [event for events in gt_groups.values() for event in events]
    pred_flat = [event for events in pred_groups.values() for event in events]
    unknown_by_cycle = unobservable_intervals or {}

    def overlaps_unknown(event: dict[str, Any]) -> bool:
        for interval in unknown_by_cycle.get(str(event["cycle_id"]), []):
            if min(float(event["end_s"]), float(interval["end_s"])) > max(float(event["start_s"]), float(interval["start_s"])):
                return True
        return False

    unmatched_gt = [event for event in gt_flat if id(event) not in matched_gt_ids]
    unmatched_pred = [event for event in pred_flat if id(event) not in matched_pred_ids]
    ambiguous_predictions = [event for event in unmatched_pred if overlaps_unknown(event)]
    known_false_positives = [event for event in unmatched_pred if not overlaps_unknown(event)]
    tp = len(matched_pairs)
    fn = len(unmatched_gt)
    fp = len(known_false_positives)
    conservative_fp = fp + len(ambiguous_predictions)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    lower_precision = tp / (tp + conservative_fp) if tp + conservative_fp else 0.0
    lower_f1 = 2 * lower_precision * recall / (lower_precision + recall) if lower_precision + recall else 0.0
    boundary = [
        {
            "start_abs_error_s": abs(float(pair["prediction"]["start_s"]) - float(pair["ground_truth"]["start_s"])),
            "end_abs_error_s": abs(float(pair["prediction"]["end_s"]) - float(pair["ground_truth"]["end_s"])),
        }
        for pair in matched_pairs
    ]
    return {
        "tiou_threshold": float(tiou_threshold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "scoreable_ground_truth_events": len(gt_flat),
        "unscoreable_ground_truth_events": len(unscoreable_gt),
        "total_ground_truth_events": len(gt_all),
        "annotated_event_scoreability_coverage": len(gt_flat) / len(gt_all) if gt_all else None,
        "matched_scoreable_ground_truth_coverage": tp / len(gt_flat) if gt_flat else None,
        "prediction_count": len(pred_flat),
        "matched_prediction_count": tp,
        "ambiguous_unmatched_prediction_count": len(ambiguous_predictions),
        "prediction_scoreability_coverage": (len(pred_flat) - len(ambiguous_predictions)) / len(pred_flat) if pred_flat else None,
        "conservative_sensitivity_diagnostic": {
            "fp_if_all_ambiguous_predictions_are_counted_as_false_positives": conservative_fp,
            "precision": lower_precision,
            "recall": recall,
            "f1": lower_f1,
            "interpretation": "Conservative sensitivity diagnostic only; predictions in unobservable regions are not established false positives.",
        },
        "matched_events": matched_pairs,
        "unmatched_prediction_events": unmatched_pred,
        "unmatched_scoreable_ground_truth_events": unmatched_gt,
        "ambiguous_unmatched_prediction_events": ambiguous_predictions,
        "boundary_errors_matched_only": {
            "matched_count": len(boundary),
            "mean_start_abs_error_s": sum(item["start_abs_error_s"] for item in boundary) / len(boundary) if boundary else None,
            "mean_end_abs_error_s": sum(item["end_abs_error_s"] for item in boundary) / len(boundary) if boundary else None,
            "samples": boundary,
        },
        "unmatched_events": {
            "known_false_positive_count": fp,
            "ambiguous_unmatched_prediction_count": len(ambiguous_predictions),
            "false_negative_count": fn,
        },
    }


def _group_metric(
    ground_truth: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    tiou_threshold: float,
    unobservable_intervals: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    result = match_events(ground_truth, predictions, tiou_threshold=tiou_threshold, unobservable_intervals=unobservable_intervals)
    # Keep group reports compact while retaining denominator and scoreability.
    return {key: result[key] for key in (
        "tp", "fp", "fn", "precision", "recall", "f1",
        "scoreable_ground_truth_events", "unscoreable_ground_truth_events",
        "total_ground_truth_events", "annotated_event_scoreability_coverage",
        "ambiguous_unmatched_prediction_count", "prediction_scoreability_coverage",
    )}


def evaluate_cycles(
    cycle_records: Iterable[dict[str, Any]],
    predictions: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
    *,
    tiou_threshold: float = 0.5,
    is_synthetic: bool,
) -> dict[str, Any]:
    """Evaluate events by exact cycle/target/action, retaining all missed GT."""
    records = list(cycle_records)
    if isinstance(predictions, dict):
        prediction_map = {str(key): [dict(row) for row in values] for key, values in predictions.items()}
    else:
        prediction_map: dict[str, list[dict[str, Any]]] = {}
        for row in predictions:
            prediction_map.setdefault(str(row["cycle_id"]), []).append(dict(row))
    gt_events: list[dict[str, Any]] = []
    pred_events: list[dict[str, Any]] = []
    cycle_context: dict[str, dict[str, Any]] = {}
    unknown_intervals: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        cid = str(record["cycle_id"])
        cycle_context[cid] = {"condition": record.get("condition", "unspecified"), "is_synthetic": bool(record.get("is_synthetic", is_synthetic))}
        unknown_intervals[cid] = list(record.get("unobservable_intervals", []))
        for event in record.get("events", []):
            gt_events.append(dict(event) | {"cycle_id": cid, "condition": record.get("condition", "unspecified"), "is_synthetic": bool(record.get("is_synthetic", is_synthetic))})
        for event in prediction_map.get(cid, []):
            pred_events.append(dict(event) | {"cycle_id": cid, "condition": record.get("condition", "unspecified"), "is_synthetic": bool(record.get("is_synthetic", is_synthetic))})
    unknown_cycle_predictions = sorted(set(prediction_map) - set(cycle_context))
    if unknown_cycle_predictions:
        raise ValueError(f"predictions contain unknown cycle_id(s): {unknown_cycle_predictions}")

    overall = match_events(gt_events, pred_events, tiou_threshold=tiou_threshold, unobservable_intervals=unknown_intervals)
    predictions_in_unobservable_regions = []
    predictions_in_scoreable_regions = []
    for event in pred_events:
        overlap = any(
            min(float(event["end_s"]), float(interval["end_s"]))
            > max(float(event["start_s"]), float(interval["start_s"]))
            for interval in unknown_intervals.get(str(event["cycle_id"]), [])
        )
        if overlap:
            predictions_in_unobservable_regions.append(event)
        else:
            predictions_in_scoreable_regions.append(event)
    condition_keys = sorted({str(row.get("condition", "unspecified")) for row in gt_events + pred_events})
    position_keys = sorted({str(row.get("target")) for row in gt_events + pred_events if row.get("target") is not None})
    action_keys = sorted({str(row.get("action")) for row in gt_events + pred_events if row.get("action") is not None})
    by_condition = {}
    for condition in condition_keys:
        by_condition[condition] = _group_metric(
            [row for row in gt_events if str(row.get("condition", "unspecified")) == condition],
            [row for row in pred_events if str(row.get("condition", "unspecified")) == condition],
            tiou_threshold=tiou_threshold,
            unobservable_intervals=unknown_intervals,
        )
    by_position = {}
    for target in position_keys:
        by_position[target] = _group_metric(
            [row for row in gt_events if str(row.get("target")) == target],
            [row for row in pred_events if str(row.get("target")) == target],
            tiou_threshold=tiou_threshold,
            unobservable_intervals=unknown_intervals,
        )
    by_action = {}
    for action in action_keys:
        by_action[action] = _group_metric(
            [row for row in gt_events if str(row.get("action")) == action],
            [row for row in pred_events if str(row.get("action")) == action],
            tiou_threshold=tiou_threshold,
            unobservable_intervals=unknown_intervals,
        )
    return {
        "metric": "positioned_event_f1",
        "is_synthetic": bool(is_synthetic),
        "source_note": "Synthetic smoke evaluation; it does not estimate or claim field accuracy." if is_synthetic else "Evaluation on manifest-declared real feature data.",
        "matching_constraints": ["same cycle_id", "same target", "same action", f"tIoU >= {tiou_threshold}", "one-to-one maximum-cardinality assignment; IoU breaks ties"],
        "unknown_and_coverage": {
            "scoreable_events": overall["scoreable_ground_truth_events"],
            "explicitly_unscoreable_events": overall["unscoreable_ground_truth_events"],
            "all_annotated_events": overall["total_ground_truth_events"],
            "scoreability_coverage": overall["annotated_event_scoreability_coverage"],
            "matched_scoreable_event_coverage": overall["matched_scoreable_ground_truth_coverage"],
            "unobservable_intervals": sum(len(record.get("unobservable_intervals", [])) for record in records),
            "predictions_overlapping_unobservable_intervals": len(predictions_in_unobservable_regions),
            "ambiguous_unmatched_prediction_count": overall["ambiguous_unmatched_prediction_count"],
            "policy": "Scoreable ground truth is matched first. Unmatched predictions overlapping explicitly unobservable intervals are ambiguous and excluded from primary FP; a conservative sensitivity diagnostic counts them as FP. A prediction overlapping an unknown interval remains a TP when it matches a scoreable event.",
        },
        "overall": overall,
        "scoreable_regions_only": {
            "ambiguous_unmatched_prediction_count": overall["ambiguous_unmatched_prediction_count"],
            "metric": {key: overall[key] for key in ("tp", "fp", "fn", "precision", "recall", "f1", "prediction_scoreability_coverage")},
        },
        "by_position": by_position,
        "by_condition": by_condition,
        "by_action": by_action,
    }


def _read_predictions(path: Path) -> dict[str, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and "predictions" in value:
        value = value["predictions"]
    if isinstance(value, dict):
        return {str(key): list(rows) for key, rows in value.items()}
    if isinstance(value, list):
        result: dict[str, list[dict[str, Any]]] = {}
        for row in value:
            result.setdefault(str(row["cycle_id"]), []).append(row)
        return result
    raise ValueError("prediction JSON must be a cycle_id-to-event-list mapping or event list")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate cycle-local, position-aware operation events.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True, help="JSON mapping cycle IDs to predicted events")
    parser.add_argument("--split", choices=("train", "val", "test", "all"), default="test")
    parser.add_argument("--tiou", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        manifest, _ = load_manifest(args.manifest)
        rows = [row for row in manifest["cycles"] if args.split == "all" or row.get("split") == args.split]
        if not rows:
            raise ValueError(f"manifest has no cycles in split {args.split!r}")
        prediction_map = _read_predictions(args.predictions)
        known_cycle_ids = {str(row["cycle_id"]) for row in manifest["cycles"]}
        unknown_ids = sorted(set(prediction_map) - known_cycle_ids)
        if unknown_ids:
            raise ValueError(f"prediction JSON contains cycle ID(s) absent from manifest: {unknown_ids}")
        selected_cycle_ids = {str(row["cycle_id"]) for row in rows}
        filtered_other_split_count = sum(len(events) for cid, events in prediction_map.items() if cid not in selected_cycle_ids)
        predictions = {cid: prediction_map.get(cid, []) for cid in (row["cycle_id"] for row in rows)}
        report = evaluate_cycles(rows, predictions, tiou_threshold=args.tiou, is_synthetic=manifest["is_synthetic"])
        report["split"] = args.split
        report["predictions_from_other_splits_filtered"] = filtered_other_split_count
        report["manifest"] = str(args.manifest.resolve())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output.resolve()), "overall": report["overall"], "is_synthetic": report["is_synthetic"]}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, DatasetValidationError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
