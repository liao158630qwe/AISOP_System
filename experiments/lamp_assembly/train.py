"""Train lamp assembly temporal models with train-only fit and val-only selection."""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import random
import sys
from typing import Any

import numpy as np
import torch
from torch.nn import functional as F

try:  # Package invocation: python -m experiments.lamp_assembly.train
    from .data import (
        DatasetValidationError,
        SPLITS,
        fit_feature_normalizer,
        load_cycle,
        load_manifest,
        normalize_features,
        validate_dataset,
        validate_split_isolation,
    )
    from .evaluate import evaluate_cycles
    from .models import build_model
except ImportError:  # Direct script invocation remains supported.
    from data import (
        DatasetValidationError,
        SPLITS,
        fit_feature_normalizer,
        load_cycle,
        load_manifest,
        normalize_features,
        validate_dataset,
        validate_split_isolation,
    )
    from evaluate import evaluate_cycles
    from models import build_model


MODEL_TYPES = {
    "concat": "concat_single_scale_tcn",
    "relation": "gated_relation_boundary_short_long_tcn",
}


def _samples_for_splits(
    manifest_path: str | Path,
    normalizer: dict[str, Any],
    selected_splits: set[str],
) -> dict[str, list[dict[str, Any]]]:
    manifest, _ = load_manifest(manifest_path)
    samples = {name: [] for name in selected_splits}
    for record in manifest["cycles"]:
        split = record["split"]
        if split not in selected_splits:
            continue
        cycle = load_cycle(manifest_path, record, validate=True, verify_hash=True)
        arrays = dict(cycle.arrays)
        arrays["features"] = normalize_features(arrays["features"], arrays["masks"], normalizer)
        samples[split].append({"record": record, "arrays": arrays})
    return samples


