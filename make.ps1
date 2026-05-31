#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Build, test, and run script for LichessBotRedux.

.DESCRIPTION
    Handles C++ engine builds (NNUE and HCE), smoke tests, NNUE training,
    and bot launch.

.PARAMETER Action
        What to do:
            build      - compile both engine targets (release)
            build-nnue - compile only redux-nnue.exe  (NNUE + HCE via runtime toggle)
            build-hce  - compile only redux-hce.exe   (compile-time HCE only)
            test       - smoke tests: perft, search, NPS, HCE/NNUE checks
            nnue-test  - Python NNUE unit tests
            train      - launch NNUE training from data\processed\mean-alltime-dedup.bin
            run        - start bot in NNUE mode
            run-hce    - start bot in HCE mode  (USE_NNUE=false)
            clean      - wipe build/, build_tmp/, __pycache__, and .pytest_cache (keeps bot\engine binaries)
            package    - assemble a release zip in releases/ (requires built binaries)
            all        - build + test + run  (default)

.EXAMPLE
    .\make.ps1              # build + test + run
    .\make.ps1 build        # compile both targets
    .\make.ps1 build-nnue   # rebuild only redux-nnue.exe
    .\make.ps1 build-hce    # rebuild only redux-hce.exe
    .\make.ps1 test         # smoke tests
    .\make.ps1 run          # start bot (NNUE)
    .\make.ps1 run-hce      # start bot (HCE)
    .\make.ps1 train        # NNUE training
    .\make.ps1 clean        # wipe CMake artifacts
    .\make.ps1 package      # create releases/lichess-bot-redux-build-N-win64.zip
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet('build', 'build-nnue', 'build-hce', 'test', 'nnue-test', 'train', 'run', 'run-hce', 'clean', 'package', 'all', 'ppm-start', 'ppm-stop', 'ppm-restart', 'ppm-logs', 'ppm-status', 'redeploy', 'redeploy-hce')]
    [string]$Action = 'all',
    # train: optional path to a checkpoint .pt file to resume from
    [string]$Resume = ''
)

if ((Get-ExecutionPolicy -Scope Process) -eq 'Restricted') {
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
}

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

#  Paths 
$BuildDir = Join-Path $Root 'build'
$EngineDir = Join-Path $Root 'bot\engine'
$NnueExe = Join-Path $EngineDir 'redux-nnue.exe'
$HceExe = Join-Path $EngineDir 'redux-hce.exe'
$NnBin = Join-Path $EngineDir 'nn.bin'
$BotDir = Join-Path $Root 'bot'
$Python = & { Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source } 2>$null
if (-not $Python) { $Python = 'python' }

#  Output helpers 
function Write-Step { param($msg) Write-Host "`n> $msg" -ForegroundColor Cyan }
function Write-Ok { param($msg) Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Warn { param($msg) Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

function Update-Version {
    $versionFile = Join-Path $BotDir 'version.json'
    $v = @{ build = 0; date = ''; commit = '' }
    if (Test-Path $versionFile) {
        try { $v = Get-Content $versionFile -Raw | ConvertFrom-Json } catch {}
    }
    $v.build = [int]$v.build + 1
    $v.date = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    try { $v.commit = & git rev-parse --short HEAD 2>$null } catch { $v.commit = '' }
    if (-not $v.commit) { $v.commit = '' }
    $v | ConvertTo-Json | ForEach-Object { $_ } | Set-Variable json
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($versionFile, $json, $enc)
    Write-Ok ("Build #{0}  ({1})" -f $v.build, $v.date)
    Archive-Build $v.build $v.date $v.commit
    Git-Tag $v.build $v.date $v.commit
}

function Archive-Build {
    param([int]$BuildNum, [string]$Date, [string]$Commit)
    $archiveRoot = Join-Path $Root 'archives'
    $dest = Join-Path $archiveRoot "build-$BuildNum"
    if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest | Out-Null }

    $copied = @()
    foreach ($src in @($NnueExe, $HceExe)) {
        if (Test-Path $src) {
            Copy-Item $src $dest -Force
            $copied += (Split-Path $src -Leaf)
        }
    }

    # Write a small manifest so we know what's in each slot
    $manifest = [ordered]@{
        build  = $BuildNum
        date   = $Date
        commit = $Commit
        files  = $copied
    }
    $manifest | ConvertTo-Json | ForEach-Object { $_ } | Set-Variable mJson
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $dest 'manifest.json'), $mJson, $enc)

    if ($copied.Count -gt 0) {
        Write-Ok ("Archived -> archives\build-$BuildNum\  [{0}]" -f ($copied -join ', '))
    }
    else {
        Write-Warn "Archive build-${BuildNum}: no binaries found to copy"
    }
}

