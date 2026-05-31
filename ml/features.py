"""
features.py — HalfKAv2 feature encoding for NNUE training.

Feature index = king_bucket * 640 + color * 320 + piece_type * 64 + square

Where:
  king_bucket = oriented king square (mirrored if king on files e-h)
  color       = piece color relative to perspective (0 = friendly, 1 = enemy)
  piece_type  = 0..4 (pawn, knight, bishop, rook, queen — no king)
  square      = oriented square (mirrored if king was mirrored)

Horizontal mirroring: if the perspective's king is on files e-h, the entire
position is mirrored horizontally. This halves the effective feature space
the network must learn.
"""

import chess
import numpy as np
from typing import Tuple, List

from .arch import (
    INPUT_SIZE, FEATURES_PER_BUCKET, NUM_PIECE_TYPES,
    NUM_SQUARES, needs_mirror, mirror_square, piece_count_bucket,
)

# Map python-chess piece types to our 0-based indices (no king)
_PT_MAP = {
    chess.PAWN:   0,
    chess.KNIGHT: 1,
    chess.BISHOP: 2,
    chess.ROOK:   3,
    chess.QUEEN:  4,
}


def _orient_square(sq: int, perspective: chess.Color, do_mirror: bool) -> int:
    """Orient a square for the given perspective.
    
    If perspective is BLACK, flip vertically (sq ^ 56).
    If do_mirror, flip horizontally (sq ^ 7).
    """
    if perspective == chess.BLACK:
        sq ^= 56
    if do_mirror:
        sq ^= 7
    return sq


def _feature_index(king_bucket: int, rel_color: int, piece_type_idx: int, oriented_sq: int) -> int:
    """Compute the single feature index within the INPUT_SIZE space."""
    return king_bucket * FEATURES_PER_BUCKET + rel_color * (NUM_PIECE_TYPES * NUM_SQUARES) + piece_type_idx * NUM_SQUARES + oriented_sq


def board_features(board: chess.Board) -> Tuple[List[int], List[int], int]:
    """
    Extract HalfKAv2 active feature indices for both perspectives.

    Returns:
        (white_features, black_features, piece_count)
        Each feature list contains indices in [0, INPUT_SIZE).
    """
    white_features: List[int] = []
    black_features: List[int] = []
    piece_count = 0

    # Find king squares
    white_king_sq = board.king(chess.WHITE)
    black_king_sq = board.king(chess.BLACK)
    
    if white_king_sq is None or black_king_sq is None:
        return [], [], 0

    # Determine mirroring for each perspective
    # White perspective: orient white king
    w_king_oriented = white_king_sq  # already from white's view
    w_mirror = needs_mirror(w_king_oriented)
    if w_mirror:
        w_king_oriented = mirror_square(w_king_oriented)
    w_king_bucket = w_king_oriented

    # Black perspective: flip white king vertically to get black's view, then check mirror
    b_king_oriented = black_king_sq ^ 56  # from black's view, flip vertically
    b_mirror = needs_mirror(b_king_oriented)
    if b_mirror:
        b_king_oriented = mirror_square(b_king_oriented)
    b_king_bucket = b_king_oriented

    # Iterate over all pieces
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None:
            continue
        piece_count += 1

        pt_idx = _PT_MAP.get(piece.piece_type)
        if pt_idx is None:
            continue  # skip kings

        piece_color = chess.WHITE if piece.color else chess.BLACK

        # ── White perspective ──
        # Relative color: 0 if piece is white (friendly), 1 if black (enemy)
        w_rel_color = 0 if piece_color == chess.WHITE else 1
        w_sq = _orient_square(sq, chess.WHITE, w_mirror)
        white_features.append(_feature_index(w_king_bucket, w_rel_color, pt_idx, w_sq))

        # ── Black perspective ──
        # Relative color: 0 if piece is black (friendly), 1 if white (enemy)
        b_rel_color = 0 if piece_color == chess.BLACK else 1
        b_sq = _orient_square(sq, chess.BLACK, b_mirror)
        black_features.append(_feature_index(b_king_bucket, b_rel_color, pt_idx, b_sq))

    return white_features, black_features, piece_count


