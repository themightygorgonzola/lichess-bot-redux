"""
export.py -- Export PyTorch NNUE weights to engine binary format (v6).

Binary layout (v6 -- HalfKAv2 + SCReLU + skip13 + PSQT head + output buckets):

  Header (9 x uint32):
    uint32  magic          = 0x4E4E5545 ("NNUE")
    uint32  version        = 6
    uint32  input_size     = 40960
    uint32  ft_size        = 1536
    uint32  l1_size        = 256
    uint32  l2_size        = 128
    uint32  l3_size        = 64
    uint32  output_buckets = 8
    uint32  psqt_buckets   = 8

  Feature Transform:
    int16[FT_SIZE]                       ft_biases
    int16[INPUT_SIZE][FT_SIZE]           ft_weights (row-major)

  PSQT head (v6 new):
    int16[INPUT_SIZE][PSQT_BUCKETS]      psqt_weights (row-major)

  Per-bucket output network (repeated OUTPUT_BUCKETS times):
    int8[L1_SIZE][2*FT_SIZE]             l1_weights (column-block layout)
    int32[L1_SIZE]                       l1_biases
    int8[L2_SIZE][L1_SIZE]               l2_weights
    int32[L2_SIZE]                       l2_biases
    int8[L3_SIZE][L1_SIZE]               skip13_weights (v6 new; L1_SIZE input!)
    int8[L3_SIZE][L2_SIZE]               l3_weights
    int32[L3_SIZE]                       l3_biases
    int8[1][L3_SIZE]                     out_weights
    int32[1]                             out_bias

Quantization:
  - FT weights/biases:   float x QA (255), int16
  - PSQT weights:        float x QA (255), int16
  - L1 weights:          float x QB (64),  int8
  - L1 biases:           float x QA*QB,    int32
  - L2, L3, skip13, out weights: float x QB, int8
  - L2, L3, out biases:  float x QB,       int32

Note on skip13:
  skip13_weight in Python has shape (BUCKETS, L3_SIZE, L1_SIZE).
  The L1 input is the L1 *output* (CReLU, range [0, 127]).
  In C++, after L2 is computed, skip13(l1_out) is added to L3 pre-activation.
"""

import struct
import os
import numpy as np
import torch

from .arch import (
    NNUE_FILE_MAGIC, NNUE_FILE_VERSION,
    INPUT_SIZE, FT_SIZE, L1_SIZE, L2_SIZE, L3_SIZE, OUTPUT_BUCKETS, PSQT_BUCKETS,
    QA, QB,
)
from .model import NNUE


def _quantize(arr: np.ndarray, scale: float, dtype=np.int16) -> np.ndarray:
    """Scale, round, and clamp to dtype range."""
    scaled = np.round(arr * scale).astype(np.int64)
    info = np.iinfo(dtype)
    return np.clip(scaled, info.min, info.max).astype(dtype)


