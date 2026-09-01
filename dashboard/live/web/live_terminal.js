/* Quant India — Live Terminal
 * Self-contained canvas charting engine + terminal app. No external libraries.
 */
"use strict";

/* ================================================================ utilities */

const $ = (sel) => document.querySelector(sel);
const INR = (v, digits = 2) =>
  v == null || !isFinite(v) ? "—" : new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(v);
const COMPACT = (v) => {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
  if (Math.abs(v) >= 1e5) return (v / 1e5).toFixed(2) + "L";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
};
const SGN = (v, digits = 2) => (v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + INR(v, digits));

const IST = "Asia/Kolkata";
const timeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: false });
const dayFmt = new Intl.DateTimeFormat("en-GB", { timeZone: IST, day: "2-digit", month: "short" });
const dayYearFmt = new Intl.DateTimeFormat("en-GB", { timeZone: IST, day: "2-digit", month: "short", year: "2-digit" });
const dateTimeFmt = new Intl.DateTimeFormat("en-GB", { timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });

const clockFmt = new Intl.DateTimeFormat("en-GB", { timeZone: IST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

function niceStep(rough) {
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const frac = rough / pow;
  let nice;
  if (frac <= 1) nice = 1; else if (frac <= 2) nice = 2; else if (frac <= 2.5) nice = 2.5;
  else if (frac <= 5) nice = 5; else nice = 10;
  return nice * pow;
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

/* =========================================================== indicators */

function emaSeries(values, period) {
  const out = new Array(values.length).fill(null);
  if (values.length < period) return out;
  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i];
  let prev = seed / period;
  out[period - 1] = prev;
  const k = 2 / (period + 1);
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}
function smaSeries(values, period) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}
function rsiSeries(closes, period = 14) {
  const out = new Array(closes.length).fill(null);
  if (closes.length <= period) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gain += d; else loss -= d;
  }
  gain /= period; loss /= period;
  out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}
function macdSeries(closes) {
  const fast = emaSeries(closes, 12), slow = emaSeries(closes, 26);
  const macd = closes.map((_, i) => (fast[i] != null && slow[i] != null ? fast[i] - slow[i] : null));
  const valid = macd.map((v, i) => v);
  const signal = emaSeries(closes.map((c, i) => (macd[i] == null ? 0 : macd[i])), 9).map((v, i) => (macd[i] == null ? null : v));
  const hist = macd.map((v, i) => (v != null && signal[i] != null ? v - signal[i] : null));
  return { macd, signal, hist };
}
function bbandsSeries(closes, period = 20, mult = 2) {
  const mid = smaSeries(closes, period);
  const up = new Array(closes.length).fill(null);
  const lo = new Array(closes.length).fill(null);
  for (let i = period - 1; i < closes.length; i++) {
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) s += (closes[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / period);
    up[i] = mid[i] + mult * sd;
    lo[i] = mid[i] - mult * sd;
  }
  return { mid, up, lo };
}
/* Session-aware VWAP over a 1-minute buffer: returns vwap per 1m bar. */
function vwapOf1m(bars) {
  const out = new Array(bars.length).fill(null);
  let cumPV = 0, cumV = 0, dayKey = "";
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    const dk = dayKeyOf(b.t);
    if (dk !== dayKey) { dayKey = dk; cumPV = 0; cumV = 0; }
    const tp = (b.h + b.l + b.c) / 3;
    cumPV += tp * b.v; cumV += b.v;
    out[i] = cumV > 0 ? cumPV / cumV : b.c;
  }
  return out;
}
function dayKeyOf(tMs) {
  return dateTimeOf(tMs).slice(0, 10);
}
function dateTimeOf(tMs) {
  return new Date(tMs).toLocaleString("en-CA", { timeZone: IST });
}

/* ================================================================ theme */

const THEMES = {
  dark: {
    bg: "#0b0e14", grid: "rgba(120,140,180,0.07)", axis: "rgba(120,140,180,0.14)",
    text: "#78829a", textStrong: "#d7dce6",
    up: "#26a69a", down: "#ef5350",
    lastUp: "#4be3c8", lastDown: "#ff7d7a",
    cross: "rgba(200,210,230,0.35)",
    crossTagBg: "#2a3446", crossTagFg: "#e6ebf4",
    ema20: "#f5a623", ema50: "#38bdf8", sma200: "#a78bfa", vwap: "#22d3ee",
    bbMid: "#64748b", bb: "#475569",
    entry: "#26a69a", stop: "#ef5350", target: "#38bdf8",
    watermark: "rgba(215,220,230,0.045)",
    rsi: "#38bdf8", macdLine: "#38bdf8", macdSignal: "#f5a623",
    axisTagBg: "#1c2431",
  },
  light: {
    bg: "#ffffff", grid: "rgba(30,50,90,0.07)", axis: "rgba(30,50,90,0.16)",
    text: "#5c6778", textStrong: "#1c2430",
    up: "#0d9488", down: "#dc2626",
    lastUp: "#0f766e", lastDown: "#b91c1c",
    cross: "rgba(20,30,50,0.4)",
    crossTagBg: "#31405a", crossTagFg: "#f2f6fc",
    ema20: "#d97706", ema50: "#0284c7", sma200: "#7c3aed", vwap: "#0891b2",
    bbMid: "#94a3b8", bb: "#cbd5e1",
    entry: "#0d9488", stop: "#dc2626", target: "#0284c7",
    watermark: "rgba(28,36,48,0.05)",
    rsi: "#0284c7", macdLine: "#0284c7", macdSignal: "#d97706",
    axisTagBg: "#31405a",
  },
};
const theme = () => THEMES[document.documentElement.dataset.theme] || THEMES.dark;

/* ================================================================ chart */

