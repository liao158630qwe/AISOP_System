"""Cycle-level feature and label contracts for lamp assembly experiments.

This module deliberately treats missing or unobservable evidence as labeled
uncertainty.  It never turns a missing detector output into background.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np


SCHEMA_VERSION = 1
SPLITS = ("train", "val", "test")
NPZ_ARRAYS = (
    "timestamps",
    "features",
    "targets",
    "masks",
    "ages",
    "quality",
    "action",
    "action_mask",
    "position",
    "position_mask",
    "boundary",
    "boundary_mask",
    "quality_label",
    "quality_mask",
    "is_synthetic",
)


class DatasetValidationError(ValueError):
    """Raised when a manifest or cycle feature package violates the contract."""


@dataclass(frozen=True)
class CycleData:
    record: dict[str, Any]
    arrays: dict[str, np.ndarray]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetValidationError(message)


def _as_bool_scalar(value: np.ndarray, name: str) -> bool:
    _require(np.asarray(value).shape in ((), (1,)), f"{name} must be a scalar boolean")
    scalar = np.asarray(value).reshape(()).item()
    _require(isinstance(scalar, (bool, np.bool_)), f"{name} must be a scalar boolean")
    return bool(scalar)


def safe_feature_path(root: Path, relative_path: str) -> Path:
    """Resolve a manifest feature path and reject absolute/path-traversal paths."""
    rel = Path(relative_path)
    _require(not rel.is_absolute(), f"feature_file must be relative: {relative_path!r}")
    root_resolved = root.resolve()
    path = (root_resolved / rel).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise DatasetValidationError(f"feature_file escapes dataset root: {relative_path!r}") from exc
    return path


def assign_operator_splits(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign whole operators to deterministic 60/20/20 train/val/test groups."""
    operators = sorted({str(row.get("operator_id", "")) for row in records})
    _require(len(operators) >= 3, "at least three operators are required for grouped train/val/test splits")
    n = len(operators)
    n_train = max(1, int(round(n * 0.60)))
    n_val = max(1, int(round(n * 0.20)))
    if n_train + n_val >= n:
        n_train = n - 2
        n_val = 1
    split_by_operator = {
        **{op: "train" for op in operators[:n_train]},
        **{op: "val" for op in operators[n_train : n_train + n_val]},
        **{op: "test" for op in operators[n_train + n_val :]},
    }
    result = []
    for row in records:
        item = dict(row)
        item["split"] = split_by_operator[str(item["operator_id"])]
        result.append(item)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_split_isolation(
    records: Iterable[dict[str, Any]],
    *,
    dataset_root: Path | None = None,
    require_feature_hash: bool = False,
) -> dict[str, int]:
    """Validate nonempty splits and operator/session/cycle-level separation."""
    rows = list(records)
    _require(bool(rows), "manifest contains no cycles")
    split_by_operator: dict[str, str] = {}
    split_by_session: dict[str, str] = {}
    cycle_ids: set[str] = set()
    path_owner: dict[str, tuple[str, str]] = {}
    hash_split: dict[str, str] = {}
    split_counts = {split: 0 for split in SPLITS}
    for row in rows:
        cycle_id = str(row.get("cycle_id", ""))
        operator_id = str(row.get("operator_id", ""))
        session_id = str(row.get("session_id", ""))
        split = str(row.get("split", ""))
        _require(cycle_id, "cycle_id must be a nonempty string")
        _require(operator_id, f"{cycle_id}: operator_id must be nonempty")
        _require(session_id, f"{cycle_id}: session_id must be nonempty")
        _require(split in SPLITS, f"{cycle_id}: split must be one of {SPLITS}, got {split!r}")
        _require(cycle_id not in cycle_ids, f"duplicate cycle_id {cycle_id!r}")
        cycle_ids.add(cycle_id)
        feature_file = row.get("feature_file")
        if feature_file is not None:
            if dataset_root is not None:
                feature_path = safe_feature_path(dataset_root, str(feature_file))
                _require(feature_path.is_file(), f"{cycle_id}: feature package does not exist: {feature_file}")
                path_key = str(feature_path).casefold()
            else:
                path_key = str(Path(str(feature_file)).as_posix()).casefold()
            prior_owner = path_owner.setdefault(path_key, (cycle_id, split))
            _require(prior_owner[0] == cycle_id, f"feature_file {feature_file!r} is referenced by multiple cycles ({prior_owner[0]!r}, {cycle_id!r})")
        feature_hash = row.get("feature_sha256")
        if require_feature_hash:
            _require(isinstance(feature_hash, str) and len(feature_hash) == 64 and all(c in "0123456789abcdef" for c in feature_hash.lower()), f"{cycle_id}: feature_sha256 must be a 64-character SHA-256 digest")
        if feature_hash is not None:
            feature_hash = str(feature_hash).lower()
            prior_split = hash_split.setdefault(feature_hash, split)
            _require(prior_split == split, f"feature SHA-256 is shared across {prior_split!r} and {split!r} splits ({cycle_id})")
        for key, value, seen in (
            ("operator_id", operator_id, split_by_operator),
            ("session_id", session_id, split_by_session),
        ):
            prior = seen.setdefault(value, split)
            _require(prior == split, f"{key} {value!r} leaks across {prior!r} and {split!r} splits")
        split_counts[split] += 1
    _require(all(split_counts[name] > 0 for name in SPLITS), f"all train/val/test splits must be nonempty: {split_counts}")
    return split_counts


