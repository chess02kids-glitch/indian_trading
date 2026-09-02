/* Quant India — unified dashboard front end.
 *
 * No frameworks, no CDN, no build step: one HTML file, one CSS file, this file.
 * Every number on screen comes from /api/*, which is served by the same process
 * that owns the data layer — so no panel can disagree with another about
 * whether we have data, what the signal is, or whether the feed is real.
 */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  route: 'overview',
  capital: Number(localStorage.getItem('qi.capital') || 100000),
  cache: {},
  timers: [],
};

/* ------------------------------------------------------------------ utils */

const fmt = {
  num(v, d = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  inr(v, d = 0) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return '₹' + Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  pct(v, d = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(d) + '%';
  },
  raw(v, d = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return Number(v).toFixed(d);
  },
  age(seconds) {
    if (seconds === null || seconds === undefined) return 'never';
    const s = Number(seconds);
    if (s < 60) return s.toFixed(0) + 's ago';
    if (s < 3600) return (s / 60).toFixed(0) + 'm ago';
    if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
    return (s / 86400).toFixed(1) + 'd ago';
  },
  stamp(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString('en-IN', { hour12: false });
  },
  day(iso) { return iso ? String(iso).slice(0, 10) : '—'; },
};

const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 6500);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ cache: 'no-store' }, options || {}));
  let body = null;
  try { body = await res.json(); } catch (e) { body = { error: 'bad response' }; }
  if (!res.ok) throw new Error((body && (body.error || body.detail)) || ('HTTP ' + res.status));
  return body;
}

// AUDIT-039: the dashboard's mutating routes are protected by a shared secret.
// The server injects `window.QUANT_DASHBOARD_TOKEN` into this page only when the
// client is trusted (loopback, or QUANT_DASHBOARD_TOKEN_IN_UI=1). When it is not
// available the server sets `QUANT_DASHBOARD_AUTH_REQUIRED` instead and every
// mutating button must be visibly blocked, never silently broken.
function authToken() { return window.QUANT_DASHBOARD_TOKEN || ''; }

function mutationsRequireAuth() {
  return window.QUANT_DASHBOARD_AUTH_REQUIRED === true && !authToken();
}

function blockedByAuth(what) {
  toast('This action is disabled: the dashboard requires an operator token '
    + '(QUANT_DASHBOARD_TOKEN) that this page was not given.' + (what ? ' (' + what + ')' : ''),
    'warn');
  return false;
}

function post(path, payload) {
  if (mutationsRequireAuth()) throw new Error('operator token required');
  const headers = { 'Content-Type': 'application/json' };
  const token = authToken();
  if (token) headers['X-Quant-Token'] = token;
  return api(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload || {}),
  });
}

function confirmDialog(title, copy, okLabel, pre) {
  return new Promise((resolve) => {
    $('#modal-title').textContent = title;
    $('#modal-copy').textContent = copy;
    const preEl = $('#modal-pre');
    if (pre) { preEl.textContent = pre; preEl.hidden = false; } else { preEl.hidden = true; }
    $('#modal-ok').textContent = okLabel || 'Confirm';
    $('#modal').classList.add('open');
    const done = (value) => {
      $('#modal').classList.remove('open');
      $('#modal-ok').onclick = null;
      $('#modal-cancel').onclick = null;
      resolve(value);
    };
    $('#modal-ok').onclick = () => done(true);
    $('#modal-cancel').onclick = () => done(false);
  });
}

/* ------------------------------------------------------------- svg charts */

