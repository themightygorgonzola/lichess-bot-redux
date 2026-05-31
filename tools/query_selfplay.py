import sqlite3, os

db_path = "bot/data/challenger.db"
if not os.path.exists(db_path):
    print("DB not found at", db_path)
    raise SystemExit(1)

con = sqlite3.connect(db_path)

# All selfplay games today
rows = con.execute(
    "SELECT date_utc, our_color, opponent, result, reason, ply_count "
    "FROM game_history WHERE service='selfplay' ORDER BY ts ASC"
).fetchall()

print(f"Total selfplay games: {len(rows)}")
print()

# Group by matchup
from collections import defaultdict
matchups = defaultdict(lambda: {"w":0,"l":0,"d":0,"inc":0})

for date, color, opp, result, reason, ply in rows:
    if reason == "stopped" or ply is not None and ply < 20:
        matchups[opp]["inc"] += 1
        continue
    # result is chess notation: 1-0, 0-1, 1/2-1/2
    if result == "1/2-1/2":
        matchups[opp]["d"] += 1
    elif (result == "1-0" and color == "white") or (result == "0-1" and color == "black"):
        matchups[opp]["w"] += 1
    elif (result == "0-1" and color == "white") or (result == "1-0" and color == "black"):
        matchups[opp]["l"] += 1
    else:
        matchups[opp]["inc"] += 1

for opp, s in sorted(matchups.items()):
    total = s["w"] + s["l"] + s["d"]
    score = s["w"] + 0.5*s["d"]
    pct = 100*score/total if total else 0
    print(f"  vs {opp}")
    print(f"    W/L/D: {s['w']}/{s['l']}/{s['d']}  ({total} games, {pct:.1f}% score)  incomplete={s['inc']}")
    print()

# Also show raw last 20
print(f"{'Date':<12}  {'Opponent':<22}  {'Col':<5}  {'Res':<7}  {'Reason':<22}  Ply")
print("-"*80)
for row in rows[-30:]:
    date, color, opp, result, reason, ply = row
    print(f"  {str(date)[:10]}  {str(opp)[:22]:<22}  {str(color):<5}  {str(result):<7}  {str(reason):<22}  {ply}")

con.close()

