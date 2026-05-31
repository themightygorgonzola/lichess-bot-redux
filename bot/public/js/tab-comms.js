/**
 * tab-comms.js — Communications + system health tab.
 *
 * Chat panel (current game), log feed (session buffer), connection badge.
 */
'use strict';

const TabComms = (() => {
  let _rendered = false;
  let _gameId = null;

  const panel = () => document.getElementById('panel-comms');

  /* ── lifecycle ──────────────────────────────────────────────────────── */

  function show() {
    _fullRender();
  }

  function onEvent(type, data) {
    if (App.activeTab() !== 'comms') { _rendered = false; return; }
    if (!_rendered) { _fullRender(); return; }

    switch (type) {
      case 'chat_line': _appendChat(data); break;
      case 'log':       _appendLog(data); break;
      case 'game_start':
      case 'game_end':
      case 'snapshot':
        _fullRender();
        break;
    }
  }

  /* ── render ─────────────────────────────────────────────────────────── */

  function _fullRender() {
    const g = App.currentGame();
    _gameId = g ? g.id : null;

    panel().innerHTML = `
      <div class="comms-section" style="flex:0.5;">
        <div class="comms-header">Chat ${g ? `— ${App.esc(g.opponent || '?')}` : ''}</div>
        <div class="comms-body" id="chat-feed"></div>
      </div>
      <div class="comms-section" style="flex:1;">
        <div class="comms-header">System Log <span class="badge" style="background:var(--accent-dim);color:var(--accent);margin-left:0.5rem;" id="conn-badge">connected</span></div>
        <div class="comms-body" id="log-feed"></div>
      </div>
    `;
    _rendered = true;

    // Populate chat
    if (g && g.chat) {
      for (const c of g.chat) _appendChat(c);
    }

    // Populate logs
    for (const entry of App.logs) _appendLog(entry);
  }

  /* ── chat messages ──────────────────────────────────────────────────── */

  function _appendChat(data) {
    const feed = document.getElementById('chat-feed');
    if (!feed) return;

    const who = data.username === 'lichess' ? 'system' :
                data.username === (App.currentGame()?.opponent) ? 'opp' : 'bot';
    const div = document.createElement('div');
    div.className = `chat-line chat-${who}`;
    const ts = new Date(data.ts || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    div.innerHTML = `<span class="chat-ts">${ts}</span><span class="chat-who">${App.esc(data.username || who)}</span> ${App.esc(data.text || '')}`;
    feed.appendChild(div);
    feed.scrollTop = feed.scrollHeight;
  }

  /* ── log entries ────────────────────────────────────────────────────── */

  /* ── Structured log renderer ──────────────────────────────────────── */

  function _parseLogLine(msg) {
    let src = '?', srcType = 'default';
    let rest = (msg || '').trim();
    const tagM = /^\[([^\]]+)\]/.exec(rest);
    if (tagM) {
      const tag = tagM[1]; rest = rest.slice(tagM[0].length).trimStart();
      if      (tag.startsWith('game '))     { src = tag.slice(5).slice(0,10); srcType = 'game'; }
      else if (tag.startsWith('selfplay:')) { src = tag.slice(9).slice(0,8);  srcType = 'selfplay'; }
      else if (tag === 'engine')            { src = 'eng';   srcType = 'engine'; }
      else if (tag === 'board')             { src = 'brd';   srcType = 'board'; }
      else if (tag === 'gameDb')            { src = 'db';    srcType = 'db'; }
      else if (tag === 'lichess')           { src = 'api';   srcType = 'api'; }
      else if (tag === 'ctrl')              { src = 'ctrl';  srcType = 'ctrl'; }
      else if (tag === 'dashState')         { src = 'state'; srcType = 'ctrl'; }
      else                                  { src = tag.slice(0,7); srcType = 'default'; }
    }
    const kvRe = /\b([a-zA-Z_]\w*)=([-\w.]+)/g;
    const kvs = []; let km;
    while ((km = kvRe.exec(rest)) !== null) kvs.push([km[1], km[2]]);
    const firstM = /\b[a-zA-Z_]\w*=[-\w.]/.exec(rest);
    const event = (firstM ? rest.slice(0, firstM.index) : rest).replace(/[:\s,()]+$/, '').trim();
    return { src, srcType, event: event || rest.slice(0, 80), kvs };
  }

  function _kvCls(key, val) {
    if (key === 'eval')   { const n = parseFloat(val); return isNaN(n) ? 'll-v' : (n >= 0 ? 'll-v ll-v-pos' : 'll-v ll-v-neg'); }
    if (key === 'val')    { const n = parseFloat(val); return isNaN(n) ? 'll-v ll-v-depth' : (n >= 0 ? 'll-v ll-v-pos' : 'll-v ll-v-neg'); }
    if (key === 'effective') return 'll-v ll-v-conf';
    if (key === 'depth' || key === 'ply' || key === 'move' || key === 'nodes') return 'll-v ll-v-depth';
    if (key === 'elapsed' || key === 'time' || key === 'clock' || key === 'maxTime' || key === 'maxTimeMs') return 'll-v ll-v-time';
    if (key === 'conf')   return 'll-v ll-v-conf';
    return 'll-v';
  }

  function _appendLog(data) {
    const feed = document.getElementById('log-feed');
    if (!feed) return;
    const lvl = (data.level || 'info').toLowerCase();
    const { src, srcType, event, kvs } = _parseLogLine(data.msg || data.message || JSON.stringify(data));
    const ts = data.ts ? new Date(data.ts).toLocaleTimeString() : '';
    const kvHtml = kvs.map(([k, v]) =>
      `<span class="ll-kv"><span class="ll-k">${k}:</span><span class="${_kvCls(k, v)}">${App.esc(v)}</span></span>`
    ).join('');
    const card = document.createElement('div');
    card.className = `ll-card ll-${lvl}`;
    card.title = ts;
    card.innerHTML =
      `<div class="ll-row1"><span class="ll-src ll-src-${srcType}">${App.esc(src)}</span>` +
      `<span class="ll-ev">${App.esc(event)}</span></div>` +
      (kvHtml ? `<div class="ll-row2">${kvHtml}</div>` : '');
    feed.appendChild(card);
    feed.scrollTop = feed.scrollHeight;
    while (feed.children.length > 500) feed.removeChild(feed.firstChild);
  }

  /* ── register ──────────────────────────────────────────────────────── */

  App.registerTab('comms', { show, onEvent });
  return { show, onEvent };
})();