const NS = 'http://www.w3.org/2000/svg';
function svgEl(name, attrs) {
  const el = document.createElementNS(NS, name);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

/** Multi-series line chart with optional ±σ bands. */
function lineChart(target, opts) {
  const o = Object.assign({ w: 1000, h: 300, padL: 58, padR: 14, padT: 14, padB: 26 }, opts);
  const series = o.series.filter((s) => s && s.points && s.points.length);
  if (!series.length) { target.innerHTML = '<p class="muted small">no data to plot</p>'; return; }
  let lo = Infinity, hi = -Infinity;
  series.forEach((s) => {
    s.points.forEach((p) => {
      const vals = [p.y, p.lo, p.hi].filter((v) => v !== undefined && v !== null);
      vals.forEach((v) => { if (v < lo) lo = v; if (v > hi) hi = v; });
    });
  });
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) { target.innerHTML = ''; return; }
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  lo -= span * 0.05; hi += span * 0.05;
  const n = Math.max(...series.map((s) => s.points.length));
  const iw = o.w - o.padL - o.padR, ih = o.h - o.padT - o.padB;
  const X = (i) => o.padL + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const Y = (v) => o.h - o.padB - ((v - lo) / (hi - lo)) * ih;

  const svg = svgEl('svg', { viewBox: `0 0 ${o.w} ${o.h}`, class: 'chart', role: 'img' });
  for (let k = 0; k <= 4; k++) {
    const gy = o.padT + (k * ih) / 4;
    const val = hi - (k * (hi - lo)) / 4;
    svg.appendChild(svgEl('line', { x1: o.padL, y1: gy, x2: o.w - o.padR, y2: gy, stroke: '#1e2733' }));
    const t = svgEl('text', { x: o.padL - 7, y: gy + 3.5, 'text-anchor': 'end', class: 'axis' });
    t.textContent = o.yFmt ? o.yFmt(val) : val.toFixed(2);
    svg.appendChild(t);
  }
  // x labels: first / middle / last
  const ref = series[0].points;
  [0, Math.floor((ref.length - 1) / 2), ref.length - 1].forEach((i) => {
    if (i < 0 || i >= ref.length) return;
    const t = svgEl('text', { x: X(i), y: o.h - 8, 'text-anchor': 'middle', class: 'axis' });
    t.textContent = ref[i].label || '';
    svg.appendChild(t);
  });
  series.forEach((s) => {
    if (s.band && s.points.some((p) => p.lo !== undefined)) {
      const up = s.points.map((p, i) => `${X(i).toFixed(1)},${Y(p.hi).toFixed(1)}`).join(' ');
      const dn = s.points.slice().reverse()
        .map((p, i) => `${X(s.points.length - 1 - i).toFixed(1)},${Y(p.lo).toFixed(1)}`).join(' ');
      svg.appendChild(svgEl('polygon', {
        points: up + ' ' + dn, fill: s.band, stroke: 'none',
      }));
    }
    const pts = s.points.map((p, i) => `${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
    svg.appendChild(svgEl('polyline', {
      points: pts, fill: 'none', stroke: s.color, 'stroke-width': s.width || 2,
      'stroke-dasharray': s.dash || 'none', opacity: s.opacity === undefined ? 1 : s.opacity,
    }));
  });
  target.innerHTML = '';
  target.appendChild(svg);
}

/** Grouped / single bar chart from {label, value, color} rows. */
function barChart(target, rows, opts) {
  const o = Object.assign({ w: 1000, h: 260, padL: 54, padR: 14, padT: 14, padB: 34 }, opts);
  if (!rows.length) { target.innerHTML = '<p class="muted small">no data</p>'; return; }
  const vals = rows.map((r) => r.value);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const iw = o.w - o.padL - o.padR, ih = o.h - o.padT - o.padB;
  const Y = (v) => o.h - o.padB - ((v - lo) / (hi - lo)) * ih;
  const zero = Y(0);
  const bw = iw / rows.length;
  const svg = svgEl('svg', { viewBox: `0 0 ${o.w} ${o.h}`, class: 'chart' });
  svg.appendChild(svgEl('line', { x1: o.padL, y1: zero, x2: o.w - o.padR, y2: zero, stroke: '#8b98a9', 'stroke-dasharray': '3 3' }));
  rows.forEach((r, i) => {
    const x = o.padL + i * bw + bw * 0.18;
    const y = Y(r.value);
    const h = Math.max(Math.abs(y - zero), 1);
    svg.appendChild(svgEl('rect', {
      x: x.toFixed(1), y: Math.min(y, zero).toFixed(1),
      width: (bw * 0.64).toFixed(1), height: h.toFixed(1),
      fill: r.color || (r.value >= 0 ? '#3fb950' : '#f85149'),
      'fill-opacity': r.opacity === undefined ? 0.92 : r.opacity,
    }));
    const t = svgEl('text', { x: (o.padL + i * bw + bw / 2).toFixed(1), y: o.h - 9, 'text-anchor': 'middle', class: 'axis' });
    t.textContent = r.label;
    svg.appendChild(t);
  });
  [lo, (lo + hi) / 2, hi].forEach((v) => {
    const t = svgEl('text', { x: o.padL - 7, y: Y(v) + 3.5, 'text-anchor': 'end', class: 'axis' });
    t.textContent = (v * 100).toFixed(0) + '%';
    svg.appendChild(t);
  });
  target.innerHTML = '';
  target.appendChild(svg);
}

/** Equity curve coloured by regime segments. */
function regimeEquityChart(target, points, regimeByDate) {
  if (!points || !points.length) { target.innerHTML = '<p class="muted small">no equity curve</p>'; return; }
  const w = 1000, h = 280, padL = 58, padR = 14, padT = 14, padB = 28;
  const ys = points.map((p) => p.equity);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo; lo -= span * 0.04; hi += span * 0.04;
  const iw = w - padL - padR, ih = h - padT - padB;
  const X = (i) => padL + (i / (points.length - 1)) * iw;
  const Y = (v) => h - padB - ((v - lo) / (hi - lo)) * ih;
  const svg = svgEl('svg', { viewBox: `0 0 ${w} ${h}`, class: 'chart' });

  const byDate = {};
  (regimeByDate || []).forEach((r) => { byDate[r.date] = r; });
  // background regime bands
  let runStart = 0;
  for (let i = 1; i <= points.length; i++) {
    const cur = byDate[points[i] && points[i].date];
    const prev = byDate[points[runStart].date];
    const sameRun = i < points.length && cur && prev && cur.label === prev.label;
    if (!sameRun) {
      const color = (prev && prev.color) || '#1e2733';
      svg.appendChild(svgEl('rect', {
        x: X(runStart).toFixed(1), y: padT,
        width: Math.max(X(Math.min(i, points.length - 1)) - X(runStart), 0.6).toFixed(1),
        height: ih, fill: color, 'fill-opacity': 0.13,
      }));
      runStart = i;
    }
  }
  for (let k = 0; k <= 4; k++) {
    const gy = padT + (k * ih) / 4;
    svg.appendChild(svgEl('line', { x1: padL, y1: gy, x2: w - padR, y2: gy, stroke: '#1e2733' }));
    const t = svgEl('text', { x: padL - 7, y: gy + 3.5, 'text-anchor': 'end', class: 'axis' });
    t.textContent = (hi - (k * (hi - lo)) / 4).toFixed(2) + '×';
    svg.appendChild(t);
  }
  const pts = points.map((p, i) => `${X(i).toFixed(1)},${Y(p.equity).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('polyline', { points: pts, fill: 'none', stroke: '#58a6ff', 'stroke-width': 2 }));
  [0, Math.floor(points.length / 2), points.length - 1].forEach((i) => {
    const t = svgEl('text', { x: X(i), y: h - 8, 'text-anchor': 'middle', class: 'axis' });
    t.textContent = points[i].date;
    svg.appendChild(t);
  });
  target.innerHTML = '';
  target.appendChild(svg);
}

function heatmap(target, names, matrix) {
  if (!names || !names.length) { target.innerHTML = '<p class="muted small">no data</p>'; return; }
  const n = names.length;
  const grid = document.createElement('div');
  grid.className = 'heat';
  grid.style.gridTemplateColumns = `110px repeat(${n}, minmax(0,1fr))`;
  grid.appendChild(Object.assign(document.createElement('div'), { className: 'lbl' }));
  names.forEach((name) => {
    const c = document.createElement('div');
    c.className = 'lbl';
    c.style.textAlign = 'center';
    c.textContent = name;
    c.title = name;
    grid.appendChild(c);
  });
  matrix.forEach((row, i) => {
    const lbl = document.createElement('div');
    lbl.className = 'lbl';
    lbl.textContent = names[i];
    lbl.title = names[i];
    grid.appendChild(lbl);
    row.values.forEach((v, j) => {
      const cell = document.createElement('div');
      cell.className = 'cell';
      if (v === null || v === undefined) { cell.textContent = '·'; grid.appendChild(cell); return; }
      const a = Math.min(Math.abs(v), 1);
      const bg = v >= 0
        ? `rgba(63,185,80,${(0.10 + a * 0.62).toFixed(2)})`
        : `rgba(248,81,73,${(0.10 + a * 0.62).toFixed(2)})`;
      cell.style.background = bg;
      cell.style.color = a > 0.55 ? '#0b0e14' : '#e6edf3';
      cell.textContent = v.toFixed(2);
      cell.title = `${names[i]} vs ${names[j]}: ${v.toFixed(3)}`;
      grid.appendChild(cell);
    });
  });
  target.innerHTML = '';
  target.appendChild(grid);
}

/* --------------------------------------------------------- shared chrome */

function setBanner(kind, html) {
  const el = $('#banner');
  if (!kind) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.className = 'banner ' + kind;
  el.innerHTML = html;
}

function chip(id, text, kind) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'chip' + (kind ? ' ' + kind : '');
}

async function refreshStatusStrip() {
  try {
    const [ds, sig] = await Promise.all([
      api('/api/data-status'),
      api('/api/strategy/signal?capital=' + state.capital).catch(() => null),
    ]);
    const fresh = ds.freshness || {};
    const ageDays = fresh.last_bar_age_days;
    chip('#chip-data',
      `data ${fresh.last_bar || '—'}${ageDays === null || ageDays === undefined ? '' : ' · ' + ageDays + 'd old'}`,
      ageDays === null || ageDays === undefined ? 'bad' : (ageDays <= 5 ? 'ok' : 'warn'));

    const uni = ds.universe || {};
    $('#sb-meta').textContent =
      `${uni.size || 0} names · ${(ds.prices_info || {}).symbols || 0} in panel · ${(ds.prices_info || {}).dates || 0} days`;

    if (sig && sig.regime) {
      const inMarket = sig.regime.state === 'IN_MARKET';
      chip('#chip-regime', `regime ${inMarket ? 'IN MARKET' : 'IN CASH'}`, inMarket ? 'ok' : 'warn');
    } else {
      chip('#chip-regime', 'regime —', 'bad');
    }
    return { ds, sig };
  } catch (e) {
    chip('#chip-data', 'data unavailable', 'bad');
    return null;
  }
}

async function refreshFeedChips() {
  try {
    const st = await api('/api/live/state');
    const feed = st.feed || {};
    const live = feed.mode === 'LIVE';
    chip('#chip-feed', 'feed ' + (feed.mode || '—'), live ? 'ok' : 'info');
    $('#chip-feed').title = feed.note || '';
  } catch (e) { chip('#chip-feed', 'feed —', 'warn'); }
  try {
    const ps = await api('/api/paper/status');
    const qh = ps.quote_health || {};
    const src = qh.source || '—';
    const kind = src === 'UPSTOX' ? 'ok' : (src === 'SIM' ? 'info' : 'warn');
    chip('#chip-quote', 'quotes ' + src + ' · ' + fmt.age(qh.age_seconds), kind);
    $('#chip-quote').title = (qh.chain && qh.chain.note) || qh.error || '';
  } catch (e) { chip('#chip-quote', 'quotes —', 'warn'); }
}

async function refreshKillSwitch() {
  try {
    const ops = await api('/api/operations');
    const sw = ops.kill_switch || {};
    const box = $('#killbox');
    box.classList.toggle('armed', !!sw.armed);
    $('#killstate').textContent = 'KILL SWITCH · ' + (sw.armed ? 'ARMED' : 'OFF');
    $('#killbtn').textContent = sw.armed ? 'DISARM' : 'ARM';
    $('#killbtn').dataset.armed = sw.armed ? '1' : '0';
    // AUDIT-039: never show an ARM button the backend will refuse to honour.
    if (mutationsRequireAuth()) {
      $('#killbtn').disabled = true;
      $('#killbtn').title = 'Operator token required (QUANT_DASHBOARD_TOKEN)';
    } else {
      $('#killbtn').disabled = false;
      $('#killbtn').title = '';
    }
    if (sw.armed) {
      setBanner('red', `<b>Kill switch armed</b> — all paper rebalancing and automation is blocked.
        Reason: ${esc(sw.reason || 'not given')} · armed ${fmt.stamp(sw.armed_at)}`);
    }
    return ops;
  } catch (e) { return null; }
}

async function toggleKillSwitch() {
  if (mutationsRequireAuth()) { blockedByAuth('kill switch'); return; }
  const armed = $('#killbtn').dataset.armed === '1';
  const ok = await confirmDialog(
    armed ? 'Disarm the kill switch?' : 'Arm the kill switch?',
    armed
      ? 'Trading will be allowed again. Only do this once you understand why it was armed.'
      : 'This immediately blocks every paper rebalance and stops automatic paper trading. It is reversible.',
    armed ? 'DISARM' : 'ARM KILL SWITCH'
  );
  if (!ok) return;
  try {
    const reason = armed ? '' : (prompt('Reason (optional):') || 'armed from the dashboard');
    await post('/api/kill-switch', { armed: !armed, reason });
    toast(armed ? 'Kill switch disarmed.' : 'Kill switch armed — trading blocked.', armed ? 'good' : 'warn');
    await refreshKillSwitch();
    route(state.route, true);
  } catch (e) { toast(e.message, 'bad'); }
}

/* -------------------------------------------------------------- Overview */

function kpi(label, value, klass, note) {
  return `<div class="kpi"><div class="k">${esc(label)}</div>
    <div class="v ${klass || ''}">${value}</div>
    ${note ? `<div class="n">${esc(note)}</div>` : ''}</div>`;
}

function renderOverview(data) {
  const ops = data.operations || {};
  const sig = data.signal;
  const div = data.divergence || {};
  const paper = data.paper || {};
  const sys = ops.system_health || {};
  const recon = ops.reconciliation || {};
  const broker = ops.broker_health || {};
  const head = ops.headline || ['UNKNOWN', ''];

  const regime = sig && sig.regime ? sig.regime : null;
  const inMarket = regime && regime.state === 'IN_MARKET';

  const heartbeats = sys.heartbeats || {};
  const hbRows = Object.keys(heartbeats).map((k) => {
    const b = heartbeats[k];
    const klass = b.state === 'ok' ? 'green' : (b.state === 'stale' ? 'amber' : 'red');
    return `<tr><td>${esc(k.replace(/_/g, ' '))}</td>
      <td><span class="badge ${klass}">${esc(b.state)}</span></td>
      <td class="num">${esc(fmt.age(b.age_seconds))}</td>
      <td class="small muted">${esc(fmt.stamp(b.at))}</td></tr>`;
  }).join('');

  const actions = `
    <div class="controls">
      <button class="btn" data-act="recompute">⟳ Recompute signal</button>
      <button class="btn ghost" data-act="quotes">Refresh quotes</button>
      <button class="btn ghost" data-act="audit">Run reconciliation</button>
      <button class="btn ghost" data-act="prices">Rebuild prices.parquet</button>
      <label class="f">Capital ₹
        <input id="ov-capital" type="number" min="1000" step="1000" value="${state.capital}">
      </label>
      <button class="btn ghost" data-act="capital">Apply</button>
    </div>`;

  return `
  <div class="grid g4">
    ${kpi('System', esc(head[0]), head[0] === 'HEALTHY' ? 'green' : (head[0] === 'COLD START' ? 'blue' : 'amber'), head[1])}
    ${kpi('Regime', inMarket ? 'IN MARKET' : 'IN CASH', inMarket ? 'green' : 'amber',
      regime ? `proxy vs 100d SMA ${fmt.pct(regime.proxy_vs_sma_pct)}` : 'signal unavailable')}
    ${kpi('Basket', sig ? sig.basket.length + ' names' : '—', '',
      sig ? `as of ${esc(sig.as_of)} · universe ${sig.universe.size}` : '')}
    ${kpi('Next rebalance', sig ? esc(fmt.day(sig.next_rebalance)) : '—', 'blue',
      sig ? `last ${esc(fmt.day(sig.last_rebalance))}` : '')}
  </div>

  <div class="grid g-2-1" style="margin-top:14px">
    <div class="card">
      <h2>Backtest vs live <span class="hint">the earliest warning that something is wrong</span></h2>
      ${div.ready ? `
        <div class="kpis">
          ${kpi('State', esc(div.state), div.state === 'ON TRACK' ? 'green' : (div.state === 'WATCH' ? 'amber' : 'red'))}
          ${kpi('z-score', fmt.raw(div.summary.z_score, 2), Math.abs(div.summary.z_score) >= 2 ? 'red' : (Math.abs(div.summary.z_score) >= 1 ? 'amber' : 'green'), 'cumulative gap in σ units')}
          ${kpi('Tracking error', div.summary.tracking_error === null ? '—' : (div.summary.tracking_error * 100).toFixed(1) + '%', '', 'annualised')}
          ${kpi('Actual', fmt.pct(div.summary.actual_return_pct), div.summary.actual_return_pct >= 0 ? 'green' : 'red')}
          ${kpi('Expected', fmt.pct(div.summary.expected_return_pct), 'muted')}
        </div>
        <div id="ov-div-chart" style="margin-top:12px"></div>
        <div class="legend">
          <span><i style="background:#58a6ff"></i>actual paper equity</span>
          <span><i style="background:#8b98a9"></i>expected from backtest</span>
          <span><i style="background:rgba(88,166,255,.25)"></i>±1σ · ±2σ cone</span>
        </div>
        <div class="note">${esc(div.advice)}</div>
      ` : `<div class="note warn"><b>Not enough history yet.</b> ${esc(div.reason || '')}
        <br>Start the paper monitor (Paper account → Start monitor) and let it run at least two sessions.</div>`}
    </div>

    <div class="card">
      <h2>Operations health</h2>
      <table>
        <tr><td>Broker / data source</td><td class="right"><span class="badge ${broker.state === 'HEALTHY' ? 'green' : (broker.state === 'NOT_CONFIGURED' ? 'amber' : 'red')}">${esc(broker.state)}</span></td></tr>
        <tr><td>Reconciliation</td><td class="right"><span class="badge ${recon.state === 'MATCHED' || recon.state === 'FLAT' ? 'green' : (recon.state === 'NOT_STARTED' ? 'amber' : 'red')}">${esc(recon.state)}</span></td></tr>
        <tr><td>System</td><td class="right"><span class="badge ${sys.overall === 'HEALTHY' ? 'green' : (sys.overall === 'COLD_START' ? 'blue' : 'amber')}">${esc(sys.overall)}</span></td></tr>
        <tr><td>Last bar</td><td class="right mono">${esc((sys.data || {}).last_bar || '—')} <span class="dim">(${esc(String((sys.data || {}).last_bar_age_days ?? '—'))}d)</span></td></tr>
        <tr><td>Universe</td><td class="right mono">${esc(String((sys.data || {}).universe_size ?? '—'))} names</td></tr>
        <tr><td>Uptime</td><td class="right mono">${esc(fmt.age((sys.process || {}).uptime_seconds))}</td></tr>
      </table>
      <p class="small muted" style="margin-top:9px">${esc(recon.detail || '')}</p>
      <p class="small muted">${esc(broker.detail || '')}</p>
      <div style="margin-top:12px"><a class="btn ghost" href="#operations">Open Operations →</a></div>
    </div>
  </div>

  <div class="grid g-2-1">
    <div class="card">
      <h2>Component heartbeats <span class="hint">"never" means it has not run yet — not that it is healthy</span></h2>
      <table><thead><tr><th>Component</th><th>State</th><th class="num">Age</th><th>Last run</th></tr></thead>
      <tbody>${hbRows || '<tr><td colspan="4" class="muted">none</td></tr>'}</tbody></table>
    </div>
    <div class="card">
      <h2>Do this now</h2>
      ${actions}
      <div class="note" style="margin-top:12px">
        <b>Daily routine (≈5 minutes, after 15:45 IST):</b>
        <ol style="margin:6px 0 0 18px">
          <li>Refresh price data — <code>python fetch_data.py</code></li>
          <li>Click <b>Recompute signal</b> above</li>
          <li>Check the regime pill and the divergence z-score</li>
          <li>Only then act on the basket</li>
        </ol>
      </div>
    </div>
  </div>`;
}

function wireOverview() {
  $('#content').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act !== 'capital' && act !== 'audit' && mutationsRequireAuth()) {
      blockedByAuth(act);
      return;
    }
    try {
      if (act === 'capital') {
        const v = Number($('#ov-capital').value);
        if (!(v > 0)) throw new Error('capital must be positive');
        state.capital = v;
        localStorage.setItem('qi.capital', String(v));
        toast('Capital set to ' + fmt.inr(v));
        route('overview', true);
        return;
      }
      btn.disabled = true;
      if (act === 'recompute') { await post('/api/signal/recompute', {}); toast('Signal recomputed.', 'good'); }
      if (act === 'quotes') { await post('/api/paper/refresh', {}); toast('Quotes refreshed.', 'good'); }
      if (act === 'audit') {
        const r = await api('/api/paper/audit');
        toast(r.passed ? 'Reconciliation PASSED' : 'Reconciliation FAILED — inspect exports', r.passed ? 'good' : 'bad');
      }
      if (act === 'prices') { await post('/api/data/rebuild-prices', {}); toast('prices.parquet rebuilt.', 'good'); }
      await refreshStatusStrip();
      route(state.route, true);
    } catch (e) { toast(e.message, 'bad'); }
    finally { btn.disabled = false; }
  });

  const div = state.cache.divergence;
  if (div && div.ready && div.series) {
    lineChart($('#ov-div-chart'), {
      h: 210,
      yFmt: (v) => '₹' + (v / 1000).toFixed(0) + 'k',
      series: [
        { color: 'rgba(88,166,255,.30)', band: 'rgba(88,166,255,.13)', width: 0,
          points: div.series.map((p) => ({ y: p.expected, lo: p.band2_lo, hi: p.band2_hi })) },
        { color: 'rgba(139,152,169,.55)', dash: '4 3', width: 1.5,
          points: div.series.map((p) => ({ y: p.expected })) },
        { color: '#58a6ff', points: div.series.map((p) => ({ y: p.actual, label: p.date })) },
      ],
    });
  }
}

