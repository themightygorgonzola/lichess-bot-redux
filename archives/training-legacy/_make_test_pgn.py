"""Quick script to create a test PGN for pipeline verification."""

import os
os.makedirs('data', exist_ok=True)

pgn = """[Event "Test"]
[Site "?"]
[Date "2025.01.01"]
[Round "1"]
[White "Player1"]
[Black "Player2"]
[Result "1-0"]
[WhiteElo "2000"]
[BlackElo "2000"]
[TimeControl "300+3"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 1-0

[Event "Test"]
[Site "?"]
[Date "2025.01.01"]
[Round "2"]
[White "Player3"]
[Black "Player4"]
[Result "0-1"]
[WhiteElo "2100"]
[BlackElo "1900"]
[TimeControl "600+0"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 Nbd7 7. Rc1 c6 8. Bd3 dxc4 9. Bxc4 Nd5 10. Bxe7 Qxe7 0-1

[Event "Test"]
[Site "?"]
[Date "2025.01.01"]
[Round "3"]
[White "Player5"]
[Black "Player6"]
[Result "1/2-1/2"]
[WhiteElo "2200"]
[BlackElo "2200"]
[TimeControl "180+2"]

1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 6. Be2 e5 7. Nb3 Be7 8. O-O O-O 9. Be3 Be6 10. f3 Nbd7 11. Qd2 b5 12. a4 b4 13. Nd5 Bxd5 14. exd5 a5 1/2-1/2
"""

with open('data/pgn/test.pgn', 'w') as f:
    f.write(pgn)
print("Test PGN created: data/pgn/test.pgn")