def _read_cycle_npz(path: Path) -> dict[str, np.ndarray]:
    try:
        with np.load(path, allow_pickle=False) as archive:
            missing = set(NPZ_ARRAYS) - set(archive.files)
            _require(not missing, f"{path.name}: missing arrays {sorted(missing)}")
            return {name: np.asarray(archive[name]) for name in NPZ_ARRAYS}
    except DatasetValidationError:
        raise
    except Exception as exc:
        raise DatasetValidationError(f"could not read feature package {path}: {exc}") from exc


def validate_cycle_arrays(
    record: dict[str, Any],
    arrays: dict[str, np.ndarray],
    *,
    num_actions: int = 4,
    num_quality_classes: int = 2,
    expected_synthetic: bool | None = None,
) -> dict[str, int]:
    """Check shapes, coordinate ranges, masks, timestamps and label validity."""
    cid = str(record.get("cycle_id", "<unknown>"))
    for key in NPZ_ARRAYS:
        _require(key in arrays, f"{cid}: missing array {key}")
    ts = np.asarray(arrays["timestamps"])
    features = np.asarray(arrays["features"])
    targets = np.asarray(arrays["targets"])
    masks = np.asarray(arrays["masks"])
    ages = np.asarray(arrays["ages"])
    quality = np.asarray(arrays["quality"])
    action = np.asarray(arrays["action"])
    action_mask = np.asarray(arrays["action_mask"])
    position = np.asarray(arrays["position"])
    position_mask = np.asarray(arrays["position_mask"])
    boundary = np.asarray(arrays["boundary"])
    boundary_mask = np.asarray(arrays["boundary_mask"])
    quality_label = np.asarray(arrays["quality_label"])
    quality_mask = np.asarray(arrays["quality_mask"])

    _require(ts.ndim == 1 and ts.size >= 2, f"{cid}: timestamps must be [T] with T>=2")
    _require(np.issubdtype(ts.dtype, np.number) and np.isfinite(ts).all(), f"{cid}: timestamps must be finite numeric values")
    _require(np.all(np.diff(ts.astype(np.float64)) > 0), f"{cid}: timestamps must be strictly increasing")
    _require(features.ndim == 3, f"{cid}: features must have shape [T,N,D]")
    t, n, d = features.shape
    _require(t == ts.size and n >= 1 and d >= 3, f"{cid}: inconsistent or empty feature dimensions")
    _require(np.issubdtype(features.dtype, np.number) and np.isfinite(features).all(), f"{cid}: features must be finite")
    _require(np.logical_and(features[..., :2] >= 0, features[..., :2] <= 1).all(), f"{cid}: feature XY coordinates must be normalized to [0,1]")
    _require(targets.ndim == 2 and targets.shape[0] >= 1 and targets.shape[1] >= 2, f"{cid}: targets must have shape [K,G]")
    k, g = targets.shape
    _require(np.issubdtype(targets.dtype, np.number) and np.isfinite(targets).all(), f"{cid}: targets must be finite")
    _require(np.logical_and(targets[:, :2] >= 0, targets[:, :2] <= 1).all(), f"{cid}: target XY coordinates must be normalized to [0,1]")
    _require(masks.shape == (t, n) and masks.dtype == np.bool_, f"{cid}: masks must be boolean [T,N]")
    _require(ages.shape == (t, n) and np.isfinite(ages).all() and (ages >= 0).all(), f"{cid}: ages must be finite, nonnegative [T,N]")
    _require(quality.ndim == 3 and quality.shape[:2] == (t, n) and quality.shape[2] >= 1, f"{cid}: quality must be [T,N,Q]")
    _require(np.isfinite(quality).all(), f"{cid}: quality must be finite")
    _require(np.logical_and(quality >= 0, quality <= 1).all(), f"{cid}: quality features must be in [0,1]")
    _require(action.shape == (t, k) and np.issubdtype(action.dtype, np.integer), f"{cid}: action must be integer [T,K]")
    _require(action_mask.shape == (t, k) and action_mask.dtype == np.bool_, f"{cid}: action_mask must be boolean [T,K]")
    _require(np.logical_and((action == -100) | ((action >= 0) & (action < num_actions)), True).all(), f"{cid}: action labels must be -100 or in [0,{num_actions - 1}]")
    _require(np.all(action[action_mask] >= 0) and np.all(action[action_mask] < num_actions), f"{cid}: supervised action labels must be valid classes")
    _require(position.shape == (t,) and np.issubdtype(position.dtype, np.integer), f"{cid}: position must be integer [T]")
    _require(position_mask.shape == (t,) and position_mask.dtype == np.bool_, f"{cid}: position_mask must be boolean [T]")
    _require(np.logical_or(position == -100, np.logical_and(position >= 0, position <= k + 1)).all(), f"{cid}: position labels must be -100 or 0..K (no target)/K+1 (unknown)")
    _require(np.all(position[position_mask] >= 0) and np.all(position[position_mask] <= k + 1), f"{cid}: supervised position labels are invalid")
    _require(boundary.shape == (t, k, 2) and np.issubdtype(boundary.dtype, np.number), f"{cid}: boundary must be numeric [T,K,2]")
    _require(np.isfinite(boundary).all() and np.logical_and(boundary >= 0, boundary <= 1).all(), f"{cid}: boundary labels must be finite in [0,1]")
    _require(boundary_mask.shape == (t, k, 2) and boundary_mask.dtype == np.bool_, f"{cid}: boundary_mask must be boolean [T,K,2]")
    _require(quality_label.shape == (t, k) and np.issubdtype(quality_label.dtype, np.integer), f"{cid}: quality_label must be integer [T,K]")
    _require(quality_mask.shape == (t, k) and quality_mask.dtype == np.bool_, f"{cid}: quality_mask must be boolean [T,K]")
    _require(np.all((quality_label == -100) | ((quality_label >= 0) & (quality_label < num_quality_classes))), f"{cid}: quality labels must be -100 or valid classes")
    _require(np.all((quality_label[quality_mask] >= 0) & (quality_label[quality_mask] < num_quality_classes)), f"{cid}: supervised quality labels are invalid")
    synthetic = _as_bool_scalar(arrays["is_synthetic"], f"{cid}: is_synthetic")
    if expected_synthetic is not None:
        _require(synthetic == expected_synthetic, f"{cid}: NPZ is_synthetic disagrees with manifest")

    events = record.get("events", [])
    _require(isinstance(events, list), f"{cid}: events must be a list")
    lower = float(ts[0])
    # Permit the final boundary to lie one sample interval after the last frame.
    upper = float(ts[-1] + np.median(np.diff(ts)))
    for index, event in enumerate(events):
        _require(isinstance(event, dict), f"{cid}: event {index} must be an object")
        start, end = event.get("start_s"), event.get("end_s")
        _require(isinstance(start, (int, float)) and isinstance(end, (int, float)), f"{cid}: event {index} needs numeric start_s/end_s")
        _require(np.isfinite([start, end]).all() and lower <= start < end <= upper, f"{cid}: event {index} is outside cycle bounds or has invalid duration")
        scoreable = bool(event.get("scoreable", True))
        if scoreable:
            target, act = event.get("target"), event.get("action")
            _require(isinstance(target, int) and 0 <= target < k, f"{cid}: scoreable event {index} needs target in [0,K)")
            _require(isinstance(act, int) and 0 <= act < num_actions, f"{cid}: scoreable event {index} needs a valid action")
    unknown = record.get("unobservable_intervals", [])
    _require(isinstance(unknown, list), f"{cid}: unobservable_intervals must be a list")
    for index, interval in enumerate(unknown):
        start, end = interval.get("start_s"), interval.get("end_s")
        _require(isinstance(start, (int, float)) and isinstance(end, (int, float)), f"{cid}: unobservable interval {index} needs numeric boundaries")
        _require(np.isfinite([start, end]).all() and lower <= start < end <= upper, f"{cid}: unobservable interval {index} is outside cycle bounds")

    return {"frames": int(t), "nodes": int(n), "feature_dim": int(d), "targets": int(k), "target_dim": int(g), "feature_quality_dim": int(quality.shape[-1])}