/* -------------------------------------------------------------- Strategy */

function renderStrategy(data) {
  const sig = data.signal;
  if (!sig) {
    return `<div class="card"><h2>Signal unavailable</h2>
      <div class="note bad">The signal could not be computed. Open <a href="#operations">Operations</a>
      to see which component is missing data, then run <code>python fetch_data.py</code>.</div></div>`;
  }
  const regime = data.regime || {};
  const cur = regime.current || {};
  const sizing = data.sizing || {};
  const inMarket = sig.regime.state === 'IN_MARKET';
  const stale = !sig.fresh;

  const basketRows = sig.basket.map((b, i) => {
    const s = (sizing.rows || [])[i] || {};
    return `<tr>
      <td><b>${esc(b.symbol)}</b></td>
      <td class="num">${fmt.pct(b.mom20_pct)}</td>
      <td class="num">${fmt.raw(b.weight_pct, 1)}%</td>
      <td class="num">${s.vol_target_weight_pct !== undefined ? fmt.raw(s.vol_target_weight_pct, 1) + '%' : '—'}</td>
      <td class="num">${s.realised_vol_pct !== undefined ? fmt.raw(s.realised_vol_pct, 1) + '%' : '—'}</td>
      <td class="num">${fmt.inr(b.last_close, 2)}</td>
      <td class="num">${b.qty > 0 ? '<b>' + fmt.num(b.qty, 0) + '</b>' : '<span class="dim">—</span>'}</td>
      <td class="num">${s.vol_target_qty !== undefined ? fmt.num(s.vol_target_qty, 0) : '—'}</td>
      <td class="num">${fmt.inr(b.spent, 0)}</td></tr>`;
  }).join('');

  const histRows = (sig.signal_history || []).slice(-10).reverse().map((h) => `
    <tr><td class="mono">${esc(h.date)}</td>
    <td>${h.regime === 'IN_MARKET' ? '<span class="badge green">IN MARKET</span>' : '<span class="badge amber">IN CASH</span>'}</td>
    <td class="small">${esc((h.top3 || []).join(', '))}</td></tr>`).join('');

  const uni = sig.universe || {};
  const rej = uni.rejected_counts || {};

  return `
  ${stale ? `<div class="note bad" style="margin-bottom:14px"><b>Data is stale.</b>
    Last bar is ${esc(sig.as_of)} (${sig.stale_days} days old). The signal below is "as of" that date —
    refresh with <code>python fetch_data.py</code> before acting on it.</div>` : ''}

  <div class="grid g4">
    ${kpi('Regime', inMarket ? 'IN MARKET' : 'IN CASH', inMarket ? 'green' : 'amber', `as of ${esc(sig.as_of)}`)}
    ${kpi('Trend', esc(cur.trend || '—'), cur.trend === 'TREND_UP' ? 'green' : (cur.trend === 'TREND_DOWN' ? 'red' : 'amber'),
      `efficiency ${fmt.raw(cur.efficiency, 2)}`)}
    ${kpi('Vol regime', esc((cur.vol_regime || '—').replace('_VOL', '')), cur.vol_regime === 'HIGH_VOL' ? 'purple' : '',
      `20d vol ${fmt.raw(cur.vol20_annualised_pct, 1)}% ann.`)}
    ${kpi('Universe', fmt.num(uni.size, 0), 'blue', `${uni.research_parity_symbols} meet the 8-year research rule`)}
  </div>

  <div class="grid g-2-1" style="margin-top:14px">
    <div class="card">
      <h2>Regime filter <span class="hint">equal-weight market proxy vs its 100-day SMA</span></h2>
      <table>
        <tr><td>Strategy position</td><td class="right">${sig.position.state === 'IN_MARKET'
          ? '<span class="pill market">HOLDING BASKET</span>' : '<span class="pill cash">IN CASH (1-day lag)</span>'}</td></tr>
        <tr><td>Market proxy</td><td class="right mono">${fmt.raw(cur.proxy, 4)}</td></tr>
        <tr><td>100-day SMA</td><td class="right mono">${fmt.raw(cur.sma, 4)}</td></tr>
        <tr><td>Proxy vs SMA</td><td class="right mono">${fmt.pct(cur.proxy_vs_sma_pct)}</td></tr>
        <tr><td>Since last rebalance</td><td class="right mono">${fmt.pct(sig.return_since_rebalance_pct)}</td></tr>
        <tr><td>Next rebalance</td><td class="right mono">${esc(sig.next_rebalance)}</td></tr>
      </table>
      <p class="small muted" style="margin-top:9px">${esc(sig.position.note || '')}</p>
      <div id="st-regime-note" class="muted small" style="margin-top:12px"></div>
      <div id="st-regime-chart" style="margin-top:6px"></div>
    </div>
    <div class="card">
      <h2>Market breadth</h2>
      <table>
        <tr><td>Above 20-day SMA</td><td class="right mono">${fmt.raw(sig.breadth.above_20d_sma_pct, 1)}%</td></tr>
        <tr><td>Advancers / decliners (5d)</td><td class="right mono">${sig.breadth.advancers_5d} / ${sig.breadth.decliners_5d}</td></tr>
        <tr><td>Universe size</td><td class="right mono">${sig.breadth.universe_size}</td></tr>
        <tr><td>Last rebalance</td><td class="right mono">${esc(sig.last_rebalance)}</td></tr>
      </table>
      <div class="note" style="margin-top:10px">A falling "% above 20-day SMA" while the proxy is still
      above its 100-day SMA is the classic early warning of a regime rollover — the filter will lag it
      by design.</div>
    </div>
  </div>

  <div class="card">
    <h2>Today's basket <span class="hint">top-20 by 20-day momentum, ${fmt.inr(sig.capital)} capital</span></h2>
    <div class="scroll"><table>
      <thead><tr><th>Symbol</th><th class="num">20d mom</th><th class="num">Equal wt</th>
      <th class="num">Vol-target wt</th><th class="num">Realised vol</th><th class="num">Last ₹</th>
      <th class="num">Qty (equal)</th><th class="num">Qty (vol-tgt)</th><th class="num">≈ Invested ₹</th></tr></thead>
      <tbody>${basketRows || '<tr><td colspan="9" class="muted">No basket — the regime filter is off, the strategy holds cash.</td></tr>'}</tbody>
    </table></div>
    <p class="small muted" style="margin-top:9px">Notional ≈ ${fmt.inr(sig.basket_notional)} ·
      cash ≈ ${fmt.inr(sig.cash)} · quantities rounded down to whole shares.
      Vol-target weights come from <a href="#risk">Risk &amp; sizing</a>.</p>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>Equity curve coloured by regime</h2>
      <div id="st-equity"></div>
      <div class="legend" id="st-regime-legend"></div>
      <p class="small muted" style="margin-top:8px">Bands show which regime the model was in.
      The −16.3% OOS drawdown is not an abstract number — you can see which regime produced it.</p>
    </div>
    <div class="card">
      <h2>Signal history <span class="hint">each 20-trading-day rebalance</span></h2>
      <table><thead><tr><th>Date</th><th>Regime</th><th>Top-3 momentum</th></tr></thead>
      <tbody>${histRows}</tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>Universe audit <span class="hint">why a name is in or out</span></h2>
    <div class="kpis">
      ${kpi('In universe', fmt.num(uni.size, 0), 'green')}
      ${kpi('In panel', fmt.num(uni.panel_symbols, 0), '')}
      ${kpi('Research parity', fmt.num(uni.research_parity_symbols, 0), 'blue', '≥8 years of history')}
      ${kpi('Rejected: history', fmt.num(rej.insufficient_history, 0), 'muted')}
      ${kpi('Rejected: liquidity', fmt.num(rej.illiquid, 0), 'muted')}
      ${kpi('Rejected: stale', fmt.num(rej.not_recently_traded, 0), 'muted')}
    </div>
    <p class="small muted" style="margin-top:10px">Window from ${esc(uni.start)} · recency window
      ${esc((uni.recency_window || [])[0] || '—')} → ${esc((uni.recency_window || []).slice(-1)[0] || '—')}.
      Expand the universe on the <a href="#data">Data &amp; universe</a> page.</p>
  </div>

  <div id="st-research-check"></div>`;
}

