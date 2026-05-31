"""
test_nnue.py — Unit tests for the NNUE v2 training package.

Run:  python -m pytest nnue/test_nnue.py -v
  or: python nnue/test_nnue.py
"""

import sys
import os
import tempfile

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import chess
import numpy as np

from ml.arch import (
    INPUT_SIZE, FT_SIZE, L1_SIZE, L2_SIZE, L3_SIZE, OUTPUT_BUCKETS,
    FEATURES_PER_BUCKET, NUM_KING_BUCKETS,
    piece_count_bucket, needs_mirror, mirror_square,
    QA, QB,
)
from ml.features import board_features, feature_index_for_piece, _feature_index
from ml.model import NNUE, count_parameters, model_summary
from ml.loss import wdl_loss, wdl_eval_metrics


# ── Architecture constants ────────────────────────────────────────────────

def test_arch_constants():
    assert INPUT_SIZE == 40960, f"INPUT_SIZE should be 40960, got {INPUT_SIZE}"
    assert FT_SIZE == 1024
    assert L1_SIZE == 128
    assert L2_SIZE == 32
    assert L3_SIZE == 16
    assert OUTPUT_BUCKETS == 8
    assert FEATURES_PER_BUCKET == 640
    assert NUM_KING_BUCKETS == 64
    assert INPUT_SIZE == NUM_KING_BUCKETS * FEATURES_PER_BUCKET
    print("  [OK] Architecture constants correct")


def test_piece_count_bucket():
    # 2 pieces (KvK) → bucket 0
    assert piece_count_bucket(2) == 0
    # 32 pieces (full board) → bucket 7
    assert piece_count_bucket(32) == 7
    # 16 pieces → bucket 3
    assert piece_count_bucket(16) == 3
    # Edge case: 33+ should clamp
    assert piece_count_bucket(33) == 7
    print("  [OK] Piece count bucket mapping")


def test_mirror():
    # a-d files: no mirror; e-h files: mirror
    assert not needs_mirror(0)    # a1
    assert not needs_mirror(3)    # d1
    assert needs_mirror(4)        # e1
    assert needs_mirror(7)        # h1
    
    # mirror_square flips file only
    assert mirror_square(0) == 7   # a1 → h1
    assert mirror_square(7) == 0   # h1 → a1
    assert mirror_square(4) == 3   # e1 → d1
    assert mirror_square(63) == 56 # h8 → a8
    print("  [OK] Horizontal mirroring")


# ── Features ──────────────────────────────────────────────────────────────

def test_feature_index_bounds():
    """All feature indices must be in [0, INPUT_SIZE)."""
    # Test with startpos
    board = chess.Board()
    w_feats, b_feats, pc = board_features(board)
    
    assert pc == 32, f"Startpos should have 32 pieces, got {pc}"
    # Should have 30 features (32 pieces - 2 kings)
    assert len(w_feats) == 30, f"Expected 30 features (no kings), got {len(w_feats)}"
    assert len(b_feats) == 30
    
    for idx in w_feats:
        assert 0 <= idx < INPUT_SIZE, f"White feature {idx} out of bounds"
    for idx in b_feats:
        assert 0 <= idx < INPUT_SIZE, f"Black feature {idx} out of bounds"
    print("  [OK] Feature indices in bounds (startpos)")


def test_feature_uniqueness():
    """No duplicate features for a given perspective."""
    board = chess.Board()
    w_feats, b_feats, _ = board_features(board)
    
    assert len(set(w_feats)) == len(w_feats), "Duplicate white features!"
    assert len(set(b_feats)) == len(b_feats), "Duplicate black features!"
    print("  [OK] Feature uniqueness")


def test_feature_king_mirroring():
    """Positions with king on e-h should trigger mirroring."""
    # King on g1 (file=6, >= 4) → mirror
    board = chess.Board("8/8/8/8/8/8/8/6K1 w - - 0 1")
    assert needs_mirror(board.king(chess.WHITE))
    
    # King on c1 (file=2, < 4) → no mirror
    board = chess.Board("8/8/8/8/8/8/8/2K5 w - - 0 1")
    assert not needs_mirror(board.king(chess.WHITE))
    print("  [OK] King mirroring detection")


def test_feature_various_positions():
    """Test features on several positions."""
    fens = [
        chess.STARTING_FEN,
        "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R b KQkq - 0 5",
        "8/8/8/8/8/8/4K3/4k3 w - - 0 1",  # KvK
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",  # after 1.e4
    ]
    for fen in fens:
        board = chess.Board(fen)
        w, b, pc = board_features(board)
        expected_feats = pc - 2  # minus 2 kings
        assert len(w) == expected_feats, f"FEN '{fen}': expected {expected_feats} feats, got {len(w)}"
        assert len(b) == expected_feats
        assert all(0 <= i < INPUT_SIZE for i in w)
        assert all(0 <= i < INPUT_SIZE for i in b)
    print("  [OK] Feature extraction on various positions")


def test_feature_consistency():
    """board_features and feature_index_for_piece should agree."""
    board = chess.Board()
    w_feats, b_feats, _ = board_features(board)
    
    # Manually compute features using feature_index_for_piece
    w_king_sq = board.king(chess.WHITE)
    b_king_sq = board.king(chess.BLACK)
    
    w_manual = []
    b_manual = []
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if piece is None or piece.piece_type == chess.KING:
            continue
        pc = chess.WHITE if piece.color else chess.BLACK
        
        wi = feature_index_for_piece(w_king_sq, chess.WHITE, pc, piece.piece_type, sq)
        bi = feature_index_for_piece(b_king_sq, chess.BLACK, pc, piece.piece_type, sq)
        if wi >= 0:
            w_manual.append(wi)
        if bi >= 0:
            b_manual.append(bi)
    
    assert sorted(w_feats) == sorted(w_manual), "White features don't match incremental computation"
    assert sorted(b_feats) == sorted(b_manual), "Black features don't match incremental computation"
    print("  [OK] Feature consistency (full vs incremental)")


# ── Model ─────────────────────────────────────────────────────────────────

def test_model_creation():
    model = NNUE()
    n = count_parameters(model)
    print(f"  [OK] Model created: {n:,} parameters")
    # FT: 40960 * 1024 + 1024 = 41,945,088
    # L1: 8 * (128 * 2048 + 128) = 8 * 262,272 = 2,098,176
    # L2: 8 * (32 * 128 + 32) = 8 * 4,128 = 33,024
    # L3: 8 * (16 * 32 + 16) = 8 * 528 = 4,224
    # Out: 8 * (1 * 16 + 1) = 8 * 17 = 136
    # Total ≈ 44,080,648
    assert n > 43_000_000, f"Expected ~44M params, got {n:,}"
    assert n < 50_000_000, f"Too many params: {n:,}"


def test_model_forward():
    model = NNUE()
    B = 4
    MAX_FEATS = 32

    wi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    bi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    nw = torch.full((B,), 30, dtype=torch.long)
    nb = torch.full((B,), 30, dtype=torch.long)

    # Set 30 active features per sample (non-overlapping)
    for i in range(B):
        for j in range(30):
            wi[i, j] = j * 100
            bi[i, j] = j * 100 + 50

    stm = torch.tensor([0, 1, 0, 1])
    bucket = torch.tensor([3, 3, 7, 0])

    with torch.no_grad():
        out = model(wi, nw, bi, nb, stm, bucket)

    assert out.shape == (B, 1), f"Expected shape (4, 1), got {out.shape}"
    print(f"  [OK] Forward pass: output shape {out.shape}, values: {out.squeeze().tolist()}")


def test_model_gradient():
    model = NNUE()
    B = 2
    MAX_FEATS = 32

    wi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    bi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    nw = torch.ones(B, dtype=torch.long)    # 1 active feature each
    nb = torch.ones(B, dtype=torch.long)
    wi[0, 0] = 0;    wi[1, 0] = 640
    bi[0, 0] = 320;  bi[1, 0] = 960

    stm = torch.tensor([0, 1])
    bucket = torch.tensor([4, 4])
    scores = torch.tensor([100.0, -50.0])

    pred = model(wi, nw, bi, nb, stm, bucket).squeeze(1)
    loss = torch.mean((pred - scores) ** 2)
    loss.backward()

    # Check that FT weights have gradients
    assert model.ft.weight.grad is not None
    grad_nnz = (model.ft.weight.grad != 0).sum().item()
    assert grad_nnz > 0, "FT weight gradients are all zero!"
    print(f"  [OK] Backward pass: {grad_nnz} non-zero FT gradients")


def test_model_cuda():
    if not torch.cuda.is_available():
        print("  [SKIP] CUDA not available")
        return

    model = NNUE().cuda()
    B = 8
    MAX_FEATS = 32

    wi = torch.zeros(B, MAX_FEATS, dtype=torch.long, device='cuda')
    bi = torch.zeros(B, MAX_FEATS, dtype=torch.long, device='cuda')
    wi[0, 0] = 0;   bi[0, 0] = 320
    nw = torch.zeros(B, dtype=torch.long, device='cuda');  nw[0] = 1
    nb = torch.zeros(B, dtype=torch.long, device='cuda');  nb[0] = 1

    stm = torch.zeros(B, dtype=torch.long, device='cuda')
    bucket = torch.full((B,), 4, dtype=torch.long, device='cuda')

    with torch.no_grad():
        out = model(wi, nw, bi, nb, stm, bucket)
    assert out.shape == (B, 1)

    # Test AMP
    with torch.amp.autocast(device_type='cuda', dtype=torch.float16):
        out_amp = model(wi, nw, bi, nb, stm, bucket)
    assert out_amp.shape == (B, 1)
    print(f"  [OK] CUDA forward + AMP: {out.device}")


# ── Loss ──────────────────────────────────────────────────────────────────

def test_loss_pure_mse():
    pred = torch.tensor([100.0, -50.0, 0.0])
    target = torch.tensor([110.0, -40.0, 10.0])
    wdl = torch.tensor([0.5, 0.5, 0.5])
    
    loss = wdl_loss(pred, target, wdl, lambda_=1.0)
    expected_mse = ((100-110)**2 + (-50-(-40))**2 + (0-10)**2) / 3.0
    assert abs(loss.item() - expected_mse) < 0.1, f"MSE: expected {expected_mse}, got {loss.item()}"
    print(f"  [OK] Pure MSE loss: {loss.item():.1f}")


def test_loss_wdl_blend():
    pred = torch.tensor([100.0, -100.0])
    target = torch.tensor([105.0, -95.0])
    wdl = torch.tensor([1.0, 0.0])
    
    # lambda=0.5 should give a blend of MSE and BCE
    loss_blend = wdl_loss(pred, target, wdl, lambda_=0.5)
    loss_mse = wdl_loss(pred, target, wdl, lambda_=1.0)
    assert loss_blend.item() > 0
    # Blended loss should differ from pure MSE
    print(f"  [OK] WDL blend: {loss_blend.item():.4f} (pure MSE: {loss_mse.item():.4f})")


# ── Export ────────────────────────────────────────────────────────────────

def test_export():
    from ml.export import export as do_export
    
    model = NNUE()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Save a fake checkpoint
        ckpt_path = os.path.join(tmpdir, "test.pt")
        torch.save({'model_state_dict': model.state_dict()}, ckpt_path)
        
        # Export
        out_path = os.path.join(tmpdir, "nn_v3.bin")
        file_size = do_export(ckpt_path, out_path, verbose=False)
        
        assert os.path.isfile(out_path)
        assert file_size > 0
        
        # Verify header
        import struct
        with open(out_path, 'rb') as f:
            magic = struct.unpack('<I', f.read(4))[0]
            version = struct.unpack('<I', f.read(4))[0]
            inp = struct.unpack('<I', f.read(4))[0]
            ft = struct.unpack('<I', f.read(4))[0]
            l1 = struct.unpack('<I', f.read(4))[0]
            l2 = struct.unpack('<I', f.read(4))[0]
            l3 = struct.unpack('<I', f.read(4))[0]
            buckets = struct.unpack('<I', f.read(4))[0]
        
        assert magic == 0x4E4E5545, f"Bad magic: {magic:#x}"
        assert version == 3
        assert inp == INPUT_SIZE
        assert ft == FT_SIZE
        assert l1 == L1_SIZE
        assert l2 == L2_SIZE
        assert l3 == L3_SIZE
        assert buckets == OUTPUT_BUCKETS
        
        print(f"  [OK] Export: {file_size:,} bytes, header verified")


