"""
model.py -- PyTorch NNUE model with HalfKAv2 + SCReLU + output buckets.

Architecture (v6):
    HalfKAv2 (40960) -> FT (1536) [SCReLU] -> concat(3072)
        -> L1(256) [CReLU] -> L2(128) [CReLU] -> L3(64)+skip13 [CReLU] -> 1
  x 8 output buckets (selected by piece count)

  v6 additions vs v5:

  skip13: residual connection from L1 output -> L3 pre-activation.
    skip13_weight shape: (BUCKETS, L3_SIZE, L1_SIZE).
    Forward: h3 = CReLU(L3(h2) + h1 @ skip13.T + l3_bias)
    Quantization: same scale as l3_weight (QB=64). At export, skip13_weight
    is stored separately per bucket in the v6 binary format.

  PSQT head: piece-square table contribution running in parallel with the FT.
    psqt_weight shape: (INPUT_SIZE+1, PSQT_BUCKETS).
    Same feature indices, separate embedding table.
    Contribution: (friendly_psqt - enemy_psqt)[bucket] added to output.
    In C++: a second accumulator [2][8] (int32) is updated with the same
    incremental mechanics as the FT -- zero latency overhead.
"""

import torch
import torch.nn as nn

from .arch import INPUT_SIZE, FT_SIZE, L1_SIZE, L2_SIZE, L3_SIZE, OUTPUT_BUCKETS, PSQT_BUCKETS


