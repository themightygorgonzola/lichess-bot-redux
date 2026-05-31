/**
 * tab-controls.js — Live policy + personality controls.
 *
 * Renders into the 'Ctrl' comms sub-pane inside the Game tab.
 * All controls patch /api/config or /api/personality in real-time.
 *
 * Bug-fixes vs previous version:
 *   • SEARCH_SLIDERS were rendered but never bound — now included in binding loop.
 *   • drawOfferMinPly was in DEFAULTS but had no UI control — added.
 *   • Environment section now shows NNUE vs HCE mode derived from ENGINE_PATH.
 *   • resignMateThreshold fmt correctly shows "–M1" at the top end.
 */
'use strict';

const TabControls = (() => {

  /* ── Control definitions ────────────────────────────────────────────────
     One entry per knob. Adding a control = add here, nothing else changes.
  ───────────────────────────────────────────────────────────────────────── */

  const TIME_SLIDERS = [
    {
      key: 'thoughtfulness', label: 'Thoughtfulness',
      sub: '0 = impatient (shallow depth target, low confidence bar, 60% of fair time share). 1 = patient (deep depth target, high confidence bar, 140% of fair share). Acts as a depth multiplier.',
      min: 0, max: 1, step: 0.05, fmt: v => v.toFixed(2),
    },
    {
      key: 'minMovetimeMs', label: 'Min move time',
      sub: 'Absolute floor on every single move. Neither the budget ceiling, the confidence gate, nor anything else can make the bot play faster than this.',
      min: 50, max: 2000, step: 50, fmt: v => `${v} ms`,
    },
  ];

  const SEARCH_SLIDERS = [
    {
      key: 'evalInfluence', label: 'Eval influence',
      sub: 'How aggressively a winning eval lowers the confidence stop threshold. 0 = flat. 1 = exit sooner when clearly winning. No effect when losing.',
      min: 0, max: 1, step: 0.05, fmt: v => v.toFixed(2),
    },
  ];

  const DRAW_SLIDERS = [
    {
      key: 'drawAcceptCpMax', label: 'Accept draw ≤',
      sub: "Accept opponent's draw offer when |eval| is within this many centipawns of zero. Always accepts when we are being mated.",
      min: 0, max: 200, step: 5, fmt: v => `${v} cp`,
    },
    {
      key: 'drawOfferCpMax', label: 'Offer draw ≤',
      sub: 'Send a draw offer when |eval| stays within this range for the required streak of moves.',
      min: 0, max: 100, step: 5, fmt: v => `${v} cp`,
    },
    {
      key: 'drawOfferStreakMoves', label: 'Offer streak',
      sub: 'Consecutive equal-eval moves required before sending a draw offer.',
      min: 2, max: 10, step: 1, fmt: v => `${v} mv`,
    },
    {
      key: 'drawOfferMinPly', label: 'Earliest offer',
      sub: 'Never offer a draw before this half-move (ply). 60 = move 30 per side.',
      min: 0, max: 120, step: 4, fmt: v => `ply ${v}`,
    },
  ];

  const RESIGN_SLIDERS = [
    {
      key: 'resignMateThreshold', label: 'Resign at',
      sub: 'Resign when engine detects forced mate against us within this many moves. –M1 = effectively never resigns.',
      min: -20, max: -1, step: 1, fmt: v => `–M${Math.abs(v)}`,
    },
  ];

  const CONDUCT_TOGGLES = [
    { key: 'allowRated',        label: 'Allow rated',          sub: 'Accept challenges that affect both players\' ratings.' },
    { key: 'allowCasual',       label: 'Allow casual',         sub: 'Accept unrated / casual challenges.' },
    { key: 'acceptTakebacks',   label: 'Accept takebacks',     sub: 'Grant opponents\' takeback requests.' },
    { key: 'claimVictoryOnGone',label: 'Claim win on abandon', sub: 'Immediately claim victory when Lichess confirms the opponent abandoned.' },
  ];

  const BOARD_THEMES = [
    { id: 'classic',  name: 'Classic',  light: '#b8c0b0', dark: '#6b8e5e' },
    { id: 'brown',    name: 'Brown',    light: '#f0d9b5', dark: '#b58863' },
    { id: 'tan',      name: 'Tan',      light: '#e8dcc8', dark: '#a07850' },
    { id: 'coral',    name: 'Coral',    light: '#f8e8e0', dark: '#c07060' },
    { id: 'lemon',    name: 'Lemon',    light: '#f4f0b0', dark: '#8aaa50' },
    { id: 'ic',       name: 'IC',       light: '#ececec', dark: '#c1c18e' },
    { id: 'blue',     name: 'Blue',     light: '#dee3e6', dark: '#8ca2ad' },
    { id: 'navy',     name: 'Navy',     light: '#c8d8f0', dark: '#2c4880' },
    { id: 'purple',   name: 'Purple',   light: '#e5d8f0', dark: '#7c5cb4' },
    { id: 'rose',     name: 'Rose',     light: '#f0d8d8', dark: '#b85858' },
    { id: 'olive',    name: 'Olive',    light: '#e8e4b0', dark: '#7b9e5e' },
    { id: 'forest',   name: 'Forest',   light: '#b8d0b0', dark: '#3a5c3a' },
    { id: 'midnight', name: 'Midnight', light: '#394562', dark: '#242b3d' },
    { id: 'obsidian', name: 'Obsidian', light: '#48404e', dark: '#201c28' },
    { id: 'mocha',    name: 'Mocha',    light: '#d0b898', dark: '#6c4830' },
    { id: 'custom',   name: 'Custom',   light: null,      dark: null      },
  ];

  const PIECE_SETS   = [{ id: 'cburnett', name: 'CBurnett' }, { id: 'unicode', name: 'Unicode' }];

  const ALL_SLIDERS = [...TIME_SLIDERS, ...SEARCH_SLIDERS, ...DRAW_SLIDERS, ...RESIGN_SLIDERS];

  /* Arrow annotation prefs — localStorage only, no server round-trip */
  const ARROW_PREFS_KEY  = 'hb-arrow-prefs';
  const ARROW_DEFAULTS   = { depth: 1, colorOurs: '#22c55e', colorOpp: '#3b82f6', opacity: 0.72 };
  function _getArrowPrefs() {
    try { return { ...ARROW_DEFAULTS, ...JSON.parse(localStorage.getItem(ARROW_PREFS_KEY) || '{}') }; }
    catch { return { ...ARROW_DEFAULTS }; }
  }
  function _setArrowPref(patch) {
    const cur = _getArrowPrefs();
    localStorage.setItem(ARROW_PREFS_KEY, JSON.stringify({ ...cur, ...patch }));
    if (typeof TabGame !== 'undefined') TabGame.refreshArrows();
  }

  /* ── State ────────────────────────────────────────────────────────────── */

  let _config   = null;   // last-fetched server config
  let _policies = {};     // server-confirmed policy values (source of truth for UI)
  let _version  = null;   // { build, date, commit } from /api/version

  const LS_KEY = 'hb-ctrl-v3';

  function _writeCache() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(_policies)); } catch {}
  }
  function _readCache() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || 'null') || {}; } catch { return {}; }
  }

  /* ── Lifecycle ────────────────────────────────────────────────────────── */

  async function show() {
    const el = document.getElementById('comms-pane-ctrl');
    if (!el) return;

    if (Object.keys(_policies).length) {
      // Already fetched at least once — render from trusted in-memory state
      // immediately (no stale-cache flash) then re-confirm in background.
      _render(el);
    } else {
      // First visit: paint from localStorage cache while the fetch is in flight
      // so the pane isn't blank.
      const cache = _readCache();
      if (Object.keys(cache).length) {
        _policies = cache;
        if (!_config) _config = { policies: cache, env: {}, personality: {} };
        _render(el);
      }
    }

    // Fetch authoritative values from server (includes live overrides persisted
    // to dash-state.json, so this is always the ground truth).
    try {
      const [cfgRes, persRes, verRes] = await Promise.all([fetch('/api/config'), fetch('/api/personality'), fetch('/api/version')]);
      _config = await cfgRes.json();
      _config.personality = await persRes.json();
      _version = verRes.ok ? await verRes.json() : null;
      _policies = { ...(_config.policies || {}) };
      _writeCache();
      _render(el);
    } catch {
      if (!_config) {
        el.innerHTML = '<div class="c2-error">Could not reach server. Showing cached values.</div>';
      }
    }
  }

  function onEvent() {}

  /* ── Render ───────────────────────────────────────────────────────────── */

  function _render(el) {
    if (!_config) { el.innerHTML = '<div class="c2-error">No config available.</div>'; return; }

    const { env = {} } = _config;
    const mode         = _config.personality?.mode ?? 'full';
    const savedTheme   = Board.getTheme();
    const savedSet     = Board.getPieceSet();
    const customCols   = Board.getCustomColors ? Board.getCustomColors() : { light: '#eeeed2', dark: '#769656' };
    const isCustom     = savedTheme === 'custom';

    // Engine / eval environment
    const enginePath = env.ENGINE_PATH || '';
    const evalFile   = env.EVAL_FILE   || '';
    const exeName    = enginePath ? enginePath.split(/[\\/]/).pop() : '–';
    const isHce      = exeName.toLowerCase().includes('hce');
    const nnueTag    = isHce ? '<span class="c2-tag warn">HCE</span>'
                     : evalFile ? '<span class="c2-tag ok">NNUE</span>'
                     : '<span class="c2-tag warn">NNUE?</span>';

    const sec = (title, body) =>
      `<div class="c2-sec"><div class="c2-sec-hdr">${title}</div>${body}</div>`;

    const slider = s => {
      const v = _policies[s.key] ?? s.min;
      const pct = ((v - s.min) / (s.max - s.min) * 100).toFixed(1);
      return `<div class="c2-row" title="${App.esc(s.sub)}">` +
        `<span class="c2-lbl">${App.esc(s.label)}</span>` +
        `<input type="range" class="c2-range" id="c2-${s.key}" ` +
        `min="${s.min}" max="${s.max}" step="${s.step}" value="${v}" ` +
        `style="--pct:${pct}%"/>` +
        `<span class="c2-val" id="c2v-${s.key}">${s.fmt(v)}</span>` +
        `<span class="c2-dot c2-dot-ok" id="c2d-${s.key}"></span>` +
        `</div>`;
    };

    const toggle = t => {
      const on = !!_policies[t.key];
      return `<div class="c2-row c2-tog-row" title="${App.esc(t.sub)}">` +
        `<span class="c2-lbl c2-lbl-grow">${App.esc(t.label)}</span>` +
        `<span class="c2-dot" id="c2d-${t.key}"></span>` +
        `<div class="c2-toggle${on ? ' on' : ''}" id="c2t-${t.key}" data-key="${t.key}"></div>` +
        `</div>`;
    };

    // Board swatches
    const swatches = BOARD_THEMES.map(t => {
      const l = t.id === 'custom' ? customCols.light : t.light;
      const d = t.id === 'custom' ? customCols.dark  : t.dark;
      return `<button class="c2-swatch${savedTheme === t.id ? ' active' : ''}" ` +
             `data-theme="${t.id}" title="${t.name}" style="--sl:${l};--sd:${d}"></button>`;
    }).join('');

    const pieceBtns = PIECE_SETS.map(s =>
      `<button class="c2-seg-btn${savedSet === s.id ? ' active' : ''}" data-set="${s.id}">${s.name}</button>`
    ).join('');

    const bv = _version || (App.getBuildVersion ? App.getBuildVersion() : null);
    const buildTag = (bv && bv.build != null)
      ? `<span class="c2-env-val">#${bv.build}</span>`
      : `<span class="c2-env-val" style="opacity:.4">–</span>`;

    const envRows = [
      ['build',   buildTag],
      ['engine',  `<span class="c2-env-val">${App.esc(exeName)}</span>`],
      ['eval',    nnueTag],
      ['threads', `<span class="c2-env-val">${App.esc(env.ENGINE_THREADS || '1')}</span>`],
      ['hash',    `<span class="c2-env-val">${App.esc(env.ENGINE_HASH || '128')} MB</span>`],
    ];
    if (env.ENGINE_MOVETIME) envRows.push(['movetime', `<span class="c2-env-val">${env.ENGINE_MOVETIME} ms</span>`]);

    el.innerHTML = [
      sec('Time & Search',
        [...TIME_SLIDERS, ...SEARCH_SLIDERS].map(slider).join('')
      ),
      sec('Draw Policy',
        DRAW_SLIDERS.map(slider).join('')
      ),
      sec('Resign',
        RESIGN_SLIDERS.map(slider).join('')
      ),
      sec('Conduct',
        CONDUCT_TOGGLES.map(toggle).join('')
      ),
      sec('Personality',
        `<div class="c2-row c2-seg-row">` +
        `<div class="c2-seg" id="c2-mode-seg">` +
        `<button class="c2-seg-btn${mode === 'silent' ? ' active' : ''}" data-mode="silent">Silent</button>` +
        `<button class="c2-seg-btn${mode === 'full'   ? ' active' : ''}" data-mode="full">Full</button>` +
        `</div></div>` +
        `<div class="c2-hint" id="c2-mode-hint">${_modeHint(mode)}</div>`
      ),
      sec('Board',
        `<div class="c2-swatch-wrap" id="c2-swatches">${swatches}</div>` +
        `<div class="c2-custom-row" id="c2-custom-row"${isCustom ? '' : ' style="display:none"'}>` +
        `<label class="c2-custom-label"><span>Light</span><input type="color" id="c2-custom-light" value="${customCols.light}" class="c2-color-pick"/></label>` +
        `<label class="c2-custom-label"><span>Dark</span><input type="color" id="c2-custom-dark" value="${customCols.dark}" class="c2-color-pick"/></label>` +
        `</div>` +
        `<div class="c2-board-row"><span class="c2-sub-lbl">Set</span><div class="c2-seg" id="c2-piece-btns">${pieceBtns}</div></div>`
      ),
      _renderSoundSection(),
      _renderArrowsSection(),
      sec('Environment',
        envRows.map(([k, v]) =>
          `<div class="c2-env-row"><span class="c2-env-key">${k}</span>${v}</div>`
        ).join('')
      ),
    ].join('');

    _bindEvents(mode);
  }

  function _modeHint(m) {
    return m === 'silent'
      ? 'No chat messages — clean competitive mode.'
      : 'H-035 persona: greetings, trash-talk, leet-speak.';
  }

  function _renderSoundSection() {
    const on = typeof Sound !== 'undefined' ? Sound.enabled : true;
    return `<div class="c2-sec"><div class="c2-sec-hdr">Sound</div>` +
      `<div class="c2-row c2-seg-row">` +
        `<span class="c2-lbl c2-lbl-grow">Effects</span>` +
        `<div class="c2-seg" id="c2-sound-onoff">` +
          `<button class="c2-seg-btn${on  ? ' active' : ''}" data-snd-on="1">On</button>` +
          `<button class="c2-seg-btn${!on ? ' active' : ''}" data-snd-on="0">Off</button>` +
        `</div>` +
      `</div>` +
      `<div class="c2-row c2-seg-row">` +
        `<span class="c2-lbl c2-lbl-grow">Preview</span>` +
        `<div class="c2-seg" id="c2-sound-preview">` +
          `<button class="c2-seg-btn" data-preview="move-white" title="White move">♙ mv</button>` +
          `<button class="c2-seg-btn" data-preview="move-black" title="Black move">♟ mv</button>` +
          `<button class="c2-seg-btn" data-preview="cap-white"  title="White capture">♙ cap</button>` +
          `<button class="c2-seg-btn" data-preview="cap-black"  title="Black capture">♟ cap</button>` +
        `</div>` +
      `</div>` +
      `</div>`;
  }

  function _renderArrowsSection() {
    const ap = _getArrowPrefs();
    const opPct = ((ap.opacity - 0.1) / 0.9 * 100).toFixed(1);
    const depthBtns = [0, 1, 2, 3].map(d =>
      `<button class="c2-seg-btn${ap.depth === d ? ' active' : ''}" data-depth="${d}">${d === 0 ? 'Off' : d}</button>`
    ).join('');
    return `<div class="c2-sec"><div class="c2-sec-hdr">Arrows</div>` +
      `<div class="c2-row c2-seg-row">` +
        `<span class="c2-lbl c2-lbl-grow">Depth</span>` +
        `<div class="c2-seg" id="c2-arrow-depth">${depthBtns}</div>` +
      `</div>` +
      `<div class="c2-row c2-arrow-color-row">` +
        `<span class="c2-lbl">Ours</span>` +
        `<input type="color" id="c2-arrow-color-ours" value="${ap.colorOurs}" class="c2-color-pick"/>` +
        `<span class="c2-lbl c2-lbl-gap">Opp</span>` +
        `<input type="color" id="c2-arrow-color-opp" value="${ap.colorOpp}" class="c2-color-pick"/>` +
      `</div>` +
      `<div class="c2-row" title="Base opacity for ply-1 arrow; deeper plies are automatically dimmed">` +
        `<span class="c2-lbl">Opacity</span>` +
        `<input type="range" class="c2-range" id="c2-arrow-opacity" min="0.1" max="1.0" step="0.05" value="${ap.opacity}" style="--pct:${opPct}%"/>` +
        `<span class="c2-val" id="c2v-arrow-opacity">${Math.round(ap.opacity * 100)}%</span>` +
      `</div>` +
      `</div>`;
  }

  /* ── Event binding ────────────────────────────────────────────────────── */

  function _bindEvents(initialMode) {
    // Sliders: live preview on input, commit+confirm on change
    for (const s of ALL_SLIDERS) {
      const input = document.getElementById(`c2-${s.key}`);
      const valEl = document.getElementById(`c2v-${s.key}`);
      if (!input) continue;
      input.addEventListener('input', () => {
        valEl.textContent = s.fmt(parseFloat(input.value));
        _setDot(s.key, 'pending');
        // Update CSS fill percentage for custom track fill
        const pct = ((parseFloat(input.value) - s.min) / (s.max - s.min) * 100).toFixed(1);
        input.style.setProperty('--pct', pct + '%');
      });
      input.addEventListener('change', () => {
        _patchKey(s.key, parseFloat(input.value), s.fmt);
      });
    }

    // Toggles
    for (const t of CONDUCT_TOGGLES) {
      const el = document.getElementById(`c2t-${t.key}`);
      if (!el) continue;
      el.addEventListener('click', () => {
        el.classList.toggle('on');
        _patchKey(t.key, el.classList.contains('on'), null);
      });
    }

    // Personality segment
    document.getElementById('c2-mode-seg')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-mode]');
      if (!btn) return;
      const m = btn.dataset.mode;
      document.querySelectorAll('#c2-mode-seg .c2-seg-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.mode === m));
      const hint = document.getElementById('c2-mode-hint');
      if (hint) hint.textContent = _modeHint(m);
      _patchMode(m);
    });

    // Board theme swatches
    document.getElementById('c2-swatches')?.addEventListener('click', e => {
      const btn = e.target.closest('.c2-swatch');
      if (!btn) return;
      const id = btn.dataset.theme;
      Board.setTheme(id);
      document.querySelectorAll('#c2-swatches .c2-swatch').forEach(b =>
        b.classList.toggle('active', b.dataset.theme === id));
      const cr = document.getElementById('c2-custom-row');
      if (cr) cr.style.display = id === 'custom' ? 'flex' : 'none';
    });

    const _updateCustom = () => {
      const l = document.getElementById('c2-custom-light')?.value;
      const d = document.getElementById('c2-custom-dark')?.value;
      if (!l || !d) return;
      const csw = document.querySelector('#c2-swatches .c2-swatch[data-theme="custom"]');
      if (csw) { csw.style.setProperty('--sl', l); csw.style.setProperty('--sd', d); }
      Board.setCustomTheme(l, d);
    };
    document.getElementById('c2-custom-light')?.addEventListener('input', _updateCustom);
    document.getElementById('c2-custom-dark')?.addEventListener('input', _updateCustom);

    // Piece set / style
    document.getElementById('c2-piece-btns')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-set]');
      if (!btn) return;
      Board.setPieceSet(btn.dataset.set);
      document.querySelectorAll('#c2-piece-btns .c2-seg-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.set === btn.dataset.set));
      // refreshPieces() is on the board instance — TabGame.redraw() calls it
      if (typeof TabGame !== 'undefined') TabGame.redraw();
    });

    // Sound on/off + preview
    document.getElementById('c2-sound-onoff')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-snd-on]');
      if (!btn || typeof Sound === 'undefined') return;
      const wantOn = btn.dataset.sndOn === '1';
      if (wantOn !== Sound.enabled) Sound.toggle();
      document.querySelectorAll('#c2-sound-onoff .c2-seg-btn').forEach(b =>
        b.classList.toggle('active', (b.dataset.sndOn === '1') === wantOn));
    });
    document.getElementById('c2-sound-preview')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-preview]');
      if (!btn || typeof Sound === 'undefined') return;
      // Ensure AudioContext is running — button click is a user gesture
      if (Sound._ensureCtx) Sound._ensureCtx();
      const p = btn.dataset.preview;
      setTimeout(() => {
        if (p === 'move-white') Sound.move(true);
        if (p === 'move-black') Sound.move(false);
        if (p === 'cap-white')  Sound.capture(true);
        if (p === 'cap-black')  Sound.capture(false);
      }, 20);
    });

    // Arrow prefs
    document.getElementById('c2-arrow-depth')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-depth]');
      if (!btn) return;
      const d = parseInt(btn.dataset.depth, 10);
      _setArrowPref({ depth: d });
      document.querySelectorAll('#c2-arrow-depth .c2-seg-btn').forEach(b =>
        b.classList.toggle('active', parseInt(b.dataset.depth, 10) === d));
    });
    document.getElementById('c2-arrow-color-ours')?.addEventListener('input', e => {
      _setArrowPref({ colorOurs: e.target.value });
    });
    document.getElementById('c2-arrow-color-opp')?.addEventListener('input', e => {
      _setArrowPref({ colorOpp: e.target.value });
    });
    const arrowOpEl  = document.getElementById('c2-arrow-opacity');
    const arrowOpVal = document.getElementById('c2v-arrow-opacity');
    arrowOpEl?.addEventListener('input', () => {
      const v = parseFloat(arrowOpEl.value);
      _setArrowPref({ opacity: v });
      if (arrowOpVal) arrowOpVal.textContent = Math.round(v * 100) + '%';
      const pct = ((v - 0.1) / 0.9 * 100).toFixed(1);
      arrowOpEl.style.setProperty('--pct', pct + '%');
    });
  }

  /* ── Status dot ───────────────────────────────────────────────────────── */

  function _setDot(key, state) {
    const el = document.getElementById(`c2d-${key}`);
    if (el) el.className = `c2-dot c2-dot-${state}`;
  }

  /* ── API ──────────────────────────────────────────────────────────────── */

  /**
   * POST a single policy key. On success, update the control to the server's
   * actually-applied value (guards against server-side clamping) and persist
   * to localStorage. On failure, revert the control to the last-good value.
   */
  async function _patchKey(key, value, fmtFn) {
    _setDot(key, 'pending');
    const lastGood = _policies[key];
    try {
      const res  = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value }),
      });
      const data = await res.json();
      // Server may have clamped — use its confirmed value as truth
      const confirmed = (data.applied?.[key] !== undefined) ? data.applied[key] : value;
      _policies[key] = confirmed;
      _writeCache();
      // Update slider/display to server-confirmed value
      if (fmtFn) {
        const input = document.getElementById(`c2-${key}`);
        const valEl = document.getElementById(`c2v-${key}`);
        if (input) {
          input.value = confirmed;
          const pct = ((confirmed - +input.min) / (+input.max - +input.min) * 100).toFixed(1);
          input.style.setProperty('--pct', pct + '%');
        }
        if (valEl) valEl.textContent = fmtFn(confirmed);
      }
      _setDot(key, 'ok');
    } catch {
      _setDot(key, 'err');
      // Revert to last-good value if available
      if (fmtFn && lastGood !== undefined) {
        const input = document.getElementById(`c2-${key}`);
        const valEl = document.getElementById(`c2v-${key}`);
        if (input) input.value = lastGood;
        if (valEl) valEl.textContent = fmtFn(lastGood);
      }
      setTimeout(() => _setDot(key, lastGood !== undefined ? 'ok' : ''), 2500);
    }
  }

  async function _patchMode(mode) {
    try {
      await fetch('/api/personality/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
    } catch {}
  }

  /* ── Register ─────────────────────────────────────────────────────────── */

  App.registerTab('controls', { show, onEvent });
  return { show, onEvent };
})();
