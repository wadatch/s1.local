// センサー一覧の更新。
//
// ページ全体を読み込み直すと、スクロール位置が飛び、押しかけたトグルの
// 操作も邪魔になる。行の中身だけを差し替える。
//
// ただし行の構造そのものが変わるとき（取得待ち → 値あり、値なしに転落など）は
// セルの数が変わるので、そのときだけ読み込み直す。
//
// 自動更新・更新ボタン・引っ張って更新の面倒は refresh-ui.js が見る。

const REFRESH_INTERVAL_MS = 30000;

function formatDuration(seconds) {
  // サーバ側の format_value(..., 'duration') と同じ出し方にそろえる。
  if (seconds === null || seconds === undefined) return "—";
  let rest = Math.floor(seconds);
  const days = Math.floor(rest / 86400);
  rest -= days * 86400;
  const hours = Math.floor(rest / 3600);
  rest -= hours * 3600;
  const minutes = Math.floor(rest / 60);
  if (days) return `${days} 日 ${hours} 時間`;
  if (hours) return `${hours} 時間 ${minutes} 分`;
  return `${minutes} 分`;
}

// ここから下の段階分けは、サーバ側の Sensor の同名プロパティと
// そろえておく。ずれると更新の前後で色が変わってしまう。

function temperatureLevel(t) {
  if (t === null || t === undefined) return "unknown";
  if (t <= 10) return "cold";
  if (t <= 18) return "cool";
  if (t <= 26) return "comfort";
  if (t <= 30) return "warm";
  return "hot";
}

function humidityLevel(h) {
  if (h === null || h === undefined) return "unknown";
  if (h <= 30) return "dry";
  if (h <= 40) return "dryish";
  if (h <= 60) return "comfort";
  if (h <= 70) return "humidish";
  return "humid";
}

function discomfort(t, h) {
  if (t === null || t === undefined || h === null || h === undefined) return null;
  return 0.81 * t + 0.01 * h * (0.99 * t - 14.3) + 46.3;
}

function discomfortLevel(di) {
  if (di === null) return "unknown";
  if (di < 55) return "cold";
  if (di < 60) return "cool";
  if (di < 75) return "comfort";
  if (di < 80) return "warm";
  if (di < 85) return "hot";
  return "severe";
}

const DISCOMFORT_TEXT = {
  cold: "寒い",
  cool: "肌寒い",
  comfort: "快適",
  warm: "やや暑い",
  hot: "暑くて汗が出る",
  severe: "暑くてたまらない",
  unknown: "—",
};

/** 暑さ指数 WBGT の推定値。sensors.py の Sensor.wbgt と同じ式。 */
function wbgt(t, h) {
  if (t === null || t === undefined || h === null || h === undefined) return null;
  return 0.735 * t + 0.0374 * h + 0.00292 * t * h - 4.064;
}

function heatLevel(value) {
  if (value === null) return "unknown";
  if (value < 21) return "safe";
  if (value < 25) return "caution";
  if (value < 28) return "warn";
  if (value < 31) return "severe";
  return "danger";
}

const HEAT_ICON = {
  safe: "shield-check",
  caution: "shield-alert",
  warn: "triangle-alert",
  severe: "octagon-alert",
  danger: "siren",
  unknown: "none",
};

const HEAT_LABEL = {
  safe: "ほぼ安全",
  caution: "注意",
  warn: "警戒",
  severe: "厳重警戒",
  danger: "危険",
  unknown: "—",
};

function heatText(value) {
  const label = HEAT_LABEL[heatLevel(value)];
  if (value === null) return `熱中症リスク ${label}`;
  return `熱中症リスク ${label}（暑さ指数の目安 ${Math.round(value)}）`;
}

/** 熱中症のセルはアイコンなので、文字ではなく形と色を差し替える。 */
function setHeat(row, temperature, humidity) {
  const cell = row.querySelector(".cell-heat");
  if (!cell) return;
  const value = wbgt(temperature, humidity);
  const text = heatText(value);

  cell.className = `num cell-heat heat-${heatLevel(value)}`;
  cell.title = text;

  const icon = cell.querySelector(".heat-icon");
  if (icon) icon.dataset.heat = HEAT_ICON[heatLevel(value)];

  const hidden = cell.querySelector(".visually-hidden");
  if (hidden) hidden.textContent = text;
}

function batteryLevel(battery) {
  if (battery === null || battery === undefined) return "none";
  if (battery <= 10) return "crit";
  if (battery <= 30) return "warn";
  return "ok";
}

