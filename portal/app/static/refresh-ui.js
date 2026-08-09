// 更新まわりの共通部分。センサー一覧とグラフの両方で使う。
//
// - 自動更新（一定間隔）
// - 更新ボタン
// - 引っ張って更新（スマホ）
//
// 3 つとも「同じ更新処理を呼ぶ」だけなので、ここでまとめて面倒を見る。
// 二重に走らないことと、更新した直後に自動更新が続けて走らないことを
// ここで保証する。

(function () {
  const PULL_THRESHOLD = 70;   // これ以上引っ張ったら更新する
  const PULL_MAX = 110;        // 指標がこれ以上は下がらない

  window.setupRefresh = function setupRefresh(options) {
    const onRefresh = options.onRefresh;
    const intervalMs = options.intervalMs || 60000;
    // 自動更新を見送りたいとき用（グラフを触っている間など）。
    // 手で押したときは見送らない。
    const shouldSkipAuto = options.shouldSkipAuto || (() => false);

    const button = document.querySelector("[data-refresh-button]");
    const indicator = createIndicator();
    let running = false;
    let timer = null;

    function scheduleNext() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(tick, intervalMs);
    }

    async function run(manual) {
      // 押し連打や、自動と手動が重なったときに二重で走らせない。
      if (running) return;
      running = true;
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }
      try {
        // 手で押したときだけ、元のデータ自体を取り直させる。
        // 自動更新でやると API の回数を使い切る。
        await onRefresh(manual);
      } finally {
        running = false;
        if (button) {
          button.disabled = false;
          button.removeAttribute("aria-busy");
        }
        // 手で更新した直後に自動更新が続けて走らないよう、間隔を測り直す。
        scheduleNext();
      }
    }

    function tick() {
      // 見ていないタブで問い合わせても意味がない。
      if (document.hidden || shouldSkipAuto()) {
        scheduleNext();
        return;
      }
      run(false);
    }

    if (button) {
      button.addEventListener("click", () => run(true));
    }

    document.addEventListener("visibilitychange", () => {
      // 戻ってきたら、止まっていたぶんをすぐ埋める。
      if (!document.hidden) run(false);
    });

    installPullToRefresh(indicator, () => run(true), () => running);

    run(false);

    return { refreshNow: () => run(true) };
  };

  function createIndicator() {
    const el = document.createElement("div");
    el.className = "pull-indicator";
    el.innerHTML = '<span class="pull-arrow">↓</span><span class="pull-text">引っ張って更新</span>';
    el.hidden = true;
    document.body.appendChild(el);
    return el;
  }

  function installPullToRefresh(indicator, trigger, isRunning) {
    let startY = null;
    let startX = 0;
    let distance = 0;

    function atTop() {
      return (window.scrollY || document.documentElement.scrollTop || 0) <= 0;
    }

    document.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 1 || !atTop() || isRunning()) {
        startY = null;
        return;
      }
      // グラフは横スクロールできるので、その上から始まった指の動きは
      // 引っ張りとして扱わない。横に送りたいだけのことが多い。
      if (event.target.closest && event.target.closest(".chart-scroll")) {
        startY = null;
        return;
      }
      startY = event.touches[0].clientY;
      startX = event.touches[0].clientX;
      distance = 0;
    }, { passive: true });

    document.addEventListener("touchmove", (event) => {
      if (startY === null) return;

      distance = event.touches[0].clientY - startY;
      const sideways = Math.abs(event.touches[0].clientX - startX);

      // 上に動かしているだけなら普通のスクロール。手を出さない。
      if (distance <= 0) {
        hide();
        startY = null;
        return;
      }

      // 横方向のほうが大きい動きは、引っ張りではなく横スクロール。
      if (sideways > distance) {
        hide();
        startY = null;
        return;
      }

      // 引っ張っている間だけブラウザ既定の動きを止める。
      // 常に止めると普通のスクロールができなくなる。
      if (event.cancelable) event.preventDefault();

      // 指の動きより小さく動かす。引っ張っている手応えを出すため。
      const shown = Math.min(PULL_MAX, distance * 0.5);
      indicator.hidden = false;
      indicator.style.transform = `translate(-50%, ${shown}px)`;
      indicator.classList.toggle("ready", distance >= PULL_THRESHOLD);
      indicator.querySelector(".pull-text").textContent =
        distance >= PULL_THRESHOLD ? "離すと更新" : "引っ張って更新";
      indicator.querySelector(".pull-arrow").textContent =
        distance >= PULL_THRESHOLD ? "↻" : "↓";
    }, { passive: false });

    function finish() {
      if (startY === null) return;
      const pulled = distance;
      startY = null;
      hide();
      if (pulled >= PULL_THRESHOLD) trigger();
    }

    function hide() {
      indicator.hidden = true;
      indicator.style.transform = "translate(-50%, 0)";
      indicator.classList.remove("ready");
    }

    document.addEventListener("touchend", finish, { passive: true });
    document.addEventListener("touchcancel", () => {
      startY = null;
      hide();
    }, { passive: true });
  }
})();
