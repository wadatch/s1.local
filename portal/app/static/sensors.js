// センサー一覧の自動更新。
//
// ページ全体を読み込み直すと、スクロール位置が飛び、押しかけたトグルの
// 操作も邪魔になる。行の中身だけを差し替える。
//
// ただし行の構造そのものが変わるとき（取得待ち → 値あり、値なしに転落など）は
// セルの数が変わるので、そのときだけ読み込み直す。

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

function batteryLevel(battery) {
  if (battery === null || battery === undefined) return "none";
  if (battery <= 10) return "crit";
  if (battery <= 30) return "warn";
  return "ok";
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

async function refresh() {
  // 見ていないタブで問い合わせを続けても意味がない。
  if (document.hidden) return;

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

    setCell(row, "cell-temperature",
      sensor.temperature === null ? "—" : `${sensor.temperature.toFixed(1)} °C`);
    setCell(row, "cell-humidity",
      sensor.humidity === null ? "—" : `${Math.round(sensor.humidity)} %`);
    setCell(row, "cell-battery",
      sensor.battery === null ? "給電" : `${Math.round(sensor.battery)} %`,
      batteryLevel(sensor.battery));
    setCell(row, "cell-age",
      `${formatDuration(sensor.age_seconds)}前`,
      freshness(sensor.age_seconds));
  }

  renderUpdatedAt(payload.updated_at);
}

refresh();
setInterval(refresh, REFRESH_INTERVAL_MS);

// 別のタブから戻ってきたら、止まっていたぶんをすぐ埋める。
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
