# analyze_recent.ps1 -- Game performance analysis for lichess-bot-redux
#
# Usage:
#   .\tools\analyze_recent.ps1                          # last 100 games, all services
#   .\tools\analyze_recent.ps1 -n 200                   # last 200 games
#   .\tools\analyze_recent.ps1 -service selfplay        # selfplay only
#   .\tools\analyze_recent.ps1 -service lichess         # lichess only
#   .\tools\analyze_recent.ps1 -build 16                # filter to specific engine build
#   .\tools\analyze_recent.ps1 -n 50 -filter losses     # last 50 losses only

param(
    [int]$n        = 100,
    [string]$service = 'all',    # all | selfplay | lichess | chesscom
    [string]$filter  = 'all',    # all | wins | losses | draws
    [string]$build   = 'all'     # all | <build number e.g. 16>
)

$ErrorActionPreference = 'Stop'

# -- Load PGN files ------------------------------------------------------------
$pgnDir   = Join-Path $PSScriptRoot '..\..\data\games'
$pgnFiles = Get-ChildItem $pgnDir -Filter '*.pgn' -ErrorAction SilentlyContinue | Sort-Object Name
if (-not $pgnFiles) { Write-Error "No PGN files found in $pgnDir"; exit 1 }

$allPgn = ($pgnFiles | ForEach-Object { Get-Content $_.FullName -Raw }) -join "`n"

# -- Split into individual games -----------------------------------------------
$rawGames = [regex]::Split($allPgn, '(?=\[Event )') | Where-Object { $_ -match '^\[Event' }

# -- Parse helper -------------------------------------------------------------
function Get-Header($text, $tag) {
    if ($text -match "\[$tag ""([^""]+)""\]") { return $matches[1] } else { return $null }
}

# -- Parse all games -----------------------------------------------------------
$games = foreach ($g in $rawGames) {
    $result   = Get-Header $g 'Result'
    $svc      = Get-Header $g 'Service'
    if (-not $svc) { $svc = 'lichess' }
    $bld      = Get-Header $g 'EngineBuild'
    if (-not $bld) { $bld = '?' }
    $white    = Get-Header $g 'White'
    $black    = Get-Header $g 'Black'
    $speed    = Get-Header $g 'Speed'
    $date     = Get-Header $g 'Date'
    $gameId   = Get-Header $g 'GameId'
    $fen      = Get-Header $g 'FEN'

    $botColor  = if ($black -eq 'Bot') { 'black' } elseif ($white -eq 'Bot') { 'white' } else { '?' }
    $opponent  = if ($botColor -eq 'black') { $white } else { $black }

    $botResult = switch ($result) {
        '1-0'     { if ($botColor -eq 'white') { 'win' } else { 'loss' } }
        '0-1'     { if ($botColor -eq 'black') { 'win' } else { 'loss' } }
        '1/2-1/2' { 'draw' }
        default   { '?' }
    }

    # Parse eval annotations: { pcteval -2.93 ... pctstop confident }
    $annotations = [regex]::Matches($g, '\{[^}]+\}') | ForEach-Object { $_.Value }

    $evals = foreach ($ann in $annotations) {
        if ($ann -match '%eval\s+(#?-?\d+(?:\.\d+)?)') {
            $raw = $matches[1]
            if ($raw -match '^#(-?\d+)') {
                # mate score: #-N = Black mating, #N = White mating
                $m = [int]$matches[1]
                if ($m -lt 0) { -100.0 } else { 100.0 }
            } else {
                [double]$raw
            }
        }
    }

    $stops = [regex]::Matches($g, '%stop\s+(\w+)') | ForEach-Object { $_.Groups[1].Value }
    $timeouts   = ($stops | Where-Object { $_ -eq 'timeout' }).Count
    $confidents = ($stops | Where-Object { $_ -eq 'confident' }).Count
    $mates      = ($stops | Where-Object { $_ -eq 'mate_found' }).Count

    # Extract first 6 half-moves (3 full moves) for opening grouping
    $moveText = $g -replace '\[[^\]]+\]', '' -replace '\{[^}]+\}', '' -replace '\s+', ' '
    $halfMoves = [regex]::Matches($moveText.Trim(), '(?<!\d)(?:\d+\.\s*)?([NBRQK]?[a-h]?[1-8]?x?[a-h][1-8](?:=[NBRQ])?[+#]?|O-O-O|O-O)') |
                 ForEach-Object { $_.Groups[1].Value } | Select-Object -First 6
    $opening = $halfMoves -join ' '
    $w1 = if ($halfMoves.Count -ge 1) { $halfMoves[0] } else { '?' }
    $w2 = if ($halfMoves.Count -ge 3) { $halfMoves[2] } else { '?' }
    $openingKey = "1.$w1 2.$w2"

    # Eval collapse: bot was winning by >2.0 pawns, then lost
    $collapseMove = $null
    if ($botResult -eq 'loss' -and $evals.Count -gt 4) {
        $wasWinning = $false
        for ($i = 0; $i -lt $evals.Count; $i++) {
            $ev = $evals[$i]
            # Normalise so positive = bot winning
            $botAdv = if ($botColor -eq 'black') { -$ev } else { $ev }
            if ($botAdv -gt 2.0) { $wasWinning = $true }
            if ($wasWinning -and $botAdv -lt -0.5) {
                $collapseMove = $i + 1
                break
            }
        }
    }

    [PSCustomObject]@{
        Result      = $botResult
        Service     = $svc
        Build       = $bld
        BotColor    = $botColor
        Opponent    = $opponent
        Speed       = $speed
        Date        = $date
        GameId      = $gameId
        Evals       = $evals
        MoveCount   = ([regex]::Matches($moveText.Trim(), '(?<!\d)(?:\d+\.\s*)?([NBRQK]?[a-h]?[1-8]?x?[a-h][1-8](?:=[NBRQ])?[+#]?|O-O-O|O-O)')).Count
        Timeouts    = $timeouts
        Confidents  = $confidents
        Mates       = $mates
        Opening     = $opening
        OpeningKey  = $openingKey
        Collapse    = $collapseMove
        PawnOdds    = ($fen -match 'RNBQKB1R|RNBQKb1R')
    }
}

