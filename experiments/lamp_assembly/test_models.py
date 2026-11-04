import random

import pytest
import torch

from algorithms.assembly_predictor import AssemblyPredictor
from experiments.lamp_assembly.models import (
    ConcatSingleScaleTCN,
    GatedRelationBoundaryTCN,
    load_checkpoint,
)


torch.set_num_threads(1)


MODEL_FACTORIES = (
    lambda: ConcatSingleScaleTCN(width=8),
    lambda: GatedRelationBoundaryTCN(width=8),
)


def make_inputs(steps=12, seed=31):
    generator = torch.Generator().manual_seed(seed)
    features = torch.rand(1, steps, 3, 8, generator=generator)
    targets = torch.rand(1, 2, 4, generator=generator)
    masks = torch.ones(1, steps, 3, dtype=torch.bool)
    ages = torch.rand(1, steps, 3, generator=generator)
    quality = torch.zeros(1, steps, 3, 2)
    quality[..., 0] = 0.9
    quality[..., 1] = 0.05
    return features, targets, masks, ages, quality


def compare_output_dicts(left, right, *, atol=2e-5):
    assert left.keys() == right.keys()
    for key in left:
        torch.testing.assert_close(left[key], right[key], rtol=2e-5, atol=atol)


@pytest.mark.parametrize("factory", MODEL_FACTORIES)
def test_output_shapes_and_checkpoint_roundtrip(factory):
    model = factory().eval()
    inputs = make_inputs(steps=5)
    output = model(*inputs)
    assert output["action_logits"].shape == (1, 5, 2, 4)
    assert output["position_logits"].shape == (1, 5, 4)
    assert output["boundary_logits"].shape == (1, 5, 2, 2)
    assert output["quality_logits"].shape == (1, 5, 2, 2)
    assert set(output) == {
        "action_logits",
        "position_logits",
        "boundary_logits",
        "quality_logits",
    }

    restored = load_checkpoint(model.checkpoint_payload()).eval()
    compare_output_dicts(output, restored(*inputs), atol=0)


@pytest.mark.parametrize("factory", MODEL_FACTORIES)
def test_future_perturbation_does_not_change_prefix(factory):
    model = factory().eval()
    inputs = list(make_inputs(steps=11))
    original = model(*inputs)
    cut = 6
    inputs[0] = inputs[0].clone()
    inputs[0][:, cut + 1 :] = torch.rand_like(inputs[0][:, cut + 1 :])
    changed = model(*inputs)
    for key in original:
        torch.testing.assert_close(
            original[key][:, : cut + 1],
            changed[key][:, : cut + 1],
            rtol=1e-6,
            atol=1e-6,
        )


@pytest.mark.parametrize("factory", MODEL_FACTORIES)
def test_random_chunks_and_single_frame_push_match_batch_forward(factory):
    model = factory().eval()
    inputs = make_inputs(steps=19, seed=99)
    expected = model(*inputs)

    random.seed(8)
    chunks = []
    start = 0
    while start < inputs[0].shape[1]:
        end = min(inputs[0].shape[1], start + random.randint(1, 5))
        chunks.append((start, end))
        start = end

    model.reset()
    chunk_outputs = {key: [] for key in expected}
    for start, end in chunks:
        pushed = model.push(
            inputs[0][:, start:end],
            inputs[1],
            inputs[2][:, start:end],
            inputs[3][:, start:end],
            inputs[4][:, start:end],
        )
        for key in pushed:
            chunk_outputs[key].append(pushed[key])
    chunk_outputs = {
        key: torch.cat(value, dim=1) for key, value in chunk_outputs.items()
    }
    compare_output_dicts(expected, chunk_outputs)

    model.reset()
    frame_outputs = {key: [] for key in expected}
    for index in range(inputs[0].shape[1]):
        pushed = model.push(
            inputs[0][0, index],
            inputs[1][0],
            inputs[2][0, index],
            inputs[3][0, index],
            inputs[4][0, index],
        )
        for key in pushed:
            frame_outputs[key].append(pushed[key])
    frame_outputs = {
        key: torch.cat(value, dim=1) for key, value in frame_outputs.items()
    }
    compare_output_dicts(expected, frame_outputs)
    assert len(model._stream_history) <= model.receptive_field


@pytest.mark.parametrize("quality_channel,quality_value", [(0, 0.0), (1, 1.0)])
def test_zero_reliability_node_is_ignored_even_when_perturbed(
    quality_channel, quality_value
):
    model = GatedRelationBoundaryTCN(width=8).eval()
    inputs = list(make_inputs(steps=4, seed=41))
    inputs[4][:, :, 2, quality_channel] = quality_value

    ignored = list(inputs)
    ignored[2] = ignored[2].clone()
    ignored[2][:, :, 2] = False
    expected = model(*ignored)

    perturbed = list(inputs)
    perturbed[0] = perturbed[0].clone()
    perturbed[0][:, :, 2, :] = 1e30
    actual = model(*perturbed)
    compare_output_dicts(expected, actual, atol=0)


