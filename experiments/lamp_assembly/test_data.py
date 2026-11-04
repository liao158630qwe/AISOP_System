from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from experiments.lamp_assembly import data


@pytest.fixture(scope="module")
def synthetic_manifest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lamp-synthetic")
    return data.build_synthetic_dataset(root / "data", seed=31, operators=6, cycles_per_operator=5, frames=48)


def test_synthetic_manifest_has_operator_session_cycle_isolation_and_explicit_unknown(synthetic_manifest: Path) -> None:
    summary = data.validate_dataset(synthetic_manifest)
    assert summary["is_synthetic"] is True
    assert summary["operator_counts"] == {"train": 4, "val": 1, "test": 1}
    manifest, _ = data.load_manifest(synthetic_manifest)
    assert {row["condition"] for row in manifest["cycles"]} >= {
        "normal_multi_position", "short_operation", "same_position_repeat", "background_only", "unobservable_interval"
    }
    for split in data.SPLITS:
        split_rows = [row for row in manifest["cycles"] if row["split"] == split]
        assert len({row["operator_id"] for row in split_rows}) == summary["operator_counts"][split]
        assert len({row["session_id"] for row in split_rows}) == summary["operator_counts"][split]
    assert len({row["cycle_id"] for row in manifest["cycles"]}) == len(manifest["cycles"])

    hidden = next(row for row in manifest["cycles"] if row["condition"] == "unobservable_interval")
    arrays = data.load_cycle(synthetic_manifest, hidden).arrays
    assert arrays["is_synthetic"].item() is True
    assert any(not event["scoreable"] for event in hidden["events"])
    unknown = arrays["position"] == 3
    assert unknown.any()
    assert np.all(arrays["action"][unknown] == -100)
    assert not arrays["action_mask"][unknown].any()
    assert np.all(arrays["quality_label"][unknown] == 0)  # unusable / unobservable
    assert np.all(arrays["masks"][unknown] == False)
    assert not np.any(arrays["action"][unknown] == 0)  # unknown was not relabeled background


def test_split_validation_rejects_operator_session_path_and_hash_leakage(synthetic_manifest: Path) -> None:
    manifest, _ = data.load_manifest(synthetic_manifest)
    rows = [dict(row) for row in manifest["cycles"]]
    rows[0]["split"] = "val"
    with pytest.raises(data.DatasetValidationError, match="operator_id"):
        data.validate_split_isolation(rows)

    rows = [dict(row) for row in manifest["cycles"]]
    rows[0]["operator_id"] = "other-operator"
    rows[0]["split"] = "val"
    with pytest.raises(data.DatasetValidationError, match="session_id"):
        data.validate_split_isolation(rows)

    rows = [dict(row) for row in manifest["cycles"]]
    rows[0]["feature_file"] = rows[-1]["feature_file"]
    with pytest.raises(data.DatasetValidationError, match="feature_file"):
        data.validate_split_isolation(rows)

    rows = [dict(row) for row in manifest["cycles"]]
    rows[0]["feature_sha256"] = rows[-1]["feature_sha256"]
    with pytest.raises(data.DatasetValidationError, match="SHA-256"):
        data.validate_split_isolation(rows)


def test_illegal_timestamps_and_coordinates_are_rejected(synthetic_manifest: Path) -> None:
    manifest, _ = data.load_manifest(synthetic_manifest)
    row = manifest["cycles"][0]
    arrays = data.load_cycle(synthetic_manifest, row).arrays
    bad_time = dict(arrays)
    bad_time["timestamps"] = arrays["timestamps"].copy()
    bad_time["timestamps"][7] = bad_time["timestamps"][6]
    with pytest.raises(data.DatasetValidationError, match="strictly increasing"):
        data.validate_cycle_arrays(row, bad_time, expected_synthetic=True)

    bad_xy = dict(arrays)
    bad_xy["features"] = arrays["features"].copy()
    bad_xy["features"][0, 0, 0] = 1.25
    with pytest.raises(data.DatasetValidationError, match="feature XY"):
        data.validate_cycle_arrays(row, bad_xy, expected_synthetic=True)

    bad_finite = dict(arrays)
    bad_finite["features"] = arrays["features"].copy()
    bad_finite["features"][0, 0, 2] = np.inf
    with pytest.raises(data.DatasetValidationError, match="finite"):
        data.validate_cycle_arrays(row, bad_finite, expected_synthetic=True)


