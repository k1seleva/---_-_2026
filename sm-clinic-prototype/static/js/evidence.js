// Подсветка доказательств: режимы показа, подсказка (тип, уверенность, правило), связь с таблицей триггеров.
(function () {
  "use strict";
  var wrap = document.querySelector(".ev-wrap");
  var dataNode = document.getElementById("ev-data");
  if (!wrap || !dataNode) return;
  var data = JSON.parse(dataNode.textContent);
  var markers = {}, triggers = {};
  data.markers.forEach(function (m) { markers[m.id] = m; });
  data.triggers.forEach(function (t) { triggers[t.id] = t; });
  var typeSelect = wrap.querySelector("#ev-type");

  // ---- режимы: «все маркеры», «только триггеры», «только найденное ИИ», «только выбранный тип» (стили — в CSS)
  function applyMode() {
    var checked = wrap.querySelector("input[name=ev-mode]:checked");
    wrap.dataset.mode = checked ? checked.value : "all";
    wrap.dataset.type = typeSelect ? typeSelect.value : "";
  }
  wrap.querySelectorAll("input[name=ev-mode]").forEach(function (r) { r.addEventListener("change", applyMode); });
  if (typeSelect) typeSelect.addEventListener("change", function () {
    wrap.querySelector("input[name=ev-mode][value=type]").checked = true;
    applyMode();
  });

  // ---- подсказка
  var tip = document.createElement("div");
  tip.className = "ev-tip";
  tip.hidden = true;
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);

  function esc(value) {
    var d = document.createElement("div");
    d.textContent = value == null ? "" : String(value);
    return d.innerHTML;
  }
  function pct(v) { return Math.round(v * 100) + "%"; }
  function level(v) { return data.levels[v] || v; }
  function factors(list) {
    return "<ul>" + (list || []).map(function (f) {
      var delta = f.code === "base" ? pct(f.delta) : (f.delta > 0 ? "+" : "−") + Math.abs(Math.round(f.delta * 100)) + "\u00a0п.\u00a0п.";  // неразрывные пробелы: «−10 п. п.» не рвётся на строки
      return "<li>" + esc(f.reason) + ": " + esc(delta) + "</li>";
    }).join("") + "</ul>";
  }
  function visible(ids) {
    var mode = wrap.dataset.mode;
    if (mode === "triggers") return [];
    return ids.filter(function (id) {
      var m = markers[id];
      return m && (mode !== "type" || m.type === wrap.dataset.type) && (mode !== "ai" || m.source === "llm");
    });
  }
  function source(item) {
    var cls = item.source === "llm" ? "tip-ai" : "";
    var note = item.source === "llm" ? ". Проверьте: словарь этого не нашёл" : "";
    return '<br><span class="tip-k">Нашёл:</span> <span class="' + cls + '">' + esc(item.source_title || "Словарь") + esc(note) + "</span>";
  }
  function tipHtml(el) {
    var parts = [];
    (el.dataset.t ? el.dataset.t.split(" ") : []).forEach(function (id) {
      var t = triggers[id];
      if (!t || wrap.dataset.mode === "type" || (wrap.dataset.mode === "ai" && t.source !== "llm")) return;
      parts.push('<div class="tip-item"><b>Триггер ' + esc(t.number) + ": " + esc(t.type_title) + "</b><br>" + esc(t.title) +
        '<br><span class="tip-k">Уверенность:</span> ' + pct(t.confidence) + " (" + esc(level(t.level)) + ")" + source(t) +
        '<br><span class="tip-k">Правило:</span> ' + esc(t.rule_title) + " <code>" + esc(t.rule_id) + "</code>" + factors(t.factors) + "</div>");
    });
    visible(el.dataset.m ? el.dataset.m.split(" ") : []).forEach(function (id) {
      var m = markers[id];
      var note = m.subsumed_by ? '<br><span class="tip-k">Внутри находки словаря, в итогах не считается второй раз</span>' : "";
      if (m.corrected) note += '<br><span class="tip-k">Найдено после исправления опечатки или раскладки</span>';
      parts.push('<div class="tip-item"><b>' + esc(m.type_title) + "</b>: " + esc(m.title) + (m.value ? " (" + esc(m.value) + ")" : "") +
        '<br><span class="tip-k">Уверенность:</span> ' + pct(m.confidence) + " (" + esc(level(m.level)) + ")" + source(m) +
        '<br><span class="tip-k">Правило:</span> ' + esc(m.rule_title) + " <code>" + esc((m.rule_ids || [m.rule_id]).join(", ")) + "</code>" +
        note + factors(m.factors) + "</div>");
    });
    return parts.join("");
  }
  function showTip(el) {
    var html = tipHtml(el);
    if (!html) { tip.hidden = true; return; }
    tip.innerHTML = html;
    tip.hidden = false;
    var r = el.getBoundingClientRect();
    var top = r.bottom + 8, left = Math.min(r.left, window.innerWidth - tip.offsetWidth - 12);
    if (top + tip.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - tip.offsetHeight - 8);
    tip.style.top = top + "px";
    tip.style.left = Math.max(8, left) + "px";
  }
  wrap.addEventListener("mouseover", function (e) {
    var el = e.target.closest(".ev");
    if (el) showTip(el); else tip.hidden = true;
  });
  wrap.addEventListener("mouseleave", function () { tip.hidden = true; });
  wrap.addEventListener("focusin", function (e) { var el = e.target.closest(".ev"); if (el) showTip(el); });
  wrap.addEventListener("focusout", function () { tip.hidden = true; });
  document.addEventListener("scroll", function () { tip.hidden = true; }, true);
  wrap.querySelectorAll(".ev-trig").forEach(function (el) { el.tabIndex = 0; });

  // ---- связь текста и таблицы триггеров
  function select(id) {
    wrap.querySelectorAll(".ev.flash").forEach(function (x) { x.classList.remove("flash"); });
    wrap.querySelectorAll('.ev[data-t~="' + id + '"]').forEach(function (x) { x.classList.add("flash"); });
    document.querySelectorAll(".trig-row.sel").forEach(function (r) { r.classList.remove("sel"); });
    var row = document.getElementById("trig-" + id);
    if (row) row.classList.add("sel");
    return row;
  }
  function openTrigger(el) {
    var id = el.dataset.t.split(" ")[0];
    var row = select(id);
    if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  wrap.addEventListener("click", function (e) {
    var el = e.target.closest(".ev[data-t]");
    if (el && wrap.dataset.mode !== "type") openTrigger(el);
  });
  wrap.addEventListener("keydown", function (e) {
    var el = e.target.closest(".ev[data-t]");
    if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openTrigger(el); }
  });
  // Найденное только ИИ: переход к маркеру в тексте (у маркера нет строки в таблице триггеров).
  document.querySelectorAll("[data-goto-m]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var id = a.dataset.gotoM;
      var details = document.getElementById("evidence");
      if (details) details.open = true;
      wrap.querySelectorAll(".ev.flash").forEach(function (x) { x.classList.remove("flash"); });
      var pieces = wrap.querySelectorAll('.ev[data-m~="' + id + '"]');
      pieces.forEach(function (x) { x.classList.add("flash"); });
      if (pieces.length) { pieces[0].scrollIntoView({ behavior: "smooth", block: "center" }); }
    });
  });
  document.querySelectorAll("[data-goto]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var id = a.dataset.goto;
      var details = document.getElementById("evidence");
      if (details) details.open = true;
      if (wrap.dataset.mode === "type" || wrap.dataset.mode === "ai") { wrap.querySelector("input[name=ev-mode][value=all]").checked = true; applyMode(); }
      select(id);
      var first = wrap.querySelector('.ev[data-tstart="' + id + '"]') || wrap.querySelector('.ev[data-t~="' + id + '"]');
      if (first) { first.scrollIntoView({ behavior: "smooth", block: "center" }); first.focus({ preventScroll: true }); }
    });
  });
})();