async function wireStrategy() {
  const regime = state.cache.regime;
  if (regime && regime.equity_curve && regime.equity_curve.length) {
    regimeEquityChart($('#st-equity'), regime.equity_curve, regime.regime_by_date);
    const shares = (regime.summary || {}).shares_pct || {};
    const colors = (regime.summary || {}).colors || {};
    $('#st-regime-legend').innerHTML = Object.keys(shares).map((k) =>
      `<span><i style="background:${esc(colors[String(k).split(' · ')[0]] || '#30363d')}"></i>${esc(k)} ${shares[k]}%</span>`
    ).join('');
  }
  const regimeLine = state.cache.regimeLine || [];
  if (regimeLine.length) {
    const cur = ((state.cache.regime || {}).summary || {}).current || {};
    const noteEl = $('#st-regime-note');
    if (noteEl) noteEl.textContent = cur.label
      ? cur.filter + ' · ' + cur.label + ' · proxy ' + (cur.proxy_vs_sma_pct > 0 ? '+' : '')
        + cur.proxy_vs_sma_pct + '% vs 100d SMA · ann. vol ' + cur.vol20_annualised_pct + '%'
      : '';
    lineChart($('#st-regime-chart'), {
      h: 170,
      yFmt: (v) => v.toFixed(1),
      series: [
        { color: '#8b98a9', dash: '4 3', width: 1.5, points: regimeLine.map((p) => ({ y: p.sma, label: p.date + ' · 100d SMA' })) },
        { color: '#58a6ff', points: regimeLine.map((p) => ({ y: p.proxy, label: p.date + ' · ' + (p.label || 'proxy') })) },
      ],
    });
  }
  // research check (published vs recomputed) — loaded separately, it is the slow one
  const box = $('#st-research-check');
  if (box) {
    box.innerHTML = '<div class="card"><h2>Published card vs fresh recomputation</h2><p class="muted small">computing…</p></div>';
    try {
      const rc = await api('/api/research/check');
      box.innerHTML = renderResearchCheck(rc);
    } catch (e) {
      box.innerHTML = `<div class="card"><h2>Published card vs recomputation</h2>
        <div class="note bad">${esc(e.message)}</div></div>`;
    }
  }
}

function renderResearchCheck(rc) {
  if (rc.error) return `<div class="card"><h2>Published vs recomputed</h2><div class="note bad">${esc(rc.detail || rc.error)}</div></div>`;
  const p = rc.published || {}, r = rc.recomputed || {};
  const row = (label, pv, rv, better) => `<tr><td>${esc(label)}</td>
    <td class="num">${pv}</td><td class="num">${rv}</td></tr>`;
  return `
  <div class="card">
    <h2>Published card vs a fresh recomputation <span class="hint">read this before trusting any number</span></h2>
    <div class="note bad"><b>Important finding.</b> ${esc(rc.note || '')}</div>
    <table>
      <thead><tr><th></th><th class="num">Published card</th><th class="num">Recomputed now</th></tr></thead>
      <tbody>
        ${row('Universe', esc(p.universe || '—'), esc(r.universe || '—'))}
        ${row('Window', esc(p.window || '—'), esc(r.window || '—'))}
        ${row('OOS Sharpe', fmt.raw((p.oos || {}).sharpe, 3), fmt.raw((r.oos || {}).sharpe, 3))}
        ${row('OOS CAGR', fmt.pct(((p.oos || {}).cagr || 0) * 100, 1), fmt.pct(((r.oos || {}).cagr || 0) * 100, 1))}
        ${row('OOS max DD', fmt.pct(((p.oos || {}).mdd || 0) * 100, 1), fmt.pct(((r.oos || {}).max_dd || 0) * 100, 1))}
        ${row('OOS Calmar', fmt.raw((p.oos || {}).calmar, 3), fmt.raw((r.oos || {}).calmar, 3))}
        ${row('OOS volatility', '—', fmt.pct(((r.oos || {}).vol || 0) * 100, 1))}
        ${row('Full-period Sharpe', fmt.raw((p.full || {}).sharpe, 3), fmt.raw((r.full || {}).sharpe, 3))}
      </tbody>
    </table>
    <div class="note warn" style="margin-top:12px"><b>What this means for you.</b>
      The number to plan around is the <b>recomputed</b> column, because that is the strategy
      implemented exactly as the card describes it, on the data you actually have. The published
      column was produced by code that held ~272 names instead of 20. Neither number is a promise:
      the <a href="#divergence">divergence tracker</a> is what tells you which one reality is following.</div>
  </div>`;
}

/* ------------------------------------------------------------- Divergence */