def load_manifest(path: str | Path) -> tuple[dict[str, Any], Path]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DatasetValidationError(f"could not read manifest {manifest_path}: {exc}") from exc
    _require(isinstance(manifest, dict), "manifest root must be an object")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, f"unsupported manifest schema_version {manifest.get('schema_version')!r}")
    _require(isinstance(manifest.get("is_synthetic"), bool), "manifest is_synthetic must be an explicit boolean")
    _require(isinstance(manifest.get("cycles"), list), "manifest cycles must be a list")
    _require(isinstance(manifest.get("config"), dict), "manifest config must be an object")
    _require(isinstance(manifest.get("labels"), dict), "manifest labels must be an object")
    for index, row in enumerate(manifest["cycles"]):
        _require(isinstance(row, dict), f"cycle record {index} must be an object")
        _require(row.get("is_synthetic") == manifest["is_synthetic"], f"cycle record {index} is_synthetic disagrees with manifest")
        _require(isinstance(row.get("feature_file"), str), f"cycle record {index} needs feature_file")
    return manifest, manifest_path.parent


def validate_dataset(
    manifest_path: str | Path,
    *,
    require_all_splits: bool = True,
    selected_splits: Iterable[str] | None = None,
) -> dict[str, Any]:
    manifest, root = load_manifest(manifest_path)
    if require_all_splits:
        split_counts = validate_split_isolation(manifest["cycles"], dataset_root=root, require_feature_hash=True)
    else:
        split_counts = {name: sum(row.get("split") == name for row in manifest["cycles"]) for name in SPLITS}
    config = manifest["config"]
    c = int(config.get("num_actions", 4))
    r = int(config.get("num_quality_classes", 2))
    cycle_shapes = []
    validate_splits = set(selected_splits) if selected_splits is not None else set(SPLITS)
    _require(validate_splits <= set(SPLITS), f"selected_splits must be within {SPLITS}")
    for record in manifest["cycles"]:
        _require(record.get("split") in SPLITS, f"{record.get('cycle_id')}: invalid split")
        feature_path = safe_feature_path(root, record["feature_file"])
        _require(feature_path.is_file(), f"{record.get('cycle_id')}: feature package does not exist: {record['feature_file']}")
        if record["split"] not in validate_splits:
            continue
        expected_hash = record.get("feature_sha256")
        _require(isinstance(expected_hash, str), f"{record.get('cycle_id')}: missing feature_sha256; prepare data before training")
        _require(sha256_file(feature_path) == expected_hash.lower(), f"{record.get('cycle_id')}: feature package SHA-256 mismatch")
        arrays = _read_cycle_npz(feature_path)
        shape = validate_cycle_arrays(record, arrays, num_actions=c, num_quality_classes=r, expected_synthetic=manifest["is_synthetic"])
        cycle_shapes.append(shape)
    signatures = {(s["nodes"], s["feature_dim"], s["targets"], s["target_dim"], s["feature_quality_dim"]) for s in cycle_shapes}
    _require(len(signatures) == 1, "all cycles must share N,D,K,Q dimensions")
    if cycle_shapes:
        only = cycle_shapes[0]
        _require(only["targets"] == int(config.get("num_targets", only["targets"])), "manifest num_targets disagrees with feature packages")
        _require(only["target_dim"] == int(config.get("target_dim", only["target_dim"])), "manifest target_dim disagrees with feature packages")
        _require(only["feature_dim"] == int(config.get("feature_dim", only["feature_dim"])), "manifest feature_dim disagrees with feature packages")
        _require(only["nodes"] == int(config.get("num_nodes", only["nodes"])), "manifest num_nodes disagrees with feature packages")
        _require(only["feature_quality_dim"] == int(config.get("feature_quality_dim", only["feature_quality_dim"])), "manifest feature_quality_dim disagrees with feature packages")
    return {
        "cycle_count": len(manifest["cycles"]),
        "split_counts": split_counts,
        "operator_counts": {name: len({row["operator_id"] for row in manifest["cycles"] if row.get("split") == name}) for name in SPLITS},
        "dimensions": cycle_shapes[0] if cycle_shapes else {},
        "is_synthetic": manifest["is_synthetic"],
        "validated_splits": sorted(validate_splits),
        "manifest_path": str(Path(manifest_path).resolve()),
    }