function Git-Tag {
    param([int]$BuildNum, [string]$Date, [string]$Commit)
    try {
        $tag = "build-$BuildNum"
        # Stage version.json so the tag lands on a commit that includes it
        & git add (Join-Path $BotDir 'version.json') 2>$null
        $status = & git status --porcelain 2>$null
        if ($status) {
            & git commit -m "build $BuildNum  [$Commit]" --quiet 2>&1 | Out-Null
        }
        & git tag -a $tag -m "build $BuildNum  $Date  [$Commit]" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Git tag       : $tag  (run 'git push --tags' to push)"
        }
        else {
            Write-Warn "Git tag '$tag' already exists — skipped"
        }
    }
    catch {
        Write-Warn "Git tagging skipped: $_"
    }
}

# ==========================================================================
# BUILD helpers
# ==========================================================================
function Invoke-CmakeBuild {
    param([string]$Target = '')

    if (-not (Test-Path $BuildDir)) {
        New-Item -ItemType Directory -Path $BuildDir | Out-Null
    }
    Push-Location $BuildDir
    try {
        if (-not (Test-Path 'CMakeCache.txt')) {
            Write-Host "  Configuring CMake..."
            $ErrorActionPreference = 'Continue'
            cmake .. -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release 2>&1 |
            Where-Object { $_ -notmatch 'plugin needed to handle lto object' } | Out-Host
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }
        }
        $buildArgs = @('--build', '.', '--config', 'Release')
        if ($Target) { $buildArgs += @('--target', $Target) }
        $ErrorActionPreference = 'Continue'
        cmake @buildArgs 2>&1 |
        ForEach-Object { "$_" } |
        Where-Object { $_ -notmatch 'plugin needed to handle lto object|lto-wrapper|warning:|note:' } |
        Out-Host
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -ne 0) { throw "CMake build failed" }
    }
    finally {
        Pop-Location
    }
}

function _EnsureNnBin {
    if (-not (Test-Path $NnBin)) {
        $rootNn = Join-Path $Root 'nn.bin'
        if (Test-Path $rootNn) {
            Copy-Item $rootNn $NnBin
            Write-Ok "Copied nn.bin -> bot\engine\nn.bin"
        }
        else {
            Write-Warn "No nn.bin found - NNUE mode unavailable until nn.bin is placed in bot\engine\"
        }
    }
    else {
        Write-Ok "NNUE weights : bot\engine\nn.bin ($([math]::Round((Get-Item $NnBin).Length/1MB,0)) MB)"
    }
}

function _EnsureNpm {
    Write-Step "Checking Node.js dependencies"
    if (-not (Test-Path (Join-Path $BotDir 'node_modules'))) {
        Write-Host "  Installing npm packages..."
        Push-Location $BotDir
        npm.cmd install 2>&1 | Out-Host
        Pop-Location
    }
    Write-Ok "Node.js dependencies ready"
}

# ==========================================================================
# BUILD
# ==========================================================================
function Invoke-Build {
    Write-Step "Building both engine targets"
    Invoke-CmakeBuild
    if (-not (Test-Path $NnueExe)) { throw "redux-nnue.exe not found after build" }
    Write-Ok "NNUE engine : bot\engine\redux-nnue.exe"
    if (-not (Test-Path $HceExe)) { throw "redux-hce.exe not found after build" }
    Write-Ok "HCE  engine : bot\engine\redux-hce.exe"
    _EnsureNnBin
    _EnsureNpm
    Update-Version
}

function Invoke-BuildNnue {
    Write-Step "Building NNUE engine (redux-nnue.exe)"
    Invoke-CmakeBuild -Target 'redux-nnue'
    if (-not (Test-Path $NnueExe)) { throw "redux-nnue.exe not found after build" }
    Write-Ok "NNUE engine : bot\engine\redux-nnue.exe"
    _EnsureNnBin
    Update-Version
}

