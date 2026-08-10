// 温湿度グラフ。
//
// ビルド工程を持たないので、折れ線は SVG を手で組み立てて描く。
// 宅内から開くだけのページに、外部ライブラリを抱える価値はない。

const STORAGE_KEY = "s1-portal.graphs.v1";

// 値は 10 分ごとにしか増えないので、これより短くしても得るものはない。
const REFRESH_INTERVAL_MS = 60000;

// グラフを触っている最中に描き直すと、読もうとしていた値が消える。
let pointerOnChart = false;

const state = {
  hours: 24,
  layout: "overlay",   // overlay | separate
  metric: "both",      // both | temperature | humidity
  devices: [],         // {id, name, home}
};

// 色は「同時に重ねたとき隣り合う線が見分けられること」だけを条件に選んでいる。
// 明るさを揃えてあるので、ダークモードでも沈まない。
const PALETTE = [
  "#4c9be8", "#e8734c", "#3fb37f", "#c264d4", "#d4a63f",
  "#4cd0d4", "#e86ba0", "#8a8ff0", "#7fb33f", "#d45f5f",
];

const colorOf = new Map();

function color(deviceId) {
  if (!colorOf.has(deviceId)) {
    colorOf.set(deviceId, PALETTE[colorOf.size % PALETTE.length]);
  }
  return colorOf.get(deviceId);
}

// --- 選んだ内容を覚えておく ------------------------------------------------
//
// 毎回選び直すのは面倒なので、前回の選択をブラウザに残す。
// 保存に失敗しても（プライベートモードなど）グラフ自体は動くこと。

function saveSelection() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      hours: state.hours,
      layout: state.layout,
      metric: state.metric,
      devices: selectedDevices().map((d) => d.id),
    }));
  } catch {
    // 保存できなくても表示には影響しない
  }
}

function loadSelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!saved || typeof saved !== "object") return null;
    return saved;
  } catch {
    return null;
  }
}

function applySegmented(id, key, value) {
  const container = document.getElementById(id);
  if (!container) return false;
  const button = container.querySelector(`button[data-${key}="${value}"]`);
  if (!button) return false;
  container.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
  button.classList.add("active");
  return true;
}

/** 保存してある条件を画面に当てる。前回の続きと、名前をつけた条件の両方で使う。
 *
 * 戻り値は「センサーを 1 台でも当てられたか」。保存したセンサーが今も居るとは
 * 限らない（取得を止めた・撤去した）ので、居ないものは黙って飛ばす。
 */
function applySelection(saved) {
  if (!saved) return false;

  if (typeof saved.hours === "number" && applySegmented("range-buttons", "hours", saved.hours)) {
    state.hours = saved.hours;
  }
  if (applySegmented("layout-buttons", "layout", saved.layout)) {
    state.layout = saved.layout;
  }
  if (applySegmented("metric-buttons", "metric", saved.metric)) {
    state.metric = saved.metric;
  }

  // 今の選択を消してから当てる。当てる側に無いセンサーが残ると、
  // 「保存したときの条件」と画面が食い違う。
  document.querySelectorAll(".device-check").forEach((box) => { box.checked = false; });

  let restored = 0;
  for (const id of saved.devices || []) {
    const box = document.querySelector(`.device-check[value="${CSS.escape(id)}"]`);
    if (box) {
      box.checked = true;
      restored++;
    }
  }
  return restored > 0;
}

function restoreSelection() {
  return applySelection(loadSelection());
}

// --- 選択 -----------------------------------------------------------------

function selectedDevices() {
  return Array.from(document.querySelectorAll(".device-check:checked")).map((el) => ({
    id: el.value,
    name: el.dataset.name,
    home: el.dataset.home,
  }));
}

function syncHomeChecks() {
  document.querySelectorAll(".home-check").forEach((homeEl) => {
    const boxes = Array.from(
      document.querySelectorAll(`.device-check[data-home="${CSS.escape(homeEl.dataset.home)}"]`)
    );
    const checked = boxes.filter((b) => b.checked).length;
    homeEl.checked = checked > 0 && checked === boxes.length;
    // 一部だけ選ばれている状態を表す
    homeEl.indeterminate = checked > 0 && checked < boxes.length;
  });
}