class Chart {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.candles = [];
    this.type = "candles";
    this.overlays = { ema20: false, ema50: false, sma200: false, vwap: true, bb: false };
    this.panes = { rsi: false, macd: false };
    this.lines = []; // {price,label,color,dash}
    this.markers = []; // {i,side,price}
    this.watermark = "";
    this.view = { start: 0, end: 0 };
    this.follow = true;
    this.minBars = 15;
    this.cross = null; // {x,y,i}
    this.hoverIndex = null;
    this.onHover = null; // (index|null) callback
    this._ind = {};
    this._dirty = true;
    this._dpr = 1;
    this._w = 0; this._h = 0;
    this._drag = null;
    this._pinch = null;
    this._bind();
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement || canvas);
    this.resize();
    const loop = (t) => { this._frame(t); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  _bind() {
    const cv = this.cv;
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const f = e.deltaY < 0 ? 1 / 1.12 : 1.12;
      this.zoomAt(e.offsetX, f);
    }, { passive: false });
    cv.addEventListener("pointerdown", (e) => {
      cv.setPointerCapture(e.pointerId);
      this._drag = { x: e.offsetX, start: this.view.start, end: this.view.end, moved: false, id: e.pointerId };
      cv.classList.add("dragging");
    });
    cv.addEventListener("pointermove", (e) => {
      this.cross = { x: e.offsetX, y: e.offsetY };
      if (this._drag && e.pointerId === this._drag.id) {
        const bw = this._barWidth();
        const di = (e.offsetX - this._drag.x) / bw;
        if (Math.abs(di) > 0.3) this._drag.moved = true;
        this._setView(this._drag.start - di, this._drag.end - di);
      }
      this._dirty = true;
    });
    const endDrag = (e) => {
      if (this._drag) {
        if (!this._drag.moved) this._dirty = true;
        this._drag = null;
        cv.classList.remove("dragging");
      }
    };
    cv.addEventListener("pointerup", endDrag);
    cv.addEventListener("pointercancel", endDrag);
    cv.addEventListener("pointerleave", () => { this.cross = null; this._updateHover(); this._dirty = true; });
    cv.addEventListener("dblclick", () => this.fit());
  }

  resize() {
    const rect = this.cv.getBoundingClientRect();
    this._dpr = Math.min(2.5, window.devicePixelRatio || 1);
    this._w = Math.max(50, rect.width);
    this._h = Math.max(50, rect.height);
    this.cv.width = Math.round(this._w * this._dpr);
    this.cv.height = Math.round(this._h * this._dpr);
    this._dirty = true;
  }

  setData(candles, opts = {}) {
    this.candles = candles;
    this.lines = opts.lines || [];
    this.markers = opts.markers || [];
    this.watermark = opts.watermark || "";
    this._computeIndicators();
    this.fit();
  }
  setOverlay(key, on) { this.overlays[key] = on; this._computeIndicators(); this._dirty = true; }
  setPane(key, on) { this.panes[key] = on; this._dirty = true; }
  setType(t) { this.type = t; this._dirty = true; }
  setLines(lines) { this.lines = lines; this._dirty = true; }
  setMarkers(m) { this.markers = m; this._dirty = true; }

  _computeIndicators() {
    const c = this.candles;
    const closes = c.map((b) => b.c);
    this._ind = {
      ema20: this.overlays.ema20 ? emaSeries(closes, 20) : null,
      ema50: this.overlays.ema50 ? emaSeries(closes, 50) : null,
      sma200: this.overlays.sma200 ? smaSeries(closes, 200) : null,
      vwap: this.overlays.vwap && c.length ? this._vwapFor() : null,
      bb: this.overlays.bb ? bbandsSeries(closes) : null,
      rsi: this.panes.rsi ? rsiSeries(closes, 14) : null,
      macd: this.panes.macd ? macdSeries(closes) : null,
    };
    this._dirty = true;
  }
  _vwapFor() {
    const c = this.candles;
    const out = new Array(c.length).fill(null);
    let cumPV = 0, cumV = 0, dk = "";
    for (let i = 0; i < c.length; i++) {
      const b = c[i];
      const key = dayKeyOf(b.t) + (b.dayIdx == null ? "" : "-" + b.dayIdx);
      if (key !== dk) { dk = key; cumPV = 0; cumV = 0; }
      const tp = (b.h + b.l + b.c) / 3;
      cumPV += tp * b.v; cumV += b.v;
      out[i] = cumV > 0 ? cumPV / cumV : b.c;
    }
    return out;
  }

  /* -------- live updates ------------------------------------------------ */

  updateLast(bar) {
    const n = this.candles.length;
    if (n === 0) { this.candles = [bar]; this._computeIndicators(); this.fit(); return; }
    const last = this.candles[n - 1];
    if (bar.t === last.t) {
      last.h = Math.max(last.h, bar.h);
      last.l = Math.min(last.l, bar.l);
      last.c = bar.c;
      last.v = bar.v; // incoming bar carries the authoritative cumulative volume
      this._refreshTail();
    } else if (bar.t > last.t) {
      this.candles.push({ ...bar });
      if (this.candles.length > 12000) this.candles.splice(0, this.candles.length - 12000);
      this._refreshTail();
      if (this.follow) this._pinToLive();
    } else {
      return;
    }
    this._dirty = true;
  }

  _refreshTail() {
    // full recompute is cheap at terminal sizes; keeps every indicator honest
    this._computeIndicators();
  }

  fit() {
    const n = this.candles.length;
    if (!n) return;
    const count = Math.min(Math.max(n, 60), 240);
    this._setView(n - count + 1, n + count * 0.06);
  }

  _pinToLive() {
    const n = this.candles.length;
    const count = this.view.end - this.view.start;
    this._setView(n - count * 0.94, n + count * 0.06);
  }

  zoomAt(px, factor) {
    const n = this.candles.length;
    if (!n) return;
    const bw = this._barWidth();
    const idx = this.view.start + (px - this._padL()) / bw;
    const count = clamp((this.view.end - this.view.start) * factor, this.minBars, n + 200);
    const frac = (idx - this.view.start) / (this.view.end - this.view.start);
    this._setView(idx - count * frac, idx - count * frac + count);
  }

  _setView(start, end) {
    const n = this.candles.length;
    const count = end - start;
    if (start < -count * 0.6) { start = -count * 0.6; end = start + count; }
    const maxEnd = n + Math.max(30, count * 0.35);
    if (end > maxEnd) { end = maxEnd; start = end - count; }
    this.view.start = start;
    this.view.end = end;
    this.follow = end >= n - 2;
    this._dirty = true;
    this._updateHover();
  }
  goLive() { this.follow = true; this._pinToLive(); }

  _updateHover() {
    if (this.onHover) this.onHover(this.hoverIndex);
  }

  /* -------- geometry ----------------------------------------------------- */

  _padL() { return 8; }
  axisW() { return 62; }
  axisH() { return 24; }
  _paneHeights() {
    const rsi = this.panes.rsi ? 96 : 0;
    const macd = this.panes.macd ? 96 : 0;
    return { rsi, macd };
  }
  _layout() {
    const { rsi, macd } = this._paneHeights();
    const plotW = this._w - this.axisW() - this._padL();
    const mainH = this._h - this.axisH() - rsi - macd;
    return {
      plotW,
      main: { x: this._padL(), y: 0, w: plotW, h: mainH },
      rsi: rsi ? { x: this._padL(), y: mainH, w: plotW, h: rsi } : null,
      macd: macd ? { x: this._padL(), y: mainH + rsi, w: plotW, h: macd } : null,
    };
  }
  _barWidth() {
    const L = this._layout();
    return L.plotW / Math.max(1e-6, this.view.end - this.view.start);
  }
  _x(i, L) { return L.main.x + (i - this.view.start + 0.5) * this._barWidth(); }

  _priceRange(L) {
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(this.candles.length - 1, Math.ceil(this.view.end));
    let lo = Infinity, hi = -Infinity;
    const consider = (v) => { if (v == null || !isFinite(v)) return; if (v < lo) lo = v; if (v > hi) hi = v; };
    for (let i = i0; i <= i1; i++) { consider(this.candles[i].l); consider(this.candles[i].h); }
    for (const key of ["ema20", "ema50", "sma200", "vwap"]) {
      const s = this._ind[key];
      if (!s) continue;
      for (let i = i0; i <= i1; i++) consider(s[i]);
    }
    if (this._ind.bb) { for (let i = i0; i <= i1; i++) { consider(this._ind.bb.up[i]); consider(this._ind.bb.lo[i]); } }
    for (const ln of this.lines) consider(ln.price);
    const lastVisible = this.view.end >= this.candles.length - 0.5;
    if (lastVisible && this.candles.length) {
      const lp = this.candles[this.candles.length - 1].c;
      consider(lp * 1.004); consider(lp * 0.996);
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.06;
    return { lo: lo - pad, hi: hi + pad };
  }
  _y(price, L, range) {
    return L.main.y + ((range.hi - price) / (range.hi - range.lo)) * L.main.h;
  }
  _priceAtY(y, L, range) {
    return range.hi - ((y - L.main.y) / L.main.h) * (range.hi - range.lo);
  }

  /* -------- frame -------------------------------------------------------- */

  _frame(t) {
    // continuous render: last-price pulse + crosshair animation
    if (this.candles.length) this._render(t || 0);
    this._dirty = false;
  }

  _render(now) {
    const ctx = this.ctx;
    const T = theme();
    const W = this._w, H = this._h;
    ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = T.bg;
    ctx.fillRect(0, 0, W, H);
    const n = this.candles.length;
    if (!n) return;
    const L = this._layout();
    const bw = this._barWidth();
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(n - 1, Math.ceil(this.view.end));
    const range = this._priceRange(L);

    /* grid + price axis */
    const step = niceStep((range.hi - range.lo) / 6);
    ctx.font = "10.5px " + MONO;
    ctx.textBaseline = "middle";
    const firstTick = Math.ceil(range.lo / step) * step;
    for (let p = firstTick; p <= range.hi; p += step) {
      const y = this._y(p, L, range);
      if (y < 8 || y > L.main.h - 4) continue;
      ctx.strokeStyle = T.grid;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(L.main.x, y + 0.5); ctx.lineTo(L.main.x + L.plotW, y + 0.5); ctx.stroke();
      ctx.fillStyle = T.text;
      ctx.textAlign = "left";
      ctx.fillText(priceLabel(p), L.main.x + L.plotW + 8, y);
    }

    /* time axis */
    const labelEvery = Math.max(1, Math.ceil(84 / bw));
    const candles = this.candles;
    let lastDayKey = "";
    for (let i = i0; i <= i1; i += 1) {
      if (i % labelEvery !== 0) continue;
      const x = this._x(i, L);
      if (x < L.main.x + 4 || x > L.main.x + L.plotW - 4) continue;
      ctx.strokeStyle = T.grid;
      ctx.beginPath(); ctx.moveTo(x + 0.5, L.main.y);
      const bottomY = L.rsi ? (L.macd ? L.macd.y + L.macd.h : L.rsi.y + L.rsi.h) : L.main.y + L.main.h;
      ctx.lineTo(x + 0.5, bottomY); ctx.stroke();
      const dk = dayKeyOf(candles[i].t);
      let label;
      if (dk !== lastDayKey) { label = dayFmt.format(new Date(candles[i].t)); lastDayKey = dk; }
      else label = timeFmt.format(new Date(candles[i].t));
      ctx.fillStyle = T.text;
      ctx.textAlign = "center";
      ctx.fillText(label, x, H - this.axisH() / 2);
    }

    /* volume */
    const volH = L.main.h * 0.16;
    let maxV = 0;
    for (let i = i0; i <= i1; i++) maxV = Math.max(maxV, candles[i].v);
    if (maxV > 0) {
      for (let i = i0; i <= i1; i++) {
        const b = candles[i];
        const x = this._x(i, L);
        const h = (b.v / maxV) * volH;
        ctx.fillStyle = b.c >= b.o ? "rgba(38,166,154,0.32)" : "rgba(239,83,80,0.30)";
        ctx.fillRect(x - Math.max(1, bw * 0.35), L.main.y + L.main.h - h, Math.max(1, bw * 0.7), h);
      }
    }

    /* bbands fill */
    if (this._ind.bb) {
      const { up, lo, mid } = this._ind.bb;
      ctx.beginPath();
      let started = false;
      for (let i = i0; i <= i1; i++) {
        if (up[i] == null) continue;
        const x = this._x(i, L), y = this._y(up[i], L, range);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      for (let i = i1; i >= i0; i--) {
        if (lo[i] == null) continue;
        ctx.lineTo(this._x(i, L), this._y(lo[i], L, range));
      }
      ctx.closePath();
      ctx.fillStyle = hexA(T.bb, 0.10);
      ctx.fill();
      this._line(lo, L, range, T.bb, 1, [3, 3]);
      this._line(up, L, range, T.bb, 1, [3, 3]);
      this._line(mid, L, range, T.bbMid, 1, [2, 4]);
    }

    /* price series */
    const bodyW = Math.max(1, Math.min(bw * 0.72, 24));
    if (this.type === "candles") {
      for (let i = i0; i <= i1; i++) {
        const b = candles[i];
        const x = this._x(i, L);
        const up = b.c >= b.o;
        const color = up ? T.up : T.down;
        const yO = this._y(b.o, L, range), yC = this._y(b.c, L, range);
        const yH = this._y(b.h, L, range), yL = this._y(b.l, L, range);
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(1, bw * 0.09);
        ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();
        ctx.fillStyle = color;
        const top = Math.min(yO, yC);
        const hgt = Math.max(1, Math.abs(yC - yO));
        if (bw < 3) ctx.fillRect(x - 0.5, top, 1, hgt);
        else ctx.fillRect(x - bodyW / 2, top, bodyW, hgt);
      }
    } else if (this.type === "bars") {
      for (let i = i0; i <= i1; i++) {
        const b = candles[i];
        const x = this._x(i, L);
        const up = b.c >= b.o;
        ctx.strokeStyle = up ? T.up : T.down;
        ctx.lineWidth = Math.max(1, bw * 0.1);
        const yH = this._y(b.h, L, range), yL = this._y(b.l, L, range);
        const yO = this._y(b.o, L, range), yC = this._y(b.c, L, range);
        ctx.beginPath(); ctx.moveTo(x, yH); ctx.lineTo(x, yL); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x - bodyW / 2, yO); ctx.lineTo(x, yO); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x, yC); ctx.lineTo(x + bodyW / 2, yC); ctx.stroke();
      }
    } else {
      const up = candles[n - 1].c >= candles[Math.max(0, i0)].o;
      const color = up ? T.up : T.down;
      if (this.type === "area") {
        const g = ctx.createLinearGradient(0, L.main.y, 0, L.main.y + L.main.h);
        g.addColorStop(0, hexA(color, 0.28));
        g.addColorStop(1, hexA(color, 0.02));
        ctx.beginPath();
        let started = false;
        for (let i = i0; i <= i1; i++) {
          const x = this._x(i, L), y = this._y(candles[i].c, L, range);
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        ctx.lineTo(this._x(i1, L), L.main.y + L.main.h);
        ctx.lineTo(this._x(i0, L), L.main.y + L.main.h);
        ctx.closePath();
        ctx.fillStyle = g;
        ctx.fill();
        this._line(candles.map((b) => b.c), L, range, color, 1.6, null);
      } else {
        this._line(candles.map((b) => b.c), L, range, color, 1.5, null);
      }
    }

    /* overlay lines */
    if (this._ind.ema20) this._line(this._ind.ema20, L, range, T.ema20, 1.4, null);
    if (this._ind.ema50) this._line(this._ind.ema50, L, range, T.ema50, 1.4, null);
    if (this._ind.sma200) this._line(this._ind.sma200, L, range, T.sma200, 1.4, null);
    if (this._ind.vwap) this._line(this._ind.vwap, L, range, T.vwap, 1.2, [1, 0]);

    /* watermark */
    if (this.watermark) {
      ctx.save();
      ctx.font = "700 40px " + SANS;
      ctx.fillStyle = T.watermark;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(this.watermark, L.main.x + L.plotW / 2, L.main.y + L.main.h / 2);
      ctx.restore();
    }

    /* position / reference lines */
    for (const ln of this.lines) {
      const y = this._y(ln.price, L, range);
      if (y < 0 || y > L.main.h + 20) continue;
      ctx.strokeStyle = ln.color;
      ctx.lineWidth = 1;
      ctx.setLineDash(ln.dash || []);
      ctx.beginPath(); ctx.moveTo(L.main.x, y + 0.5); ctx.lineTo(L.main.x + L.plotW, y + 0.5); ctx.stroke();
      ctx.setLineDash([]);
      this._axisTag(L, y, ln.label, ln.color, true);
    }

    /* markers */
    for (const mk of this.markers) {
      if (mk.i < i0 - 1 || mk.i > i1 + 1) continue;
      const b = candles[mk.i];
      if (!b) continue;
      const x = this._x(mk.i, L);
      const yP = this._y(mk.price, L, range);
      const y = mk.side === "buy" ? Math.min(yP, this._y(b.l, L, range)) + 14 : Math.max(yP, this._y(b.h, L, range)) - 14;
      ctx.fillStyle = mk.side === "buy" ? T.up : T.down;
      ctx.beginPath();
      if (mk.side === "buy") { ctx.moveTo(x, y - 7); ctx.lineTo(x - 5.5, y + 3); ctx.lineTo(x + 5.5, y + 3); }
      else { ctx.moveTo(x, y + 7); ctx.lineTo(x - 5.5, y - 3); ctx.lineTo(x + 5.5, y - 3); }
      ctx.closePath(); ctx.fill();
    }

    /* last price line */
    const lastBar = candles[n - 1];
    if (this.view.end >= n - 0.5) {
      const up = lastBar.c >= (lastBar.prevClose ?? lastBar.o);
      const color = up ? T.lastUp : T.lastDown;
      const y = this._y(lastBar.c, L, range);
      ctx.strokeStyle = color;
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(L.main.x, y + 0.5); ctx.lineTo(L.main.x + L.plotW, y + 0.5); ctx.stroke();
      ctx.setLineDash([]);
      this._axisTag(L, y, priceLabel(lastBar.c), color, false, true);
      /* pulsing dot */
      const x = this._x(n - 1, L);
      const pulse = 0.5 + 0.5 * Math.sin(now / 320);
      ctx.fillStyle = hexA(color, 0.9);
      ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = hexA(color, 0.45 * (1 - pulse) + 0.1);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 4 + pulse * 5, 0, Math.PI * 2); ctx.stroke();
    }

    /* sub-panes */
    if (L.rsi && this._ind.rsi) this._renderRSI(L, range);
    if (L.macd && this._ind.macd) this._renderMACD(L, range);

    /* crosshair */
    if (this.cross && !this._drag) {
      const { x, y } = this.cross;
      if (x >= L.main.x && x <= L.main.x + L.plotW && y >= 0 && y <= Math.min(L.main.h, this._h - this.axisH())) {
        const idx = clamp(Math.round(this.view.start + (x - L.main.x) / bw - 0.5), 0, n - 1);
        const cx = this._x(idx, L);
        this.hoverIndex = idx;
        this._updateHover();
        ctx.strokeStyle = T.cross;
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cx + 0.5, 0); ctx.lineTo(cx + 0.5, this._h - this.axisH()); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(L.main.x, y + 0.5); ctx.lineTo(L.main.x + L.plotW, y + 0.5); ctx.stroke();
        ctx.setLineDash([]);
        this._axisTag(L, y, priceLabel(this._priceAtY(y, L, range)), T.crossTagBg, false, false);
        const label = dateTimeFmt.format(new Date(candles[idx].t));
        ctx.font = "10px " + MONO;
        const tw = ctx.measureText(label).width + 12;
        const tx = clamp(cx - tw / 2, L.main.x, L.main.x + L.plotW - tw);
        ctx.fillStyle = T.crossTagBg;
        roundRect(ctx, tx, this._h - this.axisH() + 2, tw, this.axisH() - 4, 4);
        ctx.fill();
        ctx.fillStyle = T.crossTagFg;
        ctx.textAlign = "center";
        ctx.fillText(label, tx + tw / 2, this._h - this.axisH() / 2);
      } else {
        this.hoverIndex = null;
        this._updateHover();
      }
    }

    /* pane separators */
    ctx.strokeStyle = T.axis;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, this._h - this.axisH() + 0.5); ctx.lineTo(W, this._h - this.axisH() + 0.5); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(W - this.axisW() + 0.5, 0); ctx.lineTo(W - this.axisW() + 0.5, this._h - this.axisH()); ctx.stroke();
  }

  _line(values, L, range, color, width, dash) {
    const ctx = this.ctx;
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(values.length - 1, Math.ceil(this.view.end));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const v = values[i];
      if (v == null || !isFinite(v)) continue;
      const x = this._x(i, L), y = this._y(v, L, range);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  _renderRSI(L, range) {
    const ctx = this.ctx;
    const T = theme();
    const P = L.rsi;
    const rsi = this._ind.rsi;
    ctx.strokeStyle = T.axis;
    ctx.beginPath(); ctx.moveTo(0, P.y + 0.5); ctx.lineTo(this._w, P.y + 0.5); ctx.stroke();
    const yOf = (v) => P.y + ((100 - v) / 100) * P.h;
    for (const lvl of [30, 50, 70]) {
      const y = yOf(lvl);
      ctx.strokeStyle = lvl === 50 ? T.grid : T.axis;
      ctx.setLineDash(lvl === 50 ? [] : [3, 3]);
      ctx.beginPath(); ctx.moveTo(P.x, y + 0.5); ctx.lineTo(P.x + P.w, y + 0.5); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = T.text;
      ctx.font = "9.5px " + MONO;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(String(lvl), P.x + P.w + 8, y);
    }
    /* band 30-70 */
    ctx.fillStyle = hexA(T.rsi, 0.04);
    ctx.fillRect(P.x, yOf(70), P.w, yOf(30) - yOf(70));
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(rsi.length - 1, Math.ceil(this.view.end));
    ctx.strokeStyle = T.rsi;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const v = rsi[i];
      if (v == null) continue;
      const x = this._x(i, L), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    const lastV = rsi[i1];
    if (lastV != null) this._paneTag(P, yOf(lastV), lastV.toFixed(1), T.rsi);
    ctx.fillStyle = T.text;
    ctx.font = "600 9.5px " + SANS;
    ctx.fillText("RSI 14", P.x + 6, P.y + 10);
  }

  _renderMACD(L, range) {
    const ctx = this.ctx;
    const T = theme();
    const P = L.macd;
    const { macd, signal, hist } = this._ind.macd;
    ctx.strokeStyle = T.axis;
    ctx.beginPath(); ctx.moveTo(0, P.y + 0.5); ctx.lineTo(this._w, P.y + 0.5); ctx.stroke();
    let maxAbs = 1e-9;
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(macd.length - 1, Math.ceil(this.view.end));
    for (let i = i0; i <= i1; i++) {
      for (const v of [hist[i], macd[i]]) if (v != null) maxAbs = Math.max(maxAbs, Math.abs(v));
    }
    const yOf = (v) => P.y + P.h / 2 - (v / maxAbs) * (P.h / 2 - 8);
    ctx.strokeStyle = T.grid;
    ctx.beginPath(); ctx.moveTo(P.x, yOf(0) + 0.5); ctx.lineTo(P.x + P.w, yOf(0) + 0.5); ctx.stroke();
    const bw = this._barWidth();
    for (let i = i0; i <= i1; i++) {
      const v = hist[i];
      if (v == null) continue;
      const x = this._x(i, L);
      const y = yOf(v), y0 = yOf(0);
      ctx.fillStyle = v >= 0 ? hexA(T.up, 0.55) : hexA(T.down, 0.55);
      ctx.fillRect(x - Math.max(0.5, bw * 0.25), Math.min(y, y0), Math.max(1, bw * 0.5), Math.max(1, Math.abs(y - y0)));
    }
    this._paneLine(macd, P, yOf, T.macdLine, 1.3);
    this._paneLine(signal, P, yOf, T.macdSignal, 1.3);
    ctx.fillStyle = T.text;
    ctx.font = "600 9.5px " + SANS;
    ctx.fillText("MACD 12 · 26 · 9", P.x + 6, P.y + 10);
  }

  _paneLine(values, P, yOf, color, width) {
    const ctx = this.ctx;
    const i0 = Math.max(0, Math.floor(this.view.start));
    const i1 = Math.min(values.length - 1, Math.ceil(this.view.end));
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const v = values[i];
      if (v == null) continue;
      const x = this._x(i, this._layout());
      if (!started) { ctx.moveTo(x, yOf(v)); started = true; } else ctx.lineTo(x, yOf(v));
    }
    ctx.stroke();
  }

  _axisTag(L, y, label, color, lightText, last) {
    const ctx = this.ctx;
    const T = theme();
    const x = L.main.x + L.plotW + 1;
    ctx.font = "700 10px " + MONO;
    const w = this.axisW() - 4;
    const h = 17;
    const yy = clamp(y - h / 2, 1, this._h - this.axisH() - h - 1);
    ctx.fillStyle = color;
    roundRect(ctx, x, yy, w, h, 4);
    ctx.fill();
    ctx.fillStyle = lightText ? "#08110f" : T.crossTagFg;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + 6, yy + h / 2 + 0.5);
  }

  _paneTag(P, y, label, color) {
    const ctx = this.ctx;
    const x = P.x + P.w + 1;
    ctx.font = "700 9.5px " + MONO;
    const w = this.axisW() - 4;
    ctx.fillStyle = color;
    roundRect(ctx, x, clamp(y - 8, P.y + 2, P.y + P.h - 18), w, 15, 4);
    ctx.fill();
    ctx.fillStyle = "#08110f";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + 6, clamp(y - 8, P.y + 2, P.y + P.h - 18) + 7.5);
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function hexA(hex, a) {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
}
function priceLabel(p) {
  if (Math.abs(p) >= 1000) return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(p);
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3, minimumFractionDigits: 2 }).format(p);
}
const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const SANS = 'Inter, system-ui, sans-serif';