class NNUE(nn.Module):
    """
    NNUE v6 perspective network.

    Forward:
      1. Sparse feature lookup -> FT (per perspective) + PSQT (per perspective)
      2. SCReLU on both FT accumulators
      3. Concatenate [friendly_ft, enemy_ft] based on STM -> (2*FT_SIZE,)
      4. Per-bucket output network: L1 [CReLU] -> L2 [CReLU] -> L3+skip13 [CReLU] -> scalar
      5. Add (friendly_psqt - enemy_psqt)[bucket]
      6. Return centipawn score from STM's perspective
    """

    def __init__(self, dropout_p: float = 0.0):
        super().__init__()

        # -- Feature Transformer --
        self.ft      = nn.EmbeddingBag(INPUT_SIZE + 1, FT_SIZE, mode='sum',
                                       padding_idx=INPUT_SIZE)
        self.ft_bias = nn.Parameter(torch.zeros(FT_SIZE))

        # -- PSQT head (v6) --
        # Same feature indices, separate embedding: (INPUT_SIZE+1, PSQT_BUCKETS).
        # Init to zero: starts from pure main-net, learns piece-value signal independently.
        self.psqt = nn.EmbeddingBag(INPUT_SIZE + 1, PSQT_BUCKETS, mode='sum',
                                    padding_idx=INPUT_SIZE)
        nn.init.zeros_(self.psqt.weight)

        # -- Output Network (one per bucket) --
        self.l1_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L1_SIZE, 2 * FT_SIZE))
        self.l1_bias   = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L1_SIZE))

        self.l2_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L2_SIZE, L1_SIZE))
        self.l2_bias   = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L2_SIZE))

        self.l3_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L3_SIZE, L2_SIZE))
        self.l3_bias   = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L3_SIZE))

        # skip13 (v6): residual L1 -> L3 pre-activation, shape (BUCKETS, L3_SIZE, L1_SIZE).
        # Init to zero: v6 starts as pure v5 behaviour.
        self.skip13_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, L3_SIZE, L1_SIZE))

        self.out_weight = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, 1, L3_SIZE))
        self.out_bias   = nn.Parameter(torch.zeros(OUTPUT_BUCKETS, 1))

        self.dropout = nn.Dropout(p=dropout_p)

        self._init_weights()

    def _init_weights(self):
        """FT: normal(0, 0.1) gives healthy SCReLU range from epoch 1.
        skip13 and psqt start at zero."""
        nn.init.normal_(self.ft.weight[:INPUT_SIZE], mean=0.0, std=0.1)
        nn.init.zeros_(self.ft.weight[INPUT_SIZE])
        nn.init.normal_(self.ft_bias, mean=0.0, std=0.1)

        for w in [self.l1_weight, self.l2_weight, self.l3_weight, self.out_weight]:
            for b in range(OUTPUT_BUCKETS):
                nn.init.kaiming_normal_(w[b], a=0, mode='fan_in', nonlinearity='relu')

        for b_param in [self.l1_bias, self.l2_bias, self.l3_bias, self.out_bias]:
            nn.init.zeros_(b_param)

        nn.init.zeros_(self.skip13_weight)
        nn.init.zeros_(self.psqt.weight)

    def forward(self, white_idx: torch.Tensor, white_cnt: torch.Tensor,
                black_idx: torch.Tensor, black_cnt: torch.Tensor,
                stm: torch.Tensor, bucket: torch.Tensor) -> torch.Tensor:
        """
        Args:
            white_idx: (batch, MAX_FEATS) int  -- active white feature indices, zero-padded
            white_cnt: (batch,) int            -- # valid white features per sample
            black_idx: (batch, MAX_FEATS) int  -- active black feature indices, zero-padded
            black_cnt: (batch,) int            -- # valid black features per sample
            stm:       (batch,) 0=white, 1=black
            bucket:    (batch,) output bucket index 0..7
        Returns:
            (batch, 1) predicted evaluation in centipawns from STM's perspective
        """
        MAX    = white_idx.size(1)
        slots  = torch.arange(MAX, device=white_idx.device)
        mask_w = slots[None, :] >= white_cnt[:, None]
        mask_b = slots[None, :] >= black_cnt[:, None]

        wi = white_idx.masked_fill(mask_w, INPUT_SIZE)
        bi = black_idx.masked_fill(mask_b, INPUT_SIZE)

        white_acc = self.ft(wi) + self.ft_bias                    # (B, FT_SIZE)
        black_acc = self.ft(bi) + self.ft_bias

        # PSQT accumulation (v6)
        psqt_white = self.psqt(wi)                                # (B, PSQT_BUCKETS)
        psqt_black = self.psqt(bi)

        # SCReLU
        white_acc = torch.clamp(white_acc, 0.0, 1.0) ** 2
        black_acc = torch.clamp(black_acc, 0.0, 1.0) ** 2

        # Perspective concatenation
        stm_f    = stm.unsqueeze(1).float()
        friendly = white_acc * (1 - stm_f) + black_acc * stm_f
        enemy    = black_acc * (1 - stm_f) + white_acc * stm_f
        combined = torch.cat([friendly, enemy], dim=1)            # (B, 2*FT_SIZE)

        # Bucketed output network + skip13
        main_out = self._bucketed_forward(combined, bucket)       # (B, 1)

        # PSQT contribution: (friendly - enemy)[bucket]
        psqt_friendly = psqt_white * (1 - stm_f) + psqt_black * stm_f
        psqt_enemy    = psqt_black * (1 - stm_f) + psqt_white * stm_f
        psqt_out      = (psqt_friendly - psqt_enemy).gather(1, bucket.unsqueeze(1))

        return main_out + psqt_out

    def _bucketed_forward(self, x: torch.Tensor, bucket: torch.Tensor) -> torch.Tensor:
        """Output network with skip13 residual, one bucket group at a time."""
        out = torch.empty(x.size(0), 1, device=x.device, dtype=x.dtype)

        for b in range(OUTPUT_BUCKETS):
            mask = (bucket == b)
            if not mask.any():
                continue
            xb = x[mask]                                                        # (n, 2*FT_SIZE)

            h1 = self.dropout(
                torch.clamp(xb @ self.l1_weight[b].T + self.l1_bias[b], 0.0, 127.0)
            )                                                                   # (n, L1_SIZE)
            h2 = self.dropout(
                torch.clamp(h1 @ self.l2_weight[b].T + self.l2_bias[b], 0.0, 127.0)
            )                                                                   # (n, L2_SIZE)
            # L3 + skip13 (v6): project L1 directly into L3 pre-activation
            h3 = torch.clamp(
                h2 @ self.l3_weight[b].T + self.l3_bias[b]
                + h1 @ self.skip13_weight[b].T,
                0.0, 127.0)                                                     # (n, L3_SIZE)
            out[mask] = h3 @ self.out_weight[b].T + self.out_bias[b]           # (n, 1)

        return out


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def model_summary(model: NNUE) -> str:
    """Return a human-readable summary of the model architecture."""
    ft_params     = INPUT_SIZE * FT_SIZE + model.ft_bias.numel()
    psqt_params   = INPUT_SIZE * PSQT_BUCKETS
    l1_params     = model.l1_weight.numel() + model.l1_bias.numel()
    l2_params     = model.l2_weight.numel() + model.l2_bias.numel()
    l3_params     = model.l3_weight.numel() + model.l3_bias.numel()
    skip13_params = model.skip13_weight.numel()
    out_params    = model.out_weight.numel() + model.out_bias.numel()
    total         = count_parameters(model)

    lines = [
        f"NNUE Architecture v6: HalfKAv2 ({INPUT_SIZE}) -> FT ({FT_SIZE}) [SCReLU] + PSQT ({PSQT_BUCKETS})",
        f"  -> concat({2*FT_SIZE}) -> L1({L1_SIZE}) -> L2({L2_SIZE}) -> L3({L3_SIZE})+skip13 -> 1  x{OUTPUT_BUCKETS} buckets",
        f"",
        f"  Feature Transform:    {ft_params:>12,} params  ({ft_params*2/1024/1024:.1f} MB as int16)",
        f"  PSQT head:            {psqt_params:>12,} params  ({psqt_params*2/1024/1024:.2f} MB as int16)",
        f"  L1 (x{OUTPUT_BUCKETS} buckets):       {l1_params:>12,} params",
        f"  L2 (x{OUTPUT_BUCKETS} buckets):         {l2_params:>12,} params",
        f"  L3 (x{OUTPUT_BUCKETS} buckets):          {l3_params:>12,} params",
        f"  skip13 (x{OUTPUT_BUCKETS} buckets):      {skip13_params:>12,} params",
        f"  Output (x{OUTPUT_BUCKETS} buckets):          {out_params:>12,} params",
        f"  Total:                {total:>12,} params",
    ]
    return "\n".join(lines)