function paintSwatches() {
  document.querySelectorAll(".device-check").forEach((el) => {
    const swatch = document.querySelector(`.swatch[data-device="${CSS.escape(el.value)}"]`);
    if (!swatch) return;
    if (el.checked) {
      swatch.style.background = color(el.value);
      swatch.style.borderColor = color(el.value);
    } else {
      swatch.style.background = "transparent";
      swatch.style.borderColor = "";
    }
  });
}

// --- 名前をつけた表示条件 ---------------------------------------------------
//
// よく見る組み合わせを残しておくためのもの。センサーを選び直すのが毎回同じ
// 手間になるため（「夏の南棟だけ」「寝室の 7 日」など）。
//
// 保存先はこのブラウザ。端末ごとに見たいものが違うので、サーバに置く意味がない。
// 前回の続き（STORAGE_KEY）とは別にしてある。前回の続きは黙って上書きされる
// ものなので、名前をつけて残したものを同じ場所に置くと消える。

const PRESETS_KEY = "s1-portal.graphs.presets.v1";

function loadPresets() {
  try {
    const saved = JSON.parse(localStorage.getItem(PRESETS_KEY) || "[]");
    return Array.isArray(saved) ? saved.filter((p) => p && typeof p.name === "string") : [];
  } catch {
    return [];   // 読めなくてもグラフ自体は動く
  }
}

function storePresets(presets) {
  try {
    localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
  } catch {
    // 保存できなくても表示には影響しない
  }
}

/** 今の条件が、保存済みのどれかと同じか。同じなら名前を返す。 */
function matchingPresetName() {
  const now = {
    hours: state.hours,
    layout: state.layout,
    metric: state.metric,
    devices: selectedDevices().map((d) => d.id).sort(),
  };
  const found = loadPresets().find((p) =>
    p.hours === now.hours &&
    p.layout === now.layout &&
    p.metric === now.metric &&
    JSON.stringify([...(p.devices || [])].sort()) === JSON.stringify(now.devices)
  );
  return found ? found.name : null;
}

function renderPresets() {
  const box = document.getElementById("presets");
  if (!box) return;

  const presets = loadPresets();
  box.innerHTML = "";

  if (presets.length === 0) {
    const empty = document.createElement("span");
    empty.className = "presets-empty";
    empty.textContent = "まだありません";
    box.appendChild(empty);
    return;
  }

  const active = matchingPresetName();

  for (const preset of presets) {
    const chip = document.createElement("span");
    chip.className = "preset" + (preset.name === active ? " preset-active" : "");

    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "preset-apply";
    apply.textContent = preset.name;
    apply.title = `${preset.name} の条件にする（センサー ${(preset.devices || []).length} 台）`;
    apply.addEventListener("click", () => {
      applySelection(preset);
      load();
    });
    chip.appendChild(apply);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "preset-delete";
    remove.textContent = "×";
    remove.title = `${preset.name} を消す`;
    remove.setAttribute("aria-label", `${preset.name} を消す`);
    remove.addEventListener("click", () => {
      // 消すのは戻せないので一度止める。押し間違いで消えると、
      // 何を保存していたか思い出せない。
      if (!confirm(`「${preset.name}」を消しますか。`)) return;
      storePresets(loadPresets().filter((p) => p.name !== preset.name));
      renderPresets();
    });
    chip.appendChild(remove);

    box.appendChild(chip);
  }
}

function wirePresetSave() {
  const form = document.getElementById("preset-save");
  const input = document.getElementById("preset-name");
  if (!form || !input) return;

  form.addEventListener("submit", (event) => {
    event.preventDefault();

    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }

    if (selectedDevices().length === 0) {
      showRefreshNote("センサーを選んでから保存してください");
      return;
    }

    const presets = loadPresets();
    const existing = presets.findIndex((p) => p.name === name);
    // 同じ名前は上書き。別物として 2 つ残ると、どちらが新しいか分からない。
    if (existing >= 0 && !confirm(`「${name}」を今の条件で上書きしますか。`)) return;

    const entry = {
      name,
      hours: state.hours,
      layout: state.layout,
      metric: state.metric,
      devices: selectedDevices().map((d) => d.id),
    };
    if (existing >= 0) presets[existing] = entry;
    else presets.push(entry);

    storePresets(presets);
    input.value = "";
    renderPresets();
    renderControlsSummary();
  });
}

/** 選ばれているボタンの文字。表示条件の要約に使う。 */
function activeLabel(id) {
  const button = document.querySelector(`#${id} button.active`);
  return button ? button.textContent.trim() : "";
}

