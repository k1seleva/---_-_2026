// Ход разбора пачки во «Входящих»: опрос раз в 5 секунд, пока очередь не опустеет, затем перезагрузка
// страницы (появятся готовые протоколы и кнопки повтора). Сбой сети не останавливает опрос.
(function () {
  const box = document.getElementById("batch-progress");
  if (!box || box.dataset.finished === "1") return;
  const part = (k) => box.querySelector(`[data-k="${k}"]`);
  const show = (k, on) => { const el = part(k); if (el) el.hidden = !on; };
  const set = (k, v) => { const el = part(k); if (el) el.textContent = v; };
  const minutes = (sec) => (sec < 60 ? `${sec} с` : `${Math.round(sec / 60)} мин`);
  let lastDone = null;

  function render(p) {
    const pct = (n) => `${p.total ? (100 * n) / p.total : 0}%`;
    box.querySelector(".bp-done").style.width = pct(p.done);
    box.querySelector(".bp-failed").style.width = pct(p.failed);
    set("done", p.done);
    set("queued", p.queued); show("queued-tag", p.queued > 0);
    set("failed", p.failed); show("failed-tag", p.failed > 0);
    set("llm_errors", p.llm_errors); show("llm-tag", p.llm_errors > 0);
    set("avg", p.avg_ms ? `в среднем ${Math.round(p.avg_ms / 1000)} с на протокол` : "");
    set("eta", p.eta_sec ? `осталось около ${minutes(p.eta_sec)}` : "");
    set("current", p.current.map((c) => `Сейчас: ${c.filename} (${minutes(c.seconds)})`).join(" · "));
    show("paused", p.model_paused); set("reason", (p.model_reason || "").slice(0, 200));
    if (p.advice_pending && p.queued + p.running === 0) set("current", `Протоколы разобраны, ИИ готовит советы: ${p.advice_pending}`);
  }

  async function tick() {
    try {
      const res = await fetch(box.dataset.url, { headers: { "X-Requested-With": "fetch" } });
      if (res.ok) {
        const p = await res.json();
        render(p);
        // Готовые протоколы появляются в списке: обновляем страницу, когда очередь пуста или раз в 5 готовых.
        if (p.finished) return window.location.reload();
        if (lastDone !== null && p.done + p.failed - lastDone >= 5) return window.location.reload();
        if (lastDone === null) lastDone = p.done + p.failed;
      }
    } catch (e) { /* сеть моргнула: повторим на следующем шаге */ }
    setTimeout(tick, 5000);
  }
  setTimeout(tick, 3000);
})();