function Invoke-BuildHce {
    Write-Step "Building HCE engine (redux-hce.exe)"
    Invoke-CmakeBuild -Target 'redux-hce'
    if (-not (Test-Path $HceExe)) { throw "redux-hce.exe not found after build" }
    Write-Ok "HCE  engine : bot\engine\redux-hce.exe"
    Update-Version
}

# ==========================================================================
# TEST
# ==========================================================================
function _RunProcess {
    param([string]$Exe, [string[]]$Commands, [int]$TimeoutSec = 8)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $psi.RedirectStandardOutput = $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    foreach ($cmd in $Commands) { $p.StandardInput.WriteLine($cmd) }
    $lines = [System.Collections.Generic.List[string]]::new()
    $deadline = [DateTime]::Now.AddSeconds($TimeoutSec)
    while ([DateTime]::Now -lt $deadline) {
        $line = $p.StandardOutput.ReadLine()
        if ($null -eq $line) { break }
        $lines.Add($line)
        if ($line -match '^bestmove') { break }
    }
    $p.StandardInput.WriteLine("quit")
    $p | Wait-Process -Timeout 3 -ErrorAction SilentlyContinue
    if (-not $p.HasExited) { $p.Kill() }
    return , $lines.ToArray()
}

function Invoke-Test {
    Write-Step "Running engine smoke tests"
    if (-not (Test-Path $NnueExe)) { throw "redux-nnue.exe not found - run '.\make.ps1 build' first" }
    $failed = 0

    # 1: NNUE auto-discovery
    Write-Host "  [1/5] NNUE auto-discovery..."
    $uciOut = "uci`nisready`nquit`n" | & $NnueExe 2>&1
    $nnueLines = $uciOut | Select-String "NNUE loaded"
    if ($nnueLines.Count -eq 1) {
        Write-Ok "NNUE loads once: $($nnueLines[0].Line.Trim())"
    }
    elseif ($nnueLines.Count -gt 1) {
        Write-Fail "NNUE loaded $($nnueLines.Count) times (expected 1)"; $failed++
    }
    else {
        if (Test-Path $NnBin) { Write-Fail "NNUE not loaded despite nn.bin present"; $failed++ }
        else { Write-Warn "No nn.bin present - runtime HCE mode OK" }
    }

    # 2: Perft 5
    Write-Host "  [2/5] Perft 5 (startpos)..."
    $perftOut = "perft 5`nquit`n" | & $NnueExe 2>&1
    $nl = $perftOut | Select-String "Nodes:\s*(\d+)"
    if ($nl) {
        $nodes = [int64]($nl.Matches[0].Groups[1].Value)
        if ($nodes -eq 4865609) { Write-Ok "Perft 5 = 4,865,609 (correct)" }
        else { Write-Fail "Perft 5 = $nodes (expected 4,865,609)"; $failed++ }
    }
    else { Write-Fail "Could not parse perft output"; $failed++ }

    # 3: Search produces bestmove
    Write-Host "  [3/5] NNUE search produces bestmove..."
    $lines = _RunProcess $NnueExe @("uci", "setoption name Threads value 1", "setoption name Hash value 64", "ucinewgame", "isready", "position startpos", "go movetime 500")
    $bm = ($lines | Select-String '^bestmove\s+(\S+)' | Select-Object -First 1)
    if ($bm) { Write-Ok "bestmove = $($bm.Matches[0].Groups[1].Value)" }
    else { Write-Fail "No bestmove from NNUE search"; $failed++ }

    # 4: NPS sanity (informational only -- varies with machine load)
    Write-Host "  [4/6] NPS sanity (informational)..."
    $lines4 = _RunProcess $NnueExe @("uci", "setoption name Threads value 1", "setoption name Hash value 128", "ucinewgame", "isready", "position fen r2qk2r/pp1nbppp/2p1pn2/3p2B1/3P4/2N1PN2/PP3PPP/R2QKB1R w KQkq - 0 1", "go movetime 2000") -TimeoutSec 12
    $npsLine = $lines4 | Select-String 'nps\s+(\d+)' | Select-Object -Last 1
    if ($npsLine) {
        $nps = [int64]($npsLine.Matches[0].Groups[1].Value)
        if ($nps -ge 50000) { Write-Ok ("NPS = {0:N0}" -f $nps) }
        else { Write-Warn ("NPS = {0:N0} (suspiciously low -- engine may be throttled)" -f $nps) }
    }
    else { Write-Warn "Could not parse NPS from info lines" }

    # 5: HCE build  bestmove, no NNUE loading
    Write-Host "  [5/6] HCE build: bestmove (NNUE must not load)..."
    if (-not (Test-Path $HceExe)) { Write-Fail "redux-hce.exe not found"; $failed++ }
    else {
        $lines5 = _RunProcess $HceExe @("uci", "setoption name Threads value 1", "setoption name Hash value 64", "ucinewgame", "isready", "position startpos", "go movetime 500")
        $hceBm = $lines5 | Select-String '^bestmove\s+(\S+)' | Select-Object -First 1
        $hceNnue = $lines5 | Select-String 'NNUE loaded'
        if (-not $hceBm) { Write-Fail "HCE returned no bestmove"; $failed++ }
        elseif ($hceNnue) { Write-Fail "HCE loaded NNUE (DISABLE_NNUE not effective)"; $failed++ }
        else { Write-Ok "HCE bestmove = $($hceBm.Matches[0].Groups[1].Value) (NNUE correctly absent)" }
    }

    # 6: evaluate() / evalvec consistency -- catches the two eval functions drifting apart
    Write-Host "  [6/6] HCE evaluate()/evalvec consistency..."
    if (-not (Test-Path $HceExe)) { Write-Fail "redux-hce.exe not found for consistency test"; $failed++ }
    else {
        # Three positions spanning opening, middlegame, endgame (all White to move)
        $testFens = @(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "r1bqk2r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4",
            "8/3P4/8/3K4/8/8/8/3k4 w - - 0 1"
        )
        $consFailed = 0
        foreach ($fen in $testFens) {
            $cmds6 = @("uci", "isready", "position fen $fen", "evalvec", "eval", "quit")
            $out6 = _RunProcess $HceExe $cmds6 -TimeoutSec 6

            # evalvec outputs: evalvec {"total":N,"stm":N,...}
            $vecLine = $out6 | Select-String '^evalvec\s+(\{.*\})' | Select-Object -First 1
            # eval outputs: "Final score (side-to-move perspective): N cp"
            $evalLine = $out6 | Select-String 'Final score.*?:\s*(-?\d+)' | Select-Object -First 1

            if (-not $vecLine -or -not $evalLine) {
                Write-Fail "Could not parse output for FEN: $fen"; $consFailed++; continue
            }
            try {
                $vecJson = $vecLine.Matches[0].Groups[1].Value | ConvertFrom-Json
                $vecStm = [int]$vecJson.stm
                $evalScore = [int]$evalLine.Matches[0].Groups[1].Value
                $diff = [Math]::Abs($vecStm - $evalScore)
                if ($diff -le 2) {
                    $fenPreview = if ($fen.Length -gt 40) { $fen.Substring(0, 40) + '...' } else { $fen }
                    Write-Ok "Consistent (stm=$evalScore evalvec.stm=$vecStm diff=$diff) -- $fenPreview"
                }
                else {
                    Write-Fail "evaluate()/evalvec MISMATCH: eval=$evalScore evalvec.stm=$vecStm (diff=$diff cp)"; $consFailed++
                }
            }
            catch {
                Write-Fail "Parse error for FEN: $fen -- $_"; $consFailed++
            }
        }
        $failed += $consFailed
    }

    Write-Host ""
    if ($failed -gt 0) { throw "$failed smoke test(s) FAILED" }
    Write-Ok "All smoke tests passed"
}