def export(checkpoint_path: str, output_path: str, verbose: bool = True):
    """
    Export a PyTorch checkpoint to the engine v6 binary format.
    """
    ckpt = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
    state = ckpt.get('model_state_dict', ckpt)

    model = NNUE()
    model.load_state_dict(state)
    model.eval()

    # Extract float parameters
    ft_w      = model.ft.weight.data[:INPUT_SIZE].numpy()          # (INPUT_SIZE, FT_SIZE)
    ft_b      = model.ft_bias.data.numpy()                         # (FT_SIZE,)
    psqt_w    = model.psqt.weight.data[:INPUT_SIZE].numpy()        # (INPUT_SIZE, PSQT_BUCKETS)

    l1_w      = model.l1_weight.data.numpy()                       # (BUCKETS, L1_SIZE, 2*FT_SIZE)
    l1_b      = model.l1_bias.data.numpy()                         # (BUCKETS, L1_SIZE)
    l2_w      = model.l2_weight.data.numpy()                       # (BUCKETS, L2_SIZE, L1_SIZE)
    l2_b      = model.l2_bias.data.numpy()                         # (BUCKETS, L2_SIZE)
    l3_w      = model.l3_weight.data.numpy()                       # (BUCKETS, L3_SIZE, L2_SIZE)
    l3_b      = model.l3_bias.data.numpy()                         # (BUCKETS, L3_SIZE)
    skip13_w  = model.skip13_weight.data.numpy()                   # (BUCKETS, L3_SIZE, L1_SIZE)
    out_w     = model.out_weight.data.numpy()                      # (BUCKETS, 1, L3_SIZE)
    out_b     = model.out_bias.data.numpy()                        # (BUCKETS, 1)

    # Quantize FT and PSQT at QA scale
    ft_w_q    = _quantize(ft_w,   QA, np.int16)                    # (INPUT_SIZE, FT_SIZE)
    ft_b_q    = _quantize(ft_b,   QA, np.int16)                    # (FT_SIZE,)
    psqt_w_q  = _quantize(psqt_w, QA, np.int16)                    # (INPUT_SIZE, PSQT_BUCKETS)

    with open(output_path, 'wb') as f:
        # -- Header --
        f.write(struct.pack('<I', NNUE_FILE_MAGIC))
        f.write(struct.pack('<I', NNUE_FILE_VERSION))
        f.write(struct.pack('<I', INPUT_SIZE))
        f.write(struct.pack('<I', FT_SIZE))
        f.write(struct.pack('<I', L1_SIZE))
        f.write(struct.pack('<I', L2_SIZE))
        f.write(struct.pack('<I', L3_SIZE))
        f.write(struct.pack('<I', OUTPUT_BUCKETS))
        f.write(struct.pack('<I', PSQT_BUCKETS))

        # -- FT biases and weights --
        f.write(ft_b_q.tobytes())
        f.write(ft_w_q.tobytes())

        # -- PSQT weights (v6 new) --
        f.write(psqt_w_q.tobytes())

        # -- Per-bucket output networks --
        for bucket in range(OUTPUT_BUCKETS):
            # L1: reorder row-major (L1_SIZE, 2*FT_SIZE) -> column-block layout
            # l1_weight_col[b][j*4+k] = weight[j][b*4+k]
            # Reshape to (L1_SIZE, N_BLOCKS, 4), transpose 0<->1, flatten.
            N_BLOCKS = 2 * FT_SIZE // 4              # 768
            l1_raw   = _quantize(l1_w[bucket], QB, np.int8)         # (L1_SIZE, 2*FT_SIZE)
            l1_blk   = l1_raw.reshape(L1_SIZE, N_BLOCKS, 4)
            l1_col   = l1_blk.transpose(1, 0, 2).reshape(N_BLOCKS, L1_SIZE * 4)
            l1_flat  = l1_col.reshape(-1)
            l1_b_q   = _quantize(l1_b[bucket], QA * QB, np.int32)   # (L1_SIZE,)

            l2_w_q   = _quantize(l2_w[bucket], QB, np.int8)         # (L2_SIZE, L1_SIZE)
            l2_b_q   = _quantize(l2_b[bucket], QB, np.int32)        # (L2_SIZE,)

            # skip13: quantize at QB (same scale as l3 weights)
            # Shape: (L3_SIZE, L1_SIZE) -- L1_SIZE is the input dimension
            skip13_q = _quantize(skip13_w[bucket], QB, np.int8)     # (L3_SIZE, L1_SIZE)

            l3_w_q   = _quantize(l3_w[bucket], QB, np.int8)         # (L3_SIZE, L2_SIZE)
            l3_b_q   = _quantize(l3_b[bucket], QB, np.int32)        # (L3_SIZE,)

            out_w_q  = _quantize(out_w[bucket], QB, np.int8)        # (1, L3_SIZE)
            out_b_q  = _quantize(out_b[bucket], QB, np.int32)       # (1,)

            f.write(l1_flat.tobytes())
            f.write(l1_b_q.tobytes())
            f.write(l2_w_q.tobytes())
            f.write(l2_b_q.tobytes())
            f.write(skip13_q.tobytes())   # v6 new: before l3
            f.write(l3_w_q.tobytes())
            f.write(l3_b_q.tobytes())
            f.write(out_w_q.tobytes())
            f.write(out_b_q.tobytes())

    file_size = os.path.getsize(output_path)

    if verbose:
        print(f"Exported NNUE v6 weights to {output_path}")
        print(f"  File size:      {file_size:,} bytes ({file_size/1024/1024:.1f} MB)")
        print(f"  FT weight range: [{ft_w_q.min()}, {ft_w_q.max()}]")
        print(f"  FT bias range:   [{ft_b_q.min()}, {ft_b_q.max()}]")
        print(f"  PSQT range:      [{psqt_w_q.min()}, {psqt_w_q.max()}]")
        for bucket in range(OUTPUT_BUCKETS):
            l1_wq     = _quantize(l1_w[bucket], QB, np.int8)
            skip13_wq = _quantize(skip13_w[bucket], QB, np.int8)
            l3_wq     = _quantize(l3_w[bucket], QB, np.int8)
            print(f"  Bucket {bucket}: L1 [{l1_wq.min()}, {l1_wq.max()}]  "
                  f"skip13 [{skip13_wq.min()}, {skip13_wq.max()}]  "
                  f"L3 [{l3_wq.min()}, {l3_wq.max()}]")

    return file_size


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Export NNUE weights (v6)")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", default="nn.bin")
    args = parser.parse_args()
    export(args.checkpoint, args.output)


if __name__ == "__main__":
    main()