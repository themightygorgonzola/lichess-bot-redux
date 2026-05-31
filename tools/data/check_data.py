"""Quick score distribution diagnostic for a binary NNUE training file."""
import numpy as np, struct, sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent

HEADER_SIZE = 32
RECORD_DTYPE = np.dtype([
    ('score','<i2'), ('wdl','<f2'), ('stm','u1'), ('bucket','u1'),
    ('n_white','u1'), ('n_black','u1'),
    ('white_feats','<u2',(32,)), ('black_feats','<u2',(32,)),
])
RECORD_SIZE = RECORD_DTYPE.itemsize

path = sys.argv[1] if len(sys.argv) > 1 else str(_ROOT / 'data' / 'processed' / 'mean-alltime-dedup-shuffled.bin')

with open(path, 'rb') as fh:
    raw = fh.read(HEADER_SIZE)
    magic, version, n_records, input_size = struct.unpack('<8sIII12x', raw)
    print(f'File: {path}')
    print(f'Records: {n_records:,}  ({n_records * RECORD_SIZE / 1024**3:.1f} GB)')
    # Sample 30k records spread across file
    samples = []
    for frac in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        off = int(n_records * frac)
        fh.seek(HEADER_SIZE + off * RECORD_SIZE)
        chunk = np.frombuffer(fh.read(3000 * RECORD_SIZE), dtype=RECORD_DTYPE)
        samples.append(chunk)
    buf = np.concatenate(samples)

scores = buf['score'].astype(np.float32)
abs_s  = np.abs(scores)
print(f'\nScore distribution (n={len(buf):,} samples):')
print(f'  mean={scores.mean():.1f}  std={scores.std():.1f}  median={np.median(scores):.1f}')
print(f'  mean |score| = {abs_s.mean():.1f} cp')
print(f'  |score| <= 200cp : {(abs_s<=200).mean()*100:.1f}%')
print(f'  |score| <= 500cp : {(abs_s<=500).mean()*100:.1f}%')
print(f'  |score| <= 1000cp: {(abs_s<=1000).mean()*100:.1f}%')
print(f'  |score| <= 2000cp: {(abs_s<=2000).mean()*100:.1f}%')
print(f'  |score| == 3000cp (cap): {(abs_s>=3000).mean()*100:.1f}%')
print(f'\nSTM balance: white={( buf["stm"]==0).mean()*100:.1f}%  black={(buf["stm"]==1).mean()*100:.1f}%')
print(f'Bucket dist: {np.bincount(buf["bucket"].astype(int), minlength=8) / len(buf) * 100}')