/** 表示条件を畳んでいる間、何を見ているかが分かるようにする。
 *
 * ボタンの文字をそのまま使う。ここで名前を書き直すと、開いたときの
 * ボタンと畳んだときの要約で言葉が食い違う。
 */
function renderControlsSummary() {
  const el = document.getElementById("controls-summary");
  if (!el) return;
  const count = selectedDevices().length;
  const preset = matchingPresetName();
  el.textContent = [
    preset ? `「${preset}」` : "",
    activeLabel("range-buttons"),
    activeLabel("metric-buttons"),
    count ? `${count} 台` : "センサー未選択",
  ].filter(Boolean).join("・");
}

// --- 取得 -----------------------------------------------------------------

async function load(manual) {
  if (manual) {
    // 押したときは、履歴に新しい点が増えてから描く。
    const result = await pokeSwitchBot();
    showRefreshNote(
      result.triggered
        ? (result.completed ? "取り直しました" : "取り直しています…")
        : result.reason || ""
    );
  }

  state.devices = selectedDevices();
  syncHomeChecks();
  paintSwatches();
  saveSelection();
  renderControlsSummary();
  // どの条件を見ているかが分かるよう、一致するものに印を付け直す。
  renderPresets();

  const charts = document.getElementById("charts");

  if (state.devices.length === 0) {
    charts.innerHTML =
      '<p class="empty">センサーを選ぶとグラフが出ます。' +
      'ホーム名のチェックでそのホームをまとめて選べます。</p>';
    return;
  }

  charts.setAttribute("aria-busy", "true");

  let payload;
  try {
    const ids = state.devices.map((d) => d.id).join(",");
    const response = await fetch(`/api/history?device_ids=${encodeURIComponent(ids)}&hours=${state.hours}`);
    payload = await response.json();
  } catch {
    charts.innerHTML = '<p class="empty">履歴を取得できませんでした。</p>';
    return;
  } finally {
    charts.removeAttribute("aria-busy");
  }

  if (payload.error) {
    charts.innerHTML = `<p class="empty">${payload.error}</p>`;
    return;
  }

  renderSpan(payload);
  render(payload.series);
}

function renderSpan(payload) {
  const el = document.getElementById("record-span");
  if (!el) return;
  const updated = `（最終更新 ${new Date().toLocaleTimeString("ja-JP")}）`;
  if (!payload.recorded_from) {
    el.textContent = `まだ履歴がありません。数分待つと溜まりはじめます。${updated}`;
    return;
  }
  const from = new Date(payload.recorded_from * 1000);
  el.textContent = `記録期間: ${from.toLocaleString("ja-JP")} 〜 現在 ${updated}`;
}

// --- 描画 -----------------------------------------------------------------

/** 描く幅。カードの内側の余白ぶんを引いた実寸を使う。
 *
 * viewBox を実寸に合わせることで、拡大縮小が起きず文字が横に潰れない。
 * 固定幅にして拡大縮小に任せると、スマホでは文字が半分の幅に潰れるうえ、
 * 最小幅がページ全体を押し広げて画面ごとズームアウトしてしまう。
 */
function chartWidth() {
  const container = document.getElementById("charts");
  const available = container ? container.clientWidth : 900;
  return Math.max(280, Math.min(1200, available - 34));
}

// 画面の幅が変わったら描き直す。取り直しは要らないので値は覚えておく。
let lastSeries = null;

function render(series) {
  lastSeries = series;
  const charts = document.getElementById("charts");
  charts.innerHTML = "";

  const metrics = state.metric === "both"
    ? [["temperature", "温度", "℃"], ["humidity", "湿度", "%"]]
    : state.metric === "temperature"
      ? [["temperature", "温度", "℃"]]
      : [["humidity", "湿度", "%"]];

  const withData = series.filter((s) => s.points.length > 0);
  if (withData.length === 0) {
    charts.innerHTML =
      '<p class="empty">選んだ期間に記録がありません。' +
      '履歴はこの機能を入れた時点から溜まりはじめます。</p>';
    return;
  }

  for (const [key, label, unit] of metrics) {
    if (state.layout === "overlay") {
      charts.appendChild(chartCard(`${label}（同時）`, withData, key, unit));
    } else {
      for (const s of withData) {
        charts.appendChild(chartCard(`${label} — ${s.name}`, [s], key, unit));
      }
    }
  }
}

