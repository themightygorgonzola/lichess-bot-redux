import sys, os
sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), '..', '..', '..', 'tools'))
from uci.board import MinimalBoard

b = MinimalBoard()
print('startpos FEN:', b.fen())
print('push e2e4 ->', b.push_uci('e2e4'))
# move from an empty square (should not raise)
print("push a3a4 (from empty) ->", b.push_uci('a3a4'))
# test a promotion move from existing pawn (from startpos a7a8q)
b2 = MinimalBoard()
print('push a7a8q ->', b2.push_uci('a7a8q'))
