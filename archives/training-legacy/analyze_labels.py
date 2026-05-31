#!/usr/bin/env python3
"""Analyze a labeled CSV (fen,score) and print detailed stats.
Usage: python training/analyze_labels.py path/to/labels.csv
"""
import sys
import csv
import statistics
import math
from collections import Counter

if len(sys.argv) < 2:
    print("Usage: analyze_labels.py <csv>")
    sys.exit(1)

fn = sys.argv[1]
scores = []
with open(fn, 'r', encoding='utf-8') as f:
    reader = csv.reader(f)
    next(reader, None)
    for row in reader:
        try:
            s = int(row[-1])
        except Exception:
            continue
        scores.append(s)

n = len(scores)
if n == 0:
    print("No samples found")
    sys.exit(0)

mean = statistics.mean(scores)
stdev = statistics.stdev(scores) if n > 1 else 0.0
mn = min(scores)
mx = max(scores)
med = statistics.median(scores)
sorted_scores = sorted(scores)

def perc(p):
    if p >= 100:
        return sorted_scores[-1]
    idx = int(math.floor(p/100.0 * n))
    idx = min(max(0, idx), n-1)
    return sorted_scores[idx]

cnt = Counter(scores)
cnt_pos30000 = cnt.get(30000, 0)
cnt_neg30000 = cnt.get(-30000, 0)
cnt_big = sum(1 for s in scores if abs(s) > 10000)

print(f'Count: {n:,}')
print(f'Mean: {mean:.2f}  Stdev: {stdev:.2f}')
print(f'Min: {mn}  Max: {mx}  Median: {med}')
print('\nPercentiles:')
for p in [1,5,10,25,50,75,90,95,99,99.9,100]:
    print(f'  {p:6}% -> {perc(p):d}')

print('\nExtreme counts:')
print(f'  =+30000: {cnt_pos30000:,}')
print(f'  =-30000: {cnt_neg30000:,}')
print(f'  abs>10000: {cnt_big:,}')

# Show a small histogram of buckets
buckets = [(-30000,-10000),(-10000,-5000),(-5000,-2000),(-2000,-500),(-500,0),(0,500),(500,2000),(2000,5000),(5000,10000),(10000,30000)]
print('\nBucket counts:')
for a,b in buckets:
    c = sum(1 for s in scores if s >= a and s < b)
    print(f'  {a:6}..{b:6}: {c:,}')