function chartCard(title, series, key, unit) {
  const card = document.createElement("section");
  card.className = "chart-card";

  const heading = document.createElement("h2");
  heading.textContent = title;
  card.appendChild(heading);

  card.appendChild(buildChart(series, key, unit));

  if (series.length > 1) {
    const legend = document.createElement("ul");
    legend.className = "legend";
    for (const s of series) {
      const item = document.createElement("li");
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = color(s.device_id);
      item.appendChild(dot);
      item.appendChild(document.createTextNode(s.name));
      legend.appendChild(item);
    }
    card.appendChild(legend);
  }

  return card;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function el(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function niceTicks(min, max, count) {
  if (min === max) return [min];
  const raw = (max - min) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max; v += step) ticks.push(v);
  return ticks;
}

function formatTime(seconds, spanHours) {
  const date = new Date(seconds * 1000);
  if (spanHours <= 48) {
    return date.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" });
}

/** 時刻 t にいちばん近い点を返す。点数が多いので二分探索する。 */
function nearestPoint(points, t) {
  if (points.length === 0) return null;
  let low = 0;
  let high = points.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (points[mid].t < t) low = mid + 1;
    else high = mid;
  }
  const after = points[low];
  const before = points[low - 1];
  if (!before) return after;
  return Math.abs(after.t - t) < Math.abs(before.t - t) ? after : before;
}

/** 系列ごとの最新の値。線の右端に出すラベルに使う。 */
function latestOf(series, key) {
  const found = [];
  for (const s of series) {
    const usable = s.points.filter((p) => p[key] !== null && p[key] !== undefined);
    if (usable.length === 0) continue;
    found.push({ deviceId: s.device_id, point: usable[usable.length - 1] });
  }
  return found;
}

function formatValue(value, unit) {
  return `${Math.round(value * 10) / 10}${unit}`;
}

/** 重なったラベルを縦にずらす。
 *
 * 同時表示だと最新の値が近い系列どうしで文字が重なり、どちらも読めなくなる。
 * 上から順に見て、詰まっているものを下へ押しやる。押し切って下枠を越えたら
 * 全体を上へ戻す（枠外に出すと切れて読めない）。
 */
function spreadLabels(labels, minGap, top, bottom) {
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < minGap) {
      labels[i].y = labels[i - 1].y + minGap;
    }
  }
  const overflow = labels.length ? labels[labels.length - 1].y - bottom : 0;
  if (overflow > 0) {
    for (const label of labels) label.y -= overflow;
  }
  if (labels.length && labels[0].y < top) {
    const shift = top - labels[0].y;
    for (const label of labels) label.y += shift;
  }
  return labels;
}