def test_normalizer_is_train_only_and_default_validation_does_not_open_test_npz(
    synthetic_manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, root = data.load_manifest(synthetic_manifest)
    test_files = {row["feature_file"] for row in manifest["cycles"] if row["split"] == "test"}
    opened: list[str] = []
    original_reader = data._read_cycle_npz

    def tracked_reader(path: Path):
        opened.append(path.name)
        return original_reader(path)

    monkeypatch.setattr(data, "_read_cycle_npz", tracked_reader)
    summary = data.validate_dataset(synthetic_manifest, selected_splits={"train", "val"})
    norm_a = data.fit_feature_normalizer(synthetic_manifest)
    assert summary["validated_splits"] == ["train", "val"]
    assert not (set(opened) & {Path(name).name for name in test_files})
    assert norm_a["fit_split"] == "train"
    assert set(norm_a["fit_cycle_ids"]) == {row["cycle_id"] for row in manifest["cycles"] if row["split"] == "train"}
    assert norm_a["geometry_channels_preserved"] == [0, 1, 2, 3]

    copied_root = tmp_path / "perturbed"
    shutil.copytree(root, copied_root)
    copied_manifest_path = copied_root / "manifest.json"
    copied_manifest = json.loads(copied_manifest_path.read_text(encoding="utf-8"))
    test_row = next(row for row in copied_manifest["cycles"] if row["split"] == "test")
    test_npz = copied_root / test_row["feature_file"]
    with np.load(test_npz, allow_pickle=False) as archive:
        changed = {key: np.asarray(archive[key]) for key in archive.files}
    changed["features"] = changed["features"].copy()
    changed["features"][..., 4:] += 50.0
    np.savez_compressed(test_npz, **changed)
    test_row["feature_sha256"] = data.sha256_file(test_npz)
    copied_manifest_path.write_text(json.dumps(copied_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(data, "_read_cycle_npz", original_reader)
    norm_b = data.fit_feature_normalizer(copied_manifest_path)
    assert norm_a["mean"] == norm_b["mean"]
    assert norm_a["scale"] == norm_b["scale"]


def test_manifest_resolves_only_paths_inside_dataset_root(tmp_path: Path) -> None:
    with pytest.raises(data.DatasetValidationError, match="escapes"):
        data.safe_feature_path(tmp_path, "../outside.npz")
    with pytest.raises(data.DatasetValidationError, match="relative"):
        data.safe_feature_path(tmp_path, str((tmp_path / "absolute.npz").resolve()))


def test_training_loader_never_opens_test_arrays(synthetic_manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from experiments.lamp_assembly.train import load_training_data

    manifest, root = data.load_manifest(synthetic_manifest)
    test_paths = {(root / row["feature_file"]).resolve() for row in manifest["cycles"] if row["split"] == "test"}
    original_reader = data._read_cycle_npz

    def protected_reader(path: Path):
        assert path.resolve() not in test_paths, "default training opened sealed test arrays"
        return original_reader(path)

    monkeypatch.setattr(data, "_read_cycle_npz", protected_reader)
    _, _, samples = load_training_data(synthetic_manifest)
    assert set(samples) == {"train", "val"}
    assert len(samples["train"]) == 20 and len(samples["val"]) == 5


def test_variable_length_cycles_with_more_targets_than_frames(synthetic_manifest: Path) -> None:
    from experiments.lamp_assembly.train import collate_cycles

    manifest, _ = data.load_manifest(synthetic_manifest)
    source = data.load_cycle(synthetic_manifest, manifest["cycles"][0]).arrays
    temporal_keys = ("timestamps", "features", "masks", "ages", "quality", "action", "action_mask", "position", "position_mask", "boundary", "boundary_mask", "quality_label", "quality_mask")
    samples = []
    for length in (2, 3):
        arrays = {key: value.copy() for key, value in source.items()}
        for key in temporal_keys:
            arrays[key] = arrays[key][:length].copy()
        arrays["targets"] = np.tile(arrays["targets"], (4, 1))
        for key in ("action", "action_mask", "quality_label", "quality_mask"):
            arrays[key] = np.tile(arrays[key], (1, 4))
        for key in ("boundary", "boundary_mask"):
            arrays[key] = np.tile(arrays[key], (1, 4, 1))
        samples.append({"arrays": arrays, "record": {}})
    batch = collate_cycles(samples)
    assert batch["targets"].shape == (2, 8, 4)
    assert batch["action"].shape == (2, 3, 8)
    assert not batch["action_mask"][0, 2].any()
    np.testing.assert_array_equal(batch["targets"][0].numpy(), samples[0]["arrays"]["targets"])


def test_validation_loss_is_invariant_to_uneven_batch_partition(synthetic_manifest: Path) -> None:
    from experiments.lamp_assembly.models import ConcatSingleScaleTCN
    from experiments.lamp_assembly.train import _run_epoch, load_training_data

    _, _, samples = load_training_data(synthetic_manifest)
    model = ConcatSingleScaleTCN(width=8).eval()
    single = _run_epoch(model, samples["val"], optimizer=None, batch_size=1, seed=17)
    uneven = _run_epoch(model, samples["val"], optimizer=None, batch_size=4, seed=17)
    for key in single:
        assert single[key] == pytest.approx(uneven[key], rel=1e-6, abs=1e-6)