def feature_index_for_piece(king_sq: int, perspective: chess.Color,
                            piece_color: chess.Color, piece_type: chess.PieceType,
                            piece_sq: int) -> int:
    """
    Compute the feature index for a single piece (useful for incremental updates).
    
    This matches the C++ acc_add_piece / acc_sub_piece logic.
    """
    pt_idx = _PT_MAP.get(piece_type)
    if pt_idx is None:
        return -1  # kings have no feature

    # Orient king square from this perspective
    oriented_king = king_sq
    if perspective == chess.BLACK:
        oriented_king ^= 56
    do_mirror = needs_mirror(oriented_king)
    if do_mirror:
        oriented_king = mirror_square(oriented_king)
    king_bucket = oriented_king

    # Relative color
    rel_color = 0 if piece_color == perspective else 1

    # Orient piece square
    oriented_sq = piece_sq
    if perspective == chess.BLACK:
        oriented_sq ^= 56
    if do_mirror:
        oriented_sq ^= 7

    return _feature_index(king_bucket, rel_color, pt_idx, oriented_sq)


# ============================================================================
# v6 Extension: Passed Pawn Features (change 3 -- requires dataset regen)
# ============================================================================
# These functions compute HalfKAv2+ features for the extended v6 dataset.
# The base HalfKAv2 features remain in [0, INPUT_SIZE) = [0, 40960).
# Passed pawn features occupy [HALFKAV2_SIZE, HALFKAV2_SIZE + PASSED_PAWN_SIZE)
#                             = [40960, 41088).
#
# Index: HALFKAV2_SIZE + rel_color * 64 + oriented_sq
#   rel_color = 0: pawn belongs to the perspective's color (friendly passed pawn)
#   rel_color = 1: pawn belongs to the opponent (enemy passed pawn)
#   oriented_sq:   square oriented the same way as HalfKAv2 squares
# ============================================================================

from .arch import HALFKAV2_SIZE, PASSED_PAWN_SIZE, INPUT_SIZE_V6


def _is_passed_pawn(board: chess.Board, sq: int, color: chess.Color) -> bool:
    """Return True if the pawn on `sq` of `color` is a passed pawn."""
    file_ = chess.square_file(sq)
    rank_ = chess.square_rank(sq)
    enemy_color = not color

    # Build adjacent-file mask (same file + neighbouring files)
    adj_mask = 0
    for df in (-1, 0, 1):
        f = file_ + df
        if 0 <= f <= 7:
            adj_mask |= chess.BB_FILES[f]

    # Build ahead mask (squares in front of the pawn)
    if color == chess.WHITE:
        ahead_mask = sum(chess.BB_RANKS[r] for r in range(rank_ + 1, 8))
    else:
        ahead_mask = sum(chess.BB_RANKS[r] for r in range(0, rank_))

    enemy_pawns = board.pieces(chess.PAWN, enemy_color)
    return not bool(enemy_pawns & adj_mask & ahead_mask)


def passed_pawn_features(board: chess.Board,
                         perspective: chess.Color,
                         king_bucket: int,
                         do_mirror: bool) -> List[int]:
    """
    Compute passed-pawn feature indices for a given perspective.

    Returns a list of indices in [HALFKAV2_SIZE, HALFKAV2_SIZE + PASSED_PAWN_SIZE).
    Called after board_features() for the same (perspective, king_bucket, do_mirror).
    """
    indices: List[int] = []

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.piece_type != chess.PAWN:
            continue
        if not _is_passed_pawn(board, sq, piece.color):
            continue

        # Relative color from this perspective's viewpoint
        rel_color = 0 if piece.color == perspective else 1

        # Orient the square the same way as HalfKAv2
        oriented_sq = sq
        if perspective == chess.BLACK:
            oriented_sq ^= 56
        if do_mirror:
            oriented_sq ^= 7

        indices.append(HALFKAV2_SIZE + rel_color * NUM_SQUARES + oriented_sq)

    return indices