function renderDivergence(data) {
  // The overlay is drawable from the first mark; only the statistical verdict
  // needs a second session.  Bailing out here used to hide the chart entirely.
  if (!data.series || !data.series.length) {
    return `<div class="card"><h2>Backtest vs live divergence</h2>
      <div class="note warn"><b>Not enough history yet.</b> ${esc(data.reason || '')}</div>
      <div class="note">Go to <a href="#paper">Paper account</a>, set your virtual capital, click
      <b>Start monitor</b>, and leave the dashboard running. Each quote refresh records a mark.</div></div>`;
  }
  const s = data.summary || {};
  const pending = !data.ready;
  const zTxt = (s.z_score === null || s.z_score === undefined) ? '—' : s.z_score.toFixed(2);
  const zTone = (s.z_score === null || s.z_score === undefined) ? ''
    : (Math.abs(s.z_score) >= 2 ? 'red' : (Math.abs(s.z_score) >= 1 ? 'amber' : 'green'));
  const teTxt = (s.tracking_error === null || s.tracking_error === undefined)
    ? '—' : (s.tracking_error * 100).toFixed(1) + '%';
  const stateTone = data.state === 'ON TRACK' ? 'green'
    : (data.state === 'WATCH' ? 'amber'
    : (data.state === 'AWAITING SESSIONS' ? '' : 'red'));
  const a = data.assumptions || {};
  const acct = data.account || {};
  return `
  <div class="grid g4">
    ${kpi('State', esc(data.state), stateTone)}
    ${kpi('z-score', zTxt, zTone, 'cumulative gap ÷ expected σ')}
    ${kpi('Tracking error', teTxt, '', 'annualised, vs expectation')}
    ${kpi(pending ? 'Marks recorded' : 'Days observed',
          pending ? fmt.num(acct.equity_points, 0) : fmt.num(s.days_observed, 0), 'blue',
          `since ${fmt.day(acct.first_point)}`)}
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Expected vs actual equity</h2>
    <div id="dv-chart"></div>
    <div class="legend">
      <span><i style="background:#58a6ff"></i>actual paper equity</span>
      <span><i style="background:#8b98a9"></i>expected path</span>
      <span><i style="background:rgba(88,166,255,.22)"></i>±1σ cone</span>
      <span><i style="background:rgba(88,166,255,.10)"></i>±2σ cone</span>
    </div>
    ${pending ? `<div class="note warn"><b>Verdict pending.</b> ${esc(data.reason || '')}</div>` : ''}
    <div class="note ${stateTone === 'green' ? 'good' : (stateTone === 'amber' ? 'warn' : (stateTone === 'red' ? 'bad' : ''))}">${esc(data.advice || '')}</div>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>The numbers</h2>
      <table>
        <tr><td>Starting equity</td><td class="right mono">${fmt.inr(s.start_equity, 0)}</td></tr>
        <tr><td>Actual equity now</td><td class="right mono">${s.actual_equity === null || s.actual_equity === undefined ? '—' : fmt.inr(s.actual_equity, 0)}</td></tr>
        <tr><td>Expected equity now</td><td class="right mono">${fmt.inr(s.expected_equity, 0)}</td></tr>
        <tr><td>Actual return</td><td class="right mono">${s.actual_return_pct === null || s.actual_return_pct === undefined ? '—' : fmt.pct(s.actual_return_pct, 3)}</td></tr>
        <tr><td>Expected return</td><td class="right mono">${fmt.pct(s.expected_return_pct, 3)}</td></tr>
        <tr><td>Gap</td><td class="right mono">${s.gap_pct === null || s.gap_pct === undefined ? '—' : fmt.pct(s.gap_pct, 3)}</td></tr>
      </table>
    </div>
    <div class="card">
      <h2>What "expected" means</h2>
      <table>
        <tr><td>Reference</td><td class="right">${esc(a.label || '—')}</td></tr>
        <tr><td>Expected CAGR</td><td class="right mono">${fmt.pct((a.expected_cagr || 0) * 100, 1)}</td></tr>
        <tr><td>Expected volatility</td><td class="right mono">${fmt.pct((a.expected_vol || 0) * 100, 1)}</td></tr>
        <tr><td>Implied daily drift</td><td class="right mono">${fmt.raw((a.mu_daily || 0) * 10000, 2)} bps</td></tr>
        <tr><td>Implied daily σ</td><td class="right mono">${fmt.raw((a.sigma_daily || 0) * 10000, 2)} bps</td></tr>
      </table>
      <div class="note" style="margin-top:10px">${esc(a.note || '')}</div>
      <div class="note warn">A divergence is a <b>question</b>, not a verdict. Before concluding the
      strategy is broken, check in order: (1) is the quote source real or SIM, (2) is the data fresh,
      (3) did fills happen at the assumed price, (4) only then consider a regime shift.</div>
    </div>
  </div>`;
}

function wireDivergence() {
  const d = state.cache.divergence;
  if (d && d.series && d.series.length) {
    const hasActual = d.series.some((p) => p.actual !== null && p.actual !== undefined);
    lineChart($('#dv-chart'), {
      h: 320,
      yFmt: (v) => '₹' + (v / 1000).toFixed(0) + 'k',
      series: [
        { color: 'rgba(88,166,255,.22)', band: 'rgba(88,166,255,.10)', width: 0,
          points: d.series.map((p) => ({ y: p.expected, lo: p.band2_lo, hi: p.band2_hi })) },
        { color: 'rgba(88,166,255,.40)', band: 'rgba(88,166,255,.16)', width: 0,
          points: d.series.map((p) => ({ y: p.expected, lo: p.band1_lo, hi: p.band1_hi })) },
        { color: '#8b98a9', dash: '4 3', width: 1.5, points: d.series.map((p) => ({ y: p.expected })) },
      ].concat(hasActual ? [{
        color: '#58a6ff',
        points: d.series.map((p) => ({ y: p.actual, label: (p.date || '') + (p.time ? ' ' + p.time : '') })),
      }] : []),
    });
  }
}

/* ---------------------------------------------------------- Risk & sizing */

function renderRisk(data) {
  const costs = data.costs || {};
  const sizing = data.sizing || {};
  const kelly = sizing.kelly || {};
  const ror = sizing.risk_of_ruin_detail || {};
  const grid = costs.grid || [];

  const costRows = grid.map((g) => `<tr>
    <td class="num">${fmt.raw(g.cost_bps_one_way, 1)}</td>
    <td class="num">${fmt.raw(g.round_trip_bps, 1)}</td>
    <td class="num">${fmt.raw(g.sharpe, 3)}</td>
    <td class="num">${fmt.pct(g.cagr * 100, 1)}</td>
    <td class="num">${fmt.pct(g.max_dd * 100, 1)}</td>
    <td class="num">${fmt.raw(g.calmar, 2)}</td>
    <td class="num">${fmt.pct(g.cost_drag_annual_pct, 2)}</td></tr>`).join('');

  const sizeRows = (sizing.rows || []).map((r) => `<tr>
    <td><b>${esc(r.symbol)}</b></td>
    <td class="num">${fmt.raw(r.realised_vol_pct, 1)}%</td>
    <td class="num">${fmt.raw(r.equal_weight_pct, 1)}%</td>
    <td class="num">${fmt.num(r.equal_weight_qty, 0)}</td>
    <td class="num">${fmt.raw(r.vol_target_weight_pct, 1)}%</td>
    <td class="num">${fmt.num(r.vol_target_qty, 0)}</td>
    <td class="num">${fmt.inr(r.invested, 0)}</td></tr>`).join('');

  return `
  <div class="card">
    <h2>Cost &amp; slippage sensitivity <span class="hint">how fragile is the edge, really?</span></h2>
    <div class="controls" style="margin-bottom:10px">
      <label class="f" style="min-width:320px">One-way cost: <b id="cost-label">15.0 bps</b>
        <input id="cost-slider" type="range" min="0" max="${grid.length - 1}" step="1"
          value="${Math.max(0, grid.findIndex((g) => Math.abs(g.cost_bps_one_way - 15) < 0.01))}" style="width:100%">
      </label>
    </div>
    <div class="kpis" id="cost-kpis"></div>
    <div id="cost-chart" style="margin-top:12px"></div>
    <div class="legend">
      <span><i style="background:#58a6ff"></i>Sharpe</span>
      <span><i style="background:#3fb950"></i>CAGR</span>
      <span><i style="background:#f85149"></i>max drawdown</span>
    </div>
    <div class="scroll" style="margin-top:12px"><table>
      <thead><tr><th class="num">1-way bps</th><th class="num">Round trip</th><th class="num">Sharpe</th>
      <th class="num">CAGR</th><th class="num">Max DD</th><th class="num">Calmar</th><th class="num">Cost drag / yr</th></tr></thead>
      <tbody>${costRows}</tbody></table></div>
    <div class="note" style="margin-top:10px">${esc(costs.note || '')}
      Universe: ${esc(String((costs.universe || {}).size || (costs.meta || {}).symbols || '—'))} names ·
      breakeven around <b>${costs.breakeven_cost_bps === null ? 'never positive in this grid' : fmt.raw(costs.breakeven_cost_bps, 1) + ' bps one-way'}</b>.
      Annual turnover ≈ ${fmt.raw((grid[0] || {}).annual_turnover, 1)}× — that is what converts bps into a real drag.</div>
  </div>

  <div class="grid g-2-1">
    <div class="card">
      <h2>Position sizing <span class="hint">vol-targeted vs equal weight · ${fmt.inr(sizing.capital)}</span></h2>
      <div class="scroll"><table>
        <thead><tr><th>Symbol</th><th class="num">Realised vol</th><th class="num">Equal wt</th>
        <th class="num">Qty (equal)</th><th class="num">Vol-tgt wt</th><th class="num">Qty (vol-tgt)</th>
        <th class="num">Invested ₹</th></tr></thead>
        <tbody>${sizeRows || '<tr><td colspan="7" class="muted">No basket to size.</td></tr>'}</tbody></table></div>
      <p class="small muted" style="margin-top:9px">Total invested ${fmt.inr(sizing.total_invested)} ·
        portfolio vol lower bound ${sizing.portfolio_vol_lower_bound_pct === null ? '—' : fmt.raw(sizing.portfolio_vol_lower_bound_pct, 1) + '%'}
        (assumes zero correlation — the true number is higher) · max position ${fmt.raw(sizing.max_position_weight_pct, 0)}%.</p>
    </div>
    <div class="card">
      <h2>Kelly &amp; risk of ruin</h2>
      <table>
        <tr><td>Win rate</td><td class="right mono">${fmt.raw(kelly.win_rate_pct, 1)}%</td></tr>
        <tr><td>Payoff odds</td><td class="right mono">${fmt.raw(kelly.odds, 2)}</td></tr>
        <tr><td>Full Kelly</td><td class="right mono">${fmt.raw(kelly.full_kelly_pct, 1)}%</td></tr>
        <tr><td>Fraction used</td><td class="right mono">${fmt.raw(kelly.fraction_used, 2)}×</td></tr>
        <tr><td><b>Recommended exposure</b></td><td class="right mono"><b>${fmt.raw(kelly.recommended_pct, 1)}%</b></td></tr>
        <tr><td>…in rupees</td><td class="right mono">${fmt.inr(kelly.recommended_rupees, 0)}</td></tr>
      </table>
      <p class="small muted" style="margin-top:8px">${esc(kelly.note || '')}</p>
      <table style="margin-top:12px">
        <tr><td>Risk of ruin (${fmt.raw(ror.ruin_threshold_pct, 0)}% DD, ${ror.horizon_years}y)</td>
          <td class="right mono">${sizing.risk_of_ruin_pct === null ? '—' : fmt.raw(sizing.risk_of_ruin_pct, 1) + '%'}</td></tr>
        <tr><td>Median max drawdown</td><td class="right mono">${fmt.raw(ror.median_max_dd_pct, 1)}%</td></tr>
        <tr><td>10th-percentile max DD</td><td class="right mono">${fmt.raw(ror.p90_max_dd_pct, 1)}%</td></tr>
        <tr><td>Median terminal multiple</td><td class="right mono">${fmt.raw(ror.median_terminal_multiple, 2)}×</td></tr>
        <tr><td>10th-percentile terminal</td><td class="right mono">${fmt.raw(ror.p10_terminal_multiple, 2)}×</td></tr>
      </table>
      <p class="small muted" style="margin-top:8px">Bootstrap of ${ror.paths} paths × ${ror.bootstrap_days} real daily returns.</p>
    </div>
  </div>`;
}

