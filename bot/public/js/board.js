/**
 * board.js — Instance-based SVG chess board with diff rendering and piece animation.
 *
 * Instance API (preferred — used by tab-game.js):
 *   const b = Board.create(container)
 *   b.update(fen, opts)       — incremental diff render with optional animation
 *   b.refreshPieces()         — rebuild piece images (after piece-set change)
 *   b.destroy()               — clean up
 *   b.svg                     — the SVG element
 *
 * Legacy API (still works, no animation):
 *   Board.render(svg, fen, opts)
 *
 * Shared:
 *   Board.setPieceSet(name)   — 'cburnett' | 'unicode'
 *   Board.setTheme(name)      — sets data-board-theme on <html>
 *   Board.getPieceSet()       — returns current piece set name
 *   Board.getTheme()          — returns current theme name
 *   Board.parseFen(fen)       — { board[][], turn }
 *   Board.parseUci(uci)       — { from:{file,rank}, to:{file,rank}, promo } | null
 */
'use strict';

const Board = (() => {

  /* ── constants ─────────────────────────────────────────────────────── */
  const NS    = 'http://www.w3.org/2000/svg';
  const SQ    = 56;
  const BOARD = SQ * 8;
  const FILES = 'abcdefgh';
  const ANIM_MS = 180;   // slide duration

  /* ── shared state (piece set + theme) ──────────────────────────────── */
  let _pieceSet   = localStorage.getItem('board-piece-set')   || 'cburnett';
  let _pieceStyle = localStorage.getItem('board-piece-style') || 'standard';
  let _theme      = localStorage.getItem('board-theme')       || 'classic';
  document.documentElement.dataset.boardTheme  = _theme;
  document.documentElement.dataset.pieceStyle  = _pieceStyle;
  // Restore custom theme colours on init
  _applyCustomTheme(
    localStorage.getItem('board-custom-light') || '#eeeed2',
    localStorage.getItem('board-custom-dark')  || '#769656',
  );

  const PIECE_FILE = {
    K: 'wK', Q: 'wQ', R: 'wR', B: 'wB', N: 'wN', P: 'wP',
    k: 'bK', q: 'bQ', r: 'bR', b: 'bB', n: 'bN', p: 'bP',
  };
  const GLYPHS = {
    K: '\u2654', Q: '\u2655', R: '\u2656', B: '\u2657', N: '\u2658', P: '\u2659',
    k: '\u265A', q: '\u265B', r: '\u265C', b: '\u265D', n: '\u265E', p: '\u265F',
  };

  /* ── helpers ───────────────────────────────────────────────────────── */

  function parseFen(fen) {
    const parts = (fen || '').split(' ');
    const rows  = (parts[0] || '').split('/');
    const turn  = parts[1] || 'w';
    const board = [];
    for (let r = 0; r < 8; r++) {
      const row = [];
      for (const ch of (rows[r] || '').split('')) {
        if (ch >= '1' && ch <= '8') { for (let i = 0; i < parseInt(ch); i++) row.push(''); }
        else row.push(ch);
      }
      while (row.length < 8) row.push('');
      board.push(row);
    }
    return { board, turn };
  }

  function parseUci(uci) {
    if (!uci || uci.length < 4) return null;
    return {
      from: { file: uci.charCodeAt(0) - 97, rank: 8 - parseInt(uci[1]) },
      to:   { file: uci.charCodeAt(2) - 97, rank: 8 - parseInt(uci[3]) },
      promo: uci.length > 4 ? uci[4] : null,
    };
  }

  function findKing(board, color) {
    const king = color === 'w' ? 'K' : 'k';
    for (let r = 0; r < 8; r++)
      for (let f = 0; f < 8; f++)
        if (board[r][f] === king) return { rank: r, file: f };
    return null;
  }

  function svgEl(tag, attrs = {}) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  /** Display x,y in viewBox coords for board[r][f]. */
  function dxy(r, f, flip) {
    const dr = flip ? 7 - r : r;
    const df = flip ? 7 - f : f;
    return { x: df * SQ, y: dr * SQ };
  }

  /** Square name for board[r][f], e.g. 'e4'. */
  function sqName(r, f) { return FILES[f] + (8 - r); }

  /* ── piece node factory ────────────────────────────────────────────── */

  function _makePiece(piece, x, y, sq) {
    if (!piece) return null;
    if (_pieceSet !== 'unicode') {
      const pf = PIECE_FILE[piece];
      if (!pf) return null;
      return svgEl('image', {
        href: `/pieces/${_pieceSet}/${pf}.svg`,
        x: x + 1, y: y + 1,
        width: SQ - 2, height: SQ - 2,
        preserveAspectRatio: 'xMidYMid meet',
        'data-sq': sq,
      });
    }
    const glyph = GLYPHS[piece];
    if (!glyph) return null;
    const isW = piece === piece.toUpperCase();
    const txt = svgEl('text', {
      x: x + SQ / 2, y: y + SQ / 2 + 2,
      class: `piece-glyph ${isW ? 'piece-white' : 'piece-black'}`,
      'data-sq': sq,
    });
    txt.textContent = glyph;
    return txt;
  }

  /* ── animation helper ──────────────────────────────────────────────── */

  /**
   * Animate a piece node from an offset (dx,dy) back to its placed position.
   * Uses SVG transform attribute — works in viewBox coordinate space, so it's
   * resolution-independent and unaffected by container resizing.
   *
   * @returns {{ cancel: Function }}
   */
  function _animSlide(node, dx, dy, duration) {
    if (dx === 0 && dy === 0) return { cancel() {} };
    duration = duration || ANIM_MS;
    let id = 0;
    let cancelled = false;
    const start = performance.now();

    // Apply initial offset immediately
    node.setAttribute('transform', `translate(${dx},${dy})`);

    function step(now) {
      if (cancelled) return;
      const t = Math.min((now - start) / duration, 1);
      const e = 1 - (1 - t) * (1 - t);  // ease-out quadratic
      const cx = dx * (1 - e);
      const cy = dy * (1 - e);
      if (t < 1) {
        node.setAttribute('transform', `translate(${cx},${cy})`);
        id = requestAnimationFrame(step);
      } else {
        node.removeAttribute('transform');
      }
    }
    id = requestAnimationFrame(step);

    return {
      cancel() {
        cancelled = true;
        cancelAnimationFrame(id);
        node.removeAttribute('transform');
      },
    };
  }

  /* ══════════════════════════════════════════════════════════════════════
     Board.create(container) → instance
     ══════════════════════════════════════════════════════════════════════ */

  function create(container, opts = {}) {
    const { noAnnotations = false } = opts;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${BOARD} ${BOARD}`);
    container.appendChild(svg);

    /* three layers — squares below, pieces in the middle, overlay on top */
    const gSq   = svgEl('g', { class: 'layer-sq' });
    const gPc   = svgEl('g', { class: 'layer-pc' });
    const gOver = svgEl('g', { class: 'layer-over' });
    svg.appendChild(gSq);
    svg.appendChild(gPc);
    svg.appendChild(gOver);

    /* ── state ──────────────────────────────────────────────────────── */
    let grid = Array.from({ length: 8 }, () => Array(8).fill(''));
    let flip = false;
    // Overlay state — engine arrows/circles (set by setOverlay / update)
    // and user right-click annotations (drawn interactively on the SVG).
    let _overlayArrows  = [];   // [{ uci, color, opacity, width }]
    let _overlayCircles = [];   // [{ rank, file, color, opacity }]
    let _userArrows     = [];   // [{ uci, color, opacity, width }] right-click drawn
    let _userCircles    = [];   // [{ rank, file, color, opacity }]
    let _rcStart        = null; // right-click drag origin { rank, file }
    let _rcCurrent      = null; // live mouse pos during right-click drag (ghost preview)
    // 64 persistent <rect> elements (index = r*8+f)
    const sqEls = [];
    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        const { x, y } = dxy(r, f, false);
        const rect = svgEl('rect', { x, y, width: SQ, height: SQ, class: 'sq-light' });
        gSq.appendChild(rect);
        sqEls.push(rect);
      }
    }

    // piece map: key "r,f" → { node: SVGElement, piece: char }
    const pcMap = new Map();

    // active animation handles
    let anims = [];

    /* ── square layout & styling ───────────────────────────────────── */

    function _layoutSq() {
      for (let r = 0; r < 8; r++)
        for (let f = 0; f < 8; f++) {
          const { x, y } = dxy(r, f, flip);
          const el = sqEls[r * 8 + f];
          el.setAttribute('x', x);
          el.setAttribute('y', y);
        }
    }

    function _styleSq(newGrid, turn, lastMove, oppMove, inCheck) {
      const lastP  = parseUci(lastMove);
      const oppP   = parseUci(oppMove);
      const checkK = inCheck ? findKing(newGrid, turn) : null;
      for (let r = 0; r < 8; r++) {
        for (let f = 0; f < 8; f++) {
          const isLight = (r + f) % 2 === 0;
          let cls = isLight ? 'sq-light' : 'sq-dark';
          if (oppP) {
            if (r === oppP.from.rank && f === oppP.from.file) cls = 'sq-opp-src';
            if (r === oppP.to.rank   && f === oppP.to.file)   cls = 'sq-opp-dst';
          }
          if (lastP) {
            if (r === lastP.from.rank && f === lastP.from.file) cls = 'sq-last-src';
            if (r === lastP.to.rank   && f === lastP.to.file)   cls = 'sq-last-dst';
          }
          if (checkK && r === checkK.rank && f === checkK.file) cls = 'sq-check';
          sqEls[r * 8 + f].setAttribute('class', cls);
        }
      }
    }

    /* ── piece helpers ──────────────────────────────────────────────── */

    function _addPc(piece, r, f) {
      const { x, y } = dxy(r, f, flip);
      const node = _makePiece(piece, x, y, sqName(r, f));
      if (!node) return;
      gPc.appendChild(node);
      pcMap.set(`${r},${f}`, { node, piece });
    }

    function _rmPc(r, f) {
      const key = `${r},${f}`;
      const entry = pcMap.get(key);
      if (entry) { entry.node.remove(); pcMap.delete(key); }
    }

    /** Move a piece's DOM position to (r,f). Does NOT animate. */
    function _posPc(entry, r, f) {
      const { x, y } = dxy(r, f, flip);
      const n = entry.node;
      if (n.tagName === 'image') {
        n.setAttribute('x', x + 1);
        n.setAttribute('y', y + 1);
      } else {
        n.setAttribute('x', x + SQ / 2);
        n.setAttribute('y', y + SQ / 2 + 2);
      }
      n.dataset.sq = sqName(r, f);
    }

    /**
     * Slide a piece (already positioned at destR,destF) from srcR,srcF.
     * The piece is visually offset via SVG transform and animated back.
     */
    function _slidePc(entry, srcR, srcF, destR, destF) {
      const from = dxy(srcR, srcF, flip);
      const to   = dxy(destR, destF, flip);
      anims.push(_animSlide(entry.node, from.x - to.x, from.y - to.y));
    }

    /* ── cancel running animations ─────────────────────────────────── */

    function _cancelAnims() {
      for (const a of anims) a.cancel();
      anims = [];
    }

    /* ── overlay (coords + arrows + circles + border) ─────────────── */

    /** Convert a pointer event to board { rank, file }, accounting for flip. */
    function _sqFromEvent(e) {
      const rect = svg.getBoundingClientRect();
      const sx = (e.clientX - rect.left) / rect.width  * BOARD;
      const sy = (e.clientY - rect.top)  / rect.height * BOARD;
      const df = Math.floor(sx / SQ);
      const dr = Math.floor(sy / SQ);
      if (df < 0 || df > 7 || dr < 0 || dr > 7) return null;
      return { rank: flip ? 7 - dr : dr, file: flip ? 7 - df : df };
    }

    /** Render one arrow: { uci, color, opacity, width }. */
    function _drawArrow(a) {
      const bm = parseUci(a.uci);
      if (!bm) return;
      const fR = flip ? 7 - bm.from.rank : bm.from.rank;
      const fF = flip ? 7 - bm.from.file : bm.from.file;
      const tR = flip ? 7 - bm.to.rank   : bm.to.rank;
      const tF = flip ? 7 - bm.to.file   : bm.to.file;
      const x1 = fF * SQ + SQ / 2, y1 = fR * SQ + SQ / 2;
      const x2 = tF * SQ + SQ / 2, y2 = tR * SQ + SQ / 2;
      const color   = a.color   ?? '#ffffff';
      const opacity = a.opacity ?? 0.72;
      const w       = a.width   ?? 8;
      const hl      = w * 3.2;
      const angle   = Math.atan2(y2 - y1, x2 - x1);
      const x2s = x2 - Math.cos(angle) * hl * 0.62;
      const y2s = y2 - Math.sin(angle) * hl * 0.62;
      // Wrap shaft + arrowhead in a single <g> with opacity on the group.
      // SVG composites the children into an offscreen buffer at full opacity,
      // then applies the group opacity once to the result.  Without this,
      // each element has opacity set independently so the tip region where
      // the polygon overlaps the shaft line looks double-opaque.
      const g = svgEl('g', { opacity });
      g.appendChild(svgEl('line', {
        x1, y1, x2: x2s, y2: y2s,
        stroke: color, 'stroke-width': w, 'stroke-linecap': 'round',
      }));
      g.appendChild(svgEl('polygon', {
        points: `${x2},${y2} `
          + `${x2 - hl * Math.cos(angle - 0.50)},${y2 - hl * Math.sin(angle - 0.50)} `
          + `${x2 - hl * Math.cos(angle + 0.50)},${y2 - hl * Math.sin(angle + 0.50)}`,
        fill: color,
      }));
      gOver.appendChild(g);
    }

    /** Render one circle annotation: { rank, file, color, opacity }. */
    function _drawCircle(c) {
      const dr = flip ? 7 - c.rank : c.rank;
      const df = flip ? 7 - c.file : c.file;
      gOver.appendChild(svgEl('circle', {
        cx: df * SQ + SQ / 2, cy: dr * SQ + SQ / 2,
        r: SQ * 0.43,
        stroke: c.color ?? '#ffffff', 'stroke-width': 5, fill: 'none',
        opacity: c.opacity ?? 0.72,
      }));
    }

    function _renderOverlay() {
      gOver.innerHTML = '';

      // Coordinates
      for (let i = 0; i < 8; i++) {
        const fi = flip ? 7 - i : i;
        const ri = flip ? 7 - i : i;
        const fl = svgEl('text', {
          x: fi * SQ + SQ - 4, y: BOARD - 3,
          class: `coord ${(7 + i) % 2 === 0 ? 'coord-light' : 'coord-dark'}`,
          'text-anchor': 'end',
        });
        fl.textContent = FILES[i];
        gOver.appendChild(fl);
        const rl = svgEl('text', {
          x: 3, y: ri * SQ + 11,
          class: `coord ${i % 2 === 0 ? 'coord-dark' : 'coord-light'}`,
        });
        rl.textContent = String(8 - i);
        gOver.appendChild(rl);
      }

      // User annotations (drawn under engine arrows so engine stays on top)
      for (const c of _userCircles)    _drawCircle(c);
      for (const c of _overlayCircles) _drawCircle(c);
      for (const a of _userArrows)     _drawArrow(a);
      for (const a of _overlayArrows)  _drawArrow(a);

      // Ghost arrow / circle preview while right-click dragging
      if (_rcStart && _rcCurrent) {
        const same = _rcStart.rank === _rcCurrent.rank && _rcStart.file === _rcCurrent.file;
        if (same) {
          _drawCircle({ rank: _rcCurrent.rank, file: _rcCurrent.file,
                        color: USER_ANN_COLOR, opacity: 0.35 });
        } else {
          const uci = FILES[_rcStart.file] + (8 - _rcStart.rank) +
                      FILES[_rcCurrent.file] + (8 - _rcCurrent.rank);
          _drawArrow({ uci, color: USER_ANN_COLOR, opacity: 0.35, width: 7 });
        }
      }

      // Border (always topmost)
      gOver.appendChild(svgEl('rect', {
        x: 0, y: 0, width: BOARD, height: BOARD, class: 'board-border',
      }));
    }

    /* ── right-click annotation (circles + arrows) ──────────────── */

    // Constants used by _renderOverlay ghost preview regardless of noAnnotations
    const USER_ANN_COLOR   = '#e6a000';
    const USER_ANN_OPACITY = 0.80;

    if (!noAnnotations) {
      svg.addEventListener('contextmenu', e => e.preventDefault());
      svg.addEventListener('mousedown', e => {
        if (e.button !== 2) return;
        e.preventDefault();
        _rcStart   = _sqFromEvent(e);
        _rcCurrent = _rcStart ? { ..._rcStart } : null;
        _renderOverlay();
      });
      svg.addEventListener('mousemove', e => {
        if (!_rcStart || !(e.buttons & 2)) return;
        const sq = _sqFromEvent(e);
        if (!sq) return;
        if (!_rcCurrent || sq.rank !== _rcCurrent.rank || sq.file !== _rcCurrent.file) {
          _rcCurrent = sq;
          _renderOverlay();
        }
      });
      svg.addEventListener('mouseup', e => {
        if (e.button !== 2 || !_rcStart) return;
        const end = _sqFromEvent(e) ?? _rcCurrent;
        _rcCurrent = null;
        if (!end) { _rcStart = null; _renderOverlay(); return; }
        if (end.rank === _rcStart.rank && end.file === _rcStart.file) {
          const key = `${end.rank},${end.file}`;
          const idx = _userCircles.findIndex(c => `${c.rank},${c.file}` === key);
          if (idx >= 0) _userCircles.splice(idx, 1);
          else _userCircles.push({ rank: end.rank, file: end.file, color: USER_ANN_COLOR, opacity: USER_ANN_OPACITY });
        } else {
          const uci = FILES[_rcStart.file] + (8 - _rcStart.rank) + FILES[end.file] + (8 - end.rank);
          const idx = _userArrows.findIndex(a => a.uci === uci);
          if (idx >= 0) _userArrows.splice(idx, 1);
          else _userArrows.push({ uci, color: USER_ANN_COLOR, opacity: USER_ANN_OPACITY, width: 7 });
        }
        _rcStart = null;
        _renderOverlay();
      });
      svg.addEventListener('mouseleave', () => {
        if (_rcStart) { _rcStart = null; _rcCurrent = null; _renderOverlay(); }
      });
      // Escape clears all user annotations (same as Lichess)
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && (_userArrows.length || _userCircles.length)) {
          _userArrows  = [];
          _userCircles = [];
          _renderOverlay();
        }
      });
    } // end if (!noAnnotations)

    /* ── main update ───────────────────────────────────────────────── */

    /**
     * Update the board to a new FEN, optionally animating a specific move.
     *
     * @param {string} fen
     * @param {object} opts
     * @param {boolean}  opts.flip        board orientation
     * @param {string}   opts.lastMove    our last move (UCI) — for square highlights
     * @param {string}   opts.oppMove     opponent's last move (UCI) — for square highlights
     * @param {Array}    opts.arrows      engine PV arrows [{ uci, color, opacity, width }]
     * @param {Array}    opts.circles     engine circles [{ rank, file, color, opacity }]
     * @param {boolean}  opts.inCheck
     * @param {string}   opts.animateUci  UCI move to animate (slide piece from→to)
     */
    function update(fen, opts = {}) {
      const {
        flip: newFlip = false, lastMove, oppMove,
        arrows = [], circles = [],
        inCheck = false, animateUci,
      } = opts;
      console.log('[Board.update] fen=%s animateUci=%s flip=%s', fen?.slice(0,30), animateUci ?? '—', newFlip);

      const { board: newGrid, turn } = parseFen(fen);
      const oldGrid     = grid;
      const flipChanged = newFlip !== flip;
      flip = newFlip;

      // Update overlay state for this frame
      _overlayArrows  = arrows;
      _overlayCircles = circles;

      // 1. Cancel in-flight animations — snap pieces to final positions
      _cancelAnims();

      // 2. Squares
      if (flipChanged) _layoutSq();
      _styleSq(newGrid, turn, lastMove, oppMove, inCheck);

      // 3. Pieces — animated move path or plain diff
      const handled = new Set();  // "r,f" keys already processed

      if (animateUci && !flipChanged) {
        const mv = parseUci(animateUci);
        if (mv) {
          _applyAnimatedMove(oldGrid, newGrid, mv, handled);
        }
      }

      // 4. General diff for any remaining squares
      _diffRemaining(oldGrid, newGrid, flipChanged, handled);

      // 5. Save grid
      grid = newGrid;

      // 6. Overlay
      _renderOverlay();
    }

    /* ── animated move logic ───────────────────────────────────────── */

    function _applyAnimatedMove(oldGrid, newGrid, mv, handled) {
      const fr = mv.from.rank, ff = mv.from.file;
      const tr = mv.to.rank,   tf = mv.to.file;
      const promo = mv.promo;
      const movingPiece = oldGrid[fr][ff];
      console.log('[Board._applyAnim] %s%d→%s%d piece=%s fromEntry=%s',
        'abcdefgh'[ff], 8-fr, 'abcdefgh'[tf], 8-tr,
        movingPiece || '(none)', !!pcMap.get(`${fr},${ff}`));
      if (!movingPiece) return;

      const fromKey = `${fr},${ff}`;
      const toKey   = `${tr},${tf}`;

      // Detect special moves
      const isKing      = movingPiece.toLowerCase() === 'k';
      const isPawn      = movingPiece.toLowerCase() === 'p';
      const ownRook     = movingPiece === 'K' ? 'R' : 'r';
      // Chess960: UCI sends king→rook-square (e.g. e1h1). Detect before any capture removal.
      const c960Castle  = isKing && oldGrid[tr][tf] === ownRook;
      const isCastle    = c960Castle || (isKing && Math.abs(tf - ff) >= 2);
      const isEp        = isPawn && ff !== tf && !oldGrid[tr][tf];

      // For Chess960 castling the king's actual landing square is g or c file, not tf.
      const kingDestF   = c960Castle ? (tf > ff ? 6 : 2) : tf;
      const kingDestKey = `${tr},${kingDestF}`;

      // ── Remove captured piece at destination ──
      // Skip removal when it's a Chess960 castle — king is landing on own rook, not capturing it.
      if (!c960Castle && pcMap.has(toKey) && toKey !== fromKey) {
        const cap = pcMap.get(toKey);
        // Quick fade-out for captured piece
        cap.node.style.opacity = '0';
        cap.node.style.transition = 'opacity 120ms';
        const capturedNode = cap.node;
        setTimeout(() => capturedNode.remove(), 130);
        pcMap.delete(toKey);
      }

      // ── En passant — remove captured pawn ──
      if (isEp) {
        const epKey = `${fr},${tf}`;
        const epEntry = pcMap.get(epKey);
        if (epEntry) {
          epEntry.node.style.opacity = '0';
          epEntry.node.style.transition = 'opacity 120ms';
          const epNode = epEntry.node;
          setTimeout(() => epNode.remove(), 130);
          pcMap.delete(epKey);
        }
        handled.add(epKey);
      }

      // ── Move the primary piece ──
      const fromEntry = pcMap.get(fromKey);
      handled.add(fromKey);
      handled.add(kingDestKey);  // king's actual landing square
      if (c960Castle) handled.add(toKey);  // rook's original square (handled by castling block)

      if (fromEntry) {
        pcMap.delete(fromKey);

        if (promo) {
          // Promotion: replace pawn with promoted piece at destination
          fromEntry.node.remove();
          const isWhite = movingPiece === movingPiece.toUpperCase();
          const promoPiece = isWhite ? promo.toUpperCase() : promo.toLowerCase();
          _addPc(promoPiece, tr, kingDestF);
          const newEntry = pcMap.get(kingDestKey);
          if (newEntry) _slidePc(newEntry, fr, ff, tr, kingDestF);
        } else {
          // Normal move — reuse the existing DOM node
          _posPc(fromEntry, tr, kingDestF);
          fromEntry.piece = newGrid[tr][kingDestF];
          pcMap.set(kingDestKey, fromEntry);
          _slidePc(fromEntry, fr, ff, tr, kingDestF);
        }
      } else {
        // No existing element at source (shouldn't happen, but safety net)
        _addPc(newGrid[tr][kingDestF], tr, kingDestF);
      }

      // ── Castling — slide the rook ──
      if (isCastle) {
        // For Chess960, rook starts at tf (the square the king UCI'd to).
        // For standard castling, rook starts at the corner (file 0 or 7).
        const rookFromF   = c960Castle ? tf : (tf > ff ? 7 : 0);
        const rookToF     = kingDestF > ff ? 5 : 3;
        const rookFromKey = `${fr},${rookFromF}`;
        const rookToKey   = `${fr},${rookToF}`;
        const rookEntry   = pcMap.get(rookFromKey);

        if (rookEntry) {
          pcMap.delete(rookFromKey);
          _posPc(rookEntry, fr, rookToF);
          rookEntry.piece = newGrid[fr][rookToF];
          pcMap.set(rookToKey, rookEntry);
          _slidePc(rookEntry, fr, rookFromF, fr, rookToF);
        }
        handled.add(rookFromKey);
        handled.add(rookToKey);
      }
    }

    /* ── plain diff for untouched squares ──────────────────────────── */

    function _diffRemaining(oldGrid, newGrid, flipChanged, handled) {
      for (let r = 0; r < 8; r++) {
        for (let f = 0; f < 8; f++) {
          const key = `${r},${f}`;
          if (handled.has(key)) continue;

          const newPc = newGrid[r][f];
          const entry = pcMap.get(key);
          const oldPc = entry ? entry.piece : '';

          if (oldPc === newPc && !flipChanged) continue;

          if (flipChanged && oldPc === newPc && entry) {
            // Same piece, just reposition for new flip
            _posPc(entry, r, f);
            continue;
          }

          // Piece changed (or appeared/disappeared) — rebuild
          if (entry) { entry.node.remove(); pcMap.delete(key); }
          if (newPc) _addPc(newPc, r, f);
        }
      }
    }

    /* ── refreshPieces — rebuild all piece images (e.g. after set change) ── */

    function refreshPieces() {
      for (const [key, entry] of pcMap) {
        const [r, f] = key.split(',').map(Number);
        entry.node.remove();
        const { x, y } = dxy(r, f, flip);
        const node = _makePiece(entry.piece, x, y, sqName(r, f));
        if (node) {
          gPc.appendChild(node);
          entry.node = node;
        }
      }
    }

    /* ── destroy ───────────────────────────────────────────────────── */

    function destroy() {
      _cancelAnims();
      svg.remove();
    }

    /* ── public instance ───────────────────────────────────────────── */

    return { svg, update, refreshPieces, destroy,
      /**
       * Update engine overlay arrows/circles and redraw.
       * Does NOT touch pieces or cancel animations — safe to call at any time.
       * @param {Array} arrows  [{ uci, color, opacity, width }]
       * @param {Array} circles [{ rank, file, color, opacity }]
       */
      setOverlay(arrows = [], circles = []) {
        _overlayArrows  = arrows;
        _overlayCircles = circles;
        _renderOverlay();
      },
      /** Remove all user right-click annotations and redraw. */
      clearUserAnnotations() {
        _userArrows  = [];
        _userCircles = [];
        _renderOverlay();
      },
      /** Map a pointer event to { rank, file } on the board. Returns null if outside. */
      squareAt(e) { return _sqFromEvent(e); },
    };
  }

  /* ══════════════════════════════════════════════════════════════════════
     Legacy Board.render() — full teardown/rebuild, no animation.
     Kept for backward compatibility or one-off static boards.
     ══════════════════════════════════════════════════════════════════════ */

  function render(svg, fen, opts = {}) {
    const { flip = false, lastMove, oppMove, bestMove, inCheck = false } = opts;
    svg.innerHTML = '';
    svg.setAttribute('viewBox', `0 0 ${BOARD} ${BOARD}`);

    const { board, turn } = parseFen(fen);
    const lastP  = parseUci(lastMove);
    const oppP   = parseUci(oppMove);
    const checkK = inCheck ? findKing(board, turn) : null;

    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        const dr = flip ? 7 - r : r;
        const df = flip ? 7 - f : f;
        const x = df * SQ, y = dr * SQ;
        const isLight = (r + f) % 2 === 0;
        let cls = isLight ? 'sq-light' : 'sq-dark';
        if (oppP) {
          if (r === oppP.from.rank && f === oppP.from.file) cls = 'sq-opp-src';
          if (r === oppP.to.rank   && f === oppP.to.file)   cls = 'sq-opp-dst';
        }
        if (lastP) {
          if (r === lastP.from.rank && f === lastP.from.file) cls = 'sq-last-src';
          if (r === lastP.to.rank   && f === lastP.to.file)   cls = 'sq-last-dst';
        }
        if (checkK && r === checkK.rank && f === checkK.file) cls = 'sq-check';
        svg.appendChild(svgEl('rect', { x, y, width: SQ, height: SQ, class: cls }));
      }
    }

    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        const dr = flip ? 7 - r : r;
        const df = flip ? 7 - f : f;
        const x = df * SQ, y = dr * SQ;
        const piece = board[r][f];
        const sq = FILES[f] + (8 - r);
        const node = _makePiece(piece, x, y, sq);
        if (node) svg.appendChild(node);
      }
    }

    // Coordinates
    for (let i = 0; i < 8; i++) {
      const fi = flip ? 7 - i : i;
      const ri = flip ? 7 - i : i;
      const fl = svgEl('text', {
        x: fi * SQ + SQ - 4, y: BOARD - 3,
        class: `coord ${(7 + i) % 2 === 0 ? 'coord-light' : 'coord-dark'}`,
        'text-anchor': 'end',
      });
      fl.textContent = FILES[i];
      svg.appendChild(fl);
      const rl = svgEl('text', {
        x: 3, y: ri * SQ + 11,
        class: `coord ${(i) % 2 === 0 ? 'coord-dark' : 'coord-light'}`,
      });
      rl.textContent = String(8 - i);
      svg.appendChild(rl);
    }

    // Best-move arrow
    if (bestMove) {
      const bm = parseUci(bestMove);
      if (bm) {
        const fromR = flip ? 7 - bm.from.rank : bm.from.rank;
        const fromF = flip ? 7 - bm.from.file : bm.from.file;
        const toR   = flip ? 7 - bm.to.rank   : bm.to.rank;
        const toF   = flip ? 7 - bm.to.file   : bm.to.file;
        const x1 = fromF * SQ + SQ / 2, y1 = fromR * SQ + SQ / 2;
        const x2 = toF   * SQ + SQ / 2, y2 = toR   * SQ + SQ / 2;
        svg.appendChild(svgEl('line', { x1, y1, x2, y2, class: 'best-move-arrow' }));
        const angle = Math.atan2(y2 - y1, x2 - x1);
        const hl = 12;
        svg.appendChild(svgEl('polygon', {
          points: `${x2},${y2} `
            + `${x2 - hl * Math.cos(angle - 0.4)},${y2 - hl * Math.sin(angle - 0.4)} `
            + `${x2 - hl * Math.cos(angle + 0.4)},${y2 - hl * Math.sin(angle + 0.4)}`,
          class: 'best-move-arrow-head',
        }));
      }
    }

    svg.appendChild(svgEl('rect', {
      x: 0, y: 0, width: BOARD, height: BOARD, class: 'board-border',
    }));
  }

  /* ── shared settings ───────────────────────────────────────────────── */

  function setPieceSet(name) {
    _pieceSet = name;
    localStorage.setItem('board-piece-set', name);
  }
  function setPieceStyle(name) {
    _pieceStyle = name;
    localStorage.setItem('board-piece-style', name);
    document.documentElement.dataset.pieceStyle = name;
  }
  function setTheme(name) {
    _theme = name;
    localStorage.setItem('board-theme', name);
    document.documentElement.dataset.boardTheme = name;
  }
  /**
   * Inject/update CSS custom properties for the 'custom' board theme.
   * Derives highlight colours by blending the two square colours together.
   */
  function _applyCustomTheme(light, dark) {
    let el = document.getElementById('board-custom-theme-vars');
    if (!el) {
      el = document.createElement('style');
      el.id = 'board-custom-theme-vars';
      document.head.appendChild(el);
    }
    // Simple highlight: blend light+dark 50% for source, add a touch of yellow for dest
    el.textContent = `:root {
  --custom-sq-light: ${light};
  --custom-sq-dark:  ${dark};
  --custom-sq-ls:    ${_blendHex(light, dark, 0.5)};
  --custom-sq-ld:    ${_blendHex(light, '#e8e048', 0.35)};
}`;
  }
  function setCustomTheme(light, dark) {
    localStorage.setItem('board-custom-light', light);
    localStorage.setItem('board-custom-dark',  dark);
    _applyCustomTheme(light, dark);
  }
  function getCustomColors() {
    return {
      light: localStorage.getItem('board-custom-light') || '#eeeed2',
      dark:  localStorage.getItem('board-custom-dark')  || '#769656',
    };
  }
  /** Blend two hex colours. t=0 → a, t=1 → b. */
  function _blendHex(a, b, t) {
    const pa = _parseHex(a), pb = _parseHex(b);
    const r  = Math.round(pa[0] + (pb[0] - pa[0]) * t);
    const g  = Math.round(pa[1] + (pb[1] - pa[1]) * t);
    const bl = Math.round(pa[2] + (pb[2] - pa[2]) * t);
    return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${bl.toString(16).padStart(2,'0')}`;
  }
  function _parseHex(hex) {
    const h = hex.replace('#','');
    return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
  }
  function getPieceSet()   { return _pieceSet; }
  function getPieceStyle() { return _pieceStyle; }
  function getTheme()      { return _theme; }

  /* ── public module ─────────────────────────────────────────────────── */

  return {
    create, render,
    parseFen, parseUci,
    setPieceSet, setPieceStyle, setTheme, setCustomTheme,
    getPieceSet, getPieceStyle, getTheme, getCustomColors,
  };

})();
