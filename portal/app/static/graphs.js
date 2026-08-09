// 温湿度グラフ。
//
// ビルド工程を持たないので、折れ線は SVG を手で組み立てて描く。
// 宅内から開くだけのページに、外部ライブラリを抱える価値はない。

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

// --- 取得 -----------------------------------------------------------------

async function load() {
  state.devices = selectedDevices();
  syncHomeChecks();
  paintSwatches();

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
  if (!payload.recorded_from) {
    el.textContent = "まだ履歴がありません。数分待つと溜まりはじめます。";
    return;
  }
  const from = new Date(payload.recorded_from * 1000);
  el.textContent = `記録期間: ${from.toLocaleString("ja-JP")} 〜 現在`;
}

// --- 描画 -----------------------------------------------------------------

function render(series) {
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

function buildChart(series, key, unit) {
  const width = 900;
  const height = 260;
  const pad = { top: 12, right: 16, bottom: 28, left: 46 };

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
    preserveAspectRatio: "none",
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

  // 縦の目盛り
  const spanHours = (maxT - minT) / 3600;
  const steps = 5;
  for (let i = 0; i <= steps; i++) {
    const t = minT + ((maxT - minT) * i) / steps;
    const text = el("text", {
      x: x(t), y: height - 8, class: "axis", "text-anchor": i === 0 ? "start" : i === steps ? "end" : "middle",
    });
    text.textContent = formatTime(t, spanHours);
    svg.appendChild(text);
  }

  // 折れ線
  for (const s of series) {
    const usable = s.points.filter((p) => p[key] !== null && p[key] !== undefined);
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

  wrap.appendChild(svg);
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

// 最初は最初のホームを選んだ状態で出す。空のページより意図が伝わる。
const firstHome = document.querySelector(".home-check");
if (firstHome) {
  firstHome.checked = true;
  firstHome.dispatchEvent(new Event("change", { bubbles: true }));
} else {
  load();
}
