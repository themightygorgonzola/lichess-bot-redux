"""
export_weights.py — Convert a PyTorch NNUE checkpoint to the engine's binary format.

The binary format matches what nnue_network.cpp expects:
  Header:
    uint32  magic     = 0x4E4E5545 ("NNUE")
    uint32  version   = 1
    uint32  input_size  = 768
    uint32  hidden_size = 256
  Feature transform biases:   int16[256]
  Feature transform weights:  int16[768][256]  (row-major: for each input, 256 weights)
  Output weights perspective 0: int16[256]
  Output weights perspective 1: int16[256]
  Output bias:                  int16

Quantization:
  - Feature transform weights/biases: multiply float by QA_SCALE (64)
  - Output weights: multiply float × CRELU_MAX (127) × QB_SCALE (64)
    (because the hidden layer outputs are in [0, 127] after quantized clipped-ReLU,
     but the Python model has activations in [0, 1])
  - Output bias: multiply float by QA_SCALE × QB_SCALE (4096)
    (to match the overall scale chain)

Usage:
  python -m training.export_weights --checkpoint best_model.pt --output nn.bin
"""

import argparse
import struct
import sys

import numpy as np
import torch

from .model import NNUE, INPUT_SIZE, HIDDEN_SIZE


# Must match nnue_arch.h
NNUE_FILE_MAGIC = 0x4E4E5545
NNUE_FILE_VERSION = 1
QA_SCALE = 64
QB_SCALE = 64
CRELU_MAX = 127


def quantize_clamp(arr: np.ndarray, scale: float, dtype=np.int16) -> np.ndarray:
    """Scale, round, and clamp to int16 range."""
    scaled = np.round(arr * scale).astype(np.int64)
    info = np.iinfo(dtype)
    clamped = np.clip(scaled, info.min, info.max)
    return clamped.astype(dtype)


def export(checkpoint_path: str, output_path: str):
    # Load checkpoint
    ckpt = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
    state = ckpt['model_state_dict'] if 'model_state_dict' in ckpt else ckpt

    model = NNUE()
    model.load_state_dict(state)
    model.eval()

    # Extract float parameters
    ft_weight = model.ft.weight.data.numpy()  # shape: (256, 768)
    ft_bias = model.ft.bias.data.numpy()      # shape: (256,)
    out_weight = model.out.weight.data.numpy() # shape: (1, 512)
    out_bias = model.out.bias.data.numpy()     # shape: (1,)

    assert ft_weight.shape == (HIDDEN_SIZE, INPUT_SIZE)
    assert ft_bias.shape == (HIDDEN_SIZE,)
    assert out_weight.shape == (1, 2 * HIDDEN_SIZE)
    assert out_bias.shape == (1,)

    # Quantize feature transform
    # In C++ these are stored as ft_weights_[768][256] (transposed from PyTorch)
    ft_weight_q = quantize_clamp(ft_weight.T, QA_SCALE)  # (768, 256)
    ft_bias_q = quantize_clamp(ft_bias, QA_SCALE)        # (256,)

    # Quantize output layer
    # The C++ forward pass multiplies quantized accumulator (in [0, 127]) with out_weights
    # Python accumulator is in [0, 1], so we need to scale by CRELU_MAX * QB_SCALE
    # out_weights_[0][256] = friendly perspective, out_weights_[1][256] = enemy perspective
    out_w_friendly = out_weight[0, :HIDDEN_SIZE]             # (256,)
    out_w_enemy = out_weight[0, HIDDEN_SIZE:]                # (256,)
    out_w_friendly_q = quantize_clamp(out_w_friendly, QB_SCALE)
    out_w_enemy_q = quantize_clamp(out_w_enemy, QB_SCALE)

    # Output bias: the C++ code divides by (QA_SCALE * QB_SCALE), so we scale up
    out_bias_q = quantize_clamp(out_bias, QA_SCALE * QB_SCALE)

    # Write binary file
    with open(output_path, 'wb') as f:
        # Header
        f.write(struct.pack('<I', NNUE_FILE_MAGIC))
        f.write(struct.pack('<I', NNUE_FILE_VERSION))
        f.write(struct.pack('<I', INPUT_SIZE))
        f.write(struct.pack('<I', HIDDEN_SIZE))

        # Feature transform biases: int16[256]
        f.write(ft_bias_q.tobytes())

        # Feature transform weights: int16[768][256] — row major
        f.write(ft_weight_q.tobytes())

        # Output weights perspective 0 (friendly): int16[256]
        f.write(out_w_friendly_q.tobytes())

        # Output weights perspective 1 (enemy): int16[256]
        f.write(out_w_enemy_q.tobytes())

        # Output bias: int16
        f.write(out_bias_q[0:1].tobytes())

    # Verify file size
    expected_size = (
        4 * 4 +                              # header: 4 × uint32
        HIDDEN_SIZE * 2 +                    # biases: 256 × int16
        INPUT_SIZE * HIDDEN_SIZE * 2 +       # weights: 768 × 256 × int16
        HIDDEN_SIZE * 2 +                    # out_w perspective 0
        HIDDEN_SIZE * 2 +                    # out_w perspective 1
        2                                    # out_bias: 1 × int16
    )
    actual_size = _file_size(output_path)
    assert actual_size == expected_size, \
        f"File size mismatch: expected {expected_size}, got {actual_size}"

    print(f"Exported NNUE weights to {output_path}")
    print(f"  File size: {actual_size} bytes")
    print(f"  FT weight range: [{ft_weight_q.min()}, {ft_weight_q.max()}]")
    print(f"  FT bias range:   [{ft_bias_q.min()}, {ft_bias_q.max()}]")
    print(f"  Out weight range: friendly [{out_w_friendly_q.min()}, {out_w_friendly_q.max()}]"
          f"  enemy [{out_w_enemy_q.min()}, {out_w_enemy_q.max()}]")
    print(f"  Out bias: {out_bias_q[0]}")


def _file_size(path: str) -> int:
    import os
    return os.path.getsize(path)


def main():
    parser = argparse.ArgumentParser(description="Export NNUE weights to engine binary format")
    parser.add_argument("--checkpoint", required=True, help="Path to PyTorch checkpoint")
    parser.add_argument("--output", default="nn.bin", help="Output binary path")
    args = parser.parse_args()

    export(args.checkpoint, args.output)


if __name__ == "__main__":
    main()