# ==========================================================================
# RUN
# ==========================================================================
function Invoke-Run {
    param([switch]$HceMode)
    $label = if ($HceMode) { "HCE" } else { "NNUE" }
    Write-Step "Starting bot in $label mode"
    if (-not (Test-Path $NnueExe)) { throw "redux-nnue.exe not found - run '.\make.ps1 build' first" }
    $envFile = Join-Path $BotDir '.env'
    if (-not (Test-Path $envFile)) { throw "bot\.env not found - copy bot\.env.example and set LICHESS_TOKEN" }

    if ($HceMode) { $env:USE_NNUE = 'false' }
    else { Remove-Item Env:\USE_NNUE -ErrorAction SilentlyContinue }

    Write-Host ""
    Write-Host "  ========================================"
    Write-Host "  LichessBotRedux  [$label mode]"
    Write-Host "  Dashboard: http://localhost:3000"
    Write-Host "  Press Ctrl+C to stop"
    Write-Host "  ========================================"
    Write-Host ""
    Push-Location $BotDir
    try { node index.js } finally { Pop-Location }
}

# ==========================================================================
# NNUE UNIT TESTS
# ==========================================================================
function Invoke-NnueTest {
    Write-Step "Running Python NNUE unit tests"
    & $Python -m pytest ml/test_nnue.py -v
    if ($LASTEXITCODE -ne 0) { throw "NNUE unit tests FAILED" }
    Write-Ok "All NNUE unit tests passed"
}

