"""Validate, split and normalize cycle-level lamp assembly feature packages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

try:  # Package invocation: python -m experiments.lamp_assembly.prepare_data
    from .data import (
        DatasetValidationError,
        assign_operator_splits,
        build_synthetic_dataset,
        fit_feature_normalizer,
        load_manifest,
        safe_feature_path,
        sha256_file,
        validate_dataset,
        validate_split_isolation,
    )
except ImportError:  # Direct script invocation remains supported.
    from data import (
        DatasetValidationError,
        assign_operator_splits,
        build_synthetic_dataset,
        fit_feature_normalizer,
        load_manifest,
        safe_feature_path,
        sha256_file,
        validate_dataset,
        validate_split_isolation,
    )


def prepare_manifest(source_manifest: str | Path, output_dir: str | Path) -> Path:
    source_path = Path(source_manifest).resolve()
    source, source_root = load_manifest(source_path)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    records = [dict(row) for row in source["cycles"]]
    if any(row.get("split") not in ("train", "val", "test") for row in records):
        records = assign_operator_splits(records)
    else:
        validate_split_isolation(records)

    for row in records:
        src = safe_feature_path(source_root, row["feature_file"])
        if not src.is_file():
            raise DatasetValidationError(f"{row.get('cycle_id')}: missing feature package {row['feature_file']}")
        dst = safe_feature_path(out, row["feature_file"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src != dst:
            shutil.copy2(src, dst)
        row["feature_sha256"] = sha256_file(dst)

    manifest = dict(source)
    manifest["cycles"] = records
    manifest["prepared_from"] = str(source_path)
    manifest["split_protocol"] = "operator-disjoint; sessions and complete cycles remain in one split"
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = validate_dataset(manifest_path)
    normalizer = fit_feature_normalizer(manifest_path)
    (out / "normalization.json").write_text(json.dumps(normalizer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare cycle-level lamp assembly feature data.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="input manifest.json containing per-cycle feature_file entries")
    source.add_argument("--synthetic", action="store_true", help="generate deterministic synthetic smoke data")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for manifest, cycle NPZ files and train-only normalization")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--operators", type=int, default=6, help="synthetic operator count (minimum 6)")
    parser.add_argument("--cycles-per-operator", type=int, default=5, help="synthetic cycles per operator")
    parser.add_argument("--frames", type=int, default=48, help="frames per synthetic cycle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.synthetic:
            manifest_path = build_synthetic_dataset(
                args.output_dir,
                seed=args.seed,
                operators=args.operators,
                cycles_per_operator=args.cycles_per_operator,
                frames=args.frames,
            )
            summary = validate_dataset(manifest_path)
        else:
            manifest_path = prepare_manifest(args.manifest, args.output_dir)
            summary = validate_dataset(manifest_path)
        print(json.dumps({"manifest": str(manifest_path), **summary}, ensure_ascii=False, indent=2))
        return 0
    except (DatasetValidationError, OSError, ValueError) as exc:
        print(f"data preparation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
