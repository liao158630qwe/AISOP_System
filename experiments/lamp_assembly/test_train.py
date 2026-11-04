"""Decoder tests use controlled logits rather than self-generated predictions."""

import numpy as np
import pytest
import torch
from torch import nn

from experiments.lamp_assembly.train import decode_operation_events


class ControlledModel(nn.Module):
    def __init__(self, *, position=0, quality_class=1):
        super().__init__()
        self.anchor = nn.Parameter(torch.zeros(1))
        self.position = position
        self.quality_class = quality_class

    def forward(self, features, targets, masks, ages, quality):
        b, t = features.shape[:2]
        k = targets.shape[1]
        action = torch.full((b, t, k, 4), -10.0)
        action[..., 2] = 10.0
        position = torch.full((b, t, k + 2), -10.0)
        position[..., self.position] = 10.0
        usable = torch.full((b, t, k, 2), -10.0)
        usable[..., self.quality_class] = 10.0
        return {"action_logits": action, "position_logits": position, "quality_logits": usable}


def sample():
    arrays = {
        "timestamps": np.arange(4, dtype=np.float64) * 0.25,
        "features": np.zeros((4, 3, 8), np.float32),
        "targets": np.zeros((2, 4), np.float32),
        "masks": np.ones((4, 3), np.bool_),
        "ages": np.zeros((4, 3), np.float32),
        "quality": np.tile(np.array([1.0, 0.0], np.float32), (4, 3, 1)),
    }
    return {"arrays": arrays, "record": {"cycle_id": "decoder-audit"}}


def test_only_associated_target_emits_operation_and_no_truth_is_required():
    events = decode_operation_events(ControlledModel(), sample())
    assert len(events) == 1
    assert events[0]["target"] == 0
    assert events[0]["start_s"] == 0.0 and events[0]["end_s"] == 1.0


@pytest.mark.parametrize("reason", ("unknown_position", "no_target", "quality", "missing", "stale", "zero_confidence", "corrupt"))
def test_confident_action_alone_cannot_create_an_event(reason):
    item = sample()
    model = ControlledModel()
    if reason == "unknown_position":
        model.position = 3
    elif reason == "no_target":
        model.position = 2
    elif reason == "quality":
        model.quality_class = 0
    elif reason == "missing":
        item["arrays"]["masks"][:] = False
    elif reason == "stale":
        item["arrays"]["ages"][:] = 3.0
    elif reason == "zero_confidence":
        item["arrays"]["quality"][..., 0] = 0.0
    elif reason == "corrupt":
        item["arrays"]["quality"][..., 1] = 1.0
    assert decode_operation_events(model, item) == []


def test_source_gap_splits_operations_without_filling_missing_time():
    item = sample()
    item["arrays"]["timestamps"][:] = [0.0, 0.25, 4.0, 4.25]
    events = decode_operation_events(ControlledModel(), item)
    assert [(event["start_s"], event["end_s"]) for event in events] == [(0.0, 0.5), (4.0, 4.5)]
