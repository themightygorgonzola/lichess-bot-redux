"""
model.py — PyTorch NNUE model definition

Architecture: 768 → 256 → 1 (perspective net)
  - Feature transform: 768 → 256 (shared weights, applied per-perspective)
  - Clipped ReLU after feature transform
  - Output: concatenate friendly + enemy accumulator → linear → scalar
"""

import torch
import torch.nn as nn


INPUT_SIZE = 768
HIDDEN_SIZE = 256


class NNUE(nn.Module):
    """
    NNUE perspective network.

    Forward pass:
      1. Look up active features for white and black perspectives
      2. Apply shared feature-transform layer (bias + sum of active columns)
      3. Clipped ReLU on both accumulators
      4. Concatenate [friendly, enemy] (based on STM)
      5. Linear output → centipawn score
    """

    def __init__(self):
        super().__init__()

        # Feature transform: shared weight matrix + bias
        # In the C++ engine this is stored column-major: ft_weights[768][256]
        # Here we use a linear layer: (768, 256)
        self.ft = nn.Linear(INPUT_SIZE, HIDDEN_SIZE)

        # Output layer: takes concatenated [friendly, enemy] → 1
        # This matches the C++ layout: out_weights[2][HIDDEN_SIZE]
        self.out = nn.Linear(2 * HIDDEN_SIZE, 1)

    def forward(self, white_input: torch.Tensor, black_input: torch.Tensor,
                stm: torch.Tensor) -> torch.Tensor:
        """
        Args:
            white_input: (batch, 768) binary features for white perspective
            black_input: (batch, 768) binary features for black perspective
            stm: (batch,) 0 = white to move, 1 = black to move

        Returns:
            (batch, 1) predicted score in centipawns from STM's perspective
        """
        # Feature transform
        white_acc = self.ft(white_input)  # (batch, 256)
        black_acc = self.ft(black_input)  # (batch, 256)

        # Clipped ReLU: clamp to [0, 1] (we'll quantize to [0, 127] at export)
        white_acc = torch.clamp(white_acc, 0.0, 1.0)
        black_acc = torch.clamp(black_acc, 0.0, 1.0)

        # Perspective: friendly first, enemy second
        # stm=0 (white): friendly=white, enemy=black
        # stm=1 (black): friendly=black, enemy=white
        stm_f = stm.float().unsqueeze(1)  # (batch, 1)

        friendly = white_acc * (1 - stm_f) + black_acc * stm_f
        enemy = black_acc * (1 - stm_f) + white_acc * stm_f

        # Concatenate and output
        combined = torch.cat([friendly, enemy], dim=1)  # (batch, 512)
        out = self.out(combined)  # (batch, 1)

        return out


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