def load_cycle(manifest_path: str | Path, record: dict[str, Any], *, validate: bool = True, verify_hash: bool = True) -> CycleData:
    manifest, root = load_manifest(manifest_path)
    path = safe_feature_path(root, record["feature_file"])
    if verify_hash and record.get("feature_sha256") is not None:
        _require(sha256_file(path) == str(record["feature_sha256"]).lower(), f"{record.get('cycle_id')}: feature package SHA-256 mismatch")
    arrays = _read_cycle_npz(path)
    if validate:
        validate_cycle_arrays(
            record,
            arrays,
            num_actions=int(manifest["config"].get("num_actions", 4)),
            num_quality_classes=int(manifest["config"].get("num_quality_classes", 2)),
            expected_synthetic=manifest["is_synthetic"],
        )
    return CycleData(record=record, arrays=arrays)


def fit_feature_normalizer(manifest_path: str | Path) -> dict[str, Any]:
    """Fit feature statistics on train operators only; preserve XY coordinates."""
    manifest, _ = load_manifest(manifest_path)
    validate_split_isolation(manifest["cycles"], require_feature_hash=True)
    train_records = [row for row in manifest["cycles"] if row["split"] == "train"]
    _require(bool(train_records), "cannot fit normalization without train cycles")
    sums: np.ndarray | None = None
    squares: np.ndarray | None = None
    counts: np.ndarray | None = None
    for record in train_records:
        arrays = load_cycle(manifest_path, record).arrays
        x = arrays["features"].astype(np.float64)
        valid = arrays["masks"]
        if sums is None:
            d = x.shape[-1]
            sums = np.zeros(d, dtype=np.float64)
            squares = np.zeros(d, dtype=np.float64)
            counts = np.zeros(d, dtype=np.int64)
        sums += np.where(valid[..., None], x, 0).sum(axis=(0, 1))
        squares += np.where(valid[..., None], x * x, 0).sum(axis=(0, 1))
        counts += valid.sum()  # one count per observed node, for each feature
    assert sums is not None and squares is not None and counts is not None
    _require(np.all(counts > 0), "training set has a feature channel with no observed values")
    mean = sums / counts
    variance = np.maximum(0.0, squares / counts - mean * mean)
    scale = np.sqrt(variance)
    scale[scale < 1e-6] = 1.0
    # Preserve image-space XY and XY velocity as geometric quantities.
    # Quality channels and ages have separate [0,1]/seconds contracts and are
    # never passed through this normalizer.
    mean[: min(4, mean.size)] = 0.0
    scale[: min(4, scale.size)] = 1.0
    return {
        "schema_version": 1,
        "fit_split": "train",
        "fit_cycle_ids": [str(row["cycle_id"]) for row in train_records],
        "fit_operator_ids": sorted({str(row["operator_id"]) for row in train_records}),
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "geometry_channels_preserved": list(range(min(4, len(mean)))),
        "observed_node_count_per_feature": counts.tolist(),
    }


