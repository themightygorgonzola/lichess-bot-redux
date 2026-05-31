import numpy as np
from ml.data import RECORD_DTYPE, HEADER_SIZE, _read_header

bin_path = 'data/training/q2019_06.bin'
meta = _read_header(bin_path)
arr = np.memmap(bin_path, dtype=RECORD_DTYPE, mode='r', offset=HEADER_SIZE, shape=(meta['n_records'],))

print(f'File: {bin_path}')
print(f'Records: {meta["n_records"]:,}')
print(f'WDL range: [{arr["wdl"].min():.3f}, {arr["wdl"].max():.3f}]')
print(f'WDL mean: {arr["wdl"].mean():.4f}')
print()
print('Sample records (first 10):')
for i in range(min(10, len(arr))):
    r = arr[i]
    print(f'  score={r["score"]:+5d}  wdl={r["wdl"]:.3f}')
print()
print('WDL variance by score bin (should see spread, not uniform 0.5):')
for score_bin in [-1000, -500, 0, 500, 1000]:
    mask = (arr['score'] >= score_bin - 50) & (arr['score'] <= score_bin + 50)
    if mask.sum() > 0:
        wdls = arr[mask]['wdl']
        print(f'  score ~{score_bin:+5d}: {mask.sum():3d} records  mean={wdls.mean():.3f}  std={wdls.std():.3f}  range=[{wdls.min():.3f}, {wdls.max():.3f}]')
