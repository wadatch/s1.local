// 「更新」を押したときに、SwitchBot から取り直させる。
//
// 押しても値が変わらない、という状態を避けるためのもの。ポータルが持って
// いるのは exporter のキャッシュなので、読み直すだけでは 10 分間ずっと
// 同じ数字が出る。
//
// 自動更新では呼ばない。API の回数制限（1 日 10,000 回）を使い切るため。

async function pokeSwitchBot() {
  try {
    const response = await fetch("/api/switchbot/refresh", { method: "POST" });
    return await response.json();
  } catch {
    return { triggered: false, reason: "取り直しを頼めませんでした" };
  }
}

/** 更新の結果を「最終更新」の隣に短く出す。断られた理由もここに出す。 */
function showRefreshNote(text) {
  const note = document.getElementById("refresh-note");
  if (!note) return;
  note.textContent = text || "";
  if (!text) return;
  clearTimeout(showRefreshNote.timer);
  showRefreshNote.timer = setTimeout(() => { note.textContent = ""; }, 6000);
}