def board_features_v6(board: chess.Board) -> Tuple[List[int], List[int], int]:
    """
    Extract HalfKAv2 + passed-pawn active feature indices for both perspectives.

    This is the v6 variant that requires the extended dataset (INPUT_SIZE_V6=41088,
    MAX_FEATS_V6=48). Use board_features() for the v5-compatible dataset.

    Returns:
        (white_features, black_features, piece_count)
        Each list contains indices in [0, INPUT_SIZE_V6).
    """
    # Base HalfKAv2 features (same computation as board_features)
    white_features_base, black_features_base, piece_count = board_features(board)

    if not white_features_base and not black_features_base:
        return [], [], 0

    # Recompute perspective geometry (same logic as in board_features)
    white_king_sq = board.king(chess.WHITE)
    black_king_sq = board.king(chess.BLACK)

    w_king_oriented = white_king_sq
    w_mirror = needs_mirror(w_king_oriented)
    if w_mirror:
        w_king_oriented = mirror_square(w_king_oriented)
    w_king_bucket = w_king_oriented

    b_king_oriented = black_king_sq ^ 56
    b_mirror = needs_mirror(b_king_oriented)
    if b_mirror:
        b_king_oriented = mirror_square(b_king_oriented)

    # Append passed pawn features
    white_pp = passed_pawn_features(board, chess.WHITE, w_king_bucket, w_mirror)
    black_pp = passed_pawn_features(board, chess.BLACK, b_king_oriented, b_mirror)

    return (white_features_base + white_pp,
            black_features_base + black_pp,
            piece_count)

# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# ==
# v6 Extension: Passed Pawn Features (change 3 -- requires dataset regen)
# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# =# ==
# Base HalfKAv2 features remain in [0, INPUT_SIZE) = [0, 40960).
# Passed pawn features occupy [HALFKAV2_SIZE, HALFKAV2_SIZE + 128).
# Index: HALFKAV2_SIZE + rel_color * 64 + oriented_sq
#   rel_color = 0: friendly passed pawn  1: enemy passed pawn

from .arch import HALFKAV2_SIZE, PASSED_PAWN_SIZE, INPUT_SIZE_V6


def _is_passed_pawn(board: chess.Board, sq: int, color: chess.Color) -> bool:
    file_ = chess.square_file(sq)
    rank_ = chess.square_rank(sq)
    enemy_color = not color
    adj_mask = 0
    for df in (-1, 0, 1):
        f = file_ + df
        if 0 <= f <= 7:
            adj_mask |= chess.BB_FILES[f]
    if color == chess.WHITE:
        ahead_mask = sum(chess.BB_RANKS[r] for r in range(rank_ + 1, 8))
    else:
        ahead_mask = sum(chess.BB_RANKS[r] for r in range(0, rank_))
    enemy_pawns = board.pieces(chess.PAWN, enemy_color)
    return not bool(enemy_pawns & adj_mask & ahead_mask)


def passed_pawn_features(board: chess.Board, perspective: chess.Color,
                         king_bucket: int, do_mirror: bool) -> List[int]:
    indices: List[int] = []
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.piece_type != chess.PAWN:
            continue
        if not _is_passed_pawn(board, sq, piece.color):
            continue
        rel_color = 0 if piece.color == perspective else 1
        oriented_sq = sq
        if perspective == chess.BLACK:
            oriented_sq ^= 56
        if do_mirror:
            oriented_sq ^= 7
        indices.append(HALFKAV2_SIZE + rel_color * NUM_SQUARES + oriented_sq)
    return indices


def board_features_v6(board: chess.Board) -> Tuple[List[int], List[int], int]:
    white_base, black_base, piece_count = board_features(board)
    if not white_base and not black_base:
        return [], [], 0
    wksq = board.king(chess.WHITE)
    bksq = board.king(chess.BLACK)
    w_ko = wksq
    w_mir = needs_mirror(w_ko)
    if w_mir: w_ko = mirror_square(w_ko)
    w_kb = w_ko
    b_ko = bksq ^ 56
    b_mir = needs_mirror(b_ko)
    if b_mir: b_ko = mirror_square(b_ko)
    wpp = passed_pawn_features(board, chess.WHITE, w_kb, w_mir)
    bpp = passed_pawn_features(board, chess.BLACK, b_ko, b_mir)
    return (white_base + wpp, black_base + bpp, piece_count)