# -- Apply filters -------------------------------------------------------------
if ($service -ne 'all') { $games = $games | Where-Object { $_.Service -eq $service } }
if ($build   -ne 'all') { $games = $games | Where-Object { $_.Build   -eq $build   } }
if ($filter  -ne 'all') { $games = $games | Where-Object { $_.Result  -eq $filter  } }

# Take last N
$games = $games | Select-Object -Last $n
$total = $games.Count
if ($total -eq 0) { Write-Host "No games found matching filters."; exit 0 }

$wins   = @($games | Where-Object { $_.Result -eq 'win'  }).Count
$losses = @($games | Where-Object { $_.Result -eq 'loss' }).Count
$draws  = @($games | Where-Object { $_.Result -eq 'draw' }).Count
$winRate = if ($total -gt 0) { [math]::Round($wins / $total * 100, 1) } else { 0 }

# Recent trend -- last 20 of the filtered set
$recent    = $games | Select-Object -Last 20
$rWins     = ($recent | Where-Object { $_.Result -eq 'win'  }).Count
$rLosses   = ($recent | Where-Object { $_.Result -eq 'loss' }).Count
$rDraws    = ($recent | Where-Object { $_.Result -eq 'draw' }).Count
$rWinRate  = if ($recent.Count -gt 0) { [math]::Round($rWins / $recent.Count * 100, 1) } else { 0 }

# -- Opening breakdown (losses) ------------------------------------------------
$lossGames  = $games | Where-Object { $_.Result -eq 'loss' }
$openingLosses = $lossGames | Group-Object OpeningKey | Sort-Object Count -Descending | Select-Object -First 8
$openingTotal  = $games      | Group-Object OpeningKey

# -- Eval collapse -------------------------------------------------------------
$collapses    = $lossGames | Where-Object { $null -ne $_.Collapse }
$collapseRate = if ($losses -gt 0) { [math]::Round($collapses.Count / $losses * 100, 1) } else { 0 }
$avgCollapse  = if ($collapses.Count -gt 0) { [math]::Round(($collapses.Collapse | Measure-Object -Average).Average, 1) } else { 'n/a' }
$minCollapse  = if ($collapses.Count -gt 0) { ($collapses.Collapse | Measure-Object -Minimum).Minimum } else { 'n/a' }

# -- Stop reason breakdown -----------------------------------------------------
$winGames  = $games | Where-Object { $_.Result -eq 'win' }
function StopStats($set) {
    $t = ($set.Timeouts  | Measure-Object -Sum).Sum
    $c = ($set.Confidents | Measure-Object -Sum).Sum
    $m = ($set.Mates     | Measure-Object -Sum).Sum
    $tot = $t + $c + $m
    $tPct = if ($tot -gt 0) { [math]::Round($t / $tot * 100, 1) } else { 0 }
    return [PSCustomObject]@{ Timeouts = $t; Confidents = $c; Mates = $m; TimeoutPct = $tPct }
}
$winStops  = StopStats $winGames
$lossStops = StopStats $lossGames

# -- Game length ---------------------------------------------------------------
$avgWinLen  = if ($wins   -gt 0) { [math]::Round(($winGames.MoveCount  | Measure-Object -Average).Average, 1) } else { 'n/a' }
$avgLossLen = if ($losses -gt 0) { [math]::Round(($lossGames.MoveCount | Measure-Object -Average).Average, 1) } else { 'n/a' }

# -- Build breakdown -----------------------------------------------------------
$buildGroups = $games | Group-Object Build | Sort-Object Name

# -- Output --------------------------------------------------------------------
$serviceLabel = if ($service -eq 'all') { 'all services' } else { $service }
$buildLabel   = if ($build   -eq 'all') { 'all builds'   } else { "build $build" }
$dateRange    = "$($games[0].Date) -> $($games[-1].Date)"

Write-Host ""
Write-Host "========================================"
Write-Host "  GAME ANALYSIS -- Last $total games"
Write-Host "  Service: $serviceLabel   Build: $buildLabel"
Write-Host "  $dateRange"
Write-Host "========================================"

