// 折りたたみの開閉状態を覚える。
//
// 対象は `data-collapse-key` を持つ <details>。開閉そのものはブラウザに任せ、
// ここでは「次に開いたときも同じ状態にする」ことだけを引き受ける。
//
// 覚える必要があるのは、このポータルではページが何度も読み込み直されるため。
// センサー一覧は取得の ON/OFF がフォームの POST で、行の構造が変わったときは
// sensors.js が location.reload() する。覚えていないと、そのたびに全ホームが
// 開き直り、閉じておいたはずのものを何度も畳み直すことになる。
//
// 状態はこのブラウザにだけ残る。端末ごとに見たいものが違うので、
// サーバ側に持たせる意味がない。

const COLLAPSE_KEY = "s1-portal.collapse.v1";

// 保存に失敗しても（プライベートモードなど）折りたたみ自体は動くこと。

function loadCollapseState() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

function saveCollapseState(state) {
  try {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify(state));
  } catch {
    // 保存できなくても表示には影響しない
  }
}

const collapseState = loadCollapseState();

for (const details of document.querySelectorAll("details[data-collapse-key]")) {
  const key = details.dataset.collapseKey;

  // 覚えていないものは HTML の open のまま。既定は開いた状態にしてあるので、
  // 初めて開いた人には中身が見えている。
  if (key in collapseState) details.open = collapseState[key];

  details.addEventListener("toggle", () => {
    collapseState[key] = details.open;
    saveCollapseState(collapseState);
  });
}