def load_training_data(manifest_path: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Load train/val arrays only; test arrays are not opened in this function."""
    manifest, root = load_manifest(manifest_path)
    validate_split_isolation(manifest["cycles"], dataset_root=root, require_feature_hash=True)
    validate_dataset(manifest_path, selected_splits={"train", "val"})
    normalizer = fit_feature_normalizer(manifest_path)
    samples = _samples_for_splits(manifest_path, normalizer, {"train", "val"})
    if not samples.get("train") or not samples.get("val"):
        raise DatasetValidationError("training requires nonempty train and val splits")
    return manifest, normalizer, samples


def load_test_data(manifest_path: str | Path, normalizer: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Open test feature packages only when the explicit test-evaluation path calls this."""
    manifest, root = load_manifest(manifest_path)
    validate_split_isolation(manifest["cycles"], dataset_root=root, require_feature_hash=True)
    validate_dataset(manifest_path, selected_splits={"test"})
    samples = _samples_for_splits(manifest_path, normalizer, {"test"}).get("test", [])
    if not samples:
        raise DatasetValidationError("test evaluation requested but test split is empty")
    return manifest, samples


def collate_cycles(samples: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
    if not samples:
        raise ValueError("cannot collate an empty cycle batch")
    max_t = max(sample["arrays"]["timestamps"].shape[0] for sample in samples)
    first = samples[0]["arrays"]
    b = len(samples)
    n, d = first["features"].shape[1:]
    k, g = first["targets"].shape
    q = first["quality"].shape[-1]
    tensors: dict[str, np.ndarray] = {
        "features": np.zeros((b, max_t, n, d), dtype=np.float32),
        "targets": np.stack([sample["arrays"]["targets"] for sample in samples]).astype(np.float32),
        "masks": np.zeros((b, max_t, n), dtype=np.bool_),
        "ages": np.zeros((b, max_t, n), dtype=np.float32),
        "quality": np.zeros((b, max_t, n, q), dtype=np.float32),
        "action": np.full((b, max_t, k), -100, dtype=np.int64),
        "action_mask": np.zeros((b, max_t, k), dtype=np.bool_),
        "position": np.full((b, max_t), -100, dtype=np.int64),
        "position_mask": np.zeros((b, max_t), dtype=np.bool_),
        "boundary": np.zeros((b, max_t, k, 2), dtype=np.float32),
        "boundary_mask": np.zeros((b, max_t, k, 2), dtype=np.bool_),
        "quality_label": np.full((b, max_t, k), -100, dtype=np.int64),
        "quality_mask": np.zeros((b, max_t, k), dtype=np.bool_),
    }
    # Target geometry has no temporal axis and is already stacked above.
    keys = tuple(key for key in tensors if key != "targets")
    for bi, sample in enumerate(samples):
        arrays = sample["arrays"]
        t = arrays["timestamps"].shape[0]
        for key in keys:
            tensors[key][bi, :t] = arrays[key]
    return {key: torch.from_numpy(value) for key, value in tensors.items()}


def _masked_cross_entropy(logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    valid = mask & (labels >= 0)
    if not torch.any(valid):
        return logits.sum() * 0.0
    return F.cross_entropy(logits[valid], labels[valid].long())


def multitask_loss(outputs: dict[str, torch.Tensor], batch: dict[str, torch.Tensor]) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    action = _masked_cross_entropy(outputs["action_logits"], batch["action"], batch["action_mask"])
    position = _masked_cross_entropy(outputs["position_logits"], batch["position"], batch["position_mask"])
    quality = _masked_cross_entropy(outputs["quality_logits"], batch["quality_label"], batch["quality_mask"])
    boundary_mask = batch["boundary_mask"]
    if torch.any(boundary_mask):
        boundary = F.binary_cross_entropy_with_logits(
            outputs["boundary_logits"][boundary_mask], batch["boundary"][boundary_mask]
        )
    else:
        boundary = outputs["boundary_logits"].sum() * 0.0
    total = action + 0.5 * position + 0.5 * boundary + 0.5 * quality
    return total, {"action": action, "position": position, "boundary": boundary, "quality": quality}


def _model_config(manifest: dict[str, Any], kind: str, width: int) -> tuple[str, dict[str, Any]]:
    dims = manifest["config"]
    config: dict[str, Any] = {
        "feature_dim": int(dims.get("feature_dim", 8)),
        "target_dim": int(dims.get("target_dim", 4)),
        "quality_dim": int(dims.get("feature_quality_dim", 2)),
        "num_actions": int(dims.get("num_actions", 4)),
        "num_quality_classes": int(dims.get("num_quality_classes", 2)),
        "num_targets": int(dims.get("num_targets", 2)),
        "num_nodes": int(dims.get("num_nodes", 3)),
        "width": width,
    }
    if kind == "concat":
        config["dilations"] = [1, 2, 4]
    else:
        config["short_dilations"] = [1, 2]
        config["long_dilations"] = [1, 2, 4]
    return MODEL_TYPES[kind], config


def _run_epoch(
    model: torch.nn.Module,
    samples: list[dict[str, Any]],
    *,
    optimizer: torch.optim.Optimizer | None,
    batch_size: int,
    seed: int,
) -> dict[str, float]:
    training = optimizer is not None
    model.train(training)
    indices = list(range(len(samples)))
    if training:
        random.Random(seed).shuffle(indices)
    names = ("action", "position", "boundary", "quality")
    sums = {name: 0.0 for name in names}
    counts = {name: 0 for name in names}
    context = torch.enable_grad() if training else torch.no_grad()
    with context:
        for offset in range(0, len(indices), batch_size):
            batch_samples = [samples[index] for index in indices[offset : offset + batch_size]]
            batch = collate_cycles(batch_samples)
            batch = {key: value.to(next(model.parameters()).device) for key, value in batch.items()}
            if training:
                optimizer.zero_grad(set_to_none=True)
            outputs = model(
                batch["features"],
                batch["targets"],
                batch["masks"],
                batch["ages"],
                batch["quality"],
            )
            total, parts = multitask_loss(outputs, batch)
            if not torch.isfinite(total).item():
                raise ValueError("non-finite multitask loss; training stopped before updating weights")
            if training:
                total.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0, error_if_nonfinite=True)
                optimizer.step()
            supervised_counts = {
                "action": int((batch["action_mask"] & (batch["action"] >= 0)).sum().item()),
                "position": int((batch["position_mask"] & (batch["position"] >= 0)).sum().item()),
                "boundary": int(batch["boundary_mask"].sum().item()),
                "quality": int((batch["quality_mask"] & (batch["quality_label"] >= 0)).sum().item()),
            }
            for name, value in parts.items():
                sums[name] += float(value.detach().cpu()) * supervised_counts[name]
                counts[name] += supervised_counts[name]
    # Validation selection must not change when its uneven final batch changes.
    metrics = {name: sums[name] / max(1, counts[name]) for name in names}
    metrics["total"] = metrics["action"] + 0.5 * sum(metrics[name] for name in ("position", "boundary", "quality"))
    return metrics


def decode_operation_events(
    model: torch.nn.Module,
    sample: dict[str, Any],
    *,
    max_observation_age_s: float = 1.0,
    max_gap_s: float = 1.0,
) -> list[dict[str, Any]]:
    """Export observed operation runs, gated by position and current evidence.

    This is a simple offline run decoder, not a learned-boundary publisher.
    It never consults ground-truth action or supervision masks.
    """
    if not math.isfinite(max_observation_age_s) or max_observation_age_s <= 0:
        raise ValueError("max_observation_age_s must be finite and positive")
    if not math.isfinite(max_gap_s) or max_gap_s <= 0:
        raise ValueError("max_gap_s must be finite and positive")
    arrays = sample["arrays"]
    record = sample["record"]
    model.eval()
    device = next(model.parameters()).device
    with torch.no_grad():
        output = model(
            torch.from_numpy(arrays["features"]).unsqueeze(0).to(device),
            torch.from_numpy(arrays["targets"]).unsqueeze(0).to(device),
            torch.from_numpy(arrays["masks"]).unsqueeze(0).to(device),
            torch.from_numpy(arrays["ages"]).unsqueeze(0).to(device),
            torch.from_numpy(arrays["quality"]).unsqueeze(0).to(device),
        )
        action_logits = output["action_logits"][0]
        labels = action_logits.argmax(dim=-1).cpu().numpy()
        probs = torch.softmax(action_logits, dim=-1).cpu().numpy()
        positions = output["position_logits"][0].argmax(dim=-1).cpu().numpy()
        usable_quality = output["quality_logits"][0].argmax(dim=-1).cpu().numpy() == 1
    evidence = (
        arrays["masks"]
        & (arrays["quality"][..., 0] > 0)
        & (arrays["quality"][..., 1] < 1)
        & (arrays["ages"] <= max_observation_age_s)
    ).any(axis=-1)
    timestamps = arrays["timestamps"].astype(float)
    dt = float(np.median(np.diff(timestamps)))
    events = []
    for target in range(labels.shape[1]):
        active = (labels[:, target] == 2) & (positions == target) & usable_quality[:, target] & evidence
        cursor = 0
        while cursor < active.size:
            if not active[cursor]:
                cursor += 1
                continue
            start = cursor
            cursor += 1
            while (
                cursor < active.size
                and active[cursor]
                and timestamps[cursor] - timestamps[cursor - 1] <= max_gap_s
            ):
                cursor += 1
            end = cursor
            if end == len(timestamps) or timestamps[end] - timestamps[end - 1] > max_gap_s:
                end_s = float(timestamps[end - 1] + min(dt, max_gap_s))
            else:
                end_s = float(timestamps[end])
            events.append({
                "event_id": f"{record['cycle_id']}-pred-{target}-{start}",
                "cycle_id": str(record["cycle_id"]),
                "target": int(target),
                "action": 2,
                "action_name": "operation_candidate",
                "start_s": float(timestamps[start]),
                "end_s": end_s,
                "score": float(probs[start:end, target, 2].mean()),
                "scoreable": True,
            })
    return events


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def train(
    manifest_path: str | Path,
    *,
    kind: str,
    output_dir: str | Path,
    epochs: int = 3,
    patience: int = 3,
    batch_size: int = 4,
    learning_rate: float = 3e-4,
    weight_decay: float = 1e-4,
    seed: int = 17,
    width: int = 32,
    device: str = "cpu",
    evaluate_test: bool = False,
) -> dict[str, Any]:
    if kind not in MODEL_TYPES:
        raise ValueError("kind must be concat or relation")
    if epochs < 1 or patience < 1 or batch_size < 1:
        raise ValueError("epochs, patience and batch_size must be positive")
    if device != "cpu" and not (device.startswith("cuda") and torch.cuda.is_available()):
        raise ValueError(f"requested device {device!r} is unavailable")
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    manifest, normalizer, split_samples = load_training_data(manifest_path)
    model_type, config = _model_config(manifest, kind, width)
    model = build_model(model_type, config).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    history: list[dict[str, Any]] = []
    best_val = math.inf
    best_epoch = 0
    best_state: dict[str, torch.Tensor] | None = None
    stale = 0
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        train_metrics = _run_epoch(model, split_samples["train"], optimizer=optimizer, batch_size=batch_size, seed=seed + epoch)
        val_metrics = _run_epoch(model, split_samples["val"], optimizer=None, batch_size=batch_size, seed=seed)
        history.append({"epoch": epoch, "train": train_metrics, "val": val_metrics})
        if val_metrics["total"] < best_val - 1e-9:
            best_val = val_metrics["total"]
            best_epoch = epoch
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
        if stale >= patience:
            break

    if best_state is None:
        raise RuntimeError("training did not produce a validation checkpoint")
    model.load_state_dict(best_state)
    model.to(device)
    checkpoint_path = out / "best.pt"
    payload = model.checkpoint_payload()
    payload["state_dict"] = {key: value.detach().cpu() for key, value in payload["state_dict"].items()}
    temporary_checkpoint = checkpoint_path.with_suffix(".pt.tmp")
    torch.save(payload, temporary_checkpoint)
    temporary_checkpoint.replace(checkpoint_path)
    _save_json(out / "normalization.json", normalizer)

    train_operators = sorted({str(item["record"]["operator_id"]) for item in split_samples["train"]})
    val_operators = sorted({str(item["record"]["operator_id"]) for item in split_samples["val"]})
    summary: dict[str, Any] = {
        "schema_version": 1,
        "run_type": "synthetic_smoke_training" if manifest["is_synthetic"] else "lamp_assembly_training",
        "is_synthetic": bool(manifest["is_synthetic"]),
        "source_note": "Synthetic smoke run only. It does not estimate or claim field accuracy; no real lamp assembly video is present." if manifest["is_synthetic"] else "Manifest-declared feature data; interpret results only within its acquisition and labeling protocol.",
        "kind": kind,
        "model_type": model_type,
        "model_config": config,
        "manifest_path": str(Path(manifest_path).resolve()),
        "seed": seed,
        "device": device,
        "optimizer": {"name": "AdamW", "learning_rate": learning_rate, "weight_decay": weight_decay, "gradient_clip_norm": 5.0},
        "loss": {"formula": "action + 0.5*position + 0.5*boundary + 0.5*quality", "masking": "unknown action/position/quality and padded cycle frames are excluded using explicit supervision masks"},
        "selection": {"criterion": "lowest validation multitask loss", "split": "val", "best_epoch": best_epoch, "best_val_loss": best_val, "test_used_for_selection": False},
        "splits": {
            "cycle_counts": {name: len(split_samples[name]) for name in ("train", "val")},
            "operator_ids": {"train": train_operators, "val": val_operators},
            "operator_disjoint": not bool(set(train_operators) & set(val_operators)),
        },
        "normalization": {"fit_split": "train", "fit_cycle_ids": normalizer["fit_cycle_ids"], "geometry_channels_preserved": normalizer["geometry_channels_preserved"]},
        "history": history,
        "epochs_run": len(history),
        "test_evaluated": False,
        "test_arrays_opened": False,
        "event_decoder": {"type": "offline_class_runs_with_position_quality_current_evidence_gates", "operation_class": 2, "quality_usable_class": 1, "max_observation_age_s": 1.0, "max_gap_s": 1.0, "learned_boundary_used_for_publication": False},
        "checkpoint": str(checkpoint_path),
    }

    if evaluate_test:
        test_manifest, test_samples = load_test_data(manifest_path, normalizer)
        test_records = [item["record"] for item in test_samples]
        predictions: dict[str, list[dict[str, Any]]] = {}
        for sample in test_samples:
            predictions[str(sample["record"]["cycle_id"])] = decode_operation_events(model, sample)
        report = evaluate_cycles(test_records, predictions, tiou_threshold=0.5, is_synthetic=bool(test_manifest["is_synthetic"]))
        summary["test_evaluated"] = True
        summary["test_arrays_opened"] = True
        summary["test_evaluation"] = {
            "protocol": "explicit post-selection test evaluation",
            "report": report,
        }
        _save_json(out / "test-predictions.json", predictions)
        _save_json(out / "test-evaluation.json", report)
    _save_json(out / "run-summary.json", summary)
    return summary


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train concat or relation model on cycle-level lamp assembly features.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kind", choices=tuple(MODEL_TYPES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--width", type=int, default=32)
    parser.add_argument("--device", default="cpu", help="cpu by default; use cuda explicitly if desired")
    parser.add_argument("--evaluate-test", action="store_true", help="after val selection, explicitly open and evaluate the sealed test split")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        summary = train(
            args.manifest,
            kind=args.kind,
            output_dir=args.output_dir,
            epochs=args.epochs,
            patience=args.patience,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            weight_decay=args.weight_decay,
            seed=args.seed,
            width=args.width,
            device=args.device,
            evaluate_test=args.evaluate_test,
        )
        print(json.dumps({
            "run_summary": str((Path(args.output_dir).resolve() / "run-summary.json")),
            "checkpoint": summary["checkpoint"],
            "best_epoch": summary["selection"]["best_epoch"],
            "best_val_loss": summary["selection"]["best_val_loss"],
            "test_evaluated": summary["test_evaluated"],
            "is_synthetic": summary["is_synthetic"],
        }, ensure_ascii=False, indent=2))
        return 0
    except (DatasetValidationError, OSError, ValueError, RuntimeError) as exc:
        print(f"training failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
