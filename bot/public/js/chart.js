/**
 * chart.js — Eval sparkline with confidence band overlay.
 *
 * Chart.renderEval(wrap, game)       — full sparkline with band + dots
 * Chart.renderMini(wrap, game, w, h) — tiny inline sparkline
 */
'use strict';

const Chart = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const CP_MAX = 300;

  function _extractPoints(game) {
    const moves = game.moves || [];
    const isBlack = game.color === 'black';
    const pts = [];
    for (let i = 0; i < moves.length; i++) {
      const m = moves[i];
      let cp = 0;
      if (m.mate != null) cp = m.mate > 0 ? CP_MAX : -CP_MAX;
      else if (m.eval_cp != null) cp = m.eval_cp;
      if (isBlack) cp = -cp;
      cp = Math.max(-CP_MAX, Math.min(CP_MAX, cp));
      pts.push({ ply: m.ply ?? (i + 1), cp, conf: m.confidence ?? null, isMate: m.mate != null });
    }
    return pts;
  }

  function _el(tag, attrs = {}) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  function renderEval(wrap, game) {
    const W = 1000, H = 60, PAD = 4;
    const pts = _extractPoints(game);
    if (pts.length < 2) { wrap.innerHTML = ''; return; }

    const xStep = (W - 2 * PAD) / Math.max(pts.length - 1, 1);
    const cpToY = cp => PAD + (H - 2 * PAD) * (1 - (cp + CP_MAX) / (2 * CP_MAX));
    const zeroY = cpToY(0);

    const svg = _el('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none',
                              width: '100%', height: '100%' });
    svg.style.display = 'block';

    // Background halves
    svg.appendChild(_el('rect', { x: 0, y: 0, width: W, height: zeroY, fill: 'rgba(255,255,255,0.02)' }));
    svg.appendChild(_el('rect', { x: 0, y: zeroY, width: W, height: H - zeroY, fill: 'rgba(0,0,0,0.08)' }));

    // Zero line
    svg.appendChild(_el('line', { x1: PAD, y1: zeroY, x2: W - PAD, y2: zeroY, class: 'sparkline-zero' }));

    // Confidence band
    if (pts.filter(p => p.conf != null).length >= 2) {
      const upper = [], lower = [];
      for (let i = 0; i < pts.length; i++) {
        const x = PAD + i * xStep, baseY = cpToY(pts[i].cp);
        const conf = pts[i].conf ?? 0.5;
        const bh = 2 + (1 - conf) * 18;
        upper.push(`${x.toFixed(1)},${Math.max(PAD, baseY - bh).toFixed(1)}`);
        lower.push(`${x.toFixed(1)},${Math.min(H - PAD, baseY + bh).toFixed(1)}`);
      }
      svg.appendChild(_el('polygon', { points: [...upper, ...lower.reverse()].join(' '), class: 'sparkline-conf-band' }));
    }

    // Area + line
    const areaCoords = [`${PAD},${zeroY.toFixed(1)}`, ...pts.map((p, i) => `${(PAD + i * xStep).toFixed(1)},${cpToY(p.cp).toFixed(1)}`), `${(PAD + (pts.length - 1) * xStep).toFixed(1)},${zeroY.toFixed(1)}`].join(' ');
    svg.appendChild(_el('polygon', { points: areaCoords, class: 'sparkline-area' }));
    const lineCoords = pts.map((p, i) => `${(PAD + i * xStep).toFixed(1)},${cpToY(p.cp).toFixed(1)}`).join(' ');
    svg.appendChild(_el('polyline', { points: lineCoords, class: 'sparkline-line' }));

    // Mate indicators
    for (let i = 0; i < pts.length; i++) {
      if (pts[i].isMate) {
        const x = PAD + i * xStep, y = cpToY(pts[i].cp);
        svg.appendChild(_el('line', { x1: x, y1: y, x2: x, y2: y < zeroY ? PAD : H - PAD, class: 'sparkline-mate' }));
      }
    }

    // Dots
    for (let i = 0; i < pts.length; i++) {
      const x = PAD + i * xStep, y = cpToY(pts[i].cp);
      svg.appendChild(_el('circle', { cx: x.toFixed(1), cy: y.toFixed(1), r: '1.8', class: 'sparkline-dot' }));
    }

    // Last dot
    if (pts.length > 0) {
      const last = pts[pts.length - 1];
      svg.appendChild(_el('circle', { cx: (PAD + (pts.length - 1) * xStep).toFixed(1), cy: cpToY(last.cp).toFixed(1), r: '3', class: 'sparkline-dot-last' }));
    }

    wrap.innerHTML = '';
    wrap.appendChild(svg);
  }

  function renderMini(wrap, game, w = 120, h = 24) {
    const PAD = 1;
    const pts = _extractPoints(game);
    if (pts.length < 2) { wrap.innerHTML = ''; return; }
    const xStep = (w - 2 * PAD) / Math.max(pts.length - 1, 1);
    const cpToY = cp => PAD + (h - 2 * PAD) * (1 - (cp + CP_MAX) / (2 * CP_MAX));
    const zeroY = cpToY(0);
    const lineCoords = pts.map((p, i) => `${(PAD + i * xStep).toFixed(1)},${cpToY(p.cp).toFixed(1)}`).join(' ');
    const areaCoords = [`${PAD},${zeroY.toFixed(1)}`, ...pts.map((p, i) => `${(PAD + i * xStep).toFixed(1)},${cpToY(p.cp).toFixed(1)}`), `${(PAD + (pts.length - 1) * xStep).toFixed(1)},${zeroY.toFixed(1)}`].join(' ');
    wrap.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:${h}px;display:block;">
      <line x1="${PAD}" y1="${zeroY}" x2="${w - PAD}" y2="${zeroY}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 2"/>
      <polygon points="${areaCoords}" fill="var(--accent)" opacity="0.1"/>
      <polyline points="${lineCoords}" fill="none" stroke="var(--accent)" stroke-width="1.2" stroke-linejoin="round"/>
    </svg>`;
  }

  return { renderEval, renderMini };
})();