# ==========================================================================
# TRAIN
# ==========================================================================
function Invoke-Train {
    Write-Step "Launching NNUE training via nn-dojo"
    $DojoRoot = Resolve-Path (Join-Path $Root '..\ppm-projects\nn-dojo')
    $DojoConfig = Join-Path $DojoRoot 'configs\nnue_v9.yaml'
    if (-not (Test-Path $DojoConfig)) {
        throw "nn-dojo config not found: $DojoConfig"
    }
    Write-Host "  Dojo   : $DojoRoot"
    Write-Host "  Config : configs\nnue_v9.yaml"
    Write-Host "  Dashboard: http://localhost:7200"
    Push-Location $DojoRoot
    try {
        & $Python -m dojo.train configs/nnue_v9.yaml `
        $(if ($Resume) { "--resume"; $Resume })
    }
    finally {
        Pop-Location
    }
}

# ==========================================================================
# PACKAGE
# ==========================================================================
function Invoke-Package {
    Write-Step "Assembling release package"

    # -- pre-flight checks --
    if (-not (Test-Path $NnueExe)) { throw "redux-nnue.exe not found -- run '.\make.ps1 build' first" }
    if (-not (Test-Path $HceExe)) { throw "redux-hce.exe not found  -- run '.\make.ps1 build' first" }
    if (-not (Test-Path $NnBin)) { Write-Warn "nn.bin not found -- package will be HCE-only" }

    # -- version info --
    $versionFile = Join-Path $BotDir 'version.json'
    $v = @{ build = 0 }
    if (Test-Path $versionFile) { try { $v = Get-Content $versionFile -Raw | ConvertFrom-Json } catch {} }
    $buildNum = [int]$v.build
    $stageName = "lichess-bot-redux-build-$buildNum-win64"

    $releasesDir = Join-Path $Root 'releases'
    $stageDir = Join-Path $releasesDir $stageName
    $zipPath = Join-Path $releasesDir "$stageName.zip"

    if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
    New-Item -ItemType Directory -Path $stageDir | Out-Null
    Write-Ok "Staging to releases\$stageName\"

    # -- copy bot/ contents, excluding dev-only items --
    $exclude = @('node_modules', '.env', 'challenger.db', 'challenger.db-shm', 'challenger.db-wal', 'book-cache.json')
    Get-ChildItem $BotDir | Where-Object { $_.Name -notin $exclude } | ForEach-Object {
        if ($_.PSIsContainer) {
            Copy-Item $_.FullName (Join-Path $stageDir $_.Name) -Recurse -Force
        }
        else {
            Copy-Item $_.FullName $stageDir -Force
        }
    }
    Write-Ok "Copied bot/ contents"

    # -- install prod-only node_modules --
    Write-Host "  Installing prod dependencies (npm ci --omit=dev)..."
    Push-Location $stageDir
    try {
        npm.cmd ci --omit=dev --silent 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
    }
    finally { Pop-Location }
    Write-Ok "node_modules (prod only) installed"

    # -- write SETUP.txt --
    $setup = @"
LichessBotRedux -- Build $buildNum
==========================================

SETUP:
  1. Copy .env.example to .env
  2. Set LICHESS_TOKEN in .env  (get one at https://lichess.org/account/oauth/token)
  3. Run start.bat  (or start-hce.bat for HCE-only mode)

REQUIREMENTS:
  - Node.js 18 or later  (https://nodejs.org)
  - Windows x64

DASHBOARD:
  http://localhost:3000  (opens automatically after bot starts)
"@
    [IO.File]::WriteAllText((Join-Path $stageDir 'SETUP.txt'), $setup)
    Write-Ok "SETUP.txt written"

    # -- compress --
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Write-Host "  Compressing..."
    Compress-Archive -Path $stageDir -DestinationPath $zipPath
    Remove-Item -Recurse -Force $stageDir

    $sizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Ok "Package: releases\$stageName.zip  ($sizeMb MB)"
    Write-Host ""
    Write-Host "  Distribute the zip. End-user steps: unzip, edit .env, run start.bat"
}

# ==========================================================================
# CLEAN
# ==========================================================================
function Invoke-Clean {
    Write-Step "Cleaning build artifacts"
    if (Test-Path $BuildDir) {
        Remove-Item -Recurse -Force $BuildDir
        Write-Ok "Removed build/"
    }
    else {
        Write-Warn "build/ already clean"
    }
    $buildTmpDir = Join-Path $Root 'build_tmp'
    if (Test-Path $buildTmpDir) {
        Remove-Item -Recurse -Force $buildTmpDir
        Write-Ok "Removed build_tmp/"
    }
    $pytestCache = Join-Path $Root '.pytest_cache'
    if (Test-Path $pytestCache) {
        Remove-Item -Recurse -Force $pytestCache
        Write-Ok "Removed .pytest_cache/"
    }
    Get-ChildItem $Root -Recurse -Filter '__pycache__' -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\node_modules\\' } |
    ForEach-Object { Remove-Item -Recurse -Force $_.FullName; Write-Ok "Removed $($_.FullName)" }
    Write-Ok "Clean complete (bot\engine\ binaries and nn.bin preserved)"
}

# ==========================================================================
# PPM (PersonalProjectManager) — PM2-managed infra
# ==========================================================================
$PpmDir = Join-Path $Root 'PersonalProjectManager'

function Invoke-PpmStart {
    Write-Step "Starting PPM services (controller + agent) via PM2"
    Push-Location $PpmDir
    try { npx pm2 start ecosystem.config.js }
    finally { Pop-Location }
    Write-Ok "PPM running. Dashboard → http://localhost:7000/dashboard"
}

function Invoke-PpmStop {
    Write-Step "Stopping PPM services"
    Push-Location $PpmDir
    try { npx pm2 stop ecosystem.config.js }
    finally { Pop-Location }
}

function Invoke-PpmRestart {
    Write-Step "Restarting PPM services"
    Push-Location $PpmDir
    try { npx pm2 restart ecosystem.config.js }
    finally { Pop-Location }
}

function Invoke-PpmLogs {
    Push-Location $PpmDir
    try { npx pm2 logs }
    finally { Pop-Location }
}

function Invoke-PpmStatus {
    npx --prefix $PpmDir pm2 status
}

# Build then restart the agent so PPM auto-redeploys the bot with the new exe.
function Invoke-Redeploy {
    param([switch]$HceMode)
    if ($HceMode) { Invoke-BuildHce } else { Invoke-Build }
    Write-Step "Restarting PPM agent to pick up new binary"
    npx --prefix $PpmDir pm2 restart agent
    Write-Ok "Agent restarted — bot will redeploy with updated exe"
}

# ==========================================================================
# Dispatch
# ==========================================================================
switch ($Action) {
    'build' { Invoke-Build }
    'build-nnue' { Invoke-BuildNnue }
    'build-hce' { Invoke-BuildHce }
    'test' { Invoke-Test }
    'nnue-test' { Invoke-NnueTest }
    'train' { Invoke-Train }
    'run' { Invoke-Run }
    'run-hce' { Invoke-Run -HceMode }
    'clean' { Invoke-Clean }
    'package' { Invoke-Package }
    'all' { Invoke-Build; Invoke-Test; Invoke-Run }
    'ppm-start' { Invoke-PpmStart }
    'ppm-stop' { Invoke-PpmStop }
    'ppm-restart' { Invoke-PpmRestart }
    'ppm-logs' { Invoke-PpmLogs }
    'ppm-status' { Invoke-PpmStatus }
    'redeploy' { Invoke-Redeploy }
    'redeploy-hce' { Invoke-Redeploy -HceMode }
}
