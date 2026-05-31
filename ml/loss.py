"""
loss.py — WDL sigmoid-blend loss for NNUE training.

Both components operate in WDL probability space [0, 1]:
  1. MSE on sigmoid(pred/scale) vs target_wdl
     (stable gradients, scale-invariant, matches prep_data WDL labels)
  2. BCE on sigmoid(pred/scale) vs target_wdl
     (numerically stable via BCEWithLogits)

The blend ratio `lambda_` controls the mix:
  lambda_=1.0 → pure sigmoid-MSE
  lambda_=0.0 → pure WDL BCE
  lambda_=0.5 → recommended default (balanced)

Why sigmoid space (not raw centipawns)?
  Raw-cp MSE produces loss ~1,500,000 vs BCE ~0.7.  With any lambda blend,
  BCE contributes <0.00005% of the gradient — effectively dead.  In sigmoid
  space both components are bounded [0,1] and contribute equally at the
  blend ratio.  This is the same approach used by Stockfish nnue-pytorch.

Why scale=600?
  prep_data.py stores WDL as sigmoid(score_cp / 600).  Using the same scale
  here ensures the MSE and BCE targets are consistent with stored labels.
  Using scale=410 (old value) caused BCE to pull toward 0.683×score while
  MSE pulled toward 1.0×score — conflicting gradients every step.
"""

import torch
import torch.nn.functional as F

# Must match _cp_to_wdl(scale=600) in tools/prep_data.py
WDL_SCALE = 600.0

# Bucket-adaptive WDL scale.
# Buckets are ordered [0=≤5pc endgame ... 7=opening].  Endgame positions have
# small absolute scores after filtering — using a smaller scale prevents
# sigmoid(score/scale) from saturating near 0.5 for endgame positions, keeping
# the gradient alive.  Opening positions span a wider score range and need a
# larger scale to avoid saturating near 0/1.
BUCKET_WDL_SCALES = (
    280.0,  # bucket 0  (≤5 pieces)
    320.0,  # bucket 1  (6-9 pieces)
    400.0,  # bucket 2  (10-13 pieces)
    480.0,  # bucket 3  (14-17 pieces)
    520.0,  # bucket 4  (18-21 pieces)
    560.0,  # bucket 5  (22-25 pieces)
    600.0,  # bucket 6  (26-29 pieces)
    600.0,  # bucket 7  (30-32 pieces, opening)
)


def _resolve_scale(scale, buckets, device, dtype):
    """Return a per-sample scale tensor or a Python float.

    If `scale` is a tuple/list/tensor of per-bucket scales, expand it via the
    `buckets` long-tensor.  Otherwise return the scalar value unchanged.
    """
    if buckets is None:
        return float(scale) if not torch.is_tensor(scale) else scale
    if isinstance(scale, (tuple, list)) or (torch.is_tensor(scale) and scale.dim() == 1 and scale.numel() == len(BUCKET_WDL_SCALES)):
        table = torch.as_tensor(scale, device=device, dtype=dtype)
        return table[buckets.view(-1).long()]
    return float(scale) if not torch.is_tensor(scale) else scale


def target_win_probability(target_cp: torch.Tensor,
                           target_wdl: torch.Tensor | None = None,
                           source: str = 'cp',
                           scale=WDL_SCALE,
                           buckets: torch.Tensor | None = None) -> torch.Tensor:
    """Resolve the win-probability training target.

    `source='cp'` derives the target from centipawns via sigmoid(score/scale).
    `source='stored'` trusts the provided WDL tensor as-is.

    `scale` may be a scalar or a per-bucket table (tuple/list/1-D tensor of
    length 8); when a table is provided alongside `buckets`, the scale is
    expanded per-sample.
    """
    target_cp = target_cp.view(-1).float()
    eff_scale = _resolve_scale(scale, buckets, target_cp.device, target_cp.dtype)
    target_from_cp = torch.sigmoid(target_cp / eff_scale)

    if source == 'cp' or target_wdl is None:
        return target_from_cp
    if source != 'stored':
        raise ValueError(f"Unsupported target WDL source: {source}")
    return target_wdl.view(-1).float()