/* ======================================================== equity curve */

class EquityChart {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.points = []; // {t, equity}
    this.baseline = 0;
    this._dirty = true;
    this._hover = null;
    this.tip = $("#eq-tip");
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement || canvas);
    this.resize();
    canvas.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect();
      this._hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      this._dirty = true;
    });
    canvas.addEventListener("pointerleave", () => { this._hover = null; this._dirty = true; });
    const loop = () => { if (this._dirty) { this._dirty = false; this._render(); } requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }
  resize() {
    const r = this.cv.getBoundingClientRect();
    const dpr = Math.min(2.5, window.devicePixelRatio || 1);
    this._w = Math.max(40, r.width); this._h = Math.max(30, r.height);
    this.cv.width = this._w * dpr; this.cv.height = this._h * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._dirty = true;
  }
  setPoints(points, baseline) {
    this.points = points;
    this.baseline = baseline;
    this._dirty = true;
  }
  _render() {
    const ctx = this.ctx, T = theme();
    const W = this._w, H = this._h;
    ctx.clearRect(0, 0, W, H);
    const pts = this.points;
    const padL = 6, padR = 62, padT = 10, padB = 18;
    const pw = W - padL - padR, ph = H - padT - padB;
    if (pw <= 10 || ph <= 10) return;
    ctx.font = "9.5px " + MONO;
    ctx.textBaseline = "middle";
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p.equity); hi = Math.max(hi, p.equity); }
    if (!isFinite(lo)) { lo = this.baseline || 0; hi = lo + 1; }
    lo = Math.min(lo, this.baseline); hi = Math.max(hi, this.baseline);
    const span = Math.max(hi - lo, 1e-9);
    lo -= span * 0.08; hi += span * 0.08;
    const xOf = (i) => padL + (i / Math.max(1, pts.length - 1)) * pw;
    const yOf = (v) => padT + ((hi - v) / (hi - lo)) * ph;
    /* grid */
    const step = niceStep((hi - lo) / 4);
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = yOf(v);
      ctx.strokeStyle = T.grid;
      ctx.beginPath(); ctx.moveTo(padL, y + 0.5); ctx.lineTo(padL + pw, y + 0.5); ctx.stroke();
      ctx.fillStyle = T.text;
      ctx.textAlign = "left";
      ctx.fillText(COMPACT(v), padL + pw + 7, y);
    }
    /* baseline */
    if (pts.length) {
      const yb = yOf(this.baseline);
      ctx.strokeStyle = T.axis;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(padL, yb + 0.5); ctx.lineTo(padL + pw, yb + 0.5); ctx.stroke();
      ctx.setLineDash([]);
      /* area */
      const up = pts[pts.length - 1].equity >= this.baseline;
      const color = up ? T.up : T.down;
      const g = ctx.createLinearGradient(0, padT, 0, padT + ph);
      g.addColorStop(0, hexA(color, 0.30));
      g.addColorStop(1, hexA(color, 0.02));
      ctx.beginPath();
      ctx.moveTo(xOf(0), yOf(pts[0].equity));
      for (let i = 1; i < pts.length; i++) ctx.lineTo(xOf(i), yOf(pts[i].equity));
      ctx.lineTo(xOf(pts.length - 1), padT + ph);
      ctx.lineTo(xOf(0), padT + ph);
      ctx.closePath();
      ctx.fillStyle = g;
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(xOf(0), yOf(pts[0].equity));
      for (let i = 1; i < pts.length; i++) ctx.lineTo(xOf(i), yOf(pts[i].equity));
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.stroke();
      const lx = xOf(pts.length - 1), ly = yOf(pts[pts.length - 1].equity);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI * 2); ctx.fill();
      /* x labels */
      ctx.fillStyle = T.text;
      ctx.textAlign = "center";
      const every = Math.max(1, Math.ceil(pts.length / 6));
      for (let i = 0; i < pts.length; i += every) {
        ctx.fillText(timeFmt.format(new Date(pts[i].t)), clamp(xOf(i), padL + 16, padL + pw - 16), H - padB / 2);
      }
      /* hover */
      if (this._hover && this._hover.x >= padL && this._hover.x <= padL + pw) {
        const idx = clamp(Math.round(((this._hover.x - padL) / pw) * (pts.length - 1)), 0, pts.length - 1);
        const p = pts[idx];
        const hx = xOf(idx), hy = yOf(p.equity);
        ctx.strokeStyle = T.cross;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(hx + 0.5, padT); ctx.lineTo(hx + 0.5, padT + ph); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = T.textStrong;
        ctx.beginPath(); ctx.arc(hx, hy, 3.5, 0, Math.PI * 2); ctx.fill();
        if (this.tip) {
          this.tip.hidden = false;
          this.tip.style.left = hx + "px";
          this.tip.style.top = hy + "px";
          const pnl = p.equity - this.baseline;
          this.tip.innerHTML = `${dateTimeFmt.format(new Date(p.t))}<br><b>₹${INR(p.equity)}</b> <span class="${pnl >= 0 ? "pos" : "neg"}">${SGN(pnl)}</span>`;
        }
      } else if (this.tip) this.tip.hidden = true;
    }
  }
}