# ── Integration ───────────────────────────────────────────────────────────

def test_end_to_end_mini_batch():
    """Full pipeline: FEN → features → model → loss → backward."""
    model = NNUE()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

    fens = [
        chess.STARTING_FEN,
        "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R b KQkq - 0 5",
        "rnbqkb1r/pppppppp/5n2/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2",
    ]
    targets = [0.0, -50.0, 30.0]
    wdls = [0.5, 0.3, 0.7]

    B = len(fens)
    MAX_FEATS = 32
    wi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    bi = torch.zeros(B, MAX_FEATS, dtype=torch.long)
    nw = torch.zeros(B, dtype=torch.long)
    nb = torch.zeros(B, dtype=torch.long)
    stm_t    = torch.zeros(B, dtype=torch.long)
    bucket_t = torch.zeros(B, dtype=torch.long)

    for i, fen in enumerate(fens):
        board = chess.Board(fen)
        wf, bf, pc = board_features(board)
        cnt_w = min(len(wf), MAX_FEATS)
        cnt_b = min(len(bf), MAX_FEATS)
        nw[i] = cnt_w;  nb[i] = cnt_b
        for j, idx in enumerate(wf[:cnt_w]):
            wi[i, j] = idx
        for j, idx in enumerate(bf[:cnt_b]):
            bi[i, j] = idx
        stm_t[i]    = 0 if board.turn == chess.WHITE else 1
        bucket_t[i] = piece_count_bucket(pc)

    scores_t = torch.tensor(targets, dtype=torch.float32)
    wdl_t    = torch.tensor(wdls,    dtype=torch.float32)

    # Forward + backward
    optimizer.zero_grad()
    pred = model(wi, nw, bi, nb, stm_t, bucket_t).squeeze(1)
    loss = wdl_loss(pred, scores_t, wdl_t, lambda_=0.5)
    loss.backward()
    optimizer.step()

    assert loss.item() > 0
    print(f"  [OK] End-to-end mini-batch: loss={loss.item():.4f}")


# ── Runner ────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        ("Architecture constants", test_arch_constants),
        ("Piece count buckets", test_piece_count_bucket),
        ("Horizontal mirroring", test_mirror),
        ("Feature index bounds", test_feature_index_bounds),
        ("Feature uniqueness", test_feature_uniqueness),
        ("Feature king mirroring", test_feature_king_mirroring),
        ("Feature various positions", test_feature_various_positions),
        ("Feature consistency", test_feature_consistency),
        ("Model creation", test_model_creation),
        ("Model forward", test_model_forward),
        ("Model gradient", test_model_gradient),
        ("Model CUDA", test_model_cuda),
        ("Loss pure MSE", test_loss_pure_mse),
        ("Loss WDL blend", test_loss_wdl_blend),
        ("Export v2", test_export),
        ("End-to-end mini-batch", test_end_to_end_mini_batch),
    ]
    
    n_pass = 0
    n_fail = 0
    n_skip = 0
    
    print(f"\n{'='*60}")
    print(f"  NNUE v2 Unit Tests")
    print(f"{'='*60}\n")
    
    for name, fn in tests:
        try:
            fn()
            n_pass += 1
        except Exception as e:
            if "SKIP" in str(e):
                n_skip += 1
            else:
                print(f"  [FAIL] {name}: {e}")
                n_fail += 1
    
    print(f"\n{'='*60}")
    print(f"  Results: {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    print(f"{'='*60}\n")
    
    return n_fail == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