def wdl_loss(pred: torch.Tensor, target_cp: torch.Tensor,
        target_wdl: torch.Tensor | None, lambda_: float = 0.5,
        scale=WDL_SCALE,
        target_wdl_source: str = 'cp',
        buckets: torch.Tensor | None = None) -> torch.Tensor:
    """
    Blended sigmoid-MSE + WDL-BCE loss.  Both components in [0, 1] space.

    Args:
        pred:        (B,) or (B,1) — predicted score in centipawns
        target_cp:   (B,) — target score in centipawns from STM perspective
        target_wdl:  (B,) — target WDL win-probability from STM perspective
        lambda_:     sigmoid-MSE weight (0..1). (1-lambda_) = BCE weight.
        scale:       sigmoid temperature.  Either a scalar (legacy) or a
                     per-bucket table (tuple/list/1-D tensor of length 8).
        buckets:     (B,) long tensor of output bucket indices.  Required
                     when `scale` is a per-bucket table.

    Returns:
        Scalar loss tensor
    """
    pred       = pred.view(-1).float()
    target_cp  = target_cp.view(-1).float()

    eff_scale = _resolve_scale(scale, buckets, pred.device, pred.dtype)

    # Model win-probability prediction (sigmoid of logit)
    pred_logits = pred / eff_scale                              # logit in ~[-5, 5]
    pred_wp     = torch.sigmoid(pred_logits)                    # (B,) in [0, 1]

    # Default to the centipawn-derived target so score and WDL supervision are
    # internally consistent even if the stored WDL was blended with game result.
    target_wp = target_win_probability(
        target_cp,
        target_wdl=target_wdl,
        source=target_wdl_source,
        scale=scale,
        buckets=buckets,
    )

    # ── Sigmoid-MSE: bounded [0, 1²] = [0, 1], scale-invariant ──
    mse_sigmoid = torch.mean((pred_wp - target_wp) ** 2)

    if lambda_ >= 1.0:
        return mse_sigmoid

    # ── BCE with logits: numerically stable, same target ──
    bce = F.binary_cross_entropy_with_logits(
        pred_logits, target_wp.detach(), reduction='mean'
    )

    return lambda_ * mse_sigmoid + (1.0 - lambda_) * bce


def cp_huber_loss(pred: torch.Tensor, target_cp: torch.Tensor,
                  beta_cp: float = 100.0,
                  scale_cp: float = 100.0) -> torch.Tensor:
    """Huber loss in centipawn space, normalized for stable magnitudes.

    Args:
        pred: predicted centipawns.
        target_cp: target centipawns.
        beta_cp: Huber transition point in cp.
        scale_cp: divide both tensors by this before the loss so values stay O(1).
    """
    pred = pred.view(-1).float() / scale_cp
    target_cp = target_cp.view(-1).float() / scale_cp
    beta_scaled = max(beta_cp / scale_cp, 1e-6)
    return F.smooth_l1_loss(pred, target_cp, beta=beta_scaled, reduction='mean')


def training_loss(pred: torch.Tensor,
                  target_cp: torch.Tensor,
                  target_wdl: torch.Tensor | None,
                  mode: str = 'wdl',
                  lambda_: float = 0.5,
                  scale=WDL_SCALE,
                  target_wdl_source: str = 'cp',
                  cp_beta: float = 100.0,
                  cp_scale: float = 100.0,
                  wdl_aux_weight: float = 0.25,
                  buckets: torch.Tensor | None = None) -> torch.Tensor:
    """Unified training loss selector.

    Modes:
      - 'wdl': current sigmoid-space objective
      - 'cp': direct centipawn Huber regression
      - 'hybrid': cp Huber + weighted WDL BCE auxiliary term

    `scale` accepts either a scalar or a per-bucket table; bucket-adaptive
    scaling is enabled when `buckets` is provided alongside a table.
    """
    if mode == 'wdl':
        return wdl_loss(
            pred,
            target_cp,
            target_wdl,
            lambda_=lambda_,
            scale=scale,
            target_wdl_source=target_wdl_source,
            buckets=buckets,
        )

    cp_loss = cp_huber_loss(pred, target_cp, beta_cp=cp_beta, scale_cp=cp_scale)
    if mode == 'cp':
        return cp_loss
    if mode != 'hybrid':
        raise ValueError(f"Unsupported loss mode: {mode}")

    pred = pred.view(-1).float()
    eff_scale = _resolve_scale(scale, buckets, pred.device, pred.dtype)
    pred_logits = pred / eff_scale
    target_wp = target_win_probability(
        target_cp,
        target_wdl=target_wdl,
        source=target_wdl_source,
        scale=scale,
        buckets=buckets,
    )
    bce = F.binary_cross_entropy_with_logits(pred_logits, target_wp.detach(), reduction='mean')
    return cp_loss + wdl_aux_weight * bce


def wdl_eval_metrics(pred: torch.Tensor, target_cp: torch.Tensor) -> dict:
    """Compute evaluation metrics for logging (in centipawn space)."""
    pred      = pred.view(-1)
    target_cp = target_cp.view(-1)

    mse  = torch.mean((pred - target_cp) ** 2).item()
    mae  = torch.mean(torch.abs(pred - target_cp)).item()
    rmse = mse ** 0.5

    return {
        'mse':  mse,
        'mae':  mae,
        'rmse': rmse,
    }