def normalize_features(features: np.ndarray, masks: np.ndarray, normalizer: dict[str, Any]) -> np.ndarray:
    mean = np.asarray(normalizer["mean"], dtype=np.float32)
    scale = np.asarray(normalizer["scale"], dtype=np.float32)
    _require(features.shape[-1] == mean.size == scale.size, "normalizer feature dimension mismatch")
    normalized = (features.astype(np.float32) - mean) / scale
    return np.where(masks[..., None], normalized, 0.0).astype(np.float32)


def _set_action(action: np.ndarray, target: int, start: int, end: int, value: int) -> None:
    action[max(0, start) : min(action.shape[0], end), target] = value


def _synthetic_schedule(condition: str) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]], list[dict[str, Any]]]:
    """Return (target,start_frame,end_frame) operation intervals and unknown spans."""
    if condition == "normal_multi_position":
        ops = [(0, 8, 14), (1, 27, 35)]
        return ops, [], []
    if condition == "short_operation":
        ops = [(0, 8, 10), (1, 27, 35)]
        return ops, [], []
    if condition == "same_position_repeat":
        ops = [(0, 8, 13), (0, 19, 25), (1, 33, 39)]
        return ops, [], []
    if condition == "background_only":
        return [], [], []
    # Keep a visible event and add a separate interval whose visual state is unknown.
    return [(0, 8, 14)], [(27, 35)], [{"start_frame": 27, "end_frame": 35, "reason": "synthetic_occlusion"}]


