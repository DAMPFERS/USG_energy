const $ = id => document.getElementById(id);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const NS = 'http://www.w3.org/2000/svg';
const C = CONFIG.colors;

// Время купола t — минуты от начала Сола 1 (00:00). Сол длится solMin минут (24:40):
// после 23:59 часы идут 24:00…24:39, затем следующий сол.
let SOL = 1480;
const solOf = t => Math.floor(t / SOL) + 1;
function fmtHM(t) {
  const m = Math.floor(t - (solOf(t) - 1) * SOL + 1e-6);
  return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}
const fmtSolHM = t => 'Sol ' + solOf(t) + ' ' + fmtHM(t);
function fmtLeft(s) {
  s = Math.max(0, Math.ceil(s));
  return s >= 60 ? Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : s + ' s';
}
const kw = v => (v == null ? '—' : v.toFixed(2));
function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function lineStyle(idx) {
  return { color: CONFIG.lineHues[idx % 8], dash: idx < 8 ? 'dashed' : 'dotted' };
}
function svgEl(name, attrs) {
  const el = document.createElementNS(NS, name);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
function path(pts) {
  return pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
async function api(url, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(CONFIG.api + url, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}
let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

// ---------------------------------------------------------------- состояние
const state = {
  snap: null,
  history: [],       // [{t, gen, cons, max, min, soc, bat, boff, per:[]}] (+ live: true — точки с клиента)
  gaps: [],          // [[t_от, t_до]] — сервер не работал
  gapMin: 3,         // разрыв между точками (мин), с которого линия графика рвётся
  serverT: null,     // t последней точки, полученной с сервера
  forecast: null,    // {t:[], mean:[], sd:[]}
  activeLines: new Set(),
  show: { forecast: true, gen: true, cons: true, limit: true },
  range: 'cur',      // '2h' | 'cur' (текущий сол) | 'sol:N'
  rangeKey: '',
  termN: 0,
  scale: null
};

// ---------------------------------------------------------------- линии
const rowEls = [];
function buildLines(lines) {
  const wrap = $('lineButtons');
  wrap.innerHTML = ''; rowEls.length = 0;
  lines.forEach((l, i) => {
    const row = document.createElement('div');
    row.className = 'line-row';
    const sel = document.createElement('select');
    sel.className = 'prio';
    sel.setAttribute('aria-label', 'Shedding priority');
    for (let n = 1; n <= lines.length; n++) sel.add(new Option('#' + n, n));
    sel.addEventListener('change', () => setPriority(i, +sel.value));
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'line-btn';
    b.style.setProperty('--lc', lineStyle(i).color);
    b.innerHTML = '<span class="lb-top"><span class="lb-dot"></span><span class="lb-name"></span><span class="lb-badge"></span></span>' +
      '<span class="lb-kw"></span><span class="lb-reason"></span>';
    b.addEventListener('click', () => toggleLine(i));
    row.append(sel, b);
    wrap.appendChild(row);
    rowEls.push({ row, sel, b });
  });
}
const BADGE = { on: 'ON', off: 'OFF', locked: 'LOCKED', tripped: 'SHED', offline: 'NO LINK' };
function updateLines() {
  const lines = state.snap.lines;
  if (rowEls.length !== lines.length) { buildLines(lines); buildLineChips(lines); }
  lines.forEach((l, i) => {
    const { sel, b } = rowEls[i];
    if (document.activeElement !== sel) sel.value = l.priority;
    sel.title = 'Priority #' + l.priority + (l.priority === lines.length ? ' — shed first' : l.priority === 1 ? ' — shed last' : '');
    b.className = 'line-btn ' + (l.on ? 'is-on ' : 'is-off ') + 'is-' + l.status;
    b.querySelector('.lb-name').textContent = l.name;
    lineChipEls[i].querySelector('.chip-txt').textContent = l.name;
    b.querySelector('.lb-badge').textContent = BADGE[l.status] || l.status;
    b.querySelector('.lb-kw').innerHTML = (l.on ? l.kw.toFixed(2) : '—') + ' <small>kW</small>';
    b.querySelector('.lb-reason').textContent = l.reason ? l.reason + (l.until ? ' · until ' + l.until : '') : '';
    const blocked = (l.status === 'locked' || l.status === 'tripped') && !l.on;
    b.disabled = blocked || l.status === 'offline';
    b.title = blocked ? 'Line cannot be switched on: ' + l.reason : (l.on ? 'Switch off' : 'Switch on') + ' ' + l.name;
    b.setAttribute('aria-pressed', l.on);
  });
}
async function toggleLine(i) {
  const l = state.snap.lines[i];
  const b = rowEls[i].b;
  b.disabled = true;
  try {
    await api('/api/line', { line: l.id, on: !l.on });
    l.on = !l.on; l.status = l.on ? 'on' : 'off';
    updateLines();
  } catch (e) {
    toast(l.name + ': ' + e.message);
  } finally {
    b.disabled = false;
    pollState();
  }
}
async function setPriority(i, prio) {
  const l = state.snap.lines[i];
  try {
    await api('/api/priority', { line: l.id, priority: prio });
  } catch (e) {
    toast(l.name + ': ' + e.message);
  }
  rowEls[i].sel.blur();
  pollState();
}

// ---------------------------------------------------------------- АКБ
function buildBattery() {
  $('battery').innerHTML =
    '<div class="bat-top"><span class="bat-name"></span><span class="bat-st"></span></div>' +
    '<div class="bar" role="meter" aria-valuemin="0"><span class="fill"></span><span class="ticks"></span><span class="flow" hidden></span><span class="bar-txt"></span></div>' +
    '<div class="bat-meta"><span class="pw"></span></div>' +
    '<div class="pmeter"><span class="pm-lbl">charge</span><span class="pm-bar"><i class="pm-fill"></i></span><span class="pm-val"></span></div>' +
    '<div class="pmeter"><span class="pm-lbl">discharge</span><span class="pm-bar"><i class="pm-fill"></i></span><span class="pm-val"></span></div>';
}
function updateBattery() {
  const b = state.snap.battery, el = $('battery');
  if (!el.firstChild) buildBattery();
  el.querySelector('.bat-name').textContent = b.name;
  const st = el.querySelector('.bat-st');
  st.className = 'bat-st ' + (!b.connected ? 'off' : b.full ? 'full' : 'ok');
  st.textContent = !b.connected ? 'DISCONNECTED until ' + b.off_until : b.full ? 'FULL' : 'CONNECTED';
  el.classList.toggle('is-off', !b.connected);

  const fill = el.querySelector('.fill');
  fill.style.width = clamp(b.soc * 100, 0, 100) + '%';
  fill.className = 'fill' + (b.soc < 0.2 ? ' low' : b.soc < 0.4 ? ' mid' : '');
  const bar = el.querySelector('.bar');
  bar.setAttribute('aria-valuemax', b.capacity_kwh);
  bar.setAttribute('aria-valuenow', b.charge_kwh.toFixed(2));
  el.querySelector('.bar-txt').textContent =
    b.charge_kwh.toFixed(1) + ' / ' + b.capacity_kwh.toFixed(1) + ' kWh · ' + Math.round(b.soc * 100) + '%';
  const flow = el.querySelector('.flow');
  flow.hidden = Math.abs(b.power_kw) < 0.01;
  flow.classList.toggle('out', b.power_kw < 0);

  const pw = el.querySelector('.pw');
  if (b.power_kw > 0.01) pw.innerHTML = '<b class="in">▲ charging ' + b.power_kw.toFixed(2) + ' kW</b>';
  else if (b.power_kw < -0.01) pw.innerHTML = '<b class="out">▼ discharging ' + (-b.power_kw).toFixed(2) + ' kW</b>';
  else pw.textContent = b.connected ? 'idle' : 'no charge or discharge while disconnected';

  // насколько используется предельная мощность заряда / разряда
  const [mc, md] = el.querySelectorAll('.pmeter');
  const ch = Math.max(0, b.power_kw), dis = Math.max(0, -b.power_kw);
  [[mc, ch, b.max_charge_kw], [md, dis, b.max_discharge_kw]].forEach(([m, v, lim]) => {
    const f = m.querySelector('.pm-fill');
    f.style.width = clamp(lim ? v / lim * 100 : 0, 0, 100) + '%';
    f.classList.toggle('hot', lim && v >= lim - 0.01);
    m.querySelector('.pm-val').textContent = v.toFixed(1) + ' / ' + (+lim).toFixed(1) + ' kW';
  });
}

// ---------------------------------------------------------------- показатели
function updateKpis() {
  const s = state.snap;
  if (s.status === 'before') {   // до заселения — обратный отсчёт
    const left = Math.ceil((s.start_t - s.t) * 60);
    $('clock').textContent = String(Math.floor(left / 3600)).padStart(2, '0') + ':' +
      String(Math.floor(left / 60) % 60).padStart(2, '0') + ':' + String(left % 60).padStart(2, '0');
    $('solName').textContent = 'T−';
  } else {
    $('clock').textContent = s.time;
    $('solName').textContent = 'SOL ' + s.sol;
  }
  $('dayName').textContent = s.name;
  $('phaseName').textContent = s.status === 'before' ? 'settlement at ' + fmtSolHM(s.start_t)
    : s.status === 'finished' ? 'mission complete' : 'mission until ' + fmtSolHM(s.end_t);
  $('kGen').innerHTML = kw(s.gen) + '<small>kW</small>';
  $('kCons').innerHTML = kw(s.cons) + '<small>kW</small>';
  $('kNet').innerHTML = (s.net > 0 ? '+' : '') + kw(s.net) + '<small>kW</small>';

  const lim = s.limits, b = s.battery;
  const over = s.cons > lim.max;
  $('kMax').innerHTML = lim.max.toFixed(1) + '<small>kW</small>';
  $('kMaxBox').className = 'kpi limit-kpi ' + (over ? 'st-bad' : 'st-ok');
  $('kMaxState').textContent = over ? '▲ OVERLOAD' : '✓ below limit';

  const under = lim.min != null && s.cons < lim.min;
  $('kMin').innerHTML = (lim.min == null ? '—' : lim.min.toFixed(1)) + '<small>kW</small>';
  $('kMinBox').className = 'kpi limit-kpi ' + (under ? 'st-bad' : lim.min == null ? 'st-na' : 'st-ok');
  $('kMinState').textContent = under ? '▼ SURPLUS OVER CHARGE LIMIT'
    : !b.connected ? 'battery disconnected' : b.full ? 'battery full — surplus is lost' : '✓ battery absorbs surplus';

  const ALARM = {
    overload: a => 'OVERLOAD — consumption above ' + lim.max.toFixed(1) + ' kW · BLACKOUT in',
    surplus: a => 'SURPLUS ABOVE CHARGE LIMIT ' + lim.max_charge.toFixed(1) + ' kW · battery disconnects in',
    deficit: a => 'POWER DEFICIT ' + kw(s.deficit) + ' kW · load shedding in'
  };
  $('alarms').innerHTML = (s.alarms || []).map(a =>
    '<div class="alarm al-' + a.kind + '">⚠ ' + esc((ALARM[a.kind] || (() => a.title))(a)) + '<b>' + fmtLeft(a.left_s) + '</b></div>').join('');
  const st = s.stats;
  $('termStats').textContent = 'blackouts ' + st.blackouts + ' · bat off ' + st.battery_off + ' · shed ' + st.sheds;
}

// ---------------------------------------------------------------- переключатели графика
function chip(label, color, swClass, checked, onChange) {
  const c = document.createElement('label');
  c.className = 'chip' + (checked ? ' active' : '');
  c.style.setProperty('--lc', color);
  c.innerHTML = '<input type="checkbox"' + (checked ? ' checked' : '') + '><span class="sw ' + swClass + '"></span><span class="chip-txt"></span>';
  c.querySelector('.chip-txt').textContent = label;
  const cb = c.querySelector('input');
  cb.addEventListener('change', () => { c.classList.toggle('active', cb.checked); onChange(cb.checked); renderChart(); });
  return c;
}
function buildSeriesToggles() {
  const wrap = $('seriesToggles');
  [['forecast', 'Forecast ± σ', C.forecast, 'dashed'],
   ['gen', 'Generation', C.gen, ''],
   ['cons', 'Consumption', C.cons, ''],
   ['limit', 'Max load', C.limit, 'dashed']].forEach(([k, label, color, sw]) => {
    wrap.appendChild(chip(label, color, sw, state.show[k], v => { state.show[k] = v; }));
  });
}
const lineChipEls = [];
function buildLineChips(lines) {
  const wrap = $('lineToggles');
  wrap.innerHTML = ''; lineChipEls.length = 0;
  lines.forEach((l, i) => {
    const st = lineStyle(i);
    const c = chip(l.name, st.color, st.dash, state.activeLines.has(i), v => {
      if (v) state.activeLines.add(i); else state.activeLines.delete(i);
    });
    wrap.appendChild(c); lineChipEls.push(c);
  });
}
// кнопки диапазона: 2 часа, текущий сол, прошедшие солы
function updateRanges() {
  const s = state.snap;
  const cur = clamp(s.sol, solOf(s.start_t), solOf(s.end_t - 1e-6));
  const first = solOf(s.start_t);
  const key = first + ':' + cur;
  if (key === state.rangeKey) return;
  state.rangeKey = key;
  const opts = [['2h', '2 hours']];
  for (let n = first; n < cur; n++) opts.push(['sol:' + n, 'Sol ' + n]);
  opts.push(['cur', 'Sol ' + cur + ' · now']);
  if (!opts.some(o => o[0] === state.range)) state.range = 'cur';
  const seg = $('rangeSeg');
  seg.innerHTML = '';
  opts.forEach(([v, label]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.className = v === state.range ? 'is-sel' : '';
    b.addEventListener('click', () => {
      state.range = v;
      seg.querySelectorAll('button').forEach(x => x.classList.toggle('is-sel', x === b));
      renderChart(); buildTable();
    });
    seg.appendChild(b);
  });
}
function rangeWindow() {
  const s = state.snap, now = s.t;
  if (state.range === '2h') {
    const x0 = Math.max(s.start_t, now - CONFIG.zoomWindowMin[0]);
    let x1 = Math.min(s.end_t, Math.max(now, s.start_t) + CONFIG.zoomWindowMin[1]);
    if (x1 - x0 < 30) x1 = x0 + 30;
    return [x0, x1];
  }
  const n = state.range === 'cur' ? clamp(s.sol, solOf(s.start_t), solOf(s.end_t - 1e-6)) : +state.range.slice(4);
  return [Math.max(s.start_t, (n - 1) * SOL), Math.min(s.end_t, n * SOL)];
}

// ---------------------------------------------------------------- график
const PAD = { l: 40, r: 64, t: 22, b: 22 };

function forecastAt(t) {
  const f = state.forecast;
  if (!f || !f.t.length) return null;
  if (t <= f.t[0]) return { mean: f.mean[0], sd: f.sd[0] };
  for (let i = 0; i < f.t.length - 1; i++) {
    if (t <= f.t[i + 1]) {
      const k = (t - f.t[i]) / (f.t[i + 1] - f.t[i]);
      return { mean: f.mean[i] + k * (f.mean[i + 1] - f.mean[i]), sd: f.sd[i] + k * (f.sd[i + 1] - f.sd[i]) };
    }
  }
  return { mean: f.mean[f.mean.length - 1], sd: f.sd[f.sd.length - 1] };
}
const inGap = t => state.gaps.some(([a, b]) => t > a && t < b);

// ломаная, разорванная там, где между точками пропуск (сервер не работал)
function brokenPath(h, X, Y, get) {
  let d = '';
  h.forEach((p, i) => {
    const brk = !i || p.t - h[i - 1].t > state.gapMin;
    d += (brk ? 'M' : 'L') + X(p.t).toFixed(1) + ',' + Y(get(p)).toFixed(1) + ' ';
  });
  return d;
}

function renderChart() {
  const s = state.snap;
  const svg = $('powerChart'), wrap = $('powerWrap');
  if (!s) return;
  const W = Math.max(200, wrap.clientWidth), H = Math.max(120, wrap.clientHeight);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.innerHTML = '';

  const now = s.t;
  const [x0, x1] = rangeWindow();
  const h = state.history.filter(p => p.t >= x0 - 1 && p.t <= x1 + 1);
  const f = state.forecast;
  const fIdx = f ? f.t.map((t, i) => i).filter(i => f.t[i] >= x0 - 10 && f.t[i] <= x1 + 10) : [];

  let yMax = 1;
  if (state.show.forecast) fIdx.forEach(i => yMax = Math.max(yMax, f.mean[i] + f.sd[i]));
  h.forEach(p => {
    if (state.show.gen) yMax = Math.max(yMax, p.gen);
    if (state.show.cons) yMax = Math.max(yMax, p.cons);
    if (state.show.limit) yMax = Math.max(yMax, p.max);
    state.activeLines.forEach(i => yMax = Math.max(yMax, p.per[i] || 0));
  });
  if (state.show.limit) yMax = Math.max(yMax, s.limits.max);
  yMax = niceMax(yMax * 1.1);

  const X = t => PAD.l + (t - x0) / (x1 - x0) * (W - PAD.l - PAD.r);
  const Y = v => H - PAD.b - clamp(v, 0, yMax) / yMax * (H - PAD.t - PAD.b);
  state.scale = { x0, x1, W, H, X, Y };
  const top = PAD.t, bottom = H - PAD.b, plotH = bottom - top;

  const defs = svgEl('defs', {});
  const clip = 'clip-' + Date.now();
  const cp = svgEl('clipPath', { id: clip });
  cp.appendChild(svgEl('rect', { x: PAD.l, y: 0, width: W - PAD.l - PAD.r, height: H }));
  const hatch = svgEl('pattern', { id: 'hatch', width: 8, height: 8, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  hatch.appendChild(svgEl('rect', { class: 'gap-stripe', width: 3, height: 8 }));
  defs.append(cp, hatch);
  svg.appendChild(defs);

  // будущее — слегка затенено
  if (now < x1) {
    const xn = X(Math.max(now, x0));
    svg.appendChild(svgEl('rect', { class: 'future', x: xn, y: top, width: W - PAD.r - xn, height: plotH }));
  }
  const layer = svgEl('g', { 'clip-path': 'url(#' + clip + ')' });

  // сетка и оси
  const g = svgEl('g', { class: 'grid' });
  for (let i = 0; i <= 4; i++) {
    const v = yMax * i / 4, y = Y(v);
    g.appendChild(svgEl('line', { x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
    const t = svgEl('text', { x: PAD.l - 6, y: y + 3, 'text-anchor': 'end' });
    t.textContent = v.toFixed(v < 2 ? 1 : 0);
    g.appendChild(t);
  }
  const span = x1 - x0;
  const step = span > 600 ? 120 : span > 240 ? 60 : span > 90 ? 30 : 15;
  let lastX = -Infinity;
  for (let n = solOf(x0); n <= solOf(x1); n++) {
    const base = (n - 1) * SOL;
    // «марсианские» 40 минут 24:00–24:40 — едва заметной полосой
    const sx0 = X(Math.max(x0, base + 1440)), sx1 = X(Math.min(x1, base + SOL));
    if (sx1 > sx0) layer.appendChild(svgEl('rect', { class: 'slip', x: sx0, y: top, width: sx1 - sx0, height: plotH }));
    // граница солов
    if (base > x0 && base < x1) {
      g.appendChild(svgEl('line', { class: 'sol-line', x1: X(base), x2: X(base), y1: top, y2: bottom }));
      const lb = svgEl('text', { class: 'sol-lbl', x: X(base) + 4, y: top - 6 });
      lb.textContent = 'SOL ' + n;
      g.appendChild(lb);
    }
    for (let m = 0; m < SOL; m += step) {
      const tm = base + m;
      if (tm < x0 || tm > x1 || X(tm) - lastX < 36) continue;
      lastX = X(tm);
      const t = svgEl('text', { x: X(tm), y: H - 6, 'text-anchor': 'middle' });
      t.textContent = fmtHM(tm);
      g.appendChild(t);
    }
  }
  const unit = svgEl('text', { x: PAD.l - 6, y: 10, 'text-anchor': 'end' });
  unit.textContent = 'kW';
  g.appendChild(unit);
  svg.appendChild(g);
  svg.appendChild(layer);

  // пропуски связи (сервер не работал)
  state.gaps.forEach(([a, b]) => {
    if (b < x0 || a > x1) return;
    const ga = X(Math.max(a, x0)), gb = X(Math.min(b, x1));
    layer.appendChild(svgEl('rect', { class: 'gap', x: ga, y: top, width: Math.max(1, gb - ga), height: plotH }));
    if (gb - ga > 44) {
      const t = svgEl('text', { class: 'gap-lbl', x: (ga + gb) / 2, y: top + 14, 'text-anchor': 'middle' });
      t.textContent = 'NO LINK';
      layer.appendChild(t);
    }
  });

  // прогноз
  if (state.show.forecast && fIdx.length > 1) {
    const up = fIdx.map(i => [X(f.t[i]), Y(f.mean[i] + f.sd[i])]);
    const lo = fIdx.map(i => [X(f.t[i]), Y(Math.max(0, f.mean[i] - f.sd[i]))]).reverse();
    layer.appendChild(svgEl('path', { class: 'band', d: path(up) + ' ' + path(lo).replace('M', 'L') + ' Z', fill: C.forecast }));
    layer.appendChild(svgEl('path', { class: 'series thin dashed', d: path(fIdx.map(i => [X(f.t[i]), Y(f.mean[i])])), stroke: C.forecast }));
  }

  // верхняя граница (история + текущее значение до «сейчас»)
  const labels = [];
  if (state.show.limit) {
    const pts = [];
    h.forEach((p, i) => {
      if (i) pts.push([X(p.t), Y(h[i - 1].max)]);
      pts.push([X(p.t), Y(p.max)]);
    });
    const xa = h.length ? X(h[0].t) : X(x0), xb = X(clamp(now, x0, x1));
    if (h.length) pts.push([xb, Y(h[h.length - 1].max)]);
    pts.push([xb, Y(s.limits.max)]);
    if (!h.length) pts.unshift([xa, Y(s.limits.max)]);
    layer.appendChild(svgEl('path', { class: 'limit-zone', d: path(pts) + ' L' + xb + ',' + top + ' L' + xa + ',' + top + ' Z' }));
    layer.appendChild(svgEl('path', { class: 'limit', d: path(pts) }));
    labels.push([Y(s.limits.max), 'max ' + s.limits.max.toFixed(1)]);
  }

  function series(get, color, cls, label) {
    if (h.length < 2) return;
    layer.appendChild(svgEl('path', { class: 'series ' + cls, d: brokenPath(h, X, Y, get), stroke: color }));
    const last = h[h.length - 1];
    if (label && now >= x0 && now <= x1) {   // подпись текущего значения — только если «сейчас» в окне
      svg.appendChild(svgEl('circle', { class: 'dot-end', cx: X(last.t), cy: Y(get(last)), r: 4, fill: color }));
      labels.push([Y(get(last)), label]);
    }
  }
  state.activeLines.forEach(i => { const st = lineStyle(i); series(p => p.per[i] || 0, st.color, 'thin ' + st.dash); });
  if (state.show.cons) series(p => p.cons, C.cons, '', kw(s.cons));
  if (state.show.gen) series(p => p.gen, C.gen, '', kw(s.gen));

  // «сейчас»
  if (now >= x0 && now <= x1) {
    svg.appendChild(svgEl('line', { class: 'now', x1: X(now), x2: X(now), y1: top, y2: bottom }));
  }
  // подписи справа без наложений
  labels.sort((a, b) => a[0] - b[0]);
  let prev = -Infinity;
  labels.forEach(([y, txt]) => {
    y = Math.max(y, prev + 12); prev = y;
    const t = svgEl('text', { class: 'lbl', x: W - PAD.r + 6, y: y + 3 });
    t.textContent = txt;
    svg.appendChild(t);
  });
}

// ---------------------------------------------------------------- подсказка
function initHover() {
  const wrap = $('powerWrap'), tip = $('powerTip');
  let xhair = null;
  wrap.addEventListener('pointermove', e => {
    const sc = state.scale;
    if (!sc) return;
    const rect = wrap.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const t = sc.x0 + (px / rect.width * sc.W - PAD.l) / (sc.W - PAD.l - PAD.r) * (sc.x1 - sc.x0);
    if (t < sc.x0 || t > sc.x1) { tip.hidden = true; return; }
    const row = (color, dash, label, value) =>
      '<div class="tip-row"><span class="k"><i class="' + dash + '" style="--lc:' + color + '"></i>' + esc(label) + '</span><b>' + value + '</b></div>';
    let html = '';
    let tt = t;
    const h = state.history;
    if (inGap(t)) {
      html += '<div class="tip-row tip-gap">No link with the dome — no data</div>';
    } else if (h.length && t <= h[h.length - 1].t + 1 && t >= h[0].t - 1) {
      let best = h[0];
      for (const p of h) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      tt = best.t;
      if (state.show.gen) html += row(C.gen, '', 'Generation', kw(best.gen) + ' kW');
      if (state.show.cons) html += row(C.cons, '', 'Consumption', kw(best.cons) + ' kW');
      if (state.show.limit) html += row(C.limit, 'dashed', 'Max load', best.max.toFixed(1) + ' kW');
      if (best.min != null) html += row('var(--mut)', 'dotted', 'Min load', best.min.toFixed(1) + ' kW');
      if (best.soc != null) html += row('var(--ok)', '', 'Battery', best.soc.toFixed(1) + ' kWh' + (best.boff ? ' (off)' : ''));
      state.activeLines.forEach(i => {
        const st = lineStyle(i);
        html += row(st.color, st.dash, state.snap.lines[i].name, kw(best.per[i] || 0) + ' kW');
      });
    }
    const fc = state.show.forecast ? forecastAt(tt) : null;
    if (fc) html += row(C.forecast, 'dashed', 'Forecast', fc.mean.toFixed(2) + ' ± ' + fc.sd.toFixed(2) + ' kW');
    if (!html) { tip.hidden = true; return; }
    tip.innerHTML = '<div class="tip-time">' + fmtSolHM(tt) + '</div>' + html;
    tip.hidden = false;
    tip.style.left = clamp(px, 80, rect.width - 80) + 'px';
    tip.style.top = Math.max(py - 12, 12) + 'px';
    const svg = $('powerChart');
    if (!xhair || !svg.contains(xhair)) { xhair = svgEl('line', { class: 'xhair' }); svg.appendChild(xhair); }
    const x = sc.X(tt);
    xhair.setAttribute('x1', x); xhair.setAttribute('x2', x);
    xhair.setAttribute('y1', PAD.t); xhair.setAttribute('y2', sc.H - PAD.b);
  });
  wrap.addEventListener('pointerleave', () => { tip.hidden = true; if (xhair) xhair.remove(); xhair = null; });
}

// ---------------------------------------------------------------- таблица
function buildTable() {
  const det = document.querySelector('.tbl');
  if (!det.open || !state.snap) return;
  const [x0, x1] = rangeWindow();
  const h = state.history.filter(p => p.t >= x0 && p.t <= x1).slice(-240).reverse();
  let html = '<thead><tr><th>Time</th><th>Generation</th><th>Forecast</th><th>Consumption</th><th>Max</th><th>Min</th><th>Battery kWh</th>';
  state.activeLines.forEach(i => html += '<th>' + esc(state.snap.lines[i].name) + '</th>');
  html += '</tr></thead><tbody>';
  h.forEach((p, k) => {
    const next = h[k + 1];   // таблица идёт от новых к старым
    const fc = forecastAt(p.t);
    html += '<tr><td>' + fmtSolHM(p.t) + '</td><td>' + kw(p.gen) + '</td><td>' + (fc ? kw(fc.mean) : '—') + '</td><td>' +
      kw(p.cons) + '</td><td>' + p.max.toFixed(1) + '</td><td>' + (p.min == null ? '—' : p.min.toFixed(1)) + '</td><td>' +
      (p.soc == null ? '—' : p.soc.toFixed(1) + (p.boff ? ' off' : '')) + '</td>';
    state.activeLines.forEach(i => html += '<td>' + kw(p.per[i] || 0) + '</td>');
    html += '</tr>';
    if (next && p.t - next.t > state.gapMin) {
      html += '<tr class="gap-row"><td colspan="99">no link with the dome: ' + fmtSolHM(next.t) + ' – ' + fmtSolHM(p.t) + '</td></tr>';
    }
  });
  $('powerTable').innerHTML = html + '</tbody>';
}
document.querySelector('.tbl').addEventListener('toggle', buildTable);

// ---------------------------------------------------------------- терминал
const typeQueue = [];
let typing = false;
function addMessages(msgs, instant) {
  const body = $('termBody');
  msgs.forEach(m => {
    const div = document.createElement('div');
    div.className = 'msg ' + m.level;
    const ts = (m.mars || m.ts.slice(11)).replace(/^Sol (\d+) /, 'S$1 ');
    div.innerHTML = '<span class="ts">[' + esc(ts) + ']</span> <span class="lv">' + esc(m.level) + '</span> <span class="tx"></span>';
    body.appendChild(div);
    if (instant) div.querySelector('.tx').textContent = m.text;
    else typeQueue.push([div.querySelector('.tx'), m.text]);
    state.termN = Math.max(state.termN, m.n);
  });
  body.scrollTop = body.scrollHeight;
  if (!typing) typeNext();
}
function typeNext() {
  const job = typeQueue.shift();
  if (!job) { typing = false; return; }
  typing = true;
  const [el, text] = job;
  const cur = document.createElement('span');
  cur.className = 'cursor';
  el.after(cur);
  let i = 0;
  const body = $('termBody');
  const timer = setInterval(() => {
    i += 2;
    el.textContent = text.slice(0, i);
    body.scrollTop = body.scrollHeight;
    if (i >= text.length) { clearInterval(timer); cur.remove(); typeNext(); }
  }, 18);
}
async function pollTerminal(first) {
  try {
    const d = await api('/api/terminal?after=' + state.termN);
    if (d.messages.length) addMessages(d.messages, first);
  } catch (e) { /* связь пропала — повторим */ }
}

// ---------------------------------------------------------------- опрос
async function loadHistory(full) {
  const since = full || state.serverT == null ? null : state.serverT;
  const h = await api('/api/history' + (since != null ? '?since=' + since : ''));
  const base = since == null ? [] : state.history.filter(p => !p.live);
  state.history = base.concat(h.samples);
  state.gaps = h.gaps;
  state.gapMin = h.gap_min;
  if (state.history.length) state.serverT = state.history[state.history.length - 1].t;
}
async function loadForecast() {
  state.forecast = await api('/api/forecast');
}
async function pollState() {
  try {
    const s = await api('/api/state');
    state.snap = s;
    SOL = s.sol_min;
    const last = state.history[state.history.length - 1];
    if (last && s.t < last.t - 1) {
      await loadHistory(true); // время ушло назад (перемотка / новая игра)
    } else if (s.status === 'running' && (!last || s.t - last.t >= CONFIG.liveStepMin)) {
      if (last && s.t - last.t > state.gapMin) await loadHistory(false); // пропуск — догрузим с сервера
      state.history.push({ t: s.t, gen: s.gen, cons: s.cons, max: s.limits.max, min: s.limits.min,
        soc: s.battery.charge_kwh, boff: !s.battery.connected, per: s.lines.map(l => (l.on ? l.kw : 0)), live: true });
    }
    updateRanges(); updateLines(); updateBattery(); updateKpis(); renderChart(); buildTable();
    $('upd').textContent = 'linked to dome' + (s.speed !== 1 ? ' · speed ×' + s.speed : '') + (s.paused ? ' · checks paused' : '');
    $('liveDot').classList.remove('lost');
  } catch (e) {
    $('upd').textContent = 'no link to dome';
    $('liveDot').classList.add('lost');
  }
}

async function init() {
  buildSeriesToggles();
  initHover();
  try { await Promise.all([loadHistory(true), loadForecast()]); } catch (e) { /* загрузим при следующей синхронизации */ }
  await pollState();
  await pollTerminal(true);
  setInterval(pollState, CONFIG.stateMs);
  setInterval(() => pollTerminal(false), CONFIG.terminalMs);
  setInterval(() => {
    loadHistory(false).catch(() => {});
    if (!state.forecast) loadForecast().catch(() => {});
  }, CONFIG.historyResyncMs);
  window.addEventListener('resize', renderChart);
}
init();
