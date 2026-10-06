const $ = id => document.getElementById(id);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const NS = 'http://www.w3.org/2000/svg';
const C = CONFIG.colors;

function fmtHM(min) {
  min = ((Math.round(min) % 1440) + 1440) % 1440;
  return String(Math.floor(min / 60)).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0');
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
  history: [],       // [{t, gen, cons, min, max, per:[], soc:{}}]
  forecast: null,    // {t:[], mean:[], sd:[]}
  activeLines: new Set(),
  show: { forecast: true, gen: true, cons: true, corridor: false },
  range: 'day',
  termN: 0,
  scale: null
};

// ---------------------------------------------------------------- линии
const btnEls = [];
function buildLines(lines) {
  const wrap = $('lineButtons');
  wrap.innerHTML = ''; btnEls.length = 0;
  lines.forEach((l, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'line-btn';
    b.style.setProperty('--lc', lineStyle(i).color);
    b.innerHTML = '<span class="lb-top"><span class="lb-dot"></span><span class="lb-name"></span><span class="lb-badge"></span></span>' +
      '<span class="lb-kw"></span><span class="lb-reason"></span>';
    b.addEventListener('click', () => toggleLine(i));
    wrap.appendChild(b);
    btnEls.push(b);
  });
}
const BADGE = { on: 'ВКЛ', off: 'ВЫКЛ', locked: 'БЛОК', tripped: 'АВАРИЯ', offline: 'НЕТ СВЯЗИ' };
function updateLines() {
  const lines = state.snap.lines;
  if (btnEls.length !== lines.length) { buildLines(lines); buildLineChips(lines); }
  lines.forEach((l, i) => {
    const b = btnEls[i];
    b.className = 'line-btn ' + (l.on ? 'is-on ' : 'is-off ') + 'is-' + l.status;
    b.querySelector('.lb-name').textContent = l.name;
    b.querySelector('.lb-badge').textContent = BADGE[l.status] || l.status;
    b.querySelector('.lb-kw').innerHTML = (l.on ? l.kw.toFixed(2) : '—') + ' <small>кВт</small>';
    b.querySelector('.lb-reason').textContent = l.reason ? l.reason + (l.until ? ' · до ' + l.until : '') : '';
    const blocked = (l.status === 'locked' || l.status === 'tripped') && !l.on;
    b.disabled = blocked || l.status === 'offline';
    b.title = blocked ? 'Линию нельзя включить: ' + l.reason : (l.on ? 'Выключить' : 'Включить') + ' ' + l.name;
    b.setAttribute('aria-pressed', l.on);
  });
}
async function toggleLine(i) {
  const l = state.snap.lines[i];
  const b = btnEls[i];
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

// ---------------------------------------------------------------- АКБ
const STATUS_RU = { ok: 'НОРМА', degraded: 'ДЕГРАДАЦИЯ', fault: 'ЗАЩИТА', failed: 'ОТКАЗ' };
const batEls = {};
function updateBatteries() {
  const wrap = $('batteries');
  state.snap.batteries.forEach(b => {
    let el = batEls[b.id];
    if (!el) {
      el = document.createElement('button');
      el.type = 'button';
      el.className = 'bat';
      el.innerHTML = '<div class="bat-top"><span class="bat-radio"></span><span class="bat-name"></span><span class="bat-st"></span></div>' +
        '<div class="bat-desc"></div>' +
        '<div class="bar" role="meter" aria-valuemin="0"><span class="fill"></span><span class="lost"></span><span class="ticks"></span><span class="flow" hidden></span><span class="bar-txt"></span></div>' +
        '<div class="bat-meta"><span class="pw"></span><span class="lim"></span></div>';
      el.addEventListener('click', () => selectBattery(b.id));
      wrap.appendChild(el);
      batEls[b.id] = el;
    }
    const active = state.snap.active_battery === b.id;
    const nominal = b.nominal_kwh || b.capacity_kwh;
    el.classList.toggle('is-active', active);
    el.disabled = !(b.status === 'ok' || b.status === 'degraded');
    el.setAttribute('aria-pressed', active);
    el.querySelector('.bat-name').textContent = b.name;
    const st = el.querySelector('.bat-st');
    st.className = 'bat-st ' + b.status;
    st.textContent = (STATUS_RU[b.status] || b.status) + (b.fault_until ? ' до ' + b.fault_until.slice(0, 5) : '');
    el.querySelector('.bat-desc').textContent = b.description || '';

    const fill = el.querySelector('.fill');
    const pctNom = nominal ? b.charge_kwh / nominal * 100 : 0;
    fill.style.width = clamp(pctNom, 0, 100) + '%';
    fill.className = 'fill' + (b.soc < 0.2 ? ' low' : b.soc < 0.4 ? ' mid' : '');
    el.querySelector('.lost').style.width = clamp((nominal - b.capacity_kwh) / nominal * 100, 0, 100) + '%';
    const bar = el.querySelector('.bar');
    bar.setAttribute('aria-valuemax', b.capacity_kwh);
    bar.setAttribute('aria-valuenow', b.charge_kwh.toFixed(2));
    el.querySelector('.bar-txt').textContent =
      b.charge_kwh.toFixed(1) + ' / ' + b.capacity_kwh.toFixed(1) + ' кВт·ч · ' + Math.round(b.soc * 100) + '%';
    const flow = el.querySelector('.flow');
    flow.hidden = Math.abs(b.power_kw) < 0.01;
    flow.classList.toggle('out', b.power_kw < 0);

    const pw = el.querySelector('.pw');
    if (b.power_kw > 0.01) pw.innerHTML = '<b class="in">▲ заряд ' + b.power_kw.toFixed(2) + ' кВт</b>';
    else if (b.power_kw < -0.01) pw.innerHTML = '<b class="out">▼ разряд ' + (-b.power_kw).toFixed(2) + ' кВт</b>';
    else pw.textContent = active ? 'простой' : 'резерв';
    el.querySelector('.lim').textContent =
      'заряд ≤' + b.max_charge_kw + ' · разряд ≤' + b.max_discharge_kw + ' (пик ' + (+b.peak_discharge_kw).toFixed(1) + ') кВт';
  });
}
async function selectBattery(id) {
  try {
    await api('/api/battery/active', { id });
    state.snap.active_battery = id;
    updateBatteries();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- показатели
function updateKpis() {
  const s = state.snap;
  $('clock').textContent = s.now;
  $('dayName').textContent = s.day;
  $('phaseName').textContent = s.status === 'before' ? 'ожидание начала смены'
    : s.status === 'finished' ? 'день завершён' : (s.phase || 'штатный режим');
  $('kGen').innerHTML = kw(s.gen) + '<small>кВт</small>';
  $('kCons').innerHTML = kw(s.cons) + '<small>кВт</small>';
  $('kNet').innerHTML = (s.net > 0 ? '+' : '') + kw(s.net) + '<small>кВт</small>';
  $('kCorr').innerHTML = s.corridor.min.toFixed(1) + '–' + s.corridor.max.toFixed(1) + '<small>кВт</small>';
  const box = $('kCorrBox'), st = $('kCorrState');
  const over = s.cons > s.corridor.max, under = s.cons < s.corridor.min;
  box.className = 'kpi corridor-kpi ' + (over || under ? 'st-bad' : 'st-ok');
  st.textContent = over ? '▲ ПЕРЕГРУЗ' : under ? '▼ НИЖЕ ПОРОГА' : '✓ в коридоре';

  $('alarms').innerHTML = (s.alarms || []).map(a =>
    '<div class="alarm">⚠ ' + esc(a.title) + ': до срабатывания защиты<b>' + Math.ceil(a.left_s) + ' с</b></div>').join('');
  const st2 = s.stats;
  $('termStats').textContent = 'нарушения ▲' + st2.overload + ' ▼' + st2.underload + ' ⚡' + st2.deficit;
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
  [['forecast', 'Прогноз ± σ', C.forecast, 'dashed'],
   ['gen', 'Генерация', C.gen, ''],
   ['cons', 'Потребление', C.cons, ''],
   ['corridor', 'Ограничения (коридор)', C.limit, 'dashed']].forEach(([k, label, color, sw]) => {
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
document.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.seg button').forEach(x => x.classList.toggle('is-sel', x === b));
  state.range = b.dataset.range;
  renderChart();
}));

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

function renderChart() {
  const s = state.snap;
  const svg = $('powerChart'), wrap = $('powerWrap');
  if (!s) return;
  const W = Math.max(200, wrap.clientWidth), H = Math.max(120, wrap.clientHeight);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.innerHTML = '';

  const now = s.t;
  let x0 = s.start, x1 = s.end;
  if (state.range === '2h') {
    x0 = Math.max(s.start, now - CONFIG.zoomWindowMin[0]);
    x1 = Math.min(s.end, Math.max(now, s.start) + CONFIG.zoomWindowMin[1]);
    if (x1 - x0 < 30) x1 = x0 + 30;
  }
  const h = state.history.filter(p => p.t >= x0 - 1 && p.t <= x1 + 1);
  const f = state.forecast;
  const fIdx = f ? f.t.map((t, i) => i).filter(i => f.t[i] >= x0 - 30 && f.t[i] <= x1 + 30) : [];

  let yMax = 1;
  if (state.show.forecast) fIdx.forEach(i => yMax = Math.max(yMax, f.mean[i] + f.sd[i]));
  h.forEach(p => {
    if (state.show.gen) yMax = Math.max(yMax, p.gen);
    if (state.show.cons) yMax = Math.max(yMax, p.cons);
    if (state.show.corridor) yMax = Math.max(yMax, p.max);
    state.activeLines.forEach(i => yMax = Math.max(yMax, p.per[i] || 0));
  });
  if (state.show.corridor) yMax = Math.max(yMax, s.corridor.max);
  yMax = niceMax(yMax * 1.1);

  const X = t => PAD.l + (t - x0) / (x1 - x0) * (W - PAD.l - PAD.r);
  const Y = v => H - PAD.b - clamp(v, 0, yMax) / yMax * (H - PAD.t - PAD.b);
  state.scale = { x0, x1, W, H, X, Y };

  // будущее — слегка затенено
  if (now < x1) {
    const xn = X(Math.max(now, x0));
    svg.appendChild(svgEl('rect', { class: 'future', x: xn, y: PAD.t, width: W - PAD.r - xn, height: H - PAD.t - PAD.b }));
  }
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
  const step = span > 480 ? 120 : span > 240 ? 60 : span > 90 ? 30 : 15;
  for (let tm = Math.ceil(x0 / step) * step; tm <= x1; tm += step) {
    const t = svgEl('text', { x: X(tm), y: H - 6, 'text-anchor': 'middle' });
    t.textContent = fmtHM(tm);
    g.appendChild(t);
  }
  const unit = svgEl('text', { x: PAD.l - 6, y: 10, 'text-anchor': 'end' });
  unit.textContent = 'кВт';
  g.appendChild(unit);
  svg.appendChild(g);

  const clip = 'clip-' + Date.now();
  const defs = svgEl('defs', {});
  const cp = svgEl('clipPath', { id: clip });
  cp.appendChild(svgEl('rect', { x: PAD.l, y: 0, width: W - PAD.l - PAD.r, height: H }));
  defs.appendChild(cp); svg.appendChild(defs);
  const layer = svgEl('g', { 'clip-path': 'url(#' + clip + ')' });
  svg.appendChild(layer);

  // прогноз
  if (state.show.forecast && fIdx.length > 1) {
    const up = fIdx.map(i => [X(f.t[i]), Y(f.mean[i] + f.sd[i])]);
    const lo = fIdx.map(i => [X(f.t[i]), Y(Math.max(0, f.mean[i] - f.sd[i]))]).reverse();
    layer.appendChild(svgEl('path', { class: 'band', d: path(up) + ' ' + path(lo).replace('M', 'L') + ' Z', fill: C.forecast }));
    layer.appendChild(svgEl('path', { class: 'series thin dashed', d: path(fIdx.map(i => [X(f.t[i]), Y(f.mean[i])])), stroke: C.forecast }));
  }

  // коридор (история + текущее значение до «сейчас»)
  const labels = [];
  if (state.show.corridor && h.length) {
    const stepPts = key => {
      const pts = [];
      h.forEach((p, i) => {
        if (i) pts.push([X(p.t), Y(h[i - 1][key])]);
        pts.push([X(p.t), Y(p[key])]);
      });
      const cur = s.corridor[key === 'max' ? 'max' : 'min'];
      pts.push([X(now), Y(h[h.length - 1][key])], [X(now), Y(cur)]);
      return pts;
    };
    const maxPts = stepPts('max'), minPts = stepPts('min');
    layer.appendChild(svgEl('path', { class: 'limit-zone', d: path(maxPts) + ' L' + X(now) + ',' + PAD.t + ' L' + X(h[0].t) + ',' + PAD.t + ' Z' }));
    layer.appendChild(svgEl('path', { class: 'limit-zone', d: path(minPts) + ' L' + X(now) + ',' + Y(0) + ' L' + X(h[0].t) + ',' + Y(0) + ' Z' }));
    layer.appendChild(svgEl('path', { class: 'limit', d: path(maxPts) }));
    layer.appendChild(svgEl('path', { class: 'limit', d: path(minPts) }));
    labels.push([Y(s.corridor.max), 'макс ' + s.corridor.max.toFixed(1)], [Y(s.corridor.min), 'мин ' + s.corridor.min.toFixed(1)]);
  }

  function series(get, color, cls, label) {
    if (h.length < 2) return;
    const pts = h.map(p => [X(p.t), Y(get(p))]);
    layer.appendChild(svgEl('path', { class: 'series ' + cls, d: path(pts), stroke: color }));
    const last = pts[pts.length - 1];
    if (label) {
      svg.appendChild(svgEl('circle', { class: 'dot-end', cx: last[0], cy: last[1], r: 4, fill: color }));
      labels.push([last[1], label]);
    }
  }
  state.activeLines.forEach(i => { const st = lineStyle(i); series(p => p.per[i] || 0, st.color, 'thin ' + st.dash); });
  if (state.show.cons) series(p => p.cons, C.cons, '', kw(s.cons));
  if (state.show.gen) series(p => p.gen, C.gen, '', kw(s.gen));

  // «сейчас»
  if (now >= x0 && now <= x1) {
    svg.appendChild(svgEl('line', { class: 'now', x1: X(now), x2: X(now), y1: PAD.t, y2: H - PAD.b }));
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
    if (h.length && t <= h[h.length - 1].t + 1) {
      let best = h[0];
      for (const p of h) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      tt = best.t;
      if (state.show.gen) html += row(C.gen, '', 'Генерация', kw(best.gen) + ' кВт');
      if (state.show.cons) html += row(C.cons, '', 'Потребление', kw(best.cons) + ' кВт');
      if (state.show.corridor) html += row(C.limit, 'dashed', 'Коридор', best.min.toFixed(1) + '–' + best.max.toFixed(1) + ' кВт');
      state.activeLines.forEach(i => {
        const st = lineStyle(i);
        html += row(st.color, st.dash, state.snap.lines[i].name, kw(best.per[i] || 0) + ' кВт');
      });
    }
    const fc = state.show.forecast ? forecastAt(tt) : null;
    if (fc) html += row(C.forecast, 'dashed', 'Прогноз', fc.mean.toFixed(2) + ' ± ' + fc.sd.toFixed(2) + ' кВт');
    if (!html) { tip.hidden = true; return; }
    tip.innerHTML = '<div class="tip-time">' + fmtHM(tt) + '</div>' + html;
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
  if (!det.open) return;
  const h = state.history.slice(-240).reverse();
  let html = '<thead><tr><th>Время</th><th>Генерация</th><th>Прогноз</th><th>Потребление</th><th>Мин</th><th>Макс</th>';
  state.activeLines.forEach(i => html += '<th>' + esc(state.snap.lines[i].name) + '</th>');
  html += '</tr></thead><tbody>';
  h.forEach(p => {
    const fc = forecastAt(p.t);
    html += '<tr><td>' + fmtHM(p.t) + '</td><td>' + kw(p.gen) + '</td><td>' + (fc ? kw(fc.mean) : '—') + '</td><td>' +
      kw(p.cons) + '</td><td>' + p.min.toFixed(1) + '</td><td>' + p.max.toFixed(1) + '</td>';
    state.activeLines.forEach(i => html += '<td>' + kw(p.per[i] || 0) + '</td>');
    html += '</tr>';
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
    div.innerHTML = '<span class="ts">[' + esc(m.ts.slice(11)) + ']</span> <span class="lv">' + esc(m.level) + '</span> <span class="tx"></span>';
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
async function loadHistory() {
  const [h, f] = await Promise.all([api('/api/history'), api('/api/forecast')]);
  state.history = h.samples;
  state.forecast = f;
}
async function pollState() {
  try {
    const s = await api('/api/state');
    state.snap = s;
    const last = state.history[state.history.length - 1];
    if (s.status === 'running' && (!last || s.t - last.t >= CONFIG.liveStepMin)) {
      state.history.push({ t: s.t, gen: s.gen, cons: s.cons, min: s.corridor.min, max: s.corridor.max,
        per: s.lines.map(l => (l.on ? l.kw : 0)) });
    } else if (last && s.t < last.t - 1) {
      await loadHistory(); // время ушло назад (перемотка/новый день)
    }
    updateLines(); updateBatteries(); updateKpis(); renderChart(); buildTable();
    $('upd').textContent = 'связь с куполом' + (s.speed !== 1 ? ' · ускорение ×' + s.speed : '');
    $('liveDot').classList.remove('lost');
  } catch (e) {
    $('upd').textContent = 'нет связи с куполом';
    $('liveDot').classList.add('lost');
  }
}

async function init() {
  buildSeriesToggles();
  initHover();
  try { await loadHistory(); } catch (e) { /* загрузим при следующей синхронизации */ }
  await pollState();
  await pollTerminal(true);
  setInterval(pollState, CONFIG.stateMs);
  setInterval(() => pollTerminal(false), CONFIG.terminalMs);
  setInterval(() => loadHistory().catch(() => {}), CONFIG.historyResyncMs);
  window.addEventListener('resize', renderChart);
}
init();