function wireRisk() {
  const costs = state.cache.costs || {};
  const grid = costs.grid || [];
  if (!grid.length) return;
  const slider = $('#cost-slider');
  const draw = (i) => {
    const g = grid[i];
    $('#cost-label').textContent = fmt.raw(g.cost_bps_one_way, 1) + ' bps';
    $('#cost-kpis').innerHTML =
      kpi('Sharpe', fmt.raw(g.sharpe, 3), g.sharpe > 0.5 ? 'green' : (g.sharpe > 0 ? 'amber' : 'red')) +
      kpi('CAGR', fmt.pct(g.cagr * 100, 1), g.cagr > 0 ? 'green' : 'red') +
      kpi('Max DD', fmt.pct(g.max_dd * 100, 1), 'red') +
      kpi('Calmar', fmt.raw(g.calmar, 2)) +
      kpi('Cost drag / yr', fmt.pct(g.cost_drag_annual_pct, 2), 'amber');
    lineChart($('#cost-chart'), {
      h: 250,
      yFmt: (v) => v.toFixed(2),
      series: [
        { color: '#58a6ff', points: grid.map((x, k) => ({ y: x.sharpe, label: fmt.raw(x.cost_bps_one_way, 0) })) },
        { color: '#3fb950', points: grid.map((x) => ({ y: x.cagr })) },
        { color: '#f85149', points: grid.map((x) => ({ y: x.max_dd })) },
      ],
    });
  };
  slider.addEventListener('input', () => draw(Number(slider.value)));
  draw(Number(slider.value));
}

/* ----------------------------------------------------------- Correlation */

function renderCorrelation(data) {
  if (data.error) {
    return `<div class="card"><h2>Strategy correlation</h2><div class="note bad">${esc(data.detail || data.error)}</div></div>`;
  }
  const verdictClass = data.verdict === 'DIVERSIFYING' ? 'green' : (data.verdict === 'SOME OVERLAP' ? 'amber' : 'red');
  return `
  <div class="grid g4">
    ${kpi('Verdict', esc(data.verdict), verdictClass)}
    ${kpi('Avg pairwise ρ', fmt.raw(data.average_pairwise, 3),
      data.average_pairwise >= 0.8 ? 'red' : (data.average_pairwise >= 0.55 ? 'amber' : 'green'))}
    ${kpi('Worst pair', data.worst_pair ? esc(data.worst_pair.a + ' / ' + data.worst_pair.b) : '—', '',
      data.worst_pair ? 'ρ = ' + fmt.raw(data.worst_pair.corr, 3) : '')}
    ${kpi('Families', fmt.num((data.families || []).length, 0), 'blue', `on ${(data.meta || {}).symbols || '—'} names`)}
  </div>
  <div class="card" style="margin-top:14px">
    <h2>Correlation matrix <span class="hint">daily strategy returns, net of 15 bps</span></h2>
    <div id="cr-heat"></div>
    <div class="note ${verdictClass === 'green' ? 'good' : (verdictClass === 'amber' ? 'warn' : 'bad')}">
      <b>${esc(data.verdict)}.</b> ${esc(data.verdict_note || '')}</div>
    <p class="small muted">Green = positive correlation (they win and lose together), red = negative.
    Above ~0.8 you are not diversifying, you are doubling the same bet.</p>
  </div>
  <div class="card">
    <h2>Rolling ${data.rolling_window_days}-day correlation vs MomReM</h2>
    <div id="cr-rolling"></div>
    <div class="legend" id="cr-legend"></div>
  </div>`;
}

function wireCorrelation() {
  const d = state.cache.correlation || {};
  if (d.matrix) heatmap($('#cr-heat'), d.families, d.matrix);
  const rolling = d.rolling_vs_momrem || {};
  const names = Object.keys(rolling);
  const palette = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f85149', '#38bdf8', '#ff9e64', '#a5d6ff'];
  if (names.length) {
    lineChart($('#cr-rolling'), {
      h: 260, yFmt: (v) => v.toFixed(1),
      series: names.map((n, i) => ({
        color: palette[i % palette.length],
        points: rolling[n].map((p) => ({ y: p.corr === null ? 0 : p.corr, label: p.date })),
      })),
    });
    $('#cr-legend').innerHTML = names.map((n, i) =>
      `<span><i style="background:${palette[i % palette.length]}"></i>${esc(n)}</span>`).join('');
  } else {
    $('#cr-rolling').innerHTML = '<p class="muted small">not enough families to compare</p>';
  }
}

/* --------------------------------------------------------- embeds & pages */

function framePage(title, src, note) {
  return `<div class="frame-bar"><span>${esc(note)}</span>
    <a href="${esc(src)}" target="_blank" rel="noopener">open standalone ↗</a></div>
  <div class="frame-wrap"><iframe src="${esc(src)}" title="${esc(title)}" loading="lazy"></iframe></div>`;
}

function renderLive() {
  return framePage('Live terminal', '/live',
    'Full intraday terminal: simulated ticks anchored to verified EOD history, candlestick charts, indicators, and the demo AI paper trader. Same process, same ledger.');
}
function renderPaper() {
  return framePage('Paper account', '/paper',
    'The virtual INR account: watchlist, risk guardrails, rebalance preview, ledger exports. Nothing here can reach a broker order API.');
}
function renderResearch() {
  return framePage('Research cockpit', '/cockpit',
    'Run experiments against the research engine: strategy catalogue, walk-forward validation, placebo tests, and the gate verdict.');
}

function renderData(data) {
  const ds = data.ds || {};
  const exp = ds.expansion || {};
  const uni = ds.universe || {};
  const fresh = ds.freshness || {};
  const pq = ds.prices_parquet || {};
  const rej = uni.rejected_counts || {};
  return `
  <div class="grid g4">
    ${kpi('Panel', fmt.num((ds.prices_info || {}).symbols, 0) + ' symbols', 'blue',
      (ds.prices_info || {}).date_range || '')}
    ${kpi('In universe', fmt.num(uni.size, 0), 'green', 'pass every filter')}
    ${kpi('Last bar', esc(fresh.last_bar || '—'), fresh.last_bar_age_days <= 5 ? 'green' : 'amber',
      fresh.last_bar_age_days + ' days old')}
    ${kpi('prices.parquet', pq.exists ? fmt.raw(pq.size_mb, 1) + ' MB' : 'missing', pq.exists ? 'green' : 'red',
      pq.exists ? fmt.num(pq.rows, 0) + ' rows' : 'the cockpit needs this')}
  </div>

  <div class="card" style="margin-top:14px">
    <h2>One data pipeline <span class="hint">this is why the pages no longer disagree</span></h2>
    <div class="note good">Every page reads the same panel through <code>datahub</code>.
      The Research Cockpit's <code>data/clean/prices.parquet</code> is <b>materialised from that same
      panel</b>, so "Missing — no price data found" next to a working Strategy Dashboard is no longer
      possible.</div>
    <table>
      <tr><td>Clean bundle</td><td class="right mono">${esc(String((ds.bundle || {}).files ?? '—'))} parquets</td></tr>
      <tr><td>Broad cache</td><td class="right mono">${(ds.bundle || {}).broad_cache ? 'built' : 'not built'}</td></tr>
      <tr><td>Layers</td><td class="right mono">${esc(JSON.stringify((ds.bundle || {}).layers || {}))}</td></tr>
      <tr><td>Rows in panel</td><td class="right mono">${fmt.num((ds.prices_info || {}).rows, 0)}</td></tr>
      <tr><td>Source last update</td><td class="right mono">${esc(fresh.source_last_update || '—')}</td></tr>
      <tr><td>Clean-validated symbols</td><td class="right mono">${esc(String(fresh.clean_validated_symbols ?? '—'))}</td></tr>
      <tr><td>Open quality issues</td><td class="right mono">${esc(String(fresh.open_quality_issues ?? '—'))}</td></tr>
    </table>
  </div>

  <div class="grid g-2-1">
    <div class="card">
      <h2>Add more stocks <span class="hint">from the ~${fmt.num(exp.raw_files, 0)} raw NSE files already in this repo</span></h2>
      <p class="small muted">The clean bundle holds ${fmt.num(exp.bundle_symbols, 0)} names. The raw EOD mirror
      already in this repository holds ${fmt.num(exp.raw_files, 0)}. Promoting more of them widens the universe
      the strategy can choose from — and moves the live signal closer to the universe the research was run on
      (${fmt.num(uni.research_parity_symbols, 0)} of ${fmt.num(uni.size, 0)} currently meet the 8-year rule).</p>
      <div class="controls" style="margin-top:10px">
        <label class="f">Min history (years)<input id="ex-years" type="number" min="1" max="20" step="1" value="8" style="width:120px"></label>
        <label class="f">Min median traded value ₹<input id="ex-value" type="number" min="100000" step="1000000" value="10000000" style="width:160px"></label>
        <label class="f">Limit (blank = all)<input id="ex-limit" type="number" min="1" step="50" placeholder="all" style="width:110px"></label>
        <button class="btn" data-act="expand">Build universe</button>
        <button class="btn ghost" data-act="prices">Rebuild prices.parquet</button>
      </div>
      <div class="note" style="margin-top:10px">Writes to <code>var/cache/broad_universe.parquet</code> —
      derived data, gitignored, rebuildable in ~30 s from files already committed. Takes about 30–60 s.</div>
      ${exp.cache_exists ? `<table style="margin-top:10px">
        <tr><td>Cache symbols</td><td class="right mono">${esc(String(exp.cache_symbols ?? '—'))}</td></tr>
        <tr><td>Cache rows</td><td class="right mono">${fmt.num(exp.cache_rows, 0)}</td></tr>
        <tr><td>Cache size</td><td class="right mono">${fmt.raw(exp.cache_mb, 1)} MB</td></tr>
        <tr><td>Last bar in cache</td><td class="right mono">${esc(exp.cache_last_bar || '—')}</td></tr></table>` : ''}
      <div id="ex-status" class="small muted" style="margin-top:8px"></div>
    </div>
    <div class="card">
      <h2>Why names are excluded</h2>
      <table>
        <tr><td>Too little history</td><td class="right mono">${fmt.num(rej.insufficient_history, 0)}</td></tr>
        <tr><td>Not liquid enough</td><td class="right mono">${fmt.num(rej.illiquid, 0)}</td></tr>
        <tr><td>Not traded recently</td><td class="right mono">${fmt.num(rej.not_recently_traded, 0)}</td></tr>
      </table>
      <div class="note warn" style="margin-top:10px">Every rejection is counted, never silent.
      If the basket ever looks too small, this table tells you which filter to relax.</div>
    </div>
  </div>`;
}

