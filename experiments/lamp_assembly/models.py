"""Small causal models for position-conditioned assembly observations.

The first two feature and target channels are normalized image coordinates
(x, y) in the same [0, 1] camera frame. Quality channel 0 is confidence
(higher is better); channel 1 is corruption/occlusion (higher is worse).
The output heads are research logits only. They do not certify process quality.
"""

from __future__ import annotations

from collections import deque
import math
from typing import Any

import torch
from torch import Tensor, nn
from torch.nn import functional as F


_OUTPUT_KEYS = (
    "action_logits",
    "position_logits",
    "boundary_logits",
    "quality_logits",
)


def _positive_int(name: str, value: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _dilations(name: str, values: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    result = tuple(values)
    if not result:
        raise ValueError(f"{name} must contain at least one dilation")
    for value in result:
        _positive_int(name, value)
    return result


class CausalBlock(nn.Module):
    """Residual temporal convolution with left-only padding."""

    def __init__(self, width: int, dilation: int, dropout: float):
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.conv = nn.Conv1d(width, width, kernel_size=3, dilation=dilation)
        self.mix = nn.Conv1d(width, width, kernel_size=1)
        self.dropout = nn.Dropout(dropout)
        self.left_padding = 2 * dilation

    def forward(self, x: Tensor) -> Tensor:
        # x is [batch, channels, time]. Padding only on the left makes this
        # block prefix-causal for any sequence length.
        h = self.norm(x.transpose(1, 2)).transpose(1, 2)
        h = self.conv(F.pad(h, (self.left_padding, 0)))
        h = self.mix(F.gelu(h))
        return x + self.dropout(h)


class CausalStack(nn.Module):
    def __init__(self, width: int, dilations: tuple[int, ...], dropout: float):
        super().__init__()
        self.blocks = nn.Sequential(
            *(CausalBlock(width, dilation, dropout) for dilation in dilations)
        )
        self.receptive_field = 1 + 2 * sum(dilations)

    def forward(self, x: Tensor) -> Tensor:
        return self.blocks(x)


class _LampModel(nn.Module):
    """Shared validation, heads, checkpoint metadata and one-stream push API."""

    model_type = "abstract"

    def __init__(
        self,
        *,
        feature_dim: int = 8,
        target_dim: int = 4,
        quality_dim: int = 2,
        num_actions: int = 4,
        num_quality_classes: int = 2,
        num_targets: int = 2,
        num_nodes: int = 3,
        width: int = 32,
        dropout: float = 0.0,
        age_tau_s: float = 2.0,
    ):
        super().__init__()
        self.feature_dim = _positive_int("feature_dim", feature_dim, 2)
        self.target_dim = _positive_int("target_dim", target_dim, 2)
        self.quality_dim = _positive_int("quality_dim", quality_dim, 2)
        self.num_actions = _positive_int("num_actions", num_actions)
        self.num_quality_classes = _positive_int(
            "num_quality_classes", num_quality_classes, 2
        )
        self.num_targets = _positive_int("num_targets", num_targets)
        self.num_nodes = _positive_int("num_nodes", num_nodes)
        self.width = _positive_int("width", width)
        if not math.isfinite(dropout) or not 0 <= dropout < 1:
            raise ValueError("dropout must be finite and in [0, 1)")
        if not math.isfinite(age_tau_s) or age_tau_s <= 0:
            raise ValueError("age_tau_s must be finite and positive")
        self.dropout_p = float(dropout)
        self.age_tau_s = float(age_tau_s)

        self.action_head = nn.Linear(self.width, self.num_actions)
        self.position_target_head = nn.Linear(self.width, 1)
        self.position_other_head = nn.Linear(self.width, 2)
        self.boundary_head = nn.Linear(self.width, 2)
        self.quality_head = nn.Linear(self.width, self.num_quality_classes)
        self._stream_history: deque[dict[str, Tensor]] = deque()
        self._stream_targets: Tensor | None = None
        self._stream_batch: int | None = None

    def _base_config(self) -> dict[str, Any]:
        return {
            "feature_dim": self.feature_dim,
            "target_dim": self.target_dim,
            "quality_dim": self.quality_dim,
            "num_actions": self.num_actions,
            "num_quality_classes": self.num_quality_classes,
            "num_targets": self.num_targets,
            "num_nodes": self.num_nodes,
            "width": self.width,
            "dropout": self.dropout_p,
            "age_tau_s": self.age_tau_s,
        }

    @property
    def receptive_field(self) -> int:
        raise NotImplementedError

    def checkpoint_payload(self) -> dict[str, Any]:
        """Return a versioned, reproducible checkpoint payload."""
        return {
            "schema_version": 1,
            "model_type": self.model_type,
            "config": self.get_config(),
            "state_dict": self.state_dict(),
        }

    def get_config(self) -> dict[str, Any]:
        return self._base_config()

    def _canonicalize(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        if not all(isinstance(x, Tensor) for x in (features, targets, masks, ages, quality)):
            raise TypeError("features, targets, masks, ages, and quality must be tensors")
        if features.ndim != 4:
            raise ValueError("features must have shape [B,T,N,D]")
        batch, steps, nodes, feature_dim = features.shape
        if batch < 1 or steps < 1:
            raise ValueError("batch and time dimensions must be non-empty")
        if (nodes, feature_dim) != (self.num_nodes, self.feature_dim):
            raise ValueError(
                f"features must end in [{self.num_nodes},{self.feature_dim}]"
            )
        if targets.ndim != 3 or targets.shape != (
            batch,
            self.num_targets,
            self.target_dim,
        ):
            raise ValueError(
                f"targets must have shape [B,{self.num_targets},{self.target_dim}]"
            )
        if masks.shape != (batch, steps, nodes) or masks.dtype != torch.bool:
            raise ValueError("masks must be boolean with shape [B,T,N]")
        if ages.shape != (batch, steps, nodes):
            raise ValueError("ages must have shape [B,T,N]")
        if quality.shape != (batch, steps, nodes, self.quality_dim):
            raise ValueError(
                f"quality must have shape [B,T,N,{self.quality_dim}]"
            )

        param = next(self.parameters())
        device, dtype = param.device, param.dtype
        masks = masks.to(device=device)
        features = features.to(device=device, dtype=dtype)
        targets = targets.to(device=device, dtype=dtype)
        ages = ages.to(device=device, dtype=dtype)
        quality = quality.to(device=device, dtype=dtype)
        if not torch.isfinite(targets).all():
            raise ValueError("targets must contain only finite coordinates/features")
        valid = masks
        valid_features = torch.isfinite(features).all(dim=-1)
        valid_ages = torch.isfinite(ages) & (ages >= 0)
        valid_quality = torch.isfinite(quality).all(dim=-1)
        if torch.any(valid & ~valid_features):
            raise ValueError("unmasked feature rows must be finite")
        if torch.any(valid & ~valid_ages):
            raise ValueError("unmasked ages must be finite and non-negative")
        if torch.any(valid & ~valid_quality):
            raise ValueError("unmasked quality rows must be finite")
        confidence = quality[..., 0]
        corruption = quality[..., 1]
        if torch.any(valid & ((confidence < 0) | (confidence > 1))):
            raise ValueError("quality[...,0] confidence must be in [0,1]")
        if torch.any(valid & ((corruption < 0) | (corruption > 1))):
            raise ValueError("quality[...,1] corruption must be in [0,1]")
        coordinate_valid = valid & (confidence > 0) & (corruption < 1)
        coordinates = features[..., :2][coordinate_valid]
        if coordinates.numel() and torch.any((coordinates < 0) | (coordinates > 1)):
            raise ValueError(
                "unmasked feature XY coordinates must be normalized to [0,1]"
            )
        if torch.any((targets[..., :2] < 0) | (targets[..., :2] > 1)):
            raise ValueError("target XY coordinates must be normalized to [0,1]")

        # Masked observations may contain NaN sentinels. Remove them before
        # projection so missing nodes cannot poison the remaining sequence.
        features = torch.where(
            valid.unsqueeze(-1), torch.nan_to_num(features), torch.zeros_like(features)
        )
        ages = torch.where(valid, torch.nan_to_num(ages), torch.zeros_like(ages))
        quality = torch.where(
            valid.unsqueeze(-1), torch.nan_to_num(quality), torch.zeros_like(quality)
        )
        return features, targets, masks, ages, quality

    def _encode(self, *inputs: Tensor) -> tuple[Tensor, Tensor]:
        """Return target-specific causal features [B,T,K,W] and evidence mask."""
        raise NotImplementedError

    def _heads(
        self, encoded: Tensor, evidence_available: Tensor
    ) -> dict[str, Tensor]:
        action_logits = self.action_head(encoded)
        target_position = self.position_target_head(encoded).squeeze(-1)
        pooled = encoded.mean(dim=2)
        other_positions = self.position_other_head(pooled)
        position_logits = torch.cat((target_position, other_positions), dim=-1)
        boundary_logits = self.boundary_head(encoded)
        quality_logits = self.quality_head(encoded)

        # Position classes are [target_0 ... target_K-1, no_target, uncertain].
        # If no usable observation exists at this time, uncertainty is explicit.
        no_evidence = ~evidence_available
        fallback_position = torch.full_like(position_logits, -12.0)
        fallback_position[..., self.num_targets + 1] = 12.0
        position_logits = torch.where(
            no_evidence.unsqueeze(-1), fallback_position, position_logits
        )
        # Quality labels: 0 = unusable/unjudgeable evidence, 1 = usable evidence.
        # These labels describe observation evidence, never product quality.
        fallback_quality = torch.full_like(quality_logits, -12.0)
        fallback_quality[..., 0] = 12.0
        quality_logits = torch.where(
            no_evidence[:, :, None, None], fallback_quality, quality_logits
        )
        return {
            "action_logits": action_logits,
            "position_logits": position_logits,
            "boundary_logits": boundary_logits,
            "quality_logits": quality_logits,
        }

    def forward(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> dict[str, Tensor]:
        clean = self._canonicalize(features, targets, masks, ages, quality)
        encoded, evidence = self._encode(*clean)
        return self._heads(encoded, evidence)

    def reset(self) -> None:
        """Clear the model's convenience single-stream cache."""
        self._stream_history.clear()
        self._stream_targets = None
        self._stream_batch = None

    def _canonicalize_push_inputs(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        """Normalize one-frame or chunk inputs for the stateful push API."""
        if features.ndim == 2:  # [N,D]
            features = features[None, None]
        elif features.ndim == 3:  # [B,N,D]
            features = features[:, None]
        elif features.ndim != 4:
            raise ValueError("push features must be [N,D], [B,N,D], or [B,T,N,D]")
        batch, steps, nodes, _ = features.shape
        if targets.ndim == 2:
            targets = targets.unsqueeze(0)
        if masks.ndim == 1:
            masks = masks[None, None]
        elif masks.ndim == 2:
            masks = masks[:, None]
        if ages.ndim == 1:
            ages = ages[None, None]
        elif ages.ndim == 2:
            ages = ages[:, None]
        if quality.ndim == 2:
            quality = quality[None, None]
        elif quality.ndim == 3:
            quality = quality[:, None]
        if masks.ndim != 3 or ages.ndim != 3 or quality.ndim != 4:
            raise ValueError("push masks/ages/quality have invalid ranks")
        if masks.shape[1] == 1 and steps > 1:
            masks = masks.expand(batch, steps, nodes)
        if ages.shape[1] == 1 and steps > 1:
            ages = ages.expand(batch, steps, nodes)
        if quality.shape[1] == 1 and steps > 1:
            quality = quality.expand(batch, steps, nodes, self.quality_dim)
        return self._canonicalize(features, targets, masks, ages, quality)

    @torch.inference_mode()
    def push(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> dict[str, Tensor]:
        """Process one frame or a contiguous chunk; retain only bounded history.

        A model instance's convenience cache represents one stream. Use
        AssemblyPredictor for independently interleaved streams.
        """
        clean = self._canonicalize_push_inputs(
            features, targets, masks, ages, quality
        )
        features, targets, masks, ages, quality = clean
        batch, steps = features.shape[:2]
        if self._stream_batch is not None and batch != self._stream_batch:
            raise ValueError("stream batch size changed; call reset() first")
        if self._stream_targets is not None and not torch.equal(
            self._stream_targets, targets
        ):
            raise ValueError("target geometry changed; call reset() first")
        if self._stream_batch is None:
            self._stream_batch = batch
            self._stream_targets = targets.detach().clone()
        outputs: dict[str, list[Tensor]] = {key: [] for key in _OUTPUT_KEYS}
        for index in range(steps):
            self._stream_history.append(
                {
                    "features": features[:, index : index + 1].detach().clone(),
                    "masks": masks[:, index : index + 1].detach().clone(),
                    "ages": ages[:, index : index + 1].detach().clone(),
                    "quality": quality[:, index : index + 1].detach().clone(),
                }
            )
            while len(self._stream_history) > self.receptive_field:
                self._stream_history.popleft()
            window = {
                key: torch.cat([frame[key] for frame in self._stream_history], dim=1)
                for key in ("features", "masks", "ages", "quality")
            }
            result = self.forward(
                window["features"],
                targets,
                window["masks"],
                window["ages"],
                window["quality"],
            )
            for key in _OUTPUT_KEYS:
                outputs[key].append(result[key][:, -1:])
        return {key: torch.cat(values, dim=1) for key, values in outputs.items()}


class ConcatSingleScaleTCN(_LampModel):
    """Simple target-conditioned feature concatenation with a causal TCN."""

    model_type = "concat_single_scale_tcn"

    def __init__(
        self,
        feature_dim: int = 8,
        target_dim: int = 4,
        quality_dim: int = 2,
        num_actions: int = 4,
        num_quality_classes: int = 2,
        num_targets: int = 2,
        num_nodes: int = 3,
        width: int = 32,
        dilations: tuple[int, ...] | list[int] = (1, 2, 4),
        dropout: float = 0.0,
        age_tau_s: float = 2.0,
    ):
        super().__init__(
            feature_dim=feature_dim,
            target_dim=target_dim,
            quality_dim=quality_dim,
            num_actions=num_actions,
            num_quality_classes=num_quality_classes,
            num_targets=num_targets,
            num_nodes=num_nodes,
            width=width,
            dropout=dropout,
            age_tau_s=age_tau_s,
        )
        self.dilations = _dilations("dilations", dilations)
        node_width = self.feature_dim + 2 + 1 + 1 + self.quality_dim
        self.input_projection = nn.Linear(
            self.num_nodes * node_width + self.target_dim, self.width
        )
        self.temporal = CausalStack(self.width, self.dilations, self.dropout_p)

    @property
    def receptive_field(self) -> int:
        return self.temporal.receptive_field

    def get_config(self) -> dict[str, Any]:
        return {**self._base_config(), "dilations": list(self.dilations)}

    def _encode(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> tuple[Tensor, Tensor]:
        batch, steps, nodes, _ = features.shape
        target_xy = targets[:, None, None, :, :2]
        relative_xy = features[..., :2].unsqueeze(3) - target_xy
        relative_xy = relative_xy.permute(0, 1, 3, 2, 4)
        node_features = features[:, :, None].expand(
            batch, steps, self.num_targets, nodes, self.feature_dim
        )
        node_masks = masks[:, :, None, :, None].expand(
            batch, steps, self.num_targets, nodes, 1
        ).to(features.dtype)
        node_ages = (ages / self.age_tau_s).clamp(max=50)
        node_ages = node_ages[:, :, None, :, None].expand(
            batch, steps, self.num_targets, nodes, 1
        )
        node_quality = quality[:, :, None].expand(
            batch, steps, self.num_targets, nodes, self.quality_dim
        )
        node_input = torch.cat(
            (node_features, relative_xy, node_masks, node_ages, node_quality), dim=-1
        ).flatten(start_dim=3)
        target_input = targets[:, None].expand(
            batch, steps, self.num_targets, self.target_dim
        )
        projected = F.gelu(self.input_projection(torch.cat((node_input, target_input), -1)))
        temporal = self._run_stack(projected, self.temporal)
        usable_nodes = masks & (quality[..., 0] > 0) & (quality[..., 1] < 1)
        evidence = usable_nodes.any(dim=-1)
        return temporal, evidence

    @staticmethod
    def _run_stack(values: Tensor, stack: CausalStack) -> Tensor:
        batch, steps, targets, width = values.shape
        flat = values.permute(0, 2, 3, 1).reshape(batch * targets, width, steps)
        flat = stack(flat)
        return flat.reshape(batch, targets, width, steps).permute(0, 3, 1, 2)


class GatedRelationBoundaryTCN(_LampModel):
    """Position-query relation encoder with quality gates and short/long TCNs."""

    model_type = "gated_relation_boundary_short_long_tcn"

    def __init__(
        self,
        feature_dim: int = 8,
        target_dim: int = 4,
        quality_dim: int = 2,
        num_actions: int = 4,
        num_quality_classes: int = 2,
        num_targets: int = 2,
        num_nodes: int = 3,
        width: int = 32,
        short_dilations: tuple[int, ...] | list[int] = (1, 2),
        long_dilations: tuple[int, ...] | list[int] = (1, 2, 4),
        dropout: float = 0.0,
        age_tau_s: float = 2.0,
    ):
        super().__init__(
            feature_dim=feature_dim,
            target_dim=target_dim,
            quality_dim=quality_dim,
            num_actions=num_actions,
            num_quality_classes=num_quality_classes,
            num_targets=num_targets,
            num_nodes=num_nodes,
            width=width,
            dropout=dropout,
            age_tau_s=age_tau_s,
        )
        self.short_dilations = _dilations("short_dilations", short_dilations)
        self.long_dilations = _dilations("long_dilations", long_dilations)
        self.node_projection = nn.Linear(self.feature_dim, self.width)
        self.relative_projection = nn.Linear(2, self.width, bias=False)
        self.metadata_projection = nn.Linear(
            1 + 1 + self.quality_dim, self.width, bias=False
        )
        self.target_projection = nn.Linear(self.target_dim, self.width)
        self.key_projection = nn.Linear(self.width, self.width, bias=False)
        self.reliability_gate = nn.Sequential(
            nn.Linear(self.width + 2 + 1 + self.quality_dim, self.width // 2 or 1),
            nn.GELU(),
            nn.Linear(self.width // 2 or 1, 1),
            nn.Sigmoid(),
        )
        self.short_temporal = CausalStack(
            self.width, self.short_dilations, self.dropout_p
        )
        self.long_temporal = CausalStack(
            self.width, self.long_dilations, self.dropout_p
        )
        self.boundary_hint = nn.Linear(self.width, 2)
        self.scale_gate = nn.Linear(self.width * 2 + 2, self.width)

    @property
    def receptive_field(self) -> int:
        return max(
            1 + 2 * sum(self.short_dilations),
            1 + 2 * sum(self.long_dilations),
        )

    def get_config(self) -> dict[str, Any]:
        return {
            **self._base_config(),
            "short_dilations": list(self.short_dilations),
            "long_dilations": list(self.long_dilations),
        }

    def _encode(
        self,
        features: Tensor,
        targets: Tensor,
        masks: Tensor,
        ages: Tensor,
        quality: Tensor,
    ) -> tuple[Tensor, Tensor]:
        batch, steps, nodes, _ = features.shape
        target_xy = targets[:, None, None, :, :2]
        relative_xy = features[..., :2].unsqueeze(3) - target_xy
        relative_xy = relative_xy.permute(0, 1, 3, 2, 4)

        expanded_features = features[:, :, None].expand(
            batch, steps, self.num_targets, nodes, self.feature_dim
        )
        expanded_masks = masks[:, :, None, :, None].expand(
            batch, steps, self.num_targets, nodes, 1
        )
        expanded_ages = (ages / self.age_tau_s).clamp(max=50)
        expanded_ages = expanded_ages[:, :, None, :, None].expand(
            batch, steps, self.num_targets, nodes, 1
        )
        expanded_quality = quality[:, :, None].expand(
            batch, steps, self.num_targets, nodes, self.quality_dim
        )
        confidence = expanded_quality[..., 0:1].clamp(0, 1)
        corruption = expanded_quality[..., 1:2].clamp(0, 1)
        normalized_age = expanded_ages.clamp(0, 50)
        base_reliability = (
            expanded_masks.to(features.dtype)
            * confidence
            * (1.0 - corruption)
            * torch.exp(
                -ages[:, :, None, :, None].expand(
                    batch, steps, self.num_targets, nodes, 1
                )
                / self.age_tau_s
            )
        )
        active_node = base_reliability > 0
        expanded_features = torch.where(
            active_node, expanded_features, torch.zeros_like(expanded_features)
        )
        relative_xy = torch.where(
            active_node, relative_xy, torch.zeros_like(relative_xy)
        )
        metadata = torch.cat(
            (expanded_masks.to(features.dtype), normalized_age, expanded_quality), -1
        )
        metadata = torch.where(active_node, metadata, torch.zeros_like(metadata))
        node = (
            self.node_projection(expanded_features)
            + self.relative_projection(relative_xy)
            + self.metadata_projection(metadata)
        )
        learned_gate = self.reliability_gate(
            torch.cat(
                (node, relative_xy, normalized_age, expanded_quality), dim=-1
            )
        ).squeeze(-1)

        # Explicit reliability prior makes a missing, stale, low-confidence or
        # corrupted node contribute less even before the learned gate is trained.
        reliability = base_reliability.squeeze(-1) * learned_gate
        usable_nodes = reliability > 0
        # A zero-weight node must not affect attention stabilization or the
        # weighted sum, even if its finite input is very large.
        node = torch.where(
            usable_nodes.unsqueeze(-1), node, torch.zeros_like(node)
        )
        target_query = self.target_projection(targets)[:, None, :, None, :]
        keys = self.key_projection(node)
        scores = (keys * target_query).sum(-1) / math.sqrt(self.width)
        masked_scores = scores.masked_fill(~usable_nodes, -torch.inf)
        max_score = masked_scores.amax(dim=-1, keepdim=True)
        max_score = torch.where(torch.isfinite(max_score), max_score, torch.zeros_like(max_score))
        safe_scores = torch.where(
            usable_nodes, scores - max_score, torch.zeros_like(scores)
        )
        unnormalized = torch.exp(safe_scores) * reliability
        weights = unnormalized / unnormalized.sum(dim=-1, keepdim=True).clamp_min(1e-8)
        pooled = (weights.unsqueeze(-1) * node).sum(dim=3)
        query = self.target_projection(targets)[:, None].expand(
            batch, steps, self.num_targets, self.width
        )
        encoded = query + pooled

        short = ConcatSingleScaleTCN._run_stack(encoded, self.short_temporal)
        long = ConcatSingleScaleTCN._run_stack(encoded, self.long_temporal)
        hint = torch.sigmoid(self.boundary_hint(short))
        gate = torch.sigmoid(self.scale_gate(torch.cat((short, long, hint), dim=-1)))
        fused = gate * short + (1.0 - gate) * long
        evidence = reliability.sum(dim=(2, 3)) > 0
        return fused, evidence


def build_model(model_type: str, config: dict[str, Any] | None = None) -> _LampModel:
    """Build one of the supported model variants from checkpoint metadata."""
    constructors = {
        ConcatSingleScaleTCN.model_type: ConcatSingleScaleTCN,
        GatedRelationBoundaryTCN.model_type: GatedRelationBoundaryTCN,
    }
    try:
        constructor = constructors[model_type]
    except KeyError as exc:
        raise ValueError(f"unsupported model_type: {model_type}") from exc
    return constructor(**(config or {}))


def load_checkpoint(
    payload: dict[str, Any], *, map_location: str | torch.device = "cpu"
) -> _LampModel:
    """Load a schema-versioned model checkpoint without downloading anything."""
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported checkpoint schema")
    model = build_model(payload.get("model_type", ""), payload.get("config", {}))
    state_dict = payload.get("state_dict")
    if not isinstance(state_dict, dict):
        raise ValueError("checkpoint must include state_dict")
    model.load_state_dict(state_dict, strict=True)
    return model.to(map_location)
