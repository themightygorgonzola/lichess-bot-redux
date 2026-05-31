"""
generate_data.py — Generate training data by running the engine on random/game positions.

This script uses the existing engine (with HCE) to label positions with scores.
It can also import PGN games and label each position.

Usage:
  # From self-play (using the engine's own search):
  python -m training.generate_data --mode selfplay --engine build/lichess-bot.exe --output data.csv --games 1000

  # From PGN (label with engine search):
  python -m training.generate_data --mode pgn --engine build/lichess-bot.exe --pgn games.pgn --output data.csv

  # From random positions (fast, lower quality):
  python -m training.generate_data --mode random --output data.csv --count 1000000
"""

import argparse
import csv
import random
import subprocess
import sys
import time

import chess
import chess.pgn


def init_engine(engine_path: str, hash_mb: int = 64, depth: int = 8):
    """Start the engine as a UCI subprocess."""
    proc = subprocess.Popen(
        [engine_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def send(cmd: str):
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()

    def read_until(keyword: str) -> str:
        lines = []
        while True:
            line = proc.stdout.readline().strip()
            lines.append(line)
            if keyword in line:
                break
        return '\n'.join(lines)

    send('uci')
    read_until('uciok')
    send(f'setoption name Hash value {hash_mb}')
    send('isready')
    read_until('readyok')

    return proc, send, read_until


def get_eval(send, read_until, fen: str, depth: int = 8) -> int:
    """Get evaluation for a position in centipawns from white's perspective."""
    send(f'position fen {fen}')
    send(f'go depth {depth}')
    output = read_until('bestmove')

    # Parse the last "score cp" from info lines
    score_cp = 0
    stm_is_black = fen.split()[1] == 'b'

    for line in output.split('\n'):
        if 'score cp' in line:
            parts = line.split('score cp')
            if len(parts) >= 2:
                val_str = parts[1].strip().split()[0]
                try:
                    score_cp = int(val_str)
                except ValueError:
                    pass
        elif 'score mate' in line:
            parts = line.split('score mate')
            if len(parts) >= 2:
                val_str = parts[1].strip().split()[0]
                try:
                    mate_in = int(val_str)
                    score_cp = 30000 if mate_in > 0 else -30000
                except ValueError:
                    pass

    # Convert from STM perspective to white's perspective
    if stm_is_black:
        score_cp = -score_cp

    return score_cp


def generate_selfplay(engine_path: str, output_path: str, n_games: int,
                      search_depth: int, hash_mb: int):
    """Generate data from self-play games."""
    proc, send, read_until = init_engine(engine_path, hash_mb, search_depth)
    samples = []

    for game_idx in range(n_games):
        board = chess.Board()
        positions = []

        # Play a game
        while not board.is_game_over(claim_draw=True) and board.fullmove_number <= 200:
            fen = board.fen()

            # Get engine evaluation
            send(f'position fen {fen}')
            send(f'go depth {search_depth}')
            output = read_until('bestmove')

            # Parse score and bestmove
            score_cp = 0
            best_uci = None
            stm_is_black = board.turn == chess.BLACK

            for line in output.split('\n'):
                if 'score cp' in line:
                    parts = line.split('score cp')
                    if len(parts) >= 2:
                        val_str = parts[1].strip().split()[0]
                        try:
                            score_cp = int(val_str)
                        except ValueError:
                            pass
                elif 'score mate' in line:
                    parts = line.split('score mate')
                    if len(parts) >= 2:
                        val_str = parts[1].strip().split()[0]
                        try:
                            mate_in = int(val_str)
                            score_cp = 30000 if mate_in > 0 else -30000
                        except ValueError:
                            pass
                if 'bestmove' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        best_uci = parts[1]

            # Convert to white's perspective
            score_white = -score_cp if stm_is_black else score_cp

            # Skip very early positions (first 4 half-moves) and extreme scores
            if board.fullmove_number >= 3 and abs(score_white) < 10000:
                positions.append((fen, score_white))

            # Make the best move
            if best_uci:
                try:
                    move = chess.Move.from_uci(best_uci)
                    if move in board.legal_moves:
                        board.push(move)
                    else:
                        break
                except:
                    break
            else:
                break

        samples.extend(positions)

        if (game_idx + 1) % 10 == 0:
            print(f"  Game {game_idx+1}/{n_games}: {len(samples)} positions so far")

    # Clean up
    send('quit')
    proc.wait(timeout=5)

    # Write CSV
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['fen', 'score'])
        for fen, score in samples:
            writer.writerow([fen, score])

    print(f"\nGenerated {len(samples)} training samples from {n_games} games")
    print(f"Saved to: {output_path}")


def generate_random(output_path: str, n_positions: int):
    """Generate random positions by playing random moves, label with material eval."""
    samples = []
    games = 0

    while len(samples) < n_positions:
        board = chess.Board()
        # Play 5-40 random moves
        n_moves = random.randint(5, 40)

        for _ in range(n_moves):
            legal = list(board.legal_moves)
            if not legal:
                break
            board.push(random.choice(legal))

        if board.is_game_over():
            games += 1
            continue

        # Simple material evaluation from white's perspective
        material = 0
        piece_values = {
            chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
            chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0,
        }
        for sq in range(64):
            piece = board.piece_at(sq)
            if piece:
                val = piece_values[piece.piece_type]
                material += val if piece.color == chess.WHITE else -val

        if abs(material) < 5000:  # Skip very lopsided positions
            samples.append((board.fen(), material))

        games += 1
        if games % 10000 == 0:
            print(f"  {len(samples)}/{n_positions} positions generated")

    # Write CSV
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['fen', 'score'])
        for fen, score in samples[:n_positions]:
            writer.writerow([fen, score])

    print(f"Generated {min(len(samples), n_positions)} random positions")
    print(f"Saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate NNUE training data")
    parser.add_argument("--mode", choices=["selfplay", "random"], default="selfplay")
    parser.add_argument("--engine", default="build/lichess-bot.exe",
                        help="Path to engine executable (for selfplay)")
    parser.add_argument("--output", default="training_data.csv")
    parser.add_argument("--games", type=int, default=1000,
                        help="Number of self-play games")
    parser.add_argument("--count", type=int, default=100000,
                        help="Number of random positions")
    parser.add_argument("--depth", type=int, default=8,
                        help="Search depth for labeling")
    parser.add_argument("--hash", type=int, default=64,
                        help="Hash table size in MB")
    args = parser.parse_args()

    if args.mode == "selfplay":
        generate_selfplay(args.engine, args.output, args.games,
                          args.depth, args.hash)
    elif args.mode == "random":
        generate_random(args.output, args.count)


if __name__ == "__main__":
    main()
