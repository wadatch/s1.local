// 画面の中身はサーバ側で描画済み。ここがやるのは定期的な値の差し替えだけ。
// JS が動かなくてもページは読める状態を保つ。

const REFRESH_INTERVAL_MS = 15000;

const STATUS_LABEL = { up: "UP", down: "DOWN", unknown: "不明" };

function renderUpdatedAt(epochSeconds) {
  const el = document.getElementById("updated-at");
  if (!el) return;
  const date = new Date(epochSeconds * 1000);
  el.textContent = date.toLocaleTimeString("ja-JP");
  el.dateTime = date.toISOString();
}

function updateCard(card, service) {
  const dot = card.querySelector(".dot");
  if (dot) {
    dot.className = `dot dot-${service.health.status}`;
    dot.title = service.health.detail || "";
  }

  const statusText = card.querySelector(".status-text");
  if (statusText) {
    statusText.className = `status-text status-${service.health.status}`;
    statusText.textContent = STATUS_LABEL[service.health.status] || service.health.status;
  }

  // メトリクスの並びはサーバ側の描画と同じ順序。
  const nodes = card.querySelectorAll(".metric");
  service.metrics.forEach((metric, i) => {
    const node = nodes[i];
    if (!node) return;
    node.className = `metric level-${metric.level}`;
    if (metric.detail) {
      node.title = metric.detail;
    } else {
      node.removeAttribute("title");
    }
    const dd = node.querySelector("dd");
    if (dd) dd.textContent = metric.display;
  });
}

async function refresh() {
  let payload;
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    payload = await response.json();
  } catch {
    // 取得できないときは前の表示を残す。
    // ポータル自体が落ちているのか監視対象が落ちているのかは
    // カードの色ではなく更新時刻の停止で読み取る。
    return;
  }

  document.body.classList.add("refreshing");

  for (const service of payload.services || []) {
    const card = document.querySelector(`[data-service-id="${CSS.escape(service.id)}"]`);
    if (card) updateCard(card, service);
  }
  renderUpdatedAt(payload.updated_at);

  requestAnimationFrame(() => document.body.classList.remove("refreshing"));

  // services.yml にサービスを足したときは、カードそのものを増やす必要がある。
  // 差分描画を書くよりページを読み直すほうが確実で、追加は稀な操作なので
  // これで十分（この挙動が「再起動なしで増やせる」を成立させている）。
  const rendered = document.querySelectorAll("[data-service-id]").length;
  if ((payload.services || []).length !== rendered) {
    location.reload();
  }
}

refresh();
setInterval(refresh, REFRESH_INTERVAL_MS);