def _make_synthetic_cycle(
    *,
    cycle_id: str,
    operator_id: str,
    session_id: str,
    split: str,
    condition: str,
    seed: int,
    frames: int = 48,
    dt_s: float = 0.25,
    num_nodes: int = 3,
    feature_dim: int = 8,
    num_targets: int = 2,
    target_dim: int = 4,
    quality_dim: int = 2,
    num_actions: int = 4,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    if num_targets != 2 or feature_dim < 8 or num_nodes < 3 or target_dim < 2 or quality_dim < 2 or num_actions != 4:
        raise ValueError("the synthetic generator currently requires K=2, D>=8, N>=3, G>=2, Q>=2, C=4")
    rng = np.random.default_rng(seed)
    ts = np.arange(frames, dtype=np.float32) * np.float32(dt_s)
    targets = np.zeros((num_targets, target_dim), dtype=np.float32)
    targets[:, :2] = np.asarray([[0.30, 0.50], [0.70, 0.50]], dtype=np.float32)
    if target_dim > 2:
        targets[:, 2] = 0.12
    if target_dim > 3:
        targets[:, 3] = np.asarray([0.0, 1.0], dtype=np.float32)

    action = np.zeros((frames, num_targets), dtype=np.int64)
    position = np.full(frames, num_targets, dtype=np.int64)  # K means known no active target.
    position_mask = np.ones(frames, dtype=np.bool_)
    action_mask = np.ones((frames, num_targets), dtype=np.bool_)
    boundary = np.zeros((frames, num_targets, 2), dtype=np.float32)
    boundary_mask = np.ones((frames, num_targets, 2), dtype=np.bool_)
    # Quality class 0 means unusable/unobservable; class 1 means usable.
    quality_label = np.ones((frames, num_targets), dtype=np.int64)
    quality_mask = np.ones((frames, num_targets), dtype=np.bool_)
    operations, unknown_spans, unknown_meta = _synthetic_schedule(condition)

    event_rows: list[dict[str, Any]] = []
    for target, start, end in operations:
        approach_start, retreat_end = max(0, start - 3), min(frames, end + 3)
        _set_action(action, target, approach_start, start, 1)
        _set_action(action, target, start, end, 2)
        _set_action(action, target, end, retreat_end, 3)
        for frame in range(approach_start, retreat_end):
            if action[frame, target] != 0:
                position[frame] = target
        start_idx = min(frames - 1, start)
        end_idx = min(frames - 1, max(start, end - 1))
        boundary[start_idx, target, 0] = 1.0
        boundary[end_idx, target, 1] = 1.0
        event_rows.append({
            "event_id": f"{cycle_id}-op-{len(event_rows) + 1}",
            "target": int(target),
            "action": 2,
            "action_name": "operation_candidate",
            "start_s": float(ts[start]),
            "end_s": float(ts[end] if end < frames else ts[-1] + dt_s),
            "scoreable": True,
        })

    unobservable_rows: list[dict[str, Any]] = []
    for start, end in unknown_spans:
        position[start:end] = num_targets + 1
        # The unknown position is a known annotation; action itself is not supervised.
        action_mask[start:end, :] = False
        action[start:end, :] = -100
        quality_label[start:end, :] = 0
        boundary_mask[start:end, :, :] = False
        position_mask[start:end] = True
        unobservable_rows.append({
            "start_s": float(ts[start]),
            "end_s": float(ts[end] if end < frames else ts[-1] + dt_s),
            "reason": "synthetic_occlusion",
            "is_synthetic": True,
        })
    for info in unknown_meta:
        # Preserve the latent interval separately so it is never silently folded into background.
        event_rows.append({
            "event_id": f"{cycle_id}-unknown-{len(event_rows) + 1}",
            "target": None,
            "action": None,
            "action_name": "unobservable",
            "start_s": float(ts[info["start_frame"]]),
            "end_s": float(ts[info["end_frame"]]),
            "scoreable": False,
            "reason": info["reason"],
        })

    # Generate deterministic, low-dimensional observations from the independent
    # schedule. Missing detections remain in the arrays and are indicated by masks.
    features = rng.normal(0.0, 0.025, size=(frames, num_nodes, feature_dim)).astype(np.float32)
    masks = np.ones((frames, num_nodes), dtype=np.bool_)
    ages = np.zeros((frames, num_nodes), dtype=np.float32)
    quality = np.zeros((frames, num_nodes, quality_dim), dtype=np.float32)
    for ti in range(frames):
        for node in range(num_nodes):
            base = targets[0 if node != 1 else 1, :2]
            features[ti, node, :2] = base + rng.normal(0, 0.01, size=2)
            if feature_dim > 4:
                features[ti, node, 4] = 0.8
            if feature_dim > 5:
                features[ti, node, 5] = 0.0
            if feature_dim > 6:
                features[ti, node, 6] = 0.0
            if feature_dim > 7:
                features[ti, node, 7] = float(node == 2)
        for target, start, end in operations:
            approach_start, retreat_end = max(0, start - 3), min(frames, end + 3)
            if approach_start <= ti < retreat_end:
                stage = int(action[ti, target])
                xy = targets[target, :2]
                alpha = 0.0 if stage == 1 else 1.0 if stage == 2 else 0.15
                for node in range(min(2, num_nodes)):
                    origin = np.asarray([0.08 + 0.12 * node, 0.88 - 0.08 * node], dtype=np.float32)
                    point = origin * (1.0 - alpha) + xy * alpha
                    features[ti, node, :2] = np.clip(point + rng.normal(0, 0.008, size=2), 0, 1)
                    if feature_dim > 2:
                        features[ti, node, 2:4] = (xy - point) / max(dt_s, 1e-6) if stage == 1 else 0.0
                    if feature_dim > 4:
                        features[ti, node, 4] = float(np.linalg.norm(xy - point))
                    if feature_dim > 5:
                        features[ti, node, 5] = 0.85 if stage == 2 else 0.35
                    if feature_dim > 6:
                        features[ti, node, 6] = stage / 3.0
                    if feature_dim > 7:
                        features[ti, node, 7] = float(target + 1) / num_targets
        # One deliberately retained detector dropout sample per cycle.
        if ti in (3, 4):
            masks[ti, 2] = False
            ages[ti, 2] = (ti - 2) * dt_s
        quality[ti, :, 0] = np.where(masks[ti], 0.96, 0.08)
        if quality_dim > 1:
            quality[ti, :, 1] = np.clip(rng.normal(0.03, 0.01, size=num_nodes), 0, 1)
    for start, end in unknown_spans:
        masks[start:end, :] = False
        ages[start:end, :] = (np.arange(start, end) - start + 1)[:, None] * dt_s
        quality[start:end, :, 0] = 0.02
        if quality_dim > 1:
            quality[start:end, :, 1] = 0.85
    features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

    record: dict[str, Any] = {
        "cycle_id": cycle_id,
        "operator_id": operator_id,
        "session_id": session_id,
        "split": split,
        "condition": condition,
        "feature_file": f"cycles/{cycle_id}.npz",
        "duration_s": float(frames * dt_s),
        "events": event_rows,
        "unobservable_intervals": unobservable_rows,
        "is_synthetic": True,
    }
    arrays = {
        "timestamps": ts,
        "features": features,
        "targets": targets,
        "masks": masks,
        "ages": ages,
        "quality": quality,
        "action": action,
        "action_mask": action_mask,
        "position": position,
        "position_mask": position_mask,
        "boundary": boundary,
        "boundary_mask": boundary_mask,
        "quality_label": quality_label,
        "quality_mask": quality_mask,
        "is_synthetic": np.asarray(True, dtype=np.bool_),
    }
    validate_cycle_arrays(record, arrays, expected_synthetic=True)
    return record, arrays


def build_synthetic_dataset(
    output_dir: str | Path,
    *,
    seed: int = 17,
    operators: int = 6,
    cycles_per_operator: int = 5,
    frames: int = 48,
) -> Path:
    """Create a deterministic, explicitly synthetic cycle dataset and manifest."""
    _require(operators >= 6, "synthetic dataset requires at least six operators")
    _require(cycles_per_operator >= 5, "synthetic dataset requires at least five cycles per operator to cover all conditions")
    _require(frames >= 42, "synthetic dataset needs at least 42 frames for the predefined event conditions")
    output = Path(output_dir).resolve()
    cycles_dir = output / "cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    conditions = (
        "normal_multi_position",
        "short_operation",
        "same_position_repeat",
        "background_only",
        "unobservable_interval",
    )
    operator_ids = [f"operator-{index + 1:02d}" for index in range(operators)]
    split_by_op = {op: "train" for op in operator_ids[: max(4, int(round(operators * 0.60)))]}
    remaining = operator_ids[len(split_by_op) :]
    n_val = max(1, int(round(operators * 0.20)))
    for op in remaining[:n_val]:
        split_by_op[op] = "val"
    for op in remaining[n_val:]:
        split_by_op[op] = "test"
    records = []
    dimensions = {
        "num_nodes": 3,
        "feature_dim": 8,
        "num_targets": 2,
        "target_dim": 4,
        "feature_quality_dim": 2,
        "num_actions": 4,
        "num_quality_classes": 2,
    }
    for op_index, operator_id in enumerate(operator_ids):
        for cycle_index in range(cycles_per_operator):
            cycle_id = f"{operator_id}-cycle-{cycle_index + 1:02d}"
            condition = conditions[cycle_index % len(conditions)]
            cycle_dimensions = dict(dimensions)
            quality_dim = cycle_dimensions.pop("feature_quality_dim")
            cycle_dimensions.pop("num_quality_classes")
            record, arrays = _make_synthetic_cycle(
                cycle_id=cycle_id,
                operator_id=operator_id,
                session_id=f"session-{operator_id}-01",
                split=split_by_op[operator_id],
                condition=condition,
                seed=seed + op_index * 1009 + cycle_index * 31,
                frames=frames,
                quality_dim=quality_dim,
                **cycle_dimensions,
            )
            path = cycles_dir / f"{cycle_id}.npz"
            np.savez_compressed(path, **arrays)
            record["feature_sha256"] = sha256_file(path)
            records.append(record)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_name": "lamp-assembly-deterministic-synthetic-smoke",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_synthetic": True,
        "synthetic_note": "Deterministic smoke data only; no real lamp assembly video or field accuracy evidence.",
        "config": dimensions | {"frame_rate_hz": 4.0, "seed": seed},
        "labels": {
            "actions": ["background", "approach", "operation_candidate", "withdrawal"],
            "quality": ["insufficient_or_unobservable", "observable"],
            "position_no_target": 2,
            "position_unknown": 3,
            "coordinates": "feature and target XY are normalized image coordinates in [0,1]",
        },
        "split_protocol": "operator-disjoint 4/1/1 by default; full cycles and sessions stay within one split",
        "cycles": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = validate_dataset(output / "manifest.json")
    normalizer = fit_feature_normalizer(output / "manifest.json")
    (output / "normalization.json").write_text(json.dumps(normalizer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output / "manifest.json"
