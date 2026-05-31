/**
 * tab-engine-pane.js — Compact engine analysis pane for the Game tab.
 *
 * Renders into #comms-pane-eng (5th comms sub-tab, next to Chat/Seek/Log/Ctrl).
 *
 * DATA MODEL
 * ──────────
 *   _cols[col]  — one entry per absolute half-move (ply) in the game.
 *     .pred     — Map<depth, { uci, san }>   engine predictions (the "staircase")
 *     .actual   — { uci, san, cp, mate, evalColor, depth, frozen, nodes, nps, time, seldepth }
 *                 The move actually played (or live best-move during search).
 *                 frozen=true once committed; until then it is a tentative "thinking" cell.
 *     .wrongD   — highest depth at which the prediction differed from the actual move
 *     .lostD    — wrongD − D_recovery (how many depth rows are "wasted" by the wrong guess)
 *
 *   _rowStats[depth] — per-depth eval/nodes/seldepth snapshot for the frozen right-hand columns.
 *
 * The column is the primary key.  maxDepth(col) walks col.pred — no global cache.
 * Wiping stale future data is `for col >= N: delete _cols[col]`.
 */
'use strict';

const TabEnginePane = (() => {

  /* ══════════════════════════════════════════════════════════════════
   *  Constants & utilities
   * ══════════════════════════════════════════════════════════════════ */

  const UCI_RE        = /^[a-h][1-8][a-h][1-8][qrbnQRBN]?$/;
  const PLY_W         = 44;
  const PLY_LOOKAHEAD = 10;
  const EP_D_W        = 24;
  const EP_SEL_W      = 28;

  const _DEBUG = true;
  function _dbg(msg, ...rest) {
    if (!_DEBUG) return;
    const ts = performance.now().toFixed(1).padStart(9);
    const extra = rest.length
      ? ' ' + rest.map(x => typeof x === 'object' && x !== null
          ? JSON.stringify(x).slice(0, 120) : String(x)).join(' ')
      : '';
    console.log(`%c[ep ${ts}ms]%c ${msg}${extra}`, 'color:#6af;font-weight:600', 'color:#adf');
  }
  function _warn(msg, ...rest) {
    const ts = performance.now().toFixed(1).padStart(9);
    const extra = rest.length
      ? ' ' + rest.map(x => typeof x === 'object' && x !== null
          ? JSON.stringify(x).slice(0, 120) : String(x)).join(' ')
      : '';
    console.warn(`%c[ep ${ts}ms]%c ${msg}${extra}`, 'color:#fa4;font-weight:600', 'color:#fd9');
  }

  const _toGlyph = san => {
    if (!san) return san;
    const G = { K:'♔', Q:'♕', R:'♖', B:'♗', N:'♘' };
    return san.replace(/^([KQRBN])/, (_, p) => G[p] || p);
  };

  function _describeMove(san) {
    if (!san) return '';
    if (san === 'O-O')   return 'Kingside castle';
    if (san === 'O-O-O') return 'Queenside castle';
    const P = { K:'King', Q:'Queen', R:'Rook', B:'Bishop', N:'Knight' };
    const suffix = san.endsWith('#') ? ' \u2014 Checkmate'
                 : san.endsWith('+') ? ' \u2014 Check' : '';
    let s = san.replace(/[+#]$/, '');
    let promo = '';
    const pm = s.match(/=([QRBN])$/);
    if (pm) { promo = `, promotes to ${P[pm[1]]}`; s = s.slice(0, s.lastIndexOf('=')); }
    const pc = P[s[0]] ? s[0] : null;
    const name = pc ? P[pc] : 'Pawn';
    let rest = pc ? s.slice(1) : s;
    const cap = rest.includes('x');
    rest = rest.replace('x', '');
    return `${name} ${cap ? 'takes' : 'to'} ${rest.slice(-2)}${promo}${suffix}`;
  }

  function _fmtMs(ms) {
    if (!ms) return null;
    if (ms >= 60000) return (ms / 60000).toFixed(1) + 'm';
    if (ms >=  1000) return (ms / 1000).toFixed(1) + 's';
    return ms + 'ms';
  }

  function _fmtEval(cp) {
    const v = cp / 100;
    return (v >= 0 ? '+' : '') + v.toFixed(2);
  }

  function _fmtEvalTip(cp, mate) {
    if (mate != null) return mate > 0 ? `Mate in ${mate}` : `Mated in ${Math.abs(mate)}`;
    if (cp == null) return null;
    const v = cp / 100, sign = v >= 0 ? '+' : '', abs = Math.abs(cp);
    const label = abs <  10 ? 'Equal'
                : abs <  50 ? (cp > 0 ? 'Slight edge'   : 'Slight deficit')
                : abs < 150 ? (cp > 0 ? 'Edge'          : 'Deficit')
                : abs < 300 ? (cp > 0 ? 'Advantage'     : 'Disadvantage')
                : abs < 600 ? (cp > 0 ? 'Winning'       : 'Losing')
                :             (cp > 0 ? 'Decisive'      : 'Lost');
    return `${sign}${v.toFixed(2)} (${label})`;
  }

  function _evalColor(cp, mate) {
    if (mate != null) return mate > 0 ? 'hsl(142,60%,48%)' : 'hsl(0,65%,58%)';
    if (cp == null)   return 'var(--muted)';
    const t = Math.min(1, Math.abs(cp) / 200);
    if (cp >= 0) {
      return `hsl(142,${Math.round(t * 55)}%,${Math.round(62 - t * 18)}%)`;
    } else {
      return `hsl(0,${Math.round(t * 62)}%,${Math.round(62 - t * 10)}%)`;
    }
  }

  /** Tiered eval cell styling for actual-move cells. */
  function _evalCellStyle(cp, mate, frozen, rightward = false) {
    if (!frozen && !rightward) return { style: '', cls: '' };
    if (mate != null) {
      const delay = (Math.random() * 1.8).toFixed(2);
      return { style: `animation-delay:-${delay}s;`, cls: mate > 0 ? 'ep-eval-mate-win' : 'ep-eval-mate-lose' };
    }
    if (cp == null) {
      return rightward
        ? { style: 'box-shadow:inset 2px 0 0 0 rgba(120,120,140,0.35);', cls: '' }
        : { style: '', cls: '' };
    }
    const sign = cp >= 0 ? 1 : -1, absCp = Math.abs(cp), h = sign > 0 ? 142 : 0;
    if (absCp < 30) {
      const base = 'background:rgba(160,160,160,0.07);';
      return { style: rightward ? base + 'box-shadow:inset 2px 0 0 0 rgba(120,120,140,0.35);' : base, cls: '' };
    }
    let tier, bgA, sat, lit, glowA, spread, whiteText, border;
    if      (absCp < 80)  { tier=1; bgA=0.07; sat=35; lit=50; glowA=0;    spread=0;  whiteText=false; border=false; }
    else if (absCp < 180) { tier=2; bgA=0.13; sat=52; lit=42; glowA=0;    spread=0;  whiteText=false; border=true;  }
    else if (absCp < 350) { tier=3; bgA=0.22; sat=62; lit=36; glowA=0.12; spread=8;  whiteText=false; border=true;  }
    else if (absCp < 600) { tier=4; bgA=0.38; sat=72; lit=28; glowA=0.22; spread=14; whiteText=true;  border=true;  }
    else                  { tier=5; bgA=0.55; sat=80; lit=22; glowA=0.35; spread=20; whiteText=true;  border=true;  }
    const sc = frozen ? 1.0 : 0.72;
    const s1 = `hsla(${h},${sat}%,${lit+10}%,${(bgA*sc).toFixed(2)})`;
    const s2 = `hsla(${h},${sat}%,${lit}%,${((bgA+0.10)*sc).toFixed(2)})`;
    const dir = rightward ? 'to right' : '135deg';
    let style = `background:linear-gradient(${dir},${s1} 0%,${s2} 100%);`;
    if (whiteText) style += 'color:rgba(255,255,255,0.92);';
    const shadows = [];
    if (rightward) {
      const bA = border ? Math.min(0.9, 0.25 + tier * 0.13) : 0.45;
      shadows.push(`inset 2px 0 0 0 hsla(${h},70%,55%,${bA.toFixed(2)})`);
    } else if (border) {
      const bA = Math.min(0.9, 0.25 + tier * 0.13);
      shadows.push(`inset 0 2px 0 0 hsla(${h},70%,55%,${bA.toFixed(2)})`);
    }
    if (glowA > 0) {
      const gA = frozen ? glowA : glowA * 0.8;
      shadows.push(`0 0 ${Math.round(spread/2)}px hsla(${h},85%,55%,${gA.toFixed(2)})`);
      shadows.push(`0 0 ${spread}px hsla(${h},75%,45%,${(gA*0.55).toFixed(2)})`);
    }
    if (shadows.length) style += `box-shadow:${shadows.join(',')};`;
    return { style, cls: '' };
  }

  /* ══════════════════════════════════════════════════════════════════
   *  State
   * ══════════════════════════════════════════════════════════════════ */

  let _rendered    = false;
  let _gameId      = null;   // game currently shown (visual)
  let _dataGameId  = null;   // game that _cols data belongs to
  let _lastGame    = null;   // snapshot for end-of-game refills
  let _numPlyCols  = 0;      // DOM column count
  let _followMode  = true;   // auto-center current ply horizontally
  let _programmaticScroll = false; // suppress user-scroll detection during our own scrolls

  /** @type {Object<number, { pred: Map<number,{uci:string,san:string}>, actual: object|null, wrongD: number|null, lostD: number|null, pendingWrongD: number|null }>} */
  let _cols = {};

  /** @type {Object<number, { evalStr:string, evalColor:string, cp:number|null, mate:number|null, nodesStr:string, seldepth:*, isPonder:boolean, nodes:number, nps:number, time:number }>} */
  let _rowStats = {};

  let _rafId = null;

  /* ── Column helpers ─────────────────────────────────────────────── */

  function _col(c) {
    if (!_cols[c]) _cols[c] = { pred: new Map(), actual: null, wrongD: null, lostD: null, pendingWrongD: null };
    return _cols[c];
  }

  /** Highest depth with a prediction for this column, or -1. */
  function _maxDepth(c) {
    const col = _cols[c];
    if (!col || col.pred.size === 0) return -1;
    let mx = -1;
    for (const d of col.pred.keys()) if (d > mx) mx = d;
    return mx;
  }

  /** All depths present across all columns + rowStats. */
  function _allDepths() {
    const ds = new Set();
    for (const c of Object.values(_cols))
      for (const d of c.pred.keys()) ds.add(d);
    for (const d of Object.keys(_rowStats)) ds.add(+d);
    return ds;
  }

  /** Erase prediction and tentative data for cols >= startCol. */
  function _wipeFuture(startCol) {
    let wiped = 0;
    for (const c of Object.keys(_cols)) {
      if (+c >= startCol) {
        const col = _cols[+c];
        wiped += col.pred.size;
        col.pred.clear();
        if (col.actual && !col.actual.frozen) col.actual = null;
      }
    }
    if (wiped) _dbg(`wipeFuture(>=${startCol}) cleared ${wiped} predictions`);
  }

  /** Wipe ALL data — fresh game. */
  function _wipeAll() {
    _cols     = {};
    _rowStats = {};
  }

  /** True if absolute ply `col` is a move played by our bot. */
  function _isOurs(g, col) {
    return g.color === 'white' ? (col % 2 === 0) : (col % 2 !== 0);
  }

  /** Min columns needed for layout. */
  function _neededCols(gPly) {
    let mx = gPly + PLY_LOOKAHEAD - 1;
    for (const c of Object.keys(_cols)) {
      const ci = +c;
      if (_cols[ci].pred.size > 0 && ci > mx) mx = ci;
    }
    return mx + 1;
  }

  const _pane = () => document.getElementById('comms-pane-eng');

  /* ══════════════════════════════════════════════════════════════════
   *  Ingestion — the only place that writes _cols and _rowStats
   * ══════════════════════════════════════════════════════════════════ */

  /**
   * Ingest one search_info line into the column store.
   *
   * Handles all three search modes:
   *   - Normal search:    pvOffset = gPly,     isPonder = false
   *   - Parent ponder:    pvOffset = gPly,     isPonder = true  (pv[0] = opponent move, eval negated)
   *   - Ponderhit ponder: pvOffset = gPly + 1, isPonder = false (pv[0] = our reply; known opponent move at gPly)
   */
  function _ingest(info) {
    const g = App.currentGame();
    if (!g) return;

    const depth = info.depth ?? 0;
    const gPly  = g.fullMoves?.length ?? 0;

    // ── Eval (negate for parent-position ponder) ──────────────────────
    const isParentPonder = info.ponder === 'opponent';
    const isPonderHit    = info.ponder === 'ours';
    const rawCp   = info.eval_cp ?? null;
    const dispCp  = rawCp  != null ? (isParentPonder ? -rawCp  : rawCp)  : null;
    const rawMate = (info.mate != null && info.mate !== 0) ? info.mate : null;
    const dispMate= rawMate!= null ? (isParentPonder ? -rawMate: rawMate): null;
    const evalStr = dispMate != null
      ? (dispMate > 0 ? `+M${dispMate}` : `-M${Math.abs(dispMate)}`)
      : dispCp != null ? _fmtEval(dispCp) : '\u2013';
    const evColor = _evalColor(dispCp, dispMate);

    // ── Parse PV ──────────────────────────────────────────────────────
    const pvArr   = Array.isArray(info.pv) ? info.pv
                  : typeof info.pv === 'string' ? info.pv.split(' ').filter(Boolean)
                  : info.pv0 ? info.pv0.split(' ').filter(Boolean) : [];
    const pvClean = pvArr.filter(m => UCI_RE.test(m));

    // Column offset: ponderhit starts one ply ahead (opponent move implicit).
    const pvOffset = isPonderHit ? gPly + 1 : gPly;

    // ── Compute SAN ───────────────────────────────────────────────────
    const ponderUci = isPonderHit ? (g._ponderMoveUci ?? null) : null;
    let ponderSan   = null;
    let sanList     = [];
    if (typeof San !== 'undefined' && pvClean.length > 0) {
      if (isPonderHit && ponderUci) {
        const prefixed = San.buildSanList(g.fen, [ponderUci, ...pvClean]);
        ponderSan = prefixed[0] ?? null;
        sanList   = prefixed.slice(1);
      } else {
        sanList = San.buildSanList(g.fen, pvClean);
      }
    }

    _dbg(`ingest d=${depth} gPly=${gPly} ponder=${info.ponder ?? 'none'} off=${pvOffset} pv=[${pvClean.join(',')}]`);

    // ── Write predictions into columns ────────────────────────────────
    pvClean.forEach((uci, i) => {
      _col(pvOffset + i).pred.set(depth, { uci, san: sanList[i] ?? null });
    });

    // Ponderhit: fill the known opponent move at col gPly.
    if (isPonderHit && ponderUci) {
      _col(gPly).pred.set(depth, { uci: ponderUci, san: ponderSan });
    }

    // ── Backfill skipped depths ───────────────────────────────────────
    if (pvClean.length > 0) {
      for (let d = 1; d < depth; d++) {
        // Fill the first `d` moves of PV into empty slots at depth d.
        for (let i = 0; i < d && i < pvClean.length; i++) {
          const c = _col(pvOffset + i);
          if (!c.pred.has(d)) c.pred.set(d, { uci: pvClean[i], san: sanList[i] ?? null });
        }
        // Also backfill the ponderhit opponent move.
        if (isPonderHit && ponderUci) {
          const c = _col(gPly);
          if (!c.pred.has(d)) c.pred.set(d, { uci: ponderUci, san: ponderSan });
        }
      }
    }

    // ── Live tentative ("actual") cells ───────────────────────────────
    // Ponderhit: col gPly gets the known opponent move with search eval.
    if (isPonderHit && ponderUci) {
      const cOpp = _col(gPly);
      if (!cOpp.actual?.frozen) {
        cOpp.actual = {
          uci: ponderUci, san: ponderSan, evalColor: evColor,
          cp: dispCp, mate: dispMate, depth, frozen: false,
          nodes: info.nodes ?? 0, nps: info.nps ?? 0,
          time: info.time ?? 0, seldepth: info.seldepth ?? null,
        };
      }
    }

    // Our current thinking column — only for non-ponderhit searches.
    // During ponderhit the live cell belongs to the opponent's column (gPly), already written above.
    const thinkCol = gPly;
    if (!isPonderHit && pvClean.length > 0) {
      const cThink = _col(thinkCol);
      if (!cThink.actual?.frozen) {
        const existing = cThink.actual;
        if (!existing || depth >= (existing.depth ?? 0)) {
          cThink.actual = {
            uci: pvClean[0], san: sanList[0] ?? null, evalColor: evColor,
            cp: dispCp, mate: dispMate, depth, frozen: false,
            nodes: info.nodes ?? 0, nps: info.nps ?? 0,
            time: info.time ?? 0, seldepth: info.seldepth ?? null,
          };
        }
      }
    }

    // ── Row stats ─────────────────────────────────────────────────────
    _rowStats[depth] = {
      evalStr, evalColor: evColor, cp: dispCp, mate: dispMate,
      nodesStr: App.fmtN(info.nodes ?? 0), seldepth: info.seldepth ?? '\u2013',
      isPonder: isParentPonder,
      nodes: info.nodes ?? 0, nps: info.nps ?? 0, time: info.time ?? 0,
    };
  }

  /**
   * Commit a move to its column — called on 'move' and 'opponent_move'.
   * Freezes the actual cell and initiates depth-loss tracking.
   */
  function _commitMove(g, type, gPly, stopReason) {
    const newCol = gPly - 1;
    if (newCol < 0) return;

    // ── Helper: best eval from _rowStats ──────────────────────────────
    const pickEval = (allowPonder) => {
      const ds = Object.keys(_rowStats).map(Number)
        .filter(d => allowPonder || !_rowStats[d]?.isPonder);
      if (!ds.length) return {};
      const st = _rowStats[Math.max(...ds)];
      const ec = st?.evalColor;
      return {
        evalColor: (ec && !ec.startsWith('var(')) ? ec : null,
        cp: st?.cp ?? null, mate: st?.mate ?? null,
      };
    };
    const pickStats = () => {
      const ds = Object.keys(_rowStats).map(Number);
      if (!ds.length) return {};
      const st = _rowStats[Math.max(...ds)];
      return { nodes: st?.nodes ?? 0, nps: st?.nps ?? 0,
               time: st?.time ?? 0, seldepth: st?.seldepth ?? null,
               depth: Math.max(...ds) };
    };

    const c = _col(newCol);
    if (type === 'move') {
      const { evalColor, cp, mate } = pickEval(false);
      if (c.actual && !c.actual.frozen) {
        c.actual = { ...c.actual,
          evalColor: evalColor ?? c.actual.evalColor,
          cp: cp ?? c.actual.cp, mate: mate ?? c.actual.mate,
          frozen: true, stopReason: stopReason ?? null };
      } else if (!c.actual) {
        c.actual = { uci: g.fullMoves[newCol] ?? null, san: null,
          evalColor, cp, mate, frozen: true, stopReason: stopReason ?? null, ...pickStats() };
      }
    } else {
      // opponent_move
      const { evalColor, cp, mate } = pickEval(true);
      c.actual = { uci: g.fullMoves[newCol] ?? null, san: null,
        evalColor, cp, mate, frozen: true, ...pickStats() };
    }

    // ── Depth-loss tracking ───────────────────────────────────────────
    if (c.wrongD == null) {
      const actual = g.fullMoves[newCol] ?? null;
      const wD     = _maxDepth(newCol);
      const pred   = wD >= 0 ? (c.pred.get(wD)?.uci ?? null) : null;
      if (actual && pred && actual !== pred) {
        c.wrongD       = wD;
        c.pendingWrongD = wD;
        _dbg(`wrong col=${newCol} wrongD=${wD} pred=${pred} actual=${actual}`);
        setTimeout(() => {
          if (c.pendingWrongD == null) return;
          const g2 = App.currentGame();
          if (!g2 || g2.id !== _gameId) { c.pendingWrongD = null; return; }
          const recovery = _maxDepth(gPly);   // gPly = the ply AFTER the committed move
          const lost = wD - (recovery >= 0 ? recovery : 0);
          c.pendingWrongD = null;
          if (lost > 0) {
            c.lostD = lost;
            _dbg(`depthLost col=${newCol} wrongD=${wD} recovery=${recovery} lost=${lost}`);
            _redrawAllRows(g2);
          }
        }, 300);
      }
    }
  }

  /* ══════════════════════════════════════════════════════════════════
   *  Rendering — reads _cols and _rowStats, never writes them
   * ══════════════════════════════════════════════════════════════════ */

  /** Build the SAN list for full-game history (for opponent committed cells). */
  function _buildHistorySan(g) {
    if (!g.fullMoves?.length || typeof San === 'undefined') return [];
    return San.buildSanList(g.initialFen, g.fullMoves);
  }

  /** Build one depth-row's ply cells + frozen stat cells. */
  function _buildRowCells(g, depth, historySan) {
    const gPly = g.fullMoves?.length ?? 0;
    const stats = _rowStats[depth] || {};
    let html = '';

    for (let col = 0; col < _numPlyCols; col++) {
      const c        = _cols[col];
      const entry    = c?.pred.get(depth);  // { uci, san } | undefined
      const isFuture = col > gPly;
      const ours     = _isOurs(g, col);

      // ── Actual-move cell (renders one row below deepest prediction) ──
      if (!entry) {
        const maxD = c ? _maxDepth(col) : -1;
        const act  = c?.actual;
        const actualDepth = maxD >= 0 ? maxD + 1 : 1;

        if (act && depth === actualDepth) {
          // Determine display SAN: bot uses search-time SAN, opponent uses historySan
          const rawSan     = act.frozen ? ((ours ? act.san : historySan?.[col]) ?? act.uci) : (act.san ?? act.uci);
          const displaySan = _toGlyph(rawSan);
          const isLive     = !act.frozen;
          const isCurrentCol = col === gPly;
          const { style: evalStyle, cls: evalCls } =
            _evalCellStyle(act.cp, act.mate, act.frozen, isLive && isCurrentCol);

          let cls = 'ep-td-ply ep-ply-actual';
          if (evalCls) cls += ' ' + evalCls;
          cls += act.frozen ? ' ep-ply-committed' : ' ep-ply-live';
          cls += ours ? ' ep-ply-us' : ' ep-ply-them';
          const sa = evalStyle ? ` style="${evalStyle}"` : '';

          // Tooltip
          const evalTip   = _fmtEvalTip(act.cp, act.mate);
          const moveLine  = _describeMove(rawSan) || (displaySan ?? '');
          const kv = [];
          if (act.depth != null) kv.push(`Depth\t${act.depth}${act.seldepth ? ` \u00b7 sel ${act.seldepth}` : ''}`);
          if (evalTip)           kv.push(`Eval\t${evalTip}`);
          if (act.nodes)         kv.push(`Nodes\t${App.fmtN(act.nodes)}`);
          if (act.nps)           kv.push(`NPS\t${App.fmtN(act.nps)}/s`);
          if (act.time)          kv.push(`Time\t${_fmtMs(act.time)}`);
          if (act.stopReason)    kv.push(`Stop\t${act.stopReason}`);
          let tip = moveLine + (kv.length ? '\n---\n' + kv.join('\n') : '');
          // If frozen opponent and prediction was wrong, show what we predicted
          if (act.frozen && !ours && c?.wrongD != null) {
            const predEntry = c.pred.get(c.wrongD);
            if (predEntry) {
              const predDisplay = predEntry.san ?? predEntry.uci ?? '?';
              tip += `\nPredicted\t${_describeMove(predDisplay) || _toGlyph(predDisplay)}`;
            }
          }
          html += `<td class="${cls}"${sa} data-ep-tip="${App.esc(tip)}">${App.esc(displaySan ?? '')}</td>`;
          continue;
        }

        // Empty cell — no prediction, no actual at this depth
        let cls = 'ep-td-ply';
        if (col === gPly) cls += ' ep-ply-current';
        else if (isFuture) cls += ' ep-ply-future';
        html += `<td class="${cls}"></td>`;
        continue;
      }

      // ── Prediction cell (the staircase) ─────────────────────────────
      const sanDisplay = _toGlyph(entry.san ?? entry.uci);
      const isHistory  = col < gPly;
      let cls = 'ep-td-ply';
      if (isHistory) {
        cls += ' ep-ply-committed';
        cls += ours ? ' ep-ply-us' : ' ep-ply-them';
        if (!ours && c?.wrongD != null && c?.lostD != null && depth > c.wrongD - c.lostD)
          cls += ' ep-ply-wrong';
      } else if (isFuture) {
        cls += ' ep-ply-future';
        cls += ours ? ' ep-ply-us' : ' ep-ply-them';
      } else {
        cls += ' ep-ply-current';
        cls += ours ? ' ep-ply-us' : ' ep-ply-them';
      }
      const tip = _describeMove(entry.san ?? entry.uci) || sanDisplay;
      html += `<td class="${cls}" data-ep-tip="${App.esc(tip)}">${App.esc(sanDisplay)}</td>`;
    }

    const { evalStr = '\u2013', evalColor = 'var(--muted)', nodesStr = '\u2013', seldepth = '\u2013' } = stats;
    return html +
      `<td class="ep-td-frozen ep-td-d">${depth}</td>` +
      `<td class="ep-td-frozen ep-td-sel">${seldepth}</td>` +
      `<td class="ep-td-frozen ep-td-eval" style="color:${evalColor}">${evalStr}</td>` +
      `<td class="ep-td-frozen ep-td-nodes">${nodesStr}</td>`;
  }

  /** Ensure a <tr> exists for the given depth. */
  function _ensureRow(tbody, depth) {
    if (tbody.querySelector(`tr[data-depth="${depth}"]`)) return;
    const tr = document.createElement('tr');
    tr.dataset.depth = depth;
    let placed = false;
    for (const ex of tbody.querySelectorAll('tr[data-depth]')) {
      if (+ex.dataset.depth > depth) { tbody.insertBefore(tr, ex); placed = true; break; }
    }
    if (!placed) tbody.appendChild(tr);
  }

  /** Rebuild every row's cells. */
  function _redrawAllRows(g) {
    const tbody = document.getElementById('ep-tbody');
    if (!tbody || !g) return;
    const gPly = g.fullMoves?.length ?? 0;
    _expandCols(_neededCols(gPly));

    // Ensure rows exist for every depth in predictions
    for (const d of _allDepths()) _ensureRow(tbody, d);
    // Ensure rows exist for actual-move cells (one below max prediction depth)
    for (const c of Object.keys(_cols)) {
      const maxD = _maxDepth(+c);
      const col  = _cols[+c];
      if (maxD >= 0) _ensureRow(tbody, maxD + 1);
      // Also ensure row at depth 1 if actual exists but no predictions
      if (col?.actual && maxD < 0) _ensureRow(tbody, 1);
    }

    const historySan = _buildHistorySan(g);
    for (const tr of tbody.querySelectorAll('tr[data-depth]')) {
      tr.innerHTML = _buildRowCells(g, +tr.dataset.depth, historySan);
    }
    _updateFrozenOffsets();
    _followCurrentPly();
  }

  /** Center the current-ply column in the visible horizontal area of the wrap. */
  function _followCurrentPly() {
    if (!_followMode) return;
    const g = App.currentGame() ?? _lastGame;
    if (!g) return;
    const wrap = document.getElementById('ep-table-wrap');
    if (!wrap) return;
    const gPly = g.fullMoves?.length ?? 0;
    const th = wrap.querySelector(`th[data-ply-col="${gPly}"]`);
    if (!th) return;
    const evalW  = parseFloat(document.getElementById('ep-col-eval')?.style.width)  || 60;
    const nodesW = parseFloat(document.getElementById('ep-col-nodes')?.style.width) || 52;
    const frozenW = EP_D_W + EP_SEL_W + evalW + nodesW;
    const visW    = wrap.clientWidth - frozenW;
    const desired = th.offsetLeft + (th.offsetWidth || PLY_W) / 2 - visW / 2;
    _programmaticScroll = true;
    wrap.scrollLeft = Math.max(0, desired);
    // reset flag after the browser dispatches the scroll event
    requestAnimationFrame(() => { _programmaticScroll = false; });
  }

  /** Batch redraws via rAF. */
  function _scheduleRedraw() {
    if (_rafId != null) return;
    _rafId = requestAnimationFrame(() => {
      _rafId = null;
      const g = App.currentGame();
      if (g && _rendered) _redrawAllRows(g);
    });
  }

  /* ── Column header and table structure ──────────────────────────── */

  function _plyHeadersHtml(g) {
    let cells = '';
    for (let col = 0; col < _numPlyCols; col++) {
      const side = _isOurs(g, col) ? 'bot' : 'opp';
      const pc   = (col % 2 === 0) ? 'w' : 'b';
      cells += `<th class="ep-th-ply ep-th-ply-${side} ep-pc-${pc}" data-ply-col="${col}">` +
               `<span class="ep-ply-dot"></span>${col + 1}</th>`;
    }
    return cells;
  }

  function _expandCols(needed) {
    if (needed <= _numPlyCols) return;
    const g = App.currentGame();
    const prev = _numPlyCols;
    _numPlyCols = needed;

    const colEl = document.getElementById('ep-col-ply-group');
    if (colEl) colEl.setAttribute('span', _numPlyCols);

    const plyRow = document.getElementById('ep-thead-ply');
    if (plyRow && g) {
      const anchor = plyRow.querySelector('.ep-th-frozen');
      for (let col = prev; col < needed; col++) {
        const side = _isOurs(g, col) ? 'bot' : 'opp';
        const pc   = (col % 2 === 0) ? 'w' : 'b';
        const th   = document.createElement('th');
        th.className      = `ep-th-ply ep-th-ply-${side} ep-pc-${pc}`;
        th.dataset.plyCol = col;
        th.innerHTML      = `<span class="ep-ply-dot"></span>${col + 1}`;
        plyRow.insertBefore(th, anchor ?? null);
      }
    }

    const tbody = document.getElementById('ep-tbody');
    if (!tbody || !g) return;
    const historySan = _buildHistorySan(g);
    for (const tr of tbody.querySelectorAll('tr[data-depth]')) {
      tr.innerHTML = _buildRowCells(g, +tr.dataset.depth, historySan);
    }
    _updateFrozenOffsets();
  }

  /* ── Frozen column right-offset management ──────────────────────── */

  function _updateFrozenOffsets() {
    const evalW  = parseFloat(document.getElementById('ep-col-eval')?.style.width)  || 60;
    const nodesW = parseFloat(document.getElementById('ep-col-nodes')?.style.width) || 52;
    const rNodes = 0;
    const rEval  = nodesW;
    const rSel   = nodesW + evalW;
    const rD     = nodesW + evalW + EP_SEL_W;
    _setRight('#ep-table-wrap .ep-th-nodes, #ep-table-wrap .ep-td-nodes', rNodes);
    _setRight('#ep-table-wrap .ep-th-eval,  #ep-table-wrap .ep-td-eval',  rEval);
    _setRight('#ep-table-wrap .ep-th-sel,   #ep-table-wrap .ep-td-sel',   rSel);
    _setRight('#ep-table-wrap .ep-th-d,     #ep-table-wrap .ep-td-d',     rD);
  }
  function _setRight(sel, px) {
    document.querySelectorAll(sel).forEach(el => { el.style.right = px + 'px'; });
  }

  /* ── Stats strip ────────────────────────────────────────────────── */

  function _updateStats(g) {
    const el = document.getElementById('ep-stats');
    if (!el) return;

    const live = g.searchLive;
    const moves = g.moves || [];
    const isFinished = g.status === 'finished';

    if (isFinished) {
      const ds = Object.keys(_rowStats).map(Number);
      if (ds.length) {
        const bestD = Math.max(...ds);
        const st    = _rowStats[bestD];
        el.innerHTML = `
          <div class="ep-stat-row">
            <span class="ep-stat"><span class="ep-k">D</span><span class="ep-v">${bestD}</span></span>
            <span class="ep-stat"><span class="ep-k">Eval</span><span class="ep-v" style="color:${st.evalColor ?? 'var(--muted)'}">${st.evalStr ?? '\u2013'}</span></span>
            <span class="ep-stat"><span class="ep-k">N</span><span class="ep-v">${st.nodesStr ?? '\u2013'}</span></span>
            <span class="ep-stat"><span class="ep-k">NPS</span><span class="ep-v">${st.nps ? App.fmtN(st.nps) + '/s' : '\u2013'}</span></span>
            <span class="ep-phase ep-phase-idle">done</span>
          </div>`;
      } else {
        el.innerHTML = '<div class="ep-stat-row"><span class="ep-phase ep-phase-idle">done</span></div>';
      }
      return;
    }

    const depth = live?.depth ?? (moves.length ? moves[moves.length - 1]?.depth : null);
    let evalStr = '\u2013', evColor = 'var(--muted)';
    const src = live ?? (moves.length ? moves[moves.length - 1] : null);
    if (src) {
      const neg  = src.ponder === 'opponent';
      const dCp  = src.eval_cp != null ? (neg ? -src.eval_cp : src.eval_cp) : null;
      const dMt  = src.mate    != null ? (neg ? -src.mate    : src.mate)    : null;
      evalStr = dMt != null ? (dMt > 0 ? `+M${dMt}` : `-M${Math.abs(dMt)}`)
              : dCp != null ? _fmtEval(dCp) : '\u2013';
      evColor = _evalColor(dCp, dMt);
    }
    const nodes = live?.nodes ?? 0;
    const nps   = live?.nps ?? (moves.length ? Math.round(moves.reduce((s,m) => s + (m.nps||0), 0) / moves.length) : 0);
    const conf    = g.confidence ?? 0;
    const confPct = Math.round(conf * 100);
    const confClr = conf >= 0.8 ? 'var(--green)' : conf >= 0.5 ? 'var(--yellow)' : 'var(--red)';
    let phase = '', pCls = '';
    if (g.status === 'active') {
      if (live?.ponder) { phase = 'ponder'; pCls = 'ep-phase-ponder'; }
      else if (live)    { phase = 'search'; pCls = 'ep-phase-search'; }
      else              { phase = 'idle';   pCls = 'ep-phase-idle';   }
    } else if (g.status === 'finished') { phase = 'done'; pCls = 'ep-phase-idle'; }

    el.innerHTML = `
      <div class="ep-stat-row">
        <span class="ep-stat"><span class="ep-k">D</span><span class="ep-v">${depth ?? '\u2013'}</span></span>
        <span class="ep-stat"><span class="ep-k">Eval</span><span class="ep-v" style="color:${evColor}">${evalStr}</span></span>
        <span class="ep-stat"><span class="ep-k">N</span><span class="ep-v">${App.fmtN(nodes)}</span></span>
        <span class="ep-stat"><span class="ep-k">NPS</span><span class="ep-v">${App.fmtN(nps)}</span></span>
        <span class="ep-stat ep-stat-conf">
          <span class="ep-k">Conf</span>
          <span class="ep-v" style="color:${confClr}">${confPct}%</span>
          <span class="ep-conf-bar"><span class="ep-conf-fill" style="width:${confPct}%;background:${confClr}"></span></span>
        </span>
        ${phase ? `<span class="ep-phase ${pCls}">${phase}</span>` : ''}
      </div>`;
  }

  /* ══════════════════════════════════════════════════════════════════
   *  Full render (DOM scaffold)
   * ══════════════════════════════════════════════════════════════════ */

  function _fullRender(g, preserveData = false) {
    const p = _pane();
    _dbg(`_fullRender(${g.id}, preserve=${preserveData})  pane=${p ? 'ok' : 'MISSING'}  status=${g.status}`);
    if (!p) { _warn('_fullRender — pane not found'); return; }

    if (_dataGameId !== g.id) {
      _dbg(`WIPE ${_dataGameId} \u2192 ${g.id}`);
      _wipeAll();
      _dataGameId = g.id;
    } else {
      _dbg(`PRESERVE data for ${g.id}`);
    }

    const gPly  = g.fullMoves?.length ?? 0;
    _numPlyCols = _neededCols(gPly);

    p.innerHTML = `
      <div class="ep-stats" id="ep-stats"></div>
      <div class="ep-toolbar" id="ep-toolbar">
        <button class="ep-follow-btn${_followMode ? ' active' : ''}" id="ep-follow-btn" title="Auto-scroll to keep the current ply centred">Follow</button>
      </div>
      <div class="ep-table-wrap" id="ep-table-wrap">
        <table class="ep-table ${g.color === 'white' ? 'ep-bot-white' : 'ep-bot-black'}" id="ep-table">
          <colgroup>
            <col id="ep-col-ply-group" span="${_numPlyCols}" class="ep-col-ply">
            <col id="ep-col-d">
            <col id="ep-col-sel">
            <col id="ep-col-eval" style="width:60px">
            <col id="ep-col-nodes" style="width:52px">
          </colgroup>
          <thead>
            <tr id="ep-thead-ply">
              ${_plyHeadersHtml(g)}
              <th class="ep-th-frozen ep-th-d">D</th>
              <th class="ep-th-frozen ep-th-sel">Sel</th>
              <th class="ep-th-frozen ep-th-eval">Eval<div class="ep-col-resize" data-col="ep-col-eval"></div></th>
              <th class="ep-th-frozen ep-th-nodes">N<div class="ep-col-resize" data-col="ep-col-nodes"></div></th>
            </tr>
          </thead>
          <tbody id="ep-tbody"></tbody>
        </table>
      </div>
      <div id="ep-tip" class="ep-tip" hidden></div>`;

    _rendered = true;
    _lastGame = g;

    _redrawAllRows(g);
    if (g.searchLive) _ingest({ ...g.searchLive });

    _updateStats(g);
    _updateFrozenOffsets();
    _bindPvHover();
    _bindResizeHandles();
    _bindFollowControls();
  }

  /* ══════════════════════════════════════════════════════════════════
   *  Event handling & lifecycle
   * ══════════════════════════════════════════════════════════════════ */

  function show() {
    const g = App.currentGame();
    const paneEmpty = !document.getElementById('ep-tbody');
    _dbg(`show()  g=${g?.id ?? 'null'}  _gameId=${_gameId}  _rendered=${_rendered}  empty=${paneEmpty}`);
    if (!g) {
      if (_dataGameId && (!_rendered || paneEmpty) && _lastGame) {
        _dbg('show() refilling from _lastGame');
        _fullRender(_lastGame);
      } else if (!_dataGameId) {
        const p = _pane();
        if (p) p.innerHTML = '<div class="ep-empty">No active game</div>';
        _rendered = false;
      }
      return;
    }
    if (g.id !== _gameId) { _gameId = g.id; _rendered = false; }
    if (!_rendered || paneEmpty) {
      _fullRender(g);
    } else {
      _updateStats(g);
    }
  }

  function onEvent(type, data) {
    let g = App.currentGame();
    if (!g && (type === 'game_end' || type === 'snapshot')) g = _lastGame;
    _dbg(`onEvent(${type})  g=${g?.id ?? 'null'}  _gameId=${_gameId}  _rendered=${_rendered}`);
    if (!g) { _warn(`onEvent(${type}) \u2014 no game`); return; }
    if (g.id !== _gameId) { _gameId = g.id; _rendered = false; }

    // Boundary events: re-render DOM preserving data.
    if (type === 'snapshot' || type === 'game_end') {
      _dbg(`onEvent \u2192 ${type} \u2014 refilling DOM`);
      _rendered = false;
      _fullRender(g, true);
      return;
    }

    // Ignore engine events for a finished game.
    if (g.status === 'finished' && g.id === _dataGameId) {
      _dbg(`onEvent(${type}) \u2014 game finished, ignoring`);
      return;
    }

    if (!_rendered) { _fullRender(g); }

    switch (type) {
      case 'search_start': {
        const gPly = g.fullMoves?.length ?? 0;
        _dbg('search_start gPly=' + gPly);
        _wipeFuture(gPly);
        _rowStats = {};
        _redrawAllRows(g);
        _updateStats(g);
        break;
      }
      case 'search_info': {
        _ingest(data);
        _updateStats(g);
        // Grow columns + ensure row for the new depth
        const tbody = document.getElementById('ep-tbody');
        if (tbody) {
          const d = data.depth ?? 0;
          _expandCols(_neededCols(g.fullMoves?.length ?? 0));
          _ensureRow(tbody, d);
          const tr = tbody.querySelector(`tr[data-depth="${d}"]`);
          if (tr) {
            const pvClean = (Array.isArray(data.pv) ? data.pv
              : typeof data.pv === 'string' ? data.pv.split(' ').filter(Boolean)
              : []).filter(m => UCI_RE.test(m));
            tr.dataset.pv = pvClean.join(' ');
            tr.classList.remove('stale');
            tr.classList.toggle('ep-ponder', data.ponder === 'opponent');
          }
        }
        _scheduleRedraw();
        const wrap = document.getElementById('ep-table-wrap');
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
        break;
      }
      case 'move':
      case 'opponent_move': {
        const gPly = g.fullMoves?.length ?? 0;
        _dbg(type + ' gPly=' + gPly, data);
        _commitMove(g, type, gPly, type === 'move' ? data.moveStat?.stop_reason : undefined);
        _redrawAllRows(g);
        _updateStats(g);
        break;
      }
      case 'ponder_start': {
        const gPly = g.fullMoves?.length ?? 0;
        _dbg('ponder_start gPly=' + gPly);
        // Wipe stale future predictions from the main search.
        // Always wipe from gPly: col gPly carries stale main-search opponent predictions
        // which would set maxDepth too high, decoupling the staircase from the actual cell.
        // _ingest will re-fill col gPly fresh from the first ponder info line.
        _wipeFuture(gPly);
        _rowStats = {};
        _redrawAllRows(g);
        _updateStats(g);
        break;
      }
      case 'ponder_end': {
        _redrawAllRows(g);
        _updateStats(g);
        break;
      }
      case 'search_end':
        _updateStats(g);
        break;
    }
  }

  /* ══════════════════════════════════════════════════════════════════
   *  Interaction: tooltips, column resize
   * ══════════════════════════════════════════════════════════════════ */

  function _bindPvHover() {
    const wrap = document.getElementById('ep-table-wrap');
    const tip  = document.getElementById('ep-tip');
    if (!wrap || !tip) return;

    wrap.addEventListener('mouseover', e => {
      const td = e.target.closest('td[data-ep-tip]');
      if (!td) { tip.hidden = true; return; }
      const text = td.dataset.epTip || '';
      if (!text) { tip.hidden = true; return; }
      const esc2 = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      let html = '', firstLine = true;
      for (const l of text.split('\n')) {
        if (l === '---') { html += '<div class="ep-tip-hr"></div>'; continue; }
        const tab = l.indexOf('\t');
        if (tab !== -1) {
          html += `<div class="ep-tip-row"><span class="ep-tip-k">${esc2(l.slice(0,tab))}</span>` +
                  `<span class="ep-tip-v">${esc2(l.slice(tab+1))}</span></div>`;
        } else if (firstLine) {
          html += `<div class="ep-tip-move">${esc2(l)}</div>`;
        } else {
          html += `<div class="ep-tip-line">${esc2(l)}</div>`;
        }
        firstLine = false;
      }
      tip.innerHTML = html;
      tip.hidden = false;
      const ar = td.getBoundingClientRect();
      const tr = tip.getBoundingClientRect();
      const vw = window.innerWidth, vh = window.innerHeight;
      let left = ar.left, top = ar.bottom + 5;
      if (top + tr.height > vh - 8) top = ar.top - tr.height - 5;
      if (left + tr.width  > vw - 8) left = vw - tr.width - 8;
      if (left < 4) left = 4;
      tip.style.left = left + 'px';
      tip.style.top  = top  + 'px';
    });

    wrap.addEventListener('mouseleave', () => { tip.hidden = true; });
    wrap.addEventListener('scroll', () => { tip.hidden = true; }, { passive: true });
  }

  function _bindFollowControls() {
    const btn  = document.getElementById('ep-follow-btn');
    const wrap = document.getElementById('ep-table-wrap');
    if (btn) {
      btn.addEventListener('click', () => {
        _followMode = !_followMode;
        btn.classList.toggle('active', _followMode);
        if (_followMode) _followCurrentPly();
      });
    }
    if (wrap && !wrap._epFollowBound) {
      wrap._epFollowBound = true;
      wrap.addEventListener('scroll', () => {
        if (_programmaticScroll) return;
        // User scrolled horizontally — disengage follow
        _followMode = false;
        const b = document.getElementById('ep-follow-btn');
        if (b) b.classList.remove('active');
      }, { passive: true });
    }
  }

  function _bindResizeHandles() {
    const wrap = document.getElementById('ep-table-wrap');
    if (!wrap || wrap._epResizeBound) return;
    wrap._epResizeBound = true;

    wrap.addEventListener('mousedown', e => {
      const handle = e.target.closest('.ep-col-resize');
      if (!handle) return;
      const col = document.getElementById(handle.dataset.col);
      if (!col) return;
      const th = handle.parentElement;
      const startX = e.clientX, startW = th.getBoundingClientRect().width;
      handle.classList.add('dragging');
      e.preventDefault();
      const onMove = ev => {
        col.style.width = Math.max(18, startW + (ev.clientX - startX)) + 'px';
        _updateFrozenOffsets();
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  /* ══════════════════════════════════════════════════════════════════
   *  Public API
   * ══════════════════════════════════════════════════════════════════ */

  return { show, onEvent };
})();
