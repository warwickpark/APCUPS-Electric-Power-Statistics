/* UPS 전력 모니터 공통 차트/유틸 — 외부 의존성 없음 */
"use strict";

const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const fetchJSON = url => fetch(url).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

function el(name, attrs, parent) {
  const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function niceStep(rough) {
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  return [1, 2, 2.5, 5, 10].map(m => m * pow).find(s => s >= rough) || pow * 10;
}

/* 0부터 최댓값까지 3~5개 눈금 (전력·kWh 차트용) */
function niceTicks(maxVal) {
  if (maxVal <= 0) maxVal = 1;
  const step = niceStep(maxVal / 4);
  const ticks = [];
  for (let v = 0; v <= maxVal + 1e-9; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < maxVal) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

/* 0을 기준으로 삼지 않는 눈금 (전압처럼 변동 폭이 좁은 값용) */
function rangeTicks(minVal, maxVal) {
  if (!(maxVal > minVal)) { minVal -= 1; maxVal += 1; }
  const step = niceStep((maxVal - minVal) / 4);
  const lo = Math.floor(minVal / step) * step;
  const hi = Math.ceil(maxVal / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

function fmt(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v.toLocaleString("ko-KR", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function makeTooltip(container) {
  let tip = container.querySelector(".tooltip");
  if (!tip) { tip = document.createElement("div"); tip.className = "tooltip"; container.appendChild(tip); }
  return tip;
}
function moveTip(tip, container, x, y, html) {
  tip.innerHTML = html; tip.style.display = "block";
  const cw = container.clientWidth, tw = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(0, x - tw / 2), cw - tw) + "px";
  tip.style.top = Math.max(0, y - tip.offsetHeight - 12) + "px";
}

const PAD = { l: 44, r: 16, t: 10, b: 22 };
const H = 200;

/* ticks[0]이 축 하단, ticks[마지막]이 상단. 0에서 시작하지 않아도 된다. */
function frame(container, ticks, unitDigits) {
  container.querySelectorAll("svg,.empty").forEach(n => n.remove());
  const W = Math.max(container.clientWidth, 320);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
  container.appendChild(svg);
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const y = v => PAD.t + (H - PAD.t - PAD.b) * (1 - (v - lo) / (hi - lo));
  // 눈금 간격이 1 미만이면 정수 반올림 시 라벨이 중복된다(219, 219, 220…)
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const digits = Math.max(unitDigits, step < 0.1 ? 2 : (step < 1 ? 1 : 0));
  for (const t of ticks) {
    el("line", { x1: PAD.l, x2: W - PAD.r, y1: y(t), y2: y(t), class: t === lo ? "baseline" : "gridline" }, svg);
    const txt = el("text", { x: PAD.l - 8, y: y(t) + 4, "text-anchor": "end" }, svg);
    txt.textContent = fmt(t, digits);
  }
  return { svg, W, y, lo, hi };
}

function showEmpty(container, msg) {
  container.querySelectorAll("svg,.empty").forEach(n => n.remove());
  const d = document.createElement("div");
  d.className = "empty"; d.textContent = msg || "아직 데이터가 없습니다";
  container.appendChild(d);
}

/* 시각 라벨 헬퍼 */
const pad2 = n => String(n).padStart(2, "0");
function hourLabel(t) { const d = new Date(t * 1000); return d.getHours() + "시"; }
function stampLabel(t) {
  const d = new Date(t * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
function durationLabel(sec) {
  if (sec < 60) return sec + "초";
  if (sec < 3600) return Math.floor(sec / 60) + "분 " + (sec % 60) + "초";
  return Math.floor(sec / 3600) + "시간 " + Math.floor((sec % 3600) / 60) + "분";
}

/* ── 라인 차트 (단일 계열) ─────────────────────────────── */
function lineChart(container, data, opts) {
  opts = opts || {};
  if (!data.length) return showEmpty(container);
  const key = opts.key || "avg_w";
  const unit = opts.unit || "W";
  const ticks = opts.zeroBased === false
    ? rangeTicks(Math.min(...data.map(d => d[key])), Math.max(...data.map(d => d[key])))
    : niceTicks(Math.max(...data.map(d => d[key])));
  const { svg, W, y } = frame(container, ticks, opts.digits || 0);
  const x = i => PAD.l + (W - PAD.l - PAD.r) * (data.length === 1 ? 0.5 : i / (data.length - 1));
  const labelFn = opts.labelFn || hourLabel;

  data.forEach((d, i) => {
    if (opts.labelEvery ? i % opts.labelEvery === 0 : new Date(d.t * 1000).getHours() % 3 === 0) {
      if (i === data.length - 1) return;
      const txt = el("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg);
      txt.textContent = labelFn(d.t);
    }
  });

  const pts = data.map((d, i) => `${x(i)},${y(d[key])}`);
  if (opts.zeroBased !== false) {
    el("path", { class: "area", d: `M${pts.join("L")}L${x(data.length - 1)},${y(ticks[0])}L${x(0)},${y(ticks[0])}Z` }, svg);
  }
  el("path", { class: "line", d: `M${pts.join("L")}` }, svg);

  const last = data[data.length - 1];
  el("circle", { class: "dot", cx: x(data.length - 1), cy: y(last[key]), r: 4.5 }, svg);
  const lbl = el("text", { class: "end-label", x: x(data.length - 1) - 8, y: y(last[key]) - 10, "text-anchor": "end" }, svg);
  lbl.textContent = fmt(last[key], opts.digits || 0) + " " + unit;

  attachCrosshair(container, svg, data, x, y, key, W, opts.tipFn);
}

/* ── 밴드 차트 (min–max 범위 + 평균선) ─────────────────── */
function bandChart(container, data, opts) {
  opts = opts || {};
  if (!data.length) return showEmpty(container);
  // y축은 데이터 기준으로 잡는다. 허용 범위선까지 담으면 실제 변동이 눌려 보인다 —
  // 경계선은 축 안에 들어올 때만(= 전압이 그 근처까지 갔을 때만) 그린다.
  const ticks = rangeTicks(Math.min(...data.map(d => d.min_v)),
                           Math.max(...data.map(d => d.max_v)));
  const { svg, W, y } = frame(container, ticks, 0);
  const limits = (opts.limits || []).filter(
    l => l.v != null && l.v >= ticks[0] && l.v <= ticks[ticks.length - 1]);
  const x = i => PAD.l + (W - PAD.l - PAD.r) * (data.length === 1 ? 0.5 : i / (data.length - 1));
  const labelFn = opts.labelFn || hourLabel;
  const labelEvery = opts.labelEvery || Math.max(1, Math.ceil(data.length / 10));

  // 허용 범위 경계선
  for (const l of limits) {
    el("line", { class: "limit", x1: PAD.l, x2: W - PAD.r, y1: y(l.v), y2: y(l.v) }, svg);
    const t = el("text", { class: "limit-label", x: W - PAD.r, y: y(l.v) - 4, "text-anchor": "end" }, svg);
    t.textContent = l.label;
  }

  // min~max 밴드 (위쪽 경로 → 아래쪽 역순)
  const up = data.map((d, i) => `${x(i)},${y(d.max_v)}`);
  const down = data.map((d, i) => `${x(i)},${y(d.min_v)}`).reverse();
  el("path", { class: "band", d: `M${up.join("L")}L${down.join("L")}Z` }, svg);
  el("path", { class: "line", d: `M${data.map((d, i) => `${x(i)},${y(d.avg_v)}`).join("L")}` }, svg);

  data.forEach((d, i) => {
    if (i % labelEvery === 0 && i !== data.length - 1) {
      const txt = el("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg);
      txt.textContent = labelFn(d.t);
    }
  });

  const last = data[data.length - 1];
  el("circle", { class: "dot", cx: x(data.length - 1), cy: y(last.avg_v), r: 4.5 }, svg);
  const lbl = el("text", { class: "end-label", x: x(data.length - 1) - 8, y: y(last.avg_v) - 10, "text-anchor": "end" }, svg);
  lbl.textContent = fmt(last.avg_v, 0) + " V";

  attachCrosshair(container, svg, data, x, y, "avg_v", W, opts.tipFn || (d =>
    `${labelFn(d.t)} &nbsp; 평균 <b>${fmt(d.avg_v, 1)} V</b>` +
    `<br>범위 ${fmt(d.min_v, 0)} ~ ${fmt(d.max_v, 0)} V · σ ${fmt(d.sd_v, 2)}` +
    `<br>이탈 ${fmt(d.out_pct, 2)}% (${d.n_out}/${d.n})`));
}

function attachCrosshair(container, svg, data, x, y, key, W, tipFn) {
  if (!tipFn) return;
  const tip = makeTooltip(container);
  const ch = el("line", { class: "crosshair", y1: PAD.t, y2: H - PAD.b, visibility: "hidden" }, svg);
  const chDot = el("circle", { class: "dot", r: 4.5, visibility: "hidden" }, svg);
  svg.addEventListener("mousemove", ev => {
    const rect = svg.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) * (W / rect.width);
    let i = Math.round((mx - PAD.l) / (W - PAD.l - PAD.r) * (data.length - 1));
    i = Math.max(0, Math.min(data.length - 1, i));
    const d = data[i];
    ch.setAttribute("x1", x(i)); ch.setAttribute("x2", x(i)); ch.setAttribute("visibility", "visible");
    chDot.setAttribute("cx", x(i)); chDot.setAttribute("cy", y(d[key])); chDot.setAttribute("visibility", "visible");
    moveTip(tip, container, x(i) * (rect.width / W), y(d[key]) * (rect.height / H), tipFn(d));
  });
  svg.addEventListener("mouseleave", () => {
    tip.style.display = "none";
    ch.setAttribute("visibility", "hidden"); chDot.setAttribute("visibility", "hidden");
  });
}

/* ── 막대 차트 (일별/월별 kWh) ─────────────────────────── */
function barChart(container, data, labelFn, tipFn, kwhDigits) {
  if (!data.length) return showEmpty(container);
  const ticks = niceTicks(Math.max(...data.map(d => d.kwh)));
  const { svg, W, y } = frame(container, ticks, kwhDigits);
  const slot = (W - PAD.l - PAD.r) / data.length;
  const bw = Math.min(24, Math.max(3, slot - 2));  // ≤24px, 인접 막대 사이 2px 이상 간격

  const labelEvery = Math.ceil(data.length / 10);
  const tip = makeTooltip(container);

  data.forEach((d, i) => {
    const cx = PAD.l + slot * (i + 0.5);
    const h = y(0) - y(d.kwh);
    const r = Math.min(4, h, bw / 2);  // 상단(데이터 끝) 4px 라운드, 베이스라인은 직각
    const xl = cx - bw / 2;
    el("path", { class: "bar", d:
      `M${xl},${y(0)} L${xl},${y(d.kwh) + r} Q${xl},${y(d.kwh)} ${xl + r},${y(d.kwh)}` +
      ` L${xl + bw - r},${y(d.kwh)} Q${xl + bw},${y(d.kwh)} ${xl + bw},${y(d.kwh) + r}` +
      ` L${xl + bw},${y(0)} Z` }, svg);

    if (i % labelEvery === 0) {
      const txt = el("text", { x: cx, y: H - 6, "text-anchor": "middle" }, svg);
      txt.textContent = labelFn(d);
    }
    // 히트 영역(막대보다 넓게) + 툴팁
    const hit = el("rect", { x: PAD.l + slot * i, y: PAD.t, width: slot, height: H - PAD.t - PAD.b, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", () => {
      const rect = svg.getBoundingClientRect();
      moveTip(tip, container, cx * (rect.width / W), y(d.kwh) * (rect.height / H), tipFn(d));
    });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });

  // 마지막(현재) 막대 값 라벨
  const last = data[data.length - 1];
  const lx = PAD.l + slot * (data.length - 0.5);
  const lbl = el("text", { class: "end-label", x: Math.min(lx, W - PAD.r - 4), y: y(last.kwh) - 6, "text-anchor": "middle" }, svg);
  lbl.textContent = fmt(last.kwh, kwhDigits);
}

/* ── 상태 배지 ─────────────────────────────────────────── */
const STATUS_MAP = [
  [/ONBATT/, ["onbatt", "배터리 구동"]],
  [/ONLINE/, ["good", "정상 (상용전원)"]],
  [/COMMLOST|SHUTTING/, ["crit", "통신 두절"]],
];

function applyStatus(chip, status) {
  const m = STATUS_MAP.find(([re]) => re.test(status || ""));
  chip.className = "chip " + (m ? m[1][0] : "");
  chip.textContent = m ? m[1][1] : (status || "상태 미상");
}

/* 리사이즈 시 차트 다시 그리기 */
function onResize(fn) {
  let timer;
  window.addEventListener("resize", () => { clearTimeout(timer); timer = setTimeout(fn, 150); });
}
