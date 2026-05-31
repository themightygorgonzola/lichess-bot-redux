"""
experimental_model.py — Experimental NNUE variants for subset-fit analysis.

These models are for training-side architecture experiments and are not assumed to
match the current engine export format. The purpose is to identify representational
and optimization bottlenecks quickly on controlled subset-fit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import torch
import torch.nn as nn
import torch.nn.functional as F

from .arch import INPUT_SIZE, OUTPUT_BUCKETS
from .model import NNUE


@dataclass(frozen=True)
class VariantSpec:
    name: str
    ft_size: int
    hidden_sizes: tuple[int, ...]
    activation: str
    ft_activation: str
    bucketed: bool


VARIANTS: dict[str, VariantSpec] = {
    'baseline': VariantSpec('baseline', 1024, (32, 32, 16), 'crelu127', 'screlu', True),
    'micro_relu': VariantSpec('micro_relu', 256, (32, 16), 'relu', 'screlu', True),
    'tiny_relu': VariantSpec('tiny_relu', 384, (48, 24), 'relu', 'screlu', True),
    'small_relu': VariantSpec('small_relu', 512, (64, 32, 16), 'relu', 'screlu', True),
    'small_gelu': VariantSpec('small_gelu', 512, (64, 32, 16), 'gelu', 'screlu', True),
    'wide_relu': VariantSpec('wide_relu', 1024, (128, 64, 32), 'relu', 'screlu', True),
    'wide_gelu': VariantSpec('wide_gelu', 1024, (256, 128, 64), 'gelu', 'screlu', True),
    'huge_gelu': VariantSpec('huge_gelu', 1536, (256, 128, 64), 'gelu', 'screlu', True),
    'huge_relu': VariantSpec('huge_relu', 1536, (256, 128, 64), 'relu', 'screlu', True),
    # huge_crelu: same capacity as huge_relu but with CReLU127 clamped hidden layers.
    # This matches the quantization-correct bound used by the baseline model and prevents
    # activations from growing unbounded, which destabilises training at higher LRs.
    'huge_crelu': VariantSpec('huge_crelu', 1536, (256, 128, 64), 'crelu127', 'screlu', True),
}


def _activation_fn(name: str) -> Callable[[torch.Tensor], torch.Tensor]:
    if name == 'relu':
        return torch.relu
    if name == 'gelu':
        return F.gelu
    if name == 'crelu127':
        return lambda x: torch.clamp(x, 0.0, 127.0)
    raise ValueError(f'Unsupported activation: {name}')


class ExperimentalNNUE(nn.Module):
    def __init__(self, spec: VariantSpec):
        super().__init__()
        self.spec = spec
        self.ft = nn.EmbeddingBag(INPUT_SIZE + 1, spec.ft_size, mode='sum', padding_idx=INPUT_SIZE)
        self.ft_bias = nn.Parameter(torch.zeros(spec.ft_size))

        in_size = 2 * spec.ft_size
        layers = []
        last = in_size
        for hidden in spec.hidden_sizes:
            layers.append((last, hidden))
            last = hidden
        self.out_dim = last

        if spec.bucketed:
            self.weights = nn.ParameterList()
            self.biases = nn.ParameterList()
            prev = in_size
            for hidden in spec.hidden_sizes:
                self.weights.append(nn.Parameter(torch.zeros(OUTPUT_BUCKETS, hidden, prev)))
                self.biases.append(nn.Parameter(torch.zeros(OUTPUT_BUCKETS, hidden)))
                prev = hidden
            self.out_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, 1, prev))
            self.out_bias = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, 1))
        else:
            raise NotImplementedError('Non-bucketed variant not implemented')

        self._act = _activation_fn(spec.activation)
        self._init_weights()

    def _init_weights(self):
        nn.init.normal_(self.ft.weight[:INPUT_SIZE], mean=0.0, std=0.1)
        nn.init.zeros_(self.ft.weight[INPUT_SIZE])
        nn.init.normal_(self.ft_bias, mean=0.0, std=0.1)

        for weight in self.weights:
            for b in range(OUTPUT_BUCKETS):
                nn.init.kaiming_normal_(weight[b], a=0, mode='fan_in', nonlinearity='relu')
        for bias in self.biases:
            nn.init.zeros_(bias)
        for b in range(OUTPUT_BUCKETS):
            nn.init.kaiming_normal_(self.out_weight[b], a=0, mode='fan_in', nonlinearity='linear')
        nn.init.zeros_(self.out_bias)

    def _ft_activate(self, x: torch.Tensor) -> torch.Tensor:
        if self.spec.ft_activation == 'screlu':
            x = torch.clamp(x, 0.0, 1.0)
            return x * x
        if self.spec.ft_activation == 'relu':
            return torch.relu(x)
        if self.spec.ft_activation == 'gelu':
            return F.gelu(x)
        raise ValueError(f'Unsupported FT activation: {self.spec.ft_activation}')

    def forward(self, white_idx: torch.Tensor, white_cnt: torch.Tensor,
                black_idx: torch.Tensor, black_cnt: torch.Tensor,
                stm: torch.Tensor, bucket: torch.Tensor) -> torch.Tensor:
        max_feats = white_idx.size(1)
        slots = torch.arange(max_feats, device=white_idx.device)
        mask_w = slots[None, :] >= white_cnt[:, None]
        mask_b = slots[None, :] >= black_cnt[:, None]
        wi = white_idx.masked_fill(mask_w, INPUT_SIZE)
        bi = black_idx.masked_fill(mask_b, INPUT_SIZE)

        white_acc = self._ft_activate(self.ft(wi) + self.ft_bias)
        black_acc = self._ft_activate(self.ft(bi) + self.ft_bias)

        stm_f = stm.unsqueeze(1).float()
        friendly = white_acc * (1 - stm_f) + black_acc * stm_f
        enemy = black_acc * (1 - stm_f) + white_acc * stm_f
        x = torch.cat([friendly, enemy], dim=1)

        out = torch.empty(x.size(0), 1, device=x.device, dtype=x.dtype)
        for b in range(OUTPUT_BUCKETS):
            mask = (bucket == b)
            if not mask.any():
                continue
            xb = x[mask]
            h = xb
            for w, bias in zip(self.weights, self.biases):
                h = self._act(h @ w[b].T + bias[b])
            out[mask] = h @ self.out_weight[b].T + self.out_bias[b]
        return out


def build_model(variant: str) -> nn.Module:
    if variant == 'baseline':
        return NNUE()
    if variant not in VARIANTS:
        raise ValueError(f'Unknown model variant: {variant}')
    return ExperimentalNNUE(VARIANTS[variant])


def available_variants() -> list[str]:
    return list(VARIANTS.keys())


def model_param_count(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