function buildChart(series, key, unit) {
  const width = chartWidth();
  const height = 260;
  const narrow = width < 480;

  // 最新の値は線の右端に文字で出す。グラフを見て最初に知りたいのは
  // 「今いくつか」で、それがカーソルを乗せないと分からないのは遠回り。
  // そのぶんの幅を右に確保してから座標を決める。
  const latest = latestOf(series, key);
  const labelChars = Math.max(
    0, ...latest.map((l) => formatValue(l.point[key], unit).length)
  );
  const labelRoom = labelChars ? labelChars * 6.6 + 10 : 0;

  const pad = {
    top: 12,
    right: (narrow ? 10 : 16) + labelRoom,
    bottom: 28,
    left: narrow ? 38 : 46,
  };

  const points = series.flatMap((s) =>
    s.points.filter((p) => p[key] !== null && p[key] !== undefined)
  );

  const wrap = document.createElement("div");
  wrap.className = "chart-scroll";

  if (points.length === 0) {
    wrap.innerHTML = '<p class="empty">この期間の記録がありません。</p>';
    return wrap;
  }

  const times = points.map((p) => p.t);
  const values = points.map((p) => p[key]);
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  // 値がほぼ一定のとき線が枠に張り付くので、少し余白をとる
  const margin = (maxV - minV) * 0.1 || 1;
  minV -= margin;
  maxV += margin;

  const x = (t) => pad.left + ((t - minT) / (maxT - minT || 1)) * (width - pad.left - pad.right);
  const y = (v) => height - pad.bottom - ((v - minV) / (maxV - minV || 1)) * (height - pad.top - pad.bottom);

  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`,
    class: "chart",
    role: "img",
  });

  // 横の目盛りと補助線
  for (const tick of niceTicks(minV, maxV, 4)) {
    svg.appendChild(el("line", {
      x1: pad.left, x2: width - pad.right, y1: y(tick), y2: y(tick), class: "grid",
    }));
    const text = el("text", { x: pad.left - 8, y: y(tick) + 4, class: "axis", "text-anchor": "end" });
    text.textContent = `${Math.round(tick * 10) / 10}${unit}`;
    svg.appendChild(text);
  }

  // 縦の目盛り。狭い画面では数を減らさないと文字が重なる。
  const spanHours = (maxT - minT) / 3600;
  const steps = narrow ? 3 : 5;
  for (let i = 0; i <= steps; i++) {
    const t = minT + ((maxT - minT) * i) / steps;
    const text = el("text", {
      x: x(t), y: height - 8, class: "axis", "text-anchor": i === 0 ? "start" : i === steps ? "end" : "middle",
    });
    text.textContent = formatTime(t, spanHours);
    svg.appendChild(text);
  }

  // 折れ線
  const usableBySeries = new Map();
  for (const s of series) {
    const usable = s.points.filter((p) => p[key] !== null && p[key] !== undefined);
    usableBySeries.set(s.device_id, usable);
    if (usable.length === 0) continue;

    // 記録が途切れている区間は線をつながない。つなぐと、その間も
    // 測れていたように見えてしまう。10 分間隔なので 40 分空いたら切る。
    const segments = [];
    let current = [usable[0]];
    for (let i = 1; i < usable.length; i++) {
      if (usable[i].t - usable[i - 1].t > 2400) {
        segments.push(current);
        current = [];
      }
      current.push(usable[i]);
    }
    segments.push(current);

    for (const segment of segments) {
      if (segment.length === 1) {
        svg.appendChild(el("circle", {
          cx: x(segment[0].t), cy: y(segment[0][key]), r: 2, fill: color(s.device_id),
        }));
        continue;
      }
      const d = segment.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
      svg.appendChild(el("path", { d, fill: "none", stroke: color(s.device_id), "stroke-width": 1.8 }));
    }

    const last = usable[usable.length - 1];
    svg.appendChild(el("circle", {
      cx: x(last.t), cy: y(last[key]), r: 3, fill: color(s.device_id),
    }));
  }

  // 最新の値。線と同じ色にして、どの線の値かが分かるようにする。
  for (const label of spreadLabels(
    latest.map((l) => ({
      deviceId: l.deviceId,
      x: x(l.point.t),
      y: y(l.point[key]) + 4,   // 文字の中心を点に合わせる
      text: formatValue(l.point[key], unit),
    })),
    13, pad.top + 10, height - pad.bottom,
  )) {
    const text = el("text", {
      x: label.x + 7,
      y: label.y,
      class: "latest-value",
      fill: color(label.deviceId),
    });
    text.textContent = label.text;
    svg.appendChild(text);
  }

  // --- ポイントしたところの値を出す ---------------------------------------
  const guide = el("line", {
    y1: pad.top, y2: height - pad.bottom, class: "guide", visibility: "hidden",
  });
  svg.appendChild(guide);

  const markers = new Map();
  for (const s of series) {
    const marker = el("circle", {
      r: 4, fill: color(s.device_id), class: "marker", visibility: "hidden",
    });
    markers.set(s.device_id, marker);
    svg.appendChild(marker);
  }

  // SVG は「描いたところ」でしかポインタを拾わない。線と線の間の余白に
  // カーソルを置いても反応しないので、当たり判定用の透明な板を最前面に敷く。
  // これが無いと、線の真上を正確になぞらないとツールチップが出ない。
  svg.appendChild(el("rect", {
    x: pad.left,
    y: pad.top,
    width: Math.max(0, width - pad.left - pad.right),
    height: Math.max(0, height - pad.top - pad.bottom),
    fill: "transparent",
    class: "hit-area",
  }));

  wrap.appendChild(svg);

  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  wrap.appendChild(tooltip);

  function hide() {
    guide.setAttribute("visibility", "hidden");
    markers.forEach((m) => m.setAttribute("visibility", "hidden"));
    tooltip.hidden = true;
  }

  function move(event) {
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0) return;

    // viewBox は実寸に合わせてあるので基本は 1:1 だが、描き直す前に
    // 画面幅が変わっていることがあるので、比率で戻しておく。
    const viewX = ((event.clientX - rect.left) / rect.width) * width;
    if (viewX < pad.left || viewX > width - pad.right) {
      hide();
      return;
    }

    const t = minT + ((viewX - pad.left) / (width - pad.left - pad.right)) * (maxT - minT);

    const rows = [];
    let guideTime = null;
    for (const s of series) {
      const point = nearestPoint(usableBySeries.get(s.device_id) || [], t);
      const marker = markers.get(s.device_id);
      if (!point) {
        marker.setAttribute("visibility", "hidden");
        continue;
      }
      marker.setAttribute("cx", x(point.t));
      marker.setAttribute("cy", y(point[key]));
      marker.setAttribute("visibility", "visible");
      rows.push({ name: s.name, deviceId: s.device_id, value: point[key], t: point.t });
      if (guideTime === null || Math.abs(point.t - t) < Math.abs(guideTime - t)) {
        guideTime = point.t;
      }
    }

    if (rows.length === 0) {
      hide();
      return;
    }

    guide.setAttribute("x1", x(guideTime));
    guide.setAttribute("x2", x(guideTime));
    guide.setAttribute("visibility", "visible");

    const when = new Date(guideTime * 1000);
    tooltip.innerHTML = "";
    const time = document.createElement("div");
    time.className = "tooltip-time";
    time.textContent = when.toLocaleString("ja-JP", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    tooltip.appendChild(time);

    for (const row of rows) {
      const line = document.createElement("div");
      line.className = "tooltip-row";
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = color(row.deviceId);
      line.appendChild(dot);
      const name = document.createElement("span");
      name.className = "tooltip-name";
      name.textContent = row.name;
      line.appendChild(name);
      const value = document.createElement("strong");
      value.textContent = `${Math.round(row.value * 10) / 10}${unit}`;
      line.appendChild(value);
      tooltip.appendChild(line);
    }

    tooltip.hidden = false;

    // 枠の外にはみ出さないよう、右端では左側に出す。
    const guideScreenX = (x(guideTime) / width) * rect.width;
    const offset = 14;
    const flip = guideScreenX + offset + tooltip.offsetWidth > rect.width;
    tooltip.style.left = `${Math.max(0, flip ? guideScreenX - tooltip.offsetWidth - offset : guideScreenX + offset)}px`;
    tooltip.style.top = "8px";
  }

  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerdown", move);   // 触った位置でも出す
  svg.addEventListener("pointerenter", () => { pointerOnChart = true; });
  svg.addEventListener("pointerleave", () => {
    pointerOnChart = false;
    hide();
  });

  return wrap;
}

// --- 操作 -----------------------------------------------------------------

function wireSegmented(id, key, cast) {
  document.getElementById(id).addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    state[key] = cast(button.dataset[key]);
    button.parentElement.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    load();
  });
}

wireSegmented("range-buttons", "hours", Number);
wireSegmented("layout-buttons", "layout", String);
wireSegmented("metric-buttons", "metric", String);

document.addEventListener("change", (event) => {
  if (event.target.classList.contains("home-check")) {
    const home = event.target.dataset.home;
    document
      .querySelectorAll(`.device-check[data-home="${CSS.escape(home)}"]`)
      .forEach((box) => { box.checked = event.target.checked; });
    load();
  } else if (event.target.classList.contains("device-check")) {
    load();
  }
});

// 前回の選択があればそれを再現する。無ければ最初のホームを選んだ状態で出す
// （空のページより意図が伝わる）。
wirePresetSave();
renderPresets();

if (!restoreSelection()) {
  const firstHome = document.querySelector(".home-check");
  if (firstHome) {
    firstHome.checked = true;
    document
      .querySelectorAll(`.device-check[data-home="${CSS.escape(firstHome.dataset.home)}"]`)
      .forEach((box) => { box.checked = true; });
  }
}
// --- 更新 -----------------------------------------------------------------
//
// 自動更新・更新ボタン・引っ張って更新の面倒は refresh-ui.js が見る。
// 描き直すとツールチップが消えるので、グラフを触っている間は
// 自動更新だけ見送る（手で押したときは触っていても更新する）。

setupRefresh({
  onRefresh: load,
  intervalMs: REFRESH_INTERVAL_MS,
  shouldSkipAuto: () => pointerOnChart,
});

// 画面の幅が変わったら描き直す（横向きにした、ウィンドウを広げたなど）。
// 値は覚えてあるので取り直しは要らない。
let resizeTimer = null;
let lastWidth = window.innerWidth;

window.addEventListener("resize", () => {
  if (window.innerWidth === lastWidth) return;   // 縦だけの変化は無視する
  lastWidth = window.innerWidth;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (lastSeries) render(lastSeries);
  }, 200);
});