function wireData() {
  $('#content').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (mutationsRequireAuth()) { blockedByAuth(act); return; }
    try {
      btn.disabled = true;
      if (act === 'expand') {
        const limitRaw = $('#ex-limit').value;
        const payload = {
          min_years: Number($('#ex-years').value),
          min_avg_value: Number($('#ex-value').value),
        };
        if (limitRaw) payload.limit = Number(limitRaw);
        $('#ex-status').textContent = 'building… this takes 30–60 seconds';
        const out = await post('/api/universe/expand', payload);
        const res = out.result || out;
        $('#ex-status').innerHTML = res.error
          ? `<span class="badge red">failed</span> ${esc(res.error)}`
          : `<span class="badge green">done</span> accepted <b>${res.accepted}</b> symbols ·
             ${fmt.num(res.rows, 0)} rows · ${fmt.raw(res.cache_mb, 1)} MB ·
             ${esc(res.date_range || '')} · ${fmt.raw(res.seconds, 1)}s ·
             skipped ${esc(JSON.stringify(res.skipped || {}))}`;
        toast(res.error ? 'Universe build failed' : `Universe expanded: ${res.accepted} symbols`, res.error ? 'bad' : 'good');
        if (!res.error) { await post('/api/data/rebuild-prices', {}); route('data', true); }
      }
      if (act === 'prices') { await post('/api/data/rebuild-prices', {}); toast('prices.parquet rebuilt.', 'good'); route('data', true); }
      refreshStatusStrip();
    } catch (e) { toast(e.message, 'bad'); $('#ex-status').textContent = e.message; }
    finally { btn.disabled = false; }
  });
}

/* ----------------------------------------------------------- Operations */

function renderOperations(data) {
  const broker = data.broker_health || {};
  const recon = data.reconciliation || {};
  const sys = data.system_health || {};
  const sw = data.kill_switch || {};
  const audit = recon.ledger_audit || {};
  const beats = sys.heartbeats || {};
  const d = sys.data || {};
  const token = broker.token || {};

  const hbRows = Object.keys(beats).map((k) => {
    const b = beats[k];
    const klass = b.state === 'ok' ? 'green' : (b.state === 'stale' ? 'amber' : 'red');
    const detail = b.detail && typeof b.detail === 'object' ? JSON.stringify(b.detail) : (b.detail || '');
    return `<tr><td>${esc(k.replace(/_/g, ' '))}</td>
      <td><span class="badge ${klass}">${esc(b.state)}</span></td>
      <td class="num">${esc(fmt.age(b.age_seconds))}</td>
      <td class="small muted">${esc(fmt.stamp(b.at))}</td>
      <td class="small dim">${esc(String(detail).slice(0, 90))}</td></tr>`;
  }).join('');

  const histRows = (data.history || []).map((h) => `<tr>
    <td class="mono">${esc(fmt.stamp(h.at))}</td><td>${esc(h.action)}</td>
    <td class="small muted">${esc(h.reason || '')}</td><td class="small dim">${esc(h.by || '')}</td></tr>`).join('');

  const feed = sys.feed || {};

  return `
  <div class="grid g4">
    ${kpi('Headline', esc((data.headline || [])[0] || '—'),
      (data.headline || [])[0] === 'HEALTHY' ? 'green' : ((data.headline || [])[0] === 'COLD START' ? 'blue' : 'amber'),
      (data.headline || [])[1] || '')}
    ${kpi('Kill switch', sw.armed ? 'ARMED' : 'OFF', sw.armed ? 'red' : 'green',
      sw.armed ? esc(sw.reason || '') : 'trading allowed')}
    ${kpi('Broker', esc(broker.state), broker.state === 'HEALTHY' ? 'green' : (broker.state === 'NOT_CONFIGURED' ? 'amber' : 'red'))}
    ${kpi('Reconciliation', esc(recon.state), recon.state === 'MATCHED' || recon.state === 'FLAT' ? 'green' : (recon.state === 'NOT_STARTED' ? 'amber' : 'red'))}
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card">
      <h2>Broker health</h2>
      <table>
        <tr><td>State</td><td class="right"><span class="badge ${broker.state === 'HEALTHY' ? 'green' : 'amber'}">${esc(broker.state)}</span></td></tr>
        <tr><td>Token configured</td><td class="right mono">${broker.configured ? 'yes' : 'no'}</td></tr>
        <tr><td>Token expiry</td><td class="right mono">${token.expires_at ? esc(fmt.stamp(token.expires_at)) : 'no stored token'}</td></tr>
        <tr><td>Time to expiry</td><td class="right mono">${token.seconds_until_expiry === undefined ? '—'
          : (token.expired ? '<span class="badge red">EXPIRED</span>' : fmt.age(-token.seconds_until_expiry).replace(' ago', ' left'))}</td></tr>
        <tr><td>Last successful quote</td><td class="right mono">${esc(fmt.age(broker.last_quote_age_seconds))}</td></tr>
        <tr><td>Instruments mapped</td><td class="right mono">${esc(String((broker.chain || {}).instruments_mapped ?? ((broker.providers || [])[0] || {}).instruments_mapped ?? '—'))}</td></tr>
      </table>
      <div class="note" style="margin-top:10px">${esc(broker.detail || '')}</div>
      <div class="note warn">This system has <b>no order-placement path</b>. The Upstox integration is
      read-only quotes; the account is virtual and stored in <code>var/paper_trading.sqlite</code>.</div>
    </div>

    <div class="card">
      <h2>Kill switch</h2>
      <p class="small muted">A real, persisted flag. When armed it blocks paper rebalances, blocks
      automatic paper trading, and stops the demo bot. It is the operator's manual override; the
      deterministic <code>risk_kill</code> guard still owns automatic protection.</p>
      <table>
        <tr><td>State</td><td class="right"><span class="badge ${sw.armed ? 'red' : 'green'}">${sw.armed ? 'ARMED' : 'OFF'}</span></td></tr>
        <tr><td>Armed at</td><td class="right mono">${esc(fmt.stamp(sw.armed_at))}</td></tr>
        <tr><td>Reason</td><td class="right">${esc(sw.reason || '—')}</td></tr>
        <tr><td>By</td><td class="right">${esc(sw.armed_by || '—')}</td></tr>
      </table>
      <div class="controls" style="margin-top:12px">
        <button class="btn ${sw.armed ? 'green' : 'danger'}" id="ops-kill">${sw.armed ? 'DISARM KILL SWITCH' : 'ARM KILL SWITCH'}</button>
      </div>
    </div>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>Reconciliation</h2>
      <table>
        <tr><td>State</td><td class="right"><span class="badge ${recon.state === 'MATCHED' || recon.state === 'FLAT' ? 'green' : (recon.state === 'NOT_STARTED' ? 'amber' : 'red')}">${esc(recon.state)}</span></td></tr>
        <tr><td>Expected positions</td><td class="right mono">${esc(String(recon.expected_positions ?? '—'))}</td></tr>
        <tr><td>Actual positions</td><td class="right mono">${esc(String(recon.actual_positions ?? '—'))}</td></tr>
        <tr><td>Missing</td><td class="right mono">${esc((recon.missing || []).join(', ') || 'none')}</td></tr>
        <tr><td>Unexpected</td><td class="right mono">${esc((recon.unexpected || []).join(', ') || 'none')}</td></tr>
        <tr><td>Ledger audit</td><td class="right">${audit.passed === true ? '<span class="badge green">PASS</span>' : (audit.passed === false ? '<span class="badge red">FAIL</span>' : '<span class="badge">not run</span>')}</td></tr>
        <tr><td>Cash difference</td><td class="right mono">${fmt.inr(audit.cash_difference, 2)}</td></tr>
        <tr><td>Fills reconciled</td><td class="right mono">${fmt.num(audit.filled_order_count, 0)}</td></tr>
      </table>
      <p class="small muted" style="margin-top:9px">${esc(recon.detail || '')}</p>
      <div class="controls" style="margin-top:10px">
        <button class="btn ghost" id="ops-audit">Run reconciliation now</button>
        <a class="btn ghost" href="/api/paper/export?dataset=positions">positions CSV</a>
        <a class="btn ghost" href="/api/paper/export?dataset=orders">orders CSV</a>
      </div>
    </div>

    <div class="card">
      <h2>System health</h2>
      <table>
        <tr><td>Overall</td><td class="right"><span class="badge ${sys.overall === 'HEALTHY' ? 'green' : (sys.overall === 'COLD_START' ? 'blue' : 'amber')}">${esc(sys.overall)}</span></td></tr>
        <tr><td>Last bar ingested</td><td class="right mono">${esc(d.last_bar || '—')} <span class="dim">(${esc(String(d.last_bar_age_days ?? '—'))}d)</span></td></tr>
        <tr><td>Signal computed</td><td class="right mono">${esc(fmt.age((beats.signal_computed || {}).age_seconds))}</td></tr>
        <tr><td>Quotes refreshed</td><td class="right mono">${esc(fmt.age((beats.quote_refreshed || {}).age_seconds))}</td></tr>
        <tr><td>Feed mode</td><td class="right"><span class="badge ${feed.mode === 'LIVE' ? 'green' : 'blue'}">${esc(feed.mode || '—')}</span></td></tr>
        <tr><td>Uptime</td><td class="right mono">${esc(fmt.age((sys.process || {}).uptime_seconds))}</td></tr>
      </table>
      <p class="small muted" style="margin-top:9px">${esc(feed.note || '')}</p>
    </div>
  </div>

  <div class="card">
    <h2>Component heartbeats</h2>
    <div class="scroll"><table>
      <thead><tr><th>Component</th><th>State</th><th class="num">Age</th><th>Last run</th><th>Detail</th></tr></thead>
      <tbody>${hbRows}</tbody></table></div>
  </div>

  <div class="card">
    <h2>Operator action log</h2>
    <table><thead><tr><th>When</th><th>Action</th><th>Reason</th><th>By</th></tr></thead>
    <tbody>${histRows || '<tr><td colspan="4" class="muted">no recorded actions yet</td></tr>'}</tbody></table>
  </div>`;
}