/* =================================================================== app */

const App = {
  symbol: "RELIANCE",
  interval: "5m",
  universe: [],
  universeById: {},
  candles: [],          // visible interval candles (objects)
  buf1m: [],            // 1-minute buffer {t,o,h,l,c,v,d,m}
  buf1mVwap: [],
  session: {},
  indicators: {},
  position: null,
  fills: [],
  equityPoints: [],
  baseline: 0,
  bot: { enabled: false },
  portfolio: {},
  lastPrices: {},
  spark: {},            // symbol -> ring buffer of last prices
  sparkSeq: {},
  feedMode: "SIM",

  chart: null,
  eq: null,

  init() {
    this.chart = new Chart($("#chart"));
    this.chart.onHover = (i) => this.renderLegend(i);
    this.eq = new EquityChart($("#eq-canvas"));
    this.bindUI();
    this.tickClock();
    setInterval(() => this.tickClock(), 1000);
    this.boot();
  },

  async boot() {
    try {
      const [state] = await Promise.all([this.api("/api/live/state")]);
      this.applyState(state);
      await this.loadCandles();
      this.buildWatchlist();
      this.connectStream();
      $("#chart-loading").classList.add("hidden");
    } catch (e) {
      $("#chart-loading").innerHTML = `<div style="color:var(--down)">failed to load: ${e.message}</div>`;
    }
  },

  async api(path, opts) {
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.status);
    return data;
  },

  applyState(state) {
    this.universe = state.universe;
    this.universeById = Object.fromEntries(state.universe.map((u) => [u.symbol, u]));
    this.feedMode = state.feed.mode;
    this.baseline = state.paper.initial_capital;
    this.equityPoints = state.equity_history.map((p) => ({ t: new Date(p.recorded_at).getTime(), equity: p.equity }));
    this.fills = state.orders
      .filter((o) => o.strategy_id === "ai_demo" && o.status === "FILLED")
      .map((o) => ({ t: new Date(o.created_at).getTime(), side: o.side, symbol: o.symbol, quantity: o.quantity, price: o.fill_price }))
      .reverse();
    this.bot = state.portfolio.bot;
    this.lastPortfolio(state.portfolio);
    this.renderFeedBadge(state.feed);
    $("#bot-status").innerHTML = `<span class="b-dot"></span>${esc(this.bot.status || "")}`;
    $("#bot-status").classList.toggle("on", this.bot.enabled);
    $("#bot-toggle").setAttribute("aria-checked", String(!!this.bot.enabled));
    const riskSel = $("#bot-risk");
    riskSel.value = String(this.bot.risk_pct);

    const feedKey = `${state.feed.mode}_${state.universe.length}`;
    if (this._lastFeedKey !== feedKey) {
      this._lastFeedKey = feedKey;
      this.log("sys", "feed", `connected — ${state.feed.mode} feed · ${state.universe.length} symbols`);
    }

    this.renderEquity();
    this.renderTape();
  },

  renderFeedBadge(feed) {
    const live = feed && feed.mode === "LIVE";
    const badge = $("#feed-badge");
    badge.classList.toggle("live", !!live);
    badge.title = (feed && feed.note) || "";
    const up = (feed && feed.upstox) || {};
    $("#feed-label").textContent = live ? "LIVE · UPSTOX" : "SIM FEED";
    if (live) {
      const ms = up.market_status || "…";
      const lat = up.last_latency_ms != null ? ` · ${Math.round(up.last_latency_ms)}ms` : "";
      const nSim = (up.sim_fallback_symbols || []).length + (up.unmapped_symbols || []).length;
      const sim = nSim ? ` · ${nSim} sim-fallback` : "";
      $("#mkt-state").textContent = `NSE · ${ms}${lat}${sim}`;
    } else {
      const why = up.error ? ` · ${up.error}` : "";
      $("#mkt-state").textContent = "NSE · demo session (continuous clock)" + why;
    }
  },

  lastPortfolio(p) {
    this.portfolio = p;
    $("#st-equity").textContent = "₹" + INR(p.equity);
    const setPnl = (el, v) => {
      el.textContent = SGN(v);
      el.className = v >= 0 ? "pos" : "neg";
    };
    setPnl($("#st-today"), p.today_pnl);
    setPnl($("#st-total"), p.total_pnl);
    $("#st-cash").textContent = "₹" + INR(p.cash, 0);
    this.renderPositions(p.positions);
    this.renderBot(p.bot);
  },

  /* ------------------------------------------------------------- data */

  async loadCandles() {
    const intraday = !["1d", "1w"].includes(this.interval);
    const tf = intraday ? "1m" : this.interval;
    const res = await this.api(
      `/api/live/candles?symbol=${this.symbol}&interval=${tf}&limit=${intraday ? 6000 : 560}`
    );
    if (intraday) {
      this.buf1m = res.candles.map((c) => ({ t: c[0], o: c[1], h: c[2], l: c[3], c: c[4], v: c[5] }));
      this.tagDays(this.buf1m);
      this.buf1mVwap = vwapOf1m(this.buf1m);
      this.rebuildFromBuffer();
    } else {
      this.candles = res.candles.map((c) => ({ t: c[0], o: c[1], h: c[2], l: c[3], c: c[4], v: c[5] }));
    }
    this.session = res.session;
    this.indicators = res.indicators;
    this.position = res.position;
    if (this.candles.length) {
      this.candles[this.candles.length - 1].prevClose = res.session.prev_close;
    }
    this.chart.setData(this.candles, {
      lines: this.positionLines(),
      markers: this.markerFor(this.symbol),
      watermark: `${this.symbol} · ${this.interval.toUpperCase()}`,
    });
    this.chart.goLive();
    this.renderLegend();
    this.renderPosChip();
  },

  tagDays(bars) {
    let dk = "", d = 0;
    for (const b of bars) {
      const key = dayKeyOf(b.t);
      if (key !== dk) { dk = key; d++; b.dayIdx = d; } else b.dayIdx = d;
    }
  },

  bucketOf(b) {
    const t = new Date(b.t);
    const hm = t.getHours() * 60 + t.getMinutes();
    const mins = clamp(hm - 555, 0, 389);
    const bucket = Math.max(1, parseInt(this.interval) || 1);
    return (b.dayIdx || 1) * 10000 + Math.floor(mins / bucket);
  },

  rebuildFromBuffer() {
    if (this.interval === "1m") {
      this.candles = this.buf1m.map((b) => ({ ...b }));
    } else {
      const out = [];
      let cur = null, curKey = null;
      const vwapByBucket = new Map();
      for (let i = 0; i < this.buf1m.length; i++) {
        const b = this.buf1m[i];
        const key = this.bucketOf(b);
        if (key !== curKey) {
          if (cur) out.push(cur);
          curKey = key;
          cur = { t: b.t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v, dayIdx: b.dayIdx };
        } else {
          cur.h = Math.max(cur.h, b.h);
          cur.l = Math.min(cur.l, b.l);
          cur.c = b.c;
          cur.v += b.v;
        }
        vwapByBucket.set(key, this.buf1mVwap[i]);
      }
      if (cur) out.push(cur);
      for (const c of out) c.vwapHint = vwapByBucket.get(this.bucketOf(c));
      this.candles = out;
    }
    if (this.candles.length) this.candles[this.candles.length - 1].prevClose = this.session.prev_close;
  },

  positionLines() {
    const p = this.position;
    if (!p) return [];
    const T = theme();
    return [
      { price: p.entry, label: "ENTRY " + priceLabel(p.entry), color: T.entry, dash: [] },
      { price: p.stop, label: "STOP " + priceLabel(p.stop), color: T.stop, dash: [5, 4] },
      { price: p.target, label: "TARGET " + priceLabel(p.target), color: T.target, dash: [2, 3] },
    ];
  },

  markerFor(symbol) {
    if (!this.candles.length) return [];
    const out = [];
    for (const f of this.fills) {
      if (f.symbol !== symbol) continue;
      let best = -1, bestDt = Infinity;
      for (let i = 0; i < this.candles.length; i++) {
        const dt = Math.abs(this.candles[i].t - f.t);
        if (dt < bestDt) { bestDt = dt; best = i; }
        if (this.candles[i].t > f.t && dt < bestDt) break;
      }
      if (best >= 0 && bestDt < 20 * 60 * 60 * 1000) {
        out.push({ i: best, side: f.side === "BUY" ? "buy" : "sell", price: f.price });
      }
    }
    return out.slice(-40);
  },

  renderPosChip() {
    const el = $("#pos-chip");
    const p = this.position;
    const u = this.universeById[this.symbol];
    if (!p || !u) { el.hidden = true; return; }
    const pnl = (u.last - p.entry) * p.quantity;
    const pct = ((u.last / p.entry) - 1) * 100;
    el.hidden = false;
    el.className = "pos-chip " + (pnl >= 0 ? "long" : "short");
    el.textContent = `AI LONG ${p.quantity} ${this.symbol} @ ${INR(p.entry)} · P&L ${SGN(pnl, 0)} (${SGN(pct)}%)`;
  },

  /* ------------------------------------------------------------- watch */

  buildWatchlist() {
    const list = $("#wl-list");
    list.innerHTML = "";
    for (const u of this.universe) {
      const row = document.createElement("div");
      row.className = "wl-row" + (u.symbol === this.symbol ? " active" : "");
      row.dataset.sym = u.symbol;
      row.innerHTML = `
        <div class="w-sym">${u.symbol}<small>${esc(u.name)}</small></div>
        <canvas></canvas>
        <div class="w-last">—</div>
        <div class="w-chg">—</div>`;
      row.addEventListener("click", () => this.switchSymbol(u.symbol));
      list.appendChild(row);
      this.spark[u.symbol] = [];
      this.sparkSeq[u.symbol] = 0;
    }
    $("#wl-count").textContent = this.universe.length + " symbols";
    this.updateWatchlistRows();
  },

  updateWatchlistRows() {
    const rows = document.querySelectorAll(".wl-row");
    for (const row of rows) {
      const sym = row.dataset.sym;
      const lp = this.lastPrices[sym];
      const u = this.universeById[sym];
      if (!lp || !u) continue;
      const lastEl = row.querySelector(".w-last");
      const chgEl = row.querySelector(".w-chg");
      const prevText = lastEl.dataset.p;
      lastEl.textContent = INR(lp.last);
      if (prevText !== undefined && prevText !== lastEl.textContent) {
        lastEl.classList.remove("flash-up", "flash-down");
        void lastEl.offsetWidth;
        lastEl.classList.add(lp.dir > 0 ? "flash-up" : "flash-down");
      }
      lastEl.dataset.p = lastEl.textContent;
      chgEl.textContent = SGN(lp.chg, 2) + "%";
      chgEl.className = "w-chg " + (lp.chg >= 0 ? "up" : "down");
      const buf = this.spark[sym];
      if (buf) {
        this.sparkSeq[sym]++;
        if (this.sparkSeq[sym] % 2 === 0) { buf.push(lp.last); if (buf.length > 90) buf.shift(); }
        this.drawSpark(row.querySelector("canvas"), buf);
      }
    }
  },

  drawSpark(cv, buf) {
    if (!cv || buf.length < 2) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = cv.clientWidth || 100, h = cv.clientHeight || 26;
    if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    let lo = Infinity, hi = -Infinity;
    for (const v of buf) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const up = buf[buf.length - 1] >= buf[0];
    const T = theme();
    const color = up ? T.up : T.down;
    ctx.beginPath();
    for (let i = 0; i < buf.length; i++) {
      const x = (i / (buf.length - 1)) * w;
      const y = h - 2 - ((buf[i] - lo) / (hi - lo)) * (h - 4);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.stroke();
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, hexA(color, 0.22));
    g.addColorStop(1, hexA(color, 0));
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    ctx.fillStyle = g;
    ctx.fill();
  },

  /* -------------------------------------------------------- positions */

  renderPositions(positions) {
    const body = $("#pos-body");
    $("#pos-count").textContent = positions.length ? positions.length + " open" : "";
    if (!positions.length) {
      body.innerHTML = `<tr><td colspan="8" class="empty">No virtual positions — enable the AI paper trader.</td></tr>`;
      return;
    }
    body.innerHTML = positions.map((p) => {
      const cls = p.pnl >= 0 ? "pos" : "neg";
      return `<tr>
        <td class="sym">${p.symbol}${p.strategy === "ai_demo" ? '<span class="tag">AI</span>' : ""}</td>
        <td class="num">${p.quantity}</td>
        <td class="num">${INR(p.avg)}</td>
        <td class="num">${INR(p.last)}</td>
        <td class="num ${cls}">${SGN(p.pnl, 0)}</td>
        <td class="num ${cls}">${SGN(p.pnl_pct)}%</td>
        <td class="num" style="color:var(--down)">${p.stop ? INR(p.stop) : "—"}</td>
        <td class="num" style="color:var(--accent)">${p.target ? INR(p.target) : "—"}</td>
      </tr>`;
    }).join("");
  },

  renderTape() {
    const el = $("#tape");
    $("#tape-count").textContent = this.fills.length ? this.fills.length + " fills" : "";
    const rows = this.fills.slice(0, 30).reverse().map((f) => `
      <div class="tape-row">
        <span class="t-time">${timeFmt.format(new Date(f.t))}</span>
        <span class="t-side ${f.side === "BUY" ? "buy" : "sell"}">${f.side}</span>
        <span class="t-what">${f.quantity} ${f.symbol} @ ${INR(f.price)}</span>
      </div>`).join("");
    el.innerHTML = rows || `<div class="empty">No AI fills yet this session.</div>`;
  },

  renderBot(bot) {
    this.bot = bot;
    $("#bot-status").innerHTML = `<span class="b-dot"></span>${esc(bot.status || "")}`;
    $("#bot-status").classList.toggle("on", bot.enabled);
    $("#bot-toggle").setAttribute("aria-checked", String(!!bot.enabled));
    $("#bs-open").textContent = `${bot.open}/${bot.max_positions}`;
    $("#bs-wins").textContent = bot.wins;
    $("#bs-losses").textContent = bot.losses;
    $("#bs-wr").textContent = bot.win_rate == null ? "—" : (bot.win_rate * 100).toFixed(0) + "%";
    const real = $("#bs-real");
    real.textContent = SGN(bot.session_realized, 0);
    real.className = (bot.session_realized || 0) >= 0 ? "pos" : "neg";
    $("#bot-last").textContent = bot.last_signal || "No signal yet.";
  },

  renderEquity() {
    this.eq.setPoints(this.equityPoints, this.baseline);
    const pts = this.equityPoints;
    if (!pts.length) return;
    const last = pts[pts.length - 1].equity;
    let peak = -Infinity, dd = 0;
    for (const p of pts) { peak = Math.max(peak, p.equity); dd = Math.max(dd, (peak - p.equity) / peak); }
    const ret = (last / this.baseline - 1) * 100;
    $("#eq-dd").textContent = "DD " + (dd * 100).toFixed(2) + "%";
    const rp = $("#eq-ret");
    rp.textContent = "RET " + SGN(ret) + "%";
    rp.className = "pill " + (ret >= 0 ? "pos" : "neg");
    const first = new Date(pts[0].t), lastD = new Date(pts[pts.length - 1].t);
    $("#eq-period").textContent = `${dayYearFmt.format(first)} → ${dayYearFmt.format(lastD)} · live`;
  },

  renderLegend(index) {
    const el = $("#legend");
    if (!this.candles.length) { el.innerHTML = ""; return; }
    const i = index == null ? this.candles.length - 1 : clamp(index, 0, this.candles.length - 1);
    const b = this.candles[i];
    const prev = this.session.prev_close || b.o;
    const chg = ((b.c / prev) - 1) * 100;
    const cls = b.c >= b.o ? "u" : "d";
    const T = theme();
    let extra = "";
    const inds = this.chart._ind;
    if (inds.ema20 && inds.ema20[i] != null) extra += `<span>EMA20 <b style="color:${T.ema20}">${priceLabel(inds.ema20[i])}</b></span>`;
    if (inds.ema50 && inds.ema50[i] != null) extra += `<span>EMA50 <b style="color:${T.ema50}">${priceLabel(inds.ema50[i])}</b></span>`;
    if (inds.vwap && inds.vwap[i] != null) extra += `<span>VWAP <b style="color:${T.vwap}">${priceLabel(inds.vwap[i])}</b></span>`;
    if (inds.rsi && inds.rsi[i] != null) extra += `<span>RSI <b style="color:${T.rsi}">${inds.rsi[i].toFixed(1)}</b></span>`;
    el.innerHTML = `
      <span class="l-sym">${this.symbol}</span>
      <span class="l-tf">${this.interval.toUpperCase()}</span>
      <span>O <b>${priceLabel(b.o)}</b></span>
      <span>H <b>${priceLabel(b.h)}</b></span>
      <span>L <b>${priceLabel(b.l)}</b></span>
      <span>C <b class="${cls}">${priceLabel(b.c)}</b></span>
      <span class="${chg >= 0 ? "u" : "d"}">${SGN(chg)}%</span>
      <span>Vol <b>${COMPACT(b.v)}</b></span>
      ${extra}`;
  },

  /* ----------------------------------------------------------- stream */

  connectStream() {
    let es = null;
    let failures = 0;
    const connect = () => {
      if (es) {
        try { es.close(); } catch {}
      }
      es = new EventSource("/api/live/stream");
      es.addEventListener("hello", () => {
        failures = 0;
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
        this.api("/api/live/state").then((s) => this.applyState(s)).catch(() => {});
      });
      es.addEventListener("tick", (e) => this.onTick(JSON.parse(e.data)));
      es.addEventListener("portfolio", (e) => this.onPortfolio(JSON.parse(e.data)));
      es.addEventListener("fill", (e) => this.onFill(JSON.parse(e.data)));
      es.addEventListener("feed_mode", () => {
        // SIM↔LIVE transition: resync the full feed block
        this.api("/api/live/state").then((s) => {
          this.feedMode = s.feed.mode;
          this.renderFeedBadge(s.feed);
        }).catch(() => {});
      });
      es.addEventListener("heartbeat", (e) => {
        const d = JSON.parse(e.data);
        if (d.mode && d.mode !== this.feedMode) {
          this.feedMode = d.mode;
          this.renderFeedBadge({ mode: d.mode, upstox: {} });
        }
      });
      es.onerror = () => {
        failures++;
        if (failures >= 2) {
          try { es.close(); } catch {}
          if (!this._pollTimer) {
            this.log("warn", "stream", "SSE interrupted — using active polling fallback");
            this._pollTimer = setInterval(async () => {
              try {
                const s = await this.api("/api/live/state");
                this.applyState(s);
                const lp = Object.fromEntries(s.universe.map((u) => [u.symbol, u]));
                this.onTick({
                  t: s.clock?.now_ms || Date.now(),
                  p: Object.fromEntries(
                    Object.entries(lp).map(([k, u]) => [
                      k,
                      [u.last, u.open, u.prev_close, u.volume, u.session_high ?? u.last, u.session_low ?? u.last, u.vwap]
                    ])
                  )
                });
                if (s.portfolio) this.onPortfolio(s.portfolio);
              } catch { /* retry next tick */ }
            }, 2000);

            // Periodically attempt to reconnect SSE in background
            setTimeout(() => {
              if (this._pollTimer) connect();
            }, 15000);
          }
        }
      };
    };
    connect();
  },

  onTick(payload) {
    const prices = {};
    for (const [sym, arr] of Object.entries(payload.p)) {
      const [last, open, prev, vol, high, low, vwap] = arr;
      const u = this.universeById[sym];
      prices[sym] = {
        last, open, prev, vol, high, low, vwap: vwap ?? null,
        chg: prev ? ((last / prev) - 1) * 100 : 0,
        dir: 0,
      };
      const prevLast = this.lastPrices[sym] ? this.lastPrices[sym].last : null;
      if (prevLast != null) prices[sym].dir = last > prevLast ? 1 : last < prevLast ? -1 : 0;
      this.lastPrices[sym] = { last, prev: prevLast };
      this.universeById[sym] = Object.assign(u || { symbol: sym }, { last, open, prev_close: prev, chg_pct: prices[sym].chg, vwap: vwap ?? u?.vwap, volume: vol, session_high: high, session_low: low });
    }
    this.lastPrices = prices;
    this.updateWatchlistRows();
    /* header equity quick update */
    const u0 = this.universeById[this.symbol];
    if (u0) this.renderPosChip();
    /* live candle update */
    const intraday = !["1d", "1w"].includes(this.interval);
    const src = this.universeById[this.symbol];
    if (!src) return;
    const nowMin = Math.floor(Date.now() / 60000) * 60000;
    if (intraday) {
      let bar = this.buf1m[this.buf1m.length - 1];
      if (bar && bar.t >= nowMin) {
        bar.h = Math.max(bar.h, src.last);
        bar.l = Math.min(bar.l, src.last);
        bar.c = src.last;
        bar.v = Math.max(0, src.volume - (bar.vStart ?? src.volume));
      } else {
        const prevBar = bar;
        const newBar = {
          t: nowMin, o: src.last, h: src.last, l: src.last, c: src.last, v: 0,
          vStart: src.volume, dayIdx: prevBar ? prevBar.dayIdx : 1,
        };
        if (prevBar) {
          const p = new Date(prevBar.t);
          const hm = p.getHours() * 60 + p.getMinutes();
          if (hm >= 930 || hm < 555) newBar.dayIdx = prevBar.dayIdx + 1;
        }
        this.buf1m.push(newBar);
        if (this.buf1m.length > 12000) this.buf1m.shift();
        bar = newBar;
      }
      this.buf1mVwap[this.buf1m.length - 1] = src.vwap ?? this.buf1mVwap[this.buf1m.length - 1];
      this.rebuildFromBuffer();
      this.candles[this.candles.length - 1].prevClose = this.session.prev_close;
      this.chart.updateLast(this.candles[this.candles.length - 1]);
    } else {
      const c = this.candles[this.candles.length - 1];
      if (c) {
        c.h = Math.max(c.h, src.session_high ?? src.last);
        c.l = Math.min(c.l, src.session_low ?? src.last);
        c.c = src.last;
        c.v = src.volume;
        this.chart.updateLast(c);
      }
    }
    if (Math.floor(Date.now() / 2500) !== this._legendSeq) {
      this._legendSeq = Math.floor(Date.now() / 2500);
      this.renderLegend();
    }
  },

  onPortfolio(p) {
    this.lastPortfolio(p);
    /* equity curve: append a live point each second */
    const lastPt = this.equityPoints[this.equityPoints.length - 1];
    if (!lastPt || Date.now() - lastPt.t > 4000) {
      this.equityPoints.push({ t: Date.now(), equity: p.equity });
      if (this.equityPoints.length > 3600) this.equityPoints.shift();
      this.renderEquity();
    } else {
      lastPt.equity = p.equity;
      lastPt.t = Date.now();
      this.renderEquity();
    }
    if (this.position) this.renderPosChip();
  },

  onFill(f) {
    this.fills.push(f);
    if (this.fills.length > 200) this.fills.shift();
    this.renderTape();
    if (f.side === "BUY") {
      this.toast("buy", `AI BUY · ${f.quantity} ${f.symbol}`, `@ ${INR(f.price)} · ${f.reason}${f.stop ? ` · stop ${INR(f.stop)}` : ""}${f.target ? ` · target ${INR(f.target)}` : ""}`);
      this.log("ai", "buy", `${f.quantity} ${f.symbol} @ ${INR(f.price)} — ${f.reason}`);
    } else {
      const pnlTxt = f.realized != null ? ` · P&L ${SGN(f.realized, 0)}` : "";
      this.toast(f.realized != null && f.realized < 0 ? "sell" : "buy", `AI SELL · ${f.quantity} ${f.symbol}`, `@ ${INR(f.price)} · ${f.reason}${pnlTxt}`);
      this.log("ai", "sell", `${f.quantity} ${f.symbol} @ ${INR(f.price)} — ${f.reason}${pnlTxt}`);
    }
    if (f.symbol === this.symbol) {
      this.chart.setMarkers(this.markerFor(this.symbol));
      this.position = f.side === "BUY" ? { quantity: f.quantity, entry: f.price, stop: f.stop, target: f.target } : null;
      this.chart.setLines(this.positionLines());
      this.renderPosChip();
    }
  },

  /* -------------------------------------------------------------- ui */

  bindUI() {
    $("#tf-group").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-tf]");
      if (!btn) return;
      this.setInterval(btn.dataset.tf);
    });
    $("#type-group").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-type]");
      if (!btn) return;
      $("#type-group").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
      this.chart.setType(btn.dataset.type);
    });
    $("#ind-group").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.dataset.ind) {
        const on = !btn.classList.contains("on");
        btn.classList.toggle("on", on);
        this.chart.setOverlay(btn.dataset.ind, on);
      } else if (btn.dataset.pane) {
        const on = !btn.classList.contains("on");
        btn.classList.toggle("on", on);
        this.chart.setPane(btn.dataset.pane, on);
      }
    });
    /* search */
    const input = $("#sym-input");
    const drop = $("#sym-drop");
    const renderDrop = () => {
      const q = input.value.trim().toUpperCase();
      const matches = this.universe.filter((u) => !q || u.symbol.includes(q) || u.name.toUpperCase().includes(q)).slice(0, 14);
      drop.innerHTML = matches.map((u) => `
        <div class="row" data-sym="${u.symbol}">
          <span class="sym">${u.symbol}</span>
          <span class="nm">${esc(u.name)}${u.index ? " · index" : ""}</span>
          <span class="px">${INR(u.last)} <span class="${u.chg_pct >= 0 ? "pos" : "neg"}">${SGN(u.chg_pct)}%</span></span>
        </div>`).join("");
      drop.hidden = matches.length === 0;
      drop.querySelectorAll(".row").forEach((r) => {
        r.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          this.switchSymbol(r.dataset.sym);
          drop.hidden = true;
          input.value = r.dataset.sym;
        });
      });
    };
    input.addEventListener("focus", renderDrop);
    input.addEventListener("input", renderDrop);
    input.addEventListener("blur", () => setTimeout(() => { drop.hidden = true; }, 180));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const first = drop.querySelector(".row");
        if (first) this.switchSymbol(first.dataset.sym);
        drop.hidden = true;
        input.value = this.symbol;
        input.blur();
      }
      if (e.key === "Escape") { drop.hidden = true; input.blur(); }
    });
    /* bot controls */
    $("#bot-toggle").addEventListener("click", async () => {
      const next = !this.bot.enabled;
      try {
        const out = await this.api("/api/live/bot", {
          method: "POST",
          headers: window.QUANT_DASHBOARD_TOKEN
            ? { "Content-Type": "application/json", "X-Quant-Token": window.QUANT_DASHBOARD_TOKEN }
            : { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next, risk_pct: this.bot.risk_pct }),
        });
        this.renderBot(out);
        this.log("ai", "bot", next ? "AI paper trader ENABLED" : "AI paper trader disabled");
        this.toast(next ? "buy" : "warn", next ? "AI paper trader ON" : "AI paper trader OFF", next ? "Watching " + this.universe.filter((u) => !u.index).length + " symbols for momentum signals" : "Standing by — no new virtual orders");
      } catch (e) {
        this.toast("warn", "Bot toggle failed", e.message);
      }
    });
    $("#bot-risk").addEventListener("change", async (e) => {
      try {
        const out = await this.api("/api/live/bot", {
          method: "POST",
          headers: window.QUANT_DASHBOARD_TOKEN
            ? { "Content-Type": "application/json", "X-Quant-Token": window.QUANT_DASHBOARD_TOKEN }
            : { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: this.bot.enabled, risk_pct: parseFloat(e.target.value) }),
        });
        this.renderBot(out);
        this.log("sys", "bot", `risk/trade set to ${e.target.value * 100}%`);
      } catch (err) { this.toast("warn", "Risk change failed", err.message); }
    });
    /* layout toggles + theme */
    $("#btn-side").addEventListener("click", () => document.body.classList.toggle("no-side"));
    $("#btn-bottom").addEventListener("click", () => document.body.classList.toggle("no-bottom"));
    $("#btn-theme").addEventListener("click", () => {
      const el = document.documentElement;
      const next = el.dataset.theme === "dark" ? "light" : "dark";
      el.dataset.theme = next;
      localStorage.setItem("qt-theme", next);
      $("#btn-theme").textContent = next === "dark" ? "☾" : "☀";
      this.chart._dirty = true;
      this.eq._dirty = true;
    });
    const savedTheme = localStorage.getItem("qt-theme");
    if (savedTheme) {
      document.documentElement.dataset.theme = savedTheme;
      $("#btn-theme").textContent = savedTheme === "dark" ? "☾" : "☀";
    }
    /* live pill */
    const pill = document.createElement("button");
    pill.className = "live-pill";
    pill.textContent = "◉ LIVE";
    pill.style.cssText = "position:absolute;right:76px;bottom:34px;z-index:6;display:none;padding:5px 12px;border-radius:99px;border:1px solid var(--up);background:color-mix(in srgb,var(--up) 15%,var(--panel));color:var(--up-bright);font:700 10.5px ui-monospace,monospace;cursor:pointer;letter-spacing:.05em";
    pill.addEventListener("click", () => this.chart.goLive());
    $(".chart-wrap").appendChild(pill);
    setInterval(() => {
      pill.style.display = this.chart.follow ? "none" : "block";
    }, 800);
    /* keyboard */
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      if (e.key === "/") { e.preventDefault(); input.focus(); return; }
      const tfMap = { "1": "1m", "2": "3m", "3": "5m", "4": "15m", "5": "30m", "6": "1h", "7": "1d", "8": "1w" };
      if (tfMap[e.key]) this.setInterval(tfMap[e.key]);
    });
  },

  async setInterval(tf) {
    if (tf === this.interval) return;
    this.interval = tf;
    $("#tf-group").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.tf === tf));
    $("#tf-hint").textContent = tf + " · scroll to zoom · drag to pan · double-click to fit";
    await this.loadCandles();
  },

  async switchSymbol(sym) {
    if (sym === this.symbol) return;
    this.symbol = sym;
    document.querySelectorAll(".wl-row").forEach((r) => r.classList.toggle("active", r.dataset.sym === sym));
    $("#chart-loading").classList.remove("hidden");
    try {
      await this.loadCandles();
      this.renderLegend();
    } catch (e) {
      this.log("warn", "data", `cannot load ${sym}: ${e.message}`);
    }
    $("#chart-loading").classList.add("hidden");
  },

  /* ----------------------------------------------------------- misc */

  toast(kind, title, body) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = `<div class="t-title">${esc(title)}<span class="t-time">${timeFmt.format(new Date())}</span></div><div class="t-body">${esc(body)}</div>`;
    const box = $("#toasts");
    box.appendChild(el);
    while (box.children.length > 4) box.firstChild.remove();
    setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 400); }, 6500);
  },

  log(k, key, msg) {
    const el = $("#log");
    const row = document.createElement("div");
    row.className = "log-row";
    const t = clockFmt.format(new Date());
    row.innerHTML = `<span class="l-t">${t}</span><span class="l-k ${k}">${key}</span><span class="l-msg">${esc(msg)}</span>`;
    el.prepend(row);
    while (el.children.length > 80) el.lastChild.remove();
  },

  tickClock() {
    $("#clock").textContent = clockFmt.format(new Date()) + " IST";
  },
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

window.addEventListener("DOMContentLoaded", () => App.init());