@pytest.mark.parametrize("factory", MODEL_FACTORIES)
def test_all_missing_nodes_return_finite_unknown_outputs(factory):
    model = factory().eval()
    inputs = list(make_inputs(steps=3))
    inputs[2].zero_()
    inputs[0].fill_(float("nan"))
    inputs[3].fill_(float("nan"))
    inputs[4].fill_(float("nan"))
    output = model(*inputs)
    for value in output.values():
        assert torch.isfinite(value).all()
    assert torch.all(output["position_logits"][..., -1] > 0)
    assert torch.all(output["quality_logits"][..., 0] > 0)


@pytest.mark.parametrize("factory", MODEL_FACTORIES)
def test_gradients_are_finite(factory):
    model = factory().train()
    output = model(*make_inputs(steps=7))
    loss = sum(value.square().mean() for value in output.values())
    loss.backward()
    gradients = [parameter.grad for parameter in model.parameters() if parameter.grad is not None]
    assert gradients
    assert all(torch.isfinite(gradient).all() for gradient in gradients)


def one_frame_inputs(seed=12, *, usable=True):
    features, targets, masks, ages, quality = make_inputs(steps=1, seed=seed)
    if not usable:
        masks.zero_()
    return features[0, 0], targets[0], masks[0, 0], ages[0, 0], quality[0, 0]


def set_runtime_heads(model, *, usable_quality=True):
    with torch.no_grad():
        model.position_target_head.weight.zero_()
        model.position_target_head.bias.fill_(10.0)
        model.position_other_head.weight.zero_()
        model.position_other_head.bias.fill_(-10.0)
        model.boundary_head.weight.zero_()
        model.boundary_head.bias.fill_(10.0)
        model.quality_head.weight.zero_()
        model.quality_head.bias.copy_(
            torch.tensor([-10.0, 10.0] if usable_quality else [10.0, -10.0])
        )


def runtime_push(predictor, *, timestamp, cycle, signature="camera-A", usable=True):
    features, targets, masks, ages, quality = one_frame_inputs(usable=usable)
    return predictor.push(
        "stream-1",
        cycle,
        signature,
        timestamp,
        features,
        targets,
        masks,
        ages,
        quality,
        published_at_s=1234.5,
    )


def test_runtime_separates_cycle_time_publication_and_quality_gate():
    model = GatedRelationBoundaryTCN(width=8).eval()
    set_runtime_heads(model, usable_quality=True)
    predictor = AssemblyPredictor(model)
    first = runtime_push(predictor, timestamp=40.25, cycle="cycle-A")
    second = runtime_push(predictor, timestamp=41.0, cycle="cycle-A")
    assert first["cycle_elapsed_s"] == pytest.approx(0.0)
    assert second["cycle_elapsed_s"] == pytest.approx(0.75)
    assert second["published_at_s"] == pytest.approx(1234.5)
    candidate = second["observation_candidates"][0]
    assert candidate["action_candidate"] is not None
    assert candidate["boundary_start_candidate_offset_s"] == pytest.approx(0.75)
    assert candidate["boundary_end_candidate_offset_s"] == pytest.approx(0.75)
    assert second.get("verified") is None
    assert second.get("result") is None

    set_runtime_heads(model, usable_quality=False)
    rejected = runtime_push(predictor, timestamp=41.25, cycle="cycle-A")
    assert rejected["status"] == "uncertain"
    assert all(item["action_candidate"] is None for item in rejected["observation_candidates"])
    assert all(
        item["boundary_start_candidate_offset_s"] is None
        and item["boundary_end_candidate_offset_s"] is None
        for item in rejected["observation_candidates"]
    )


def test_runtime_unknown_and_gap_reset_keep_cycle_and_segment_times_distinct():
    model = GatedRelationBoundaryTCN(width=8).eval()
    set_runtime_heads(model, usable_quality=True)
    predictor = AssemblyPredictor(model, max_gap_s=1.0)
    first = runtime_push(predictor, timestamp=10.0, cycle="cycle-A", usable=False)
    assert first["position_state"] == "uncertain"
    assert all(
        item["boundary_start_candidate_offset_s"] is None
        for item in first["observation_candidates"]
    )

    runtime_push(predictor, timestamp=10.5, cycle="cycle-A")
    gap = runtime_push(predictor, timestamp=13.0, cycle="cycle-A")
    assert gap["gap_reset"] is True
    assert gap["cycle_elapsed_s"] == pytest.approx(3.0)
    assert gap["context_segment_started_at_s"] == pytest.approx(13.0)
    assert gap["segment_elapsed_s"] == pytest.approx(0.0)