function wireOperations() {
  const kill = $('#ops-kill');
  if (kill) kill.addEventListener('click', toggleKillSwitch);
  const audit = $('#ops-audit');
  if (audit) audit.addEventListener('click', async () => {
    try {
      const r = await api('/api/paper/audit');
      toast(r.passed ? `Reconciliation PASSED — ${r.filled_order_count} fills` : 'Reconciliation FAILED', r.passed ? 'good' : 'bad');
      route('operations', true);
    } catch (e) { toast(e.message, 'bad'); }
  });
}

/* ----------------------------------------------------------------- guide */

function renderGuide() {
  return `<div class="card guide" id="guide-body"><p class="muted">loading the guide…</p></div>`;
}

/** Minimal, dependency-free markdown renderer (headings, lists, tables, code, quotes). */
function mdToHtml(md) {
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let inCode = false, inList = null, inTable = false, inQuote = false;
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
  const closeList = () => { if (inList) { out.push(`</${inList}>`); inList = null; } };
  const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; } };
  const closeQuote = () => { if (inQuote) { out.push('</blockquote>'); inQuote = false; } };

  for (let raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (/^```/.test(line)) {
      closeList(); closeTable(); closeQuote();
      out.push(inCode ? '</pre>' : '<pre>');
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(esc(raw)); continue; }
    if (/^\s*$/.test(line)) { closeList(); closeTable(); closeQuote(); continue; }
    if (/^---+$/.test(line)) { closeList(); closeTable(); closeQuote(); out.push('<hr>'); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); closeTable(); closeQuote(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    if (/^>\s?/.test(line)) {
      closeList(); closeTable();
      if (!inQuote) { out.push('<blockquote>'); inQuote = true; }
      out.push(inline(line.replace(/^>\s?/, '')) + '<br>');
      continue;
    }
    closeQuote();
    const tr = line.match(/^\|(.+)\|\s*$/);
    if (tr) {
      closeList();
      const cells = tr[1].split('|').map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      if (!inTable) {
        out.push('<table><thead><tr>' + cells.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>');
        inTable = true;
      } else {
        out.push('<tr>' + cells.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>');
      }
      continue;
    }
    closeTable();
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? 'ul' : 'ol';
      if (inList !== want) { closeList(); out.push(`<${want}>`); inList = want; }
      out.push('<li>' + inline((ul || ol)[1]) + '</li>');
      continue;
    }
    closeList();
    out.push('<p>' + inline(line) + '</p>');
  }
  closeList(); closeTable(); closeQuote();
  if (inCode) out.push('</pre>');
  return out.join('\n');
}

async function wireGuide() {
  try {
    const res = await fetch('/guide.md', { cache: 'no-store' });
    const text = await res.text();
    $('#guide-body').innerHTML = mdToHtml(text);
    window.scrollTo(0, 0);
  } catch (e) {
    $('#guide-body').innerHTML = `<div class="note bad">Could not load the guide: ${esc(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- router */

const ROUTES = {
  overview: {
    title: 'Overview',
    load: async () => {
      const [ov, div, sig] = await Promise.all([
        api('/api/overview?capital=' + state.capital),
        api('/api/divergence?capital=' + state.capital),
        api('/api/strategy/signal?capital=' + state.capital).catch(() => null),
      ]);
      state.cache.overview = ov;
      state.cache.divergence = div;
      state.cache.signal = sig;
      return { render: () => renderOverview(ov), wire: wireOverview };
    },
  },
  strategy: {
    title: 'Strategy',
    load: async () => {
      const [sig, regime, sizing] = await Promise.all([
        api('/api/strategy/signal?capital=' + state.capital),
        api('/api/regime').catch(() => null),
        api('/api/sizing?capital=' + state.capital).catch(() => null),
      ]);
      state.cache.signal = sig.signal;
      state.cache.regime = regime;
      state.cache.sizing = sizing;
      state.cache.regimeLine = regime ? buildRegimeLine(regime) : [];
      const data = {
        signal: sig.signal,
        regime: (regime || {}).summary,
        sizing,
        universe: (sig.signal || {}).universe,
      };
      return { render: () => renderStrategy(data), wire: wireStrategy };
    },
  },
  divergence: {
    title: 'Backtest vs live divergence',
    load: async () => {
      const d = await api('/api/divergence?capital=' + state.capital);
      state.cache.divergence = d;
      return { render: () => renderDivergence(d), wire: wireDivergence };
    },
  },
  risk: {
    title: 'Risk, costs & position sizing',
    load: async () => {
      const [costs, sizing] = await Promise.all([
        api('/api/cost-sensitivity'),
        api('/api/sizing?capital=' + state.capital),
      ]);
      state.cache.costs = costs;
      state.cache.sizing = sizing;
      return { render: () => renderRisk({ costs, sizing }), wire: wireRisk };
    },
  },
  correlation: {
    title: 'Strategy correlation',
    load: async () => {
      const c = await api('/api/correlation');
      state.cache.correlation = c;
      return { render: () => renderCorrelation(c), wire: wireCorrelation };
    },
  },
  live: { title: 'Live terminal', load: async () => ({ render: renderLive, wire: null }) },
  paper: { title: 'Paper account', load: async () => ({ render: renderPaper, wire: null }) },
  research: { title: 'Research cockpit', load: async () => ({ render: renderResearch, wire: null }) },
  data: {
    title: 'Data & universe',
    load: async () => {
      const ds = await api('/api/data-status');
      return { render: () => renderData({ ds }), wire: wireData };
    },
  },
  operations: {
    title: 'Operations',
    load: async () => {
      const ops = await api('/api/operations');
      return { render: () => renderOperations(ops), wire: wireOperations };
    },
  },
  guide: { title: 'Beginner guide', load: async () => ({ render: renderGuide, wire: wireGuide }) },
};

function buildRegimeLine(regime) {
  // The API already downsamples the real market proxy and its 100d SMA.
  // (This used to plot the equity curve twice and call it a proxy, which
  // drew a plausible-looking but meaningless pair of identical lines.)
  const series = (regime && regime.proxy_series) || [];
  return series.map((p) => ({
    date: p.date, proxy: p.proxy, sma: p.sma, label: p.label,
  }));
}

async function route(name, force) {
  const def = ROUTES[name] || ROUTES.overview;
  state.route = ROUTES[name] ? name : 'overview';
  $('#page-title').textContent = def.title;
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === state.route));
  if (!force && state.route !== name) return;
  $('#content').innerHTML = '<div class="loading">Loading ' + esc(def.title) + '…</div>';
  try {
    const page = await def.load();
    $('#content').innerHTML = page.render();
    if (page.wire) page.wire();
  } catch (e) {
    $('#content').innerHTML = `<div class="card"><h2>${esc(def.title)}</h2>
      <div class="note bad"><b>Could not load this panel.</b> ${esc(e.message)}
      <br><span class="dim">Check <a href="#operations">Operations</a> for the component that is missing data.</span></div></div>`;
  }
}

function startAutoRefresh() {
  state.timers.forEach(clearInterval);
  state.timers = [
    setInterval(refreshStatusStrip, 60000),
    setInterval(refreshFeedChips, 30000),
  ];
}

async function boot() {
  $('#btn-nav').addEventListener('click', () => $('#sidebar').classList.toggle('collapsed'));
  $('#btn-refresh').addEventListener('click', async () => {
    $('#btn-refresh').disabled = true;
    try {
      await Promise.all([refreshStatusStrip(), refreshFeedChips(), refreshKillSwitch()]);
      await route(state.route, true);
      toast('Refreshed.', 'good');
    } catch (e) { toast(e.message, 'bad'); }
    finally { $('#btn-refresh').disabled = false; }
  });
  $('#killbtn').addEventListener('click', toggleKillSwitch);
  window.addEventListener('hashchange', () => route(location.hash.replace('#', '') || 'overview'));

  const initial = location.hash.replace('#', '') || 'overview';
  await Promise.all([refreshStatusStrip(), refreshFeedChips(), refreshKillSwitch()]);
  await route(initial, true);
  startAutoRefresh();
}

boot();