function batteryIcon(battery) {
  if (battery === null || battery === undefined) return "charging";
  if (battery <= 10) return "warning";
  if (battery <= 30) return "low";
  if (battery <= 70) return "medium";
  return "full";
}

function batteryText(battery) {
  if (battery === null || battery === undefined) return "給電";
  return `電池 ${Math.round(battery)}%`;
}

/** 電池のセルはアイコンなので、文字ではなく見た目の種類を差し替える。 */
function setBattery(row, battery) {
  const cell = row.querySelector(".cell-battery");
  if (!cell) return;
  cell.className = `num cell-battery level-${batteryLevel(battery)}`;

  const icon = cell.querySelector(".battery-icon");
  const text = batteryText(battery);
  if (icon) {
    icon.dataset.battery = batteryIcon(battery);
    icon.title = text;
  }
  const hidden = cell.querySelector(".visually-hidden");
  if (hidden) hidden.textContent = text;
}

function freshness(age) {
  if (age === null || age === undefined) return "unknown";
  if (age > 3600) return "crit";
  if (age > 1800) return "warn";
  return "ok";
}

function stateOf(sensor) {
  if (!sensor.enabled) return "stopped";
  if (sensor.offline) return "offline";
  if (sensor.temperature === null && sensor.humidity === null) return "pending";
  return "active";
}

function setCell(row, className, text, level) {
  const cell = row.querySelector(`.${className}`);
  if (!cell) return;
  cell.textContent = text;
  if (level !== undefined) {
    cell.className = `num ${className} level-${level}`;
    if (className === "cell-temperature") cell.classList.add("strong");
  }
}

function renderUpdatedAt(epochSeconds) {
  const el = document.getElementById("updated-at");
  if (!el) return;
  const date = new Date(epochSeconds * 1000);
  el.textContent = date.toLocaleTimeString("ja-JP");
  el.dateTime = date.toISOString();
}

async function refresh(manual) {
  if (manual) {
    // 押したのに数字が動かない、を避ける。SwitchBot から取り直させてから読む。
    const result = await pokeSwitchBot();
    showRefreshNote(
      result.triggered
        ? (result.completed ? "取り直しました" : "取り直しています…")
        : result.reason || ""
    );
  }

  let payload;
  try {
    const response = await fetch("/api/sensors", { cache: "no-store" });
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    // 取れないときは前の表示を残す。更新時刻が止まることで気づける。
    return;
  }

  if (payload.error) return;

  const rows = new Map(
    Array.from(document.querySelectorAll("tr[data-device-id]")).map((tr) => [
      tr.dataset.deviceId,
      tr,
    ])
  );

  // 行の構造が変わる場合と、センサーが増減した場合は読み込み直す。
  if (payload.sensors.length !== rows.size) {
    location.reload();
    return;
  }
  for (const sensor of payload.sensors) {
    const row = rows.get(sensor.device_id);
    if (!row || row.dataset.state !== stateOf(sensor)) {
      location.reload();
      return;
    }
  }

  for (const sensor of payload.sensors) {
    const row = rows.get(sensor.device_id);
    if (stateOf(sensor) !== "active") continue;

    const temp = row.querySelector(".cell-temperature");
    if (temp) {
      temp.textContent =
        sensor.temperature === null ? "—" : `${sensor.temperature.toFixed(1)} °C`;
      temp.className =
        `num strong cell-temperature temp-${temperatureLevel(sensor.temperature)}`;
    }

    const hum = row.querySelector(".cell-humidity");
    if (hum) {
      hum.textContent =
        sensor.humidity === null ? "—" : `${Math.round(sensor.humidity)} %`;
      hum.className = `num cell-humidity hum-${humidityLevel(sensor.humidity)}`;
    }

    const di = row.querySelector(".cell-discomfort");
    if (di) {
      const value = discomfort(sensor.temperature, sensor.humidity);
      const level = discomfortLevel(value);
      di.textContent = value === null ? "—" : String(Math.round(value));
      di.className = `num cell-discomfort di-${level}`;
      di.title = DISCOMFORT_TEXT[level];
    }

    setHeat(row, sensor.temperature, sensor.humidity);
    setBattery(row, sensor.battery);
    setCell(row, "cell-age",
      `${formatDuration(sensor.age_seconds)}前`,
      freshness(sensor.age_seconds));
  }

  renderUpdatedAt(payload.updated_at);
}

setupRefresh({ onRefresh: refresh, intervalMs: REFRESH_INTERVAL_MS });