Write-Host ""
Write-Host "-- RESULTS ------------------------------"
Write-Host "  Total : $total"
Write-Host "  Win   : $wins  |  Loss : $losses  |  Draw : $draws"
Write-Host "  Win rate : $($winRate) pct"
Write-Host ""
Write-Host "  Recent trend (last $($recent.Count)):"
Write-Host ("    W:{0} L:{1} D:{2}  ({3} pct)" -f $rWins, $rLosses, $rDraws, $rWinRate)

if ($buildGroups.Count -gt 1) {
    Write-Host ""
    Write-Host "-- BY BUILD -----------------------------"
    foreach ($bg in $buildGroups) {
        $bw = @($bg.Group | Where-Object { $_.Result -eq 'win'  }).Count
        $bl = @($bg.Group | Where-Object { $_.Result -eq 'loss' }).Count
        $bd = @($bg.Group | Where-Object { $_.Result -eq 'draw' }).Count
        $bt = $bg.Group.Count
        $br = if ($bt -gt 0) { [math]::Round($bw / $bt * 100, 1) } else { 0 }
        Write-Host ("  Build {0,-4}  {1,4} games  W:{2} L:{3} D:{4}  ({5} pct)" -f $bg.Name, $bt, $bw, $bl, $bd, $br)
    }
}

Write-Host ""
Write-Host "-- OPENING BREAKDOWN (top loss openings) "
Write-Host "  (White's 1st & 2nd move)"
foreach ($og in $openingLosses) {
    $totalForOpening = ($openingTotal | Where-Object { $_.Name -eq $og.Name }).Count
    $lossRate = if ($totalForOpening -gt 0) { [math]::Round($og.Count / $totalForOpening * 100, 1) } else { 0 }
    Write-Host ("  {0,-18}  {1,2} losses / {2,3} games  ({3} pct loss rate)" -f $og.Name, $og.Count, $totalForOpening, $lossRate)
}

Write-Host ""
Write-Host "-- EVAL COLLAPSE (was +2.0, then lost) --"
Write-Host "  Collapses    : $($collapses.Count) of $losses losses (${collapseRate} pct)"
Write-Host "  Avg move     : $avgCollapse"
Write-Host "  Earliest     : move $minCollapse"

Write-Host ""
Write-Host "-- STOP REASONS -------------------------"
Write-Host "  (per move totals across all games in set)"
Write-Host ("  {0,-12} {1,8} {2,8} {3,8}" -f '', 'Wins', 'Losses', 'Draws')
$drawGames = $games | Where-Object { $_.Result -eq 'draw' }
$drawStops = StopStats $drawGames
Write-Host ("  {0,-12} {1,8} {2,8} {3,8}" -f 'confident',  $winStops.Confidents, $lossStops.Confidents, $drawStops.Confidents)
Write-Host ("  {0,-12} {1,8} {2,8} {3,8}" -f 'timeout',    $winStops.Timeouts,   $lossStops.Timeouts,   $drawStops.Timeouts)
Write-Host ("  {0,-12} {1,8} {2,8} {3,8}" -f 'mate_found', $winStops.Mates,      $lossStops.Mates,      $drawStops.Mates)
Write-Host ""
Write-Host ("  Timeout rate: Wins {0} pct  Losses {1} pct" -f $winStops.TimeoutPct, $lossStops.TimeoutPct)

Write-Host ""
Write-Host "-- GAME LENGTH --------------------------"
Write-Host "  Wins avg   : $avgWinLen moves"
Write-Host "  Losses avg : $avgLossLen moves"

Write-Host ""
Write-Host "-- PAWN ODDS SPLIT ----------------------"
$pawnOdds = $games | Where-Object { $_.PawnOdds }
$nonPawn  = $games | Where-Object { -not $_.PawnOdds }
if ($pawnOdds.Count -gt 0) {
    $pw = @($pawnOdds | Where-Object { $_.Result -eq 'win'  }).Count
    $pl = @($pawnOdds | Where-Object { $_.Result -eq 'loss' }).Count
    $pd = @($pawnOdds | Where-Object { $_.Result -eq 'draw' }).Count
    $pr = [math]::Round($pw / $pawnOdds.Count * 100, 1)
    Write-Host ("{0,-18} games  W:{1} L:{2} D:{3}  ({4} pct)" -f "  Pawn odds", $pw, $pl, $pd, $pr)
}
if ($nonPawn.Count -gt 0) {
    $nw = @($nonPawn | Where-Object { $_.Result -eq 'win'  }).Count
    $nl = @($nonPawn | Where-Object { $_.Result -eq 'loss' }).Count
    $nd = @($nonPawn | Where-Object { $_.Result -eq 'draw' }).Count
    $nr = [math]::Round($nw / $nonPawn.Count * 100, 1)
    Write-Host ("{0,-18} games  W:{1} L:{2} D:{3}  ({4} pct)" -f "  Equal pos", $nw, $nl, $nd, $nr)
}

Write-Host ""
Write-Host "========================================"
Write-Host ""

