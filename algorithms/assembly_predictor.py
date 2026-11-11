"""Bounded multi-stream inference for lamp-assembly observation candidates.

This adapter reports visual candidates and evidence timing only. It never
creates a verified process attempt, an OK/NG result, or a product-quality claim.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Any

import torch
from torch import Tensor

from experiments.lamp_assembly.models import _LampModel


@dataclass
class _StreamState:
    cycle_id: str
    signature: str
    started_at_s: float
    context_segment_started_at_s: float
    last_timestamp_s: float
    last_used_monotonic: float
    targets: Tensor
    history: deque[dict[str, Tensor]] = field(default_factory=deque)


class AssemblyPredictor:
    """Keep an independent finite causal window for each active video stream.

    Each stream slot represents one cycle and one feature/calibration signature.
    Changing either automatically discards the previous slot and its evidence.
    """

    def __init__(
        self,
        model: _LampModel,
        *,
        max_streams: int = 16,
        ttl_s: float = 60.0,
        max_gap_s: float = 2.0,
        max_observation_age_s: float | None = None,
        boundary_threshold: float = 0.5,
    ):
        if not isinstance(model, _LampModel):
            raise TypeError("model must be a supported lamp-assembly model")
        if isinstance(max_streams, bool) or not isinstance(max_streams, int) or max_streams < 1:
            raise ValueError("max_streams must be a positive integer")
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("ttl_s must be finite and positive")
        if not math.isfinite(max_gap_s) or max_gap_s <= 0:
            raise ValueError("max_gap_s must be finite and positive")
        if max_observation_age_s is None:
            max_observation_age_s = max_gap_s
        if not math.isfinite(max_observation_age_s) or max_observation_age_s <= 0:
            raise ValueError("max_observation_age_s must be finite and positive")
        if not math.isfinite(boundary_threshold) or not 0 <= boundary_threshold <= 1:
            raise ValueError("boundary_threshold must be in [0, 1]")
        from .gpu import require_cuda
        self.device = torch.device(f'cuda:{require_cuda()}')
        self.model = model.to(self.device).eval()
        self.max_streams = max_streams
        self.ttl_s = float(ttl_s)
        self.max_gap_s = float(max_gap_s)
        self.max_observation_age_s = float(max_observation_age_s)
        self.boundary_threshold = float(boundary_threshold)
        self._states: OrderedDict[str, _StreamState] = OrderedDict()
        self._lock = threading.RLock()

    def get_config(self) -> dict[str, float | int]:
        """Return the active limits so callers can include them in run records."""
        return {
            "max_streams": self.max_streams,
            "ttl_s": self.ttl_s,
            "max_gap_s": self.max_gap_s,
            "max_observation_age_s": self.max_observation_age_s,
            "boundary_threshold": self.boundary_threshold,
        }

    @staticmethod
    def _identifier(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    def clear(self, stream_id: str | None = None) -> None:
        """Drop one stream or all stream states."""
        with self._lock:
            if stream_id is None:
                self._states.clear()
            else:
                self._states.pop(stream_id, None)

    def _expire(self, now: float) -> None:
        expired = [
            stream_id
            for stream_id, state in self._states.items()
            if now - state.last_used_monotonic >= self.ttl_s
        ]
        for stream_id in expired:
            self._states.pop(stream_id, None)

    def _frame(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> tuple[dict[str, Tensor], Tensor]:
        clean = self.model._canonicalize_push_inputs(
            *(value.to(self.device) for value in (features, targets, masks, ages, quality))
        )
        features, targets, masks, ages, quality = clean
        if features.shape[0] != 1 or features.shape[1] != 1:
            raise ValueError("one AssemblyPredictor push must contain one stream frame")
        item = {
            "features": features.detach().clone(),
            "masks": masks.detach().clone(),
            "ages": ages.detach().clone(),
            "quality": quality.detach().clone(),
        }
        return item, targets.detach().clone()

    @torch.inference_mode()
    def push(
        self,
        stream_id: str,
        cycle_id: str,
        signature: str,
        timestamp_s: float,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
        *,
        published_at_s: float | None = None,
    ) -> dict[str, Any]:
        """Add one sampled frame and return observation scores for that frame.

        timestamp_s is the source-video time. cycle_elapsed_s and boundary
        candidate offsets are relative to the first source timestamp in this
        active cycle. published_at_s is wall-clock result publication time.
        """
        stream_id = self._identifier("stream_id", stream_id)
        cycle_id = self._identifier("cycle_id", cycle_id)
        signature = self._identifier("signature", signature)
        if not math.isfinite(timestamp_s) or timestamp_s < 0:
            raise ValueError("timestamp_s must be finite and non-negative")
        if published_at_s is None:
            published_at_s = time.time()
        if not math.isfinite(published_at_s) or published_at_s < 0:
            raise ValueError("published_at_s must be finite and non-negative")

        frame, targets = self._frame(features, targets, masks, ages, quality)
        # Age is the observation's age in source-time seconds. Expired nodes
        # are removed before inference as well as before candidate publication,
        # so they cannot masquerade as a current action.
        frame["masks"] = frame["masks"] & (
            frame["ages"] <= self.max_observation_age_s
        )
        now = time.monotonic()
        gap_reset = False
        with self._lock:
            self._expire(now)
            state = self._states.get(stream_id)
            if state is None or state.cycle_id != cycle_id or state.signature != signature:
                if state is not None:
                    self._states.pop(stream_id, None)
                while len(self._states) >= self.max_streams:
                    self._states.popitem(last=False)
                state = _StreamState(
                    cycle_id=cycle_id,
                    signature=signature,
                    started_at_s=float(timestamp_s),
                    context_segment_started_at_s=float(timestamp_s),
                    last_timestamp_s=float(timestamp_s),
                    last_used_monotonic=now,
                    targets=targets,
                )
                self._states[stream_id] = state
            else:
                if timestamp_s <= state.last_timestamp_s:
                    raise ValueError("timestamp_s must strictly increase within a stream cycle")
                if not torch.equal(state.targets, targets):
                    raise ValueError(
                        "target geometry changed; update signature or start a new cycle"
                    )
                gap_reset = timestamp_s - state.last_timestamp_s > self.max_gap_s
                if gap_reset:
                    state.history.clear()
                    state.context_segment_started_at_s = float(timestamp_s)
                state.last_timestamp_s = float(timestamp_s)
                state.last_used_monotonic = now
                self._states.move_to_end(stream_id)

            state.history.append(frame)
            while len(state.history) > self.model.receptive_field:
                state.history.popleft()
            window = {
                key: torch.cat([sample[key] for sample in state.history], dim=1)
                for key in ("features", "masks", "ages", "quality")
            }
            output = self.model(
                window["features"],
                state.targets,
                window["masks"],
                window["ages"],
                window["quality"],
            )
            action = output["action_logits"][:, -1].softmax(-1)[0]
            position = output["position_logits"][:, -1].softmax(-1)[0]
            boundary = output["boundary_logits"][:, -1].sigmoid()[0]
            quality_scores = output["quality_logits"][:, -1].softmax(-1)[0]
            frame_mask = frame["masks"][0, 0]
            frame_quality = frame["quality"][0, 0]
            usable = (
                frame_mask
                & (frame_quality[:, 0] > 0)
                & (frame_quality[:, 1] < 1)
            )
            evidence_available = bool(usable.any().item())

            position_index = int(position.argmax().item())
            target_count = self.model.num_targets
            selected_quality_class = (
                int(quality_scores[position_index].argmax().item())
                if position_index < target_count
                else None
            )
            selected_candidate_available = (
                evidence_available and selected_quality_class == 1
            )
            if position_index < target_count:
                position_state = (
                    "target_candidate"
                    if selected_candidate_available
                    else "uncertain"
                )
                position_target_index: int | None = position_index
            elif position_index == target_count:
                position_state = "no_target_candidate"
                position_target_index = None
            else:
                position_state = "uncertain"
                position_target_index = None

            elapsed = float(timestamp_s - state.started_at_s)
            candidates = []
            for target_index in range(target_count):
                action_index = int(action[target_index].argmax().item())
                quality_index = int(quality_scores[target_index].argmax().item())
                selected_position = position_index == target_index
                boundary_allowed = (
                    selected_candidate_available
                    and selected_position
                )
                start_score = float(boundary[target_index, 0].item())
                end_score = float(boundary[target_index, 1].item())
                candidates.append(
                    {
                        "target_index": target_index,
                        "action_candidate": (
                            action_index if boundary_allowed else None
                        ),
                        "action_score": (
                            float(action[target_index, action_index].item())
                            if boundary_allowed
                            else None
                        ),
                        "action_scores": action[target_index].cpu().tolist(),
                        "position_score": float(position[target_index].item()),
                        "boundary_start_score": start_score,
                        "boundary_end_score": end_score,
                        "boundary_start_candidate_offset_s": (
                            elapsed
                            if boundary_allowed
                            and start_score >= self.boundary_threshold
                            else None
                        ),
                        "boundary_end_candidate_offset_s": (
                            elapsed
                            if boundary_allowed
                            and end_score >= self.boundary_threshold
                            else None
                        ),
                        "quality_state": (
                            "insufficient_evidence"
                            if not evidence_available or quality_index == 0
                            else "usable_observation_candidate"
                            if quality_index == 1
                            else f"unknown_quality_class_{quality_index}"
                        ),
                        "quality_scores": quality_scores[target_index].cpu().tolist(),
                    }
                )
            result = {
                "status": (
                    "candidate"
                    if selected_candidate_available
                    else "no_target_candidate"
                    if evidence_available and position_index == target_count
                    else "uncertain"
                ),
                "cycle_id": cycle_id,
                "stream_id": stream_id,
                "signature": signature,
                "position_state": (
                    position_state if evidence_available else "uncertain"
                ),
                "position_target_index": (
                    position_target_index if selected_candidate_available else None
                ),
                "position_scores": position.cpu().tolist(),
                "observation_candidates": candidates,
                "source_timestamp_s": float(timestamp_s),
                "cycle_elapsed_s": elapsed,
                "context_segment_started_at_s": state.context_segment_started_at_s,
                "segment_elapsed_s": float(
                    timestamp_s - state.context_segment_started_at_s
                ),
                "gap_reset": gap_reset,
                "published_at_s": float(published_at_s),
                "runtime_config": self.get_config(),
                "model_type": self.model.model_type,
            }
            return result