def test_runtime_rejects_expired_observations_but_uses_fresh_nodes():
    model = GatedRelationBoundaryTCN(width=8).eval()
    set_runtime_heads(model, usable_quality=True)
    predictor = AssemblyPredictor(model, max_gap_s=1.0, max_observation_age_s=0.75)
    assert predictor.get_config()["max_observation_age_s"] == pytest.approx(0.75)

    features, targets, masks, ages, quality = one_frame_inputs(seed=77)
    ages.fill_(0.9)
    stale = predictor.push(
        "stream-1",
        "cycle-A",
        "camera-A",
        30.0,
        features,
        targets,
        masks,
        ages,
        quality,
        published_at_s=2000.0,
    )
    assert stale["status"] == "uncertain"
    assert stale["position_state"] == "uncertain"
    assert stale["runtime_config"]["max_observation_age_s"] == pytest.approx(0.75)
    assert all(item["action_candidate"] is None for item in stale["observation_candidates"])
    assert all(
        item["boundary_start_candidate_offset_s"] is None
        and item["boundary_end_candidate_offset_s"] is None
        for item in stale["observation_candidates"]
    )

    # The middle node is expired; the other two are current and must remain
    # usable. Expired nodes are masked before they reach the model window.
    fresh_ages = torch.tensor([0.25, 1.5, 0.75])
    fresh = predictor.push(
        "stream-1",
        "cycle-A",
        "camera-A",
        30.25,
        features,
        targets,
        masks,
        fresh_ages,
        quality,
        published_at_s=2000.25,
    )
    assert fresh["status"] == "candidate"
    assert fresh["position_state"] == "target_candidate"
    assert fresh["observation_candidates"][0]["action_candidate"] is not None
    assert fresh["observation_candidates"][0][
        "boundary_start_candidate_offset_s"
    ] == pytest.approx(0.25)


def test_runtime_observation_age_defaults_to_source_gap_limit():
    model = GatedRelationBoundaryTCN(width=8).eval()
    predictor = AssemblyPredictor(model, max_gap_s=1.25)
    assert predictor.max_observation_age_s == pytest.approx(1.25)


def test_stream_cycle_or_signature_change_starts_fresh_state():
    model = GatedRelationBoundaryTCN(width=8).eval()
    set_runtime_heads(model, usable_quality=True)
    predictor = AssemblyPredictor(model)
    inputs = one_frame_inputs(seed=88)
    predictor.push(
        "stream-1", "cycle-A", "camera-A", 20.0, *inputs, published_at_s=1000.0
    )
    switched_cycle = predictor.push(
        "stream-1", "cycle-B", "camera-A", 2.0, *inputs, published_at_s=1001.0
    )
    assert switched_cycle["cycle_elapsed_s"] == 0
    assert switched_cycle["context_segment_started_at_s"] == 2.0
    assert switched_cycle["gap_reset"] is False

    switched_signature = predictor.push(
        "stream-1", "cycle-B", "camera-B", 0.0, *inputs, published_at_s=1002.0
    )
    fresh = AssemblyPredictor(model).push(
        "stream-1", "cycle-B", "camera-B", 0.0, *inputs, published_at_s=1002.0
    )
    assert switched_signature["cycle_elapsed_s"] == 0
    assert switched_signature["context_segment_started_at_s"] == 0.0
    assert switched_signature["position_scores"] == pytest.approx(
        fresh["position_scores"], abs=0
    )
    assert switched_signature["observation_candidates"] == fresh["observation_candidates"]


def test_invalid_shapes_and_nonmonotonic_timestamps_are_rejected():
    model = GatedRelationBoundaryTCN(width=8).eval()
    inputs = list(make_inputs(steps=2))
    with pytest.raises(ValueError, match="features"):
        model(inputs[0][..., :7], *inputs[1:])
    with pytest.raises(ValueError, match="masks"):
        model(inputs[0], inputs[1], inputs[2].float(), inputs[3], inputs[4])

    predictor = AssemblyPredictor(model)
    frame = one_frame_inputs()
    predictor.push(
        "stream-1", "cycle-A", "camera-A", 1.0, *frame, published_at_s=1000.0
    )
    with pytest.raises(ValueError, match="strictly increase"):
        predictor.push(
            "stream-1", "cycle-A", "camera-A", 1.0, *frame, published_at_s=1001.0
        )
