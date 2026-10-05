/* Общие элементы интерфейса без библиотек.
   Выпадающий список (popover): клик по кнопке открывает, повторный клик, клик вне области или Esc — закрывает. */
(function () {
  function closeAll(except) {
    document.querySelectorAll('[data-popover-toggle][aria-expanded="true"]').forEach(function (btn) {
      if (btn === except) return;
      btn.setAttribute('aria-expanded', 'false');
      var pop = document.getElementById(btn.getAttribute('aria-controls'));
      if (pop) pop.hidden = true;
    });
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-popover-toggle]');
    if (btn) {
      e.preventDefault();
      var pop = document.getElementById(btn.getAttribute('aria-controls'));
      var open = btn.getAttribute('aria-expanded') !== 'true';
      closeAll(btn);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (pop) {
        pop.hidden = !open;
        if (open) { var first = pop.querySelector('a, button'); if (first) first.focus({preventScroll: true}); }
      }
      return;
    }
    if (!e.target.closest('.popover')) closeAll(null);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var opened = document.querySelector('[data-popover-toggle][aria-expanded="true"]');
    closeAll(null);
    if (opened) opened.focus();
  });

  /* Формы с data-autosubmit отправляются при изменении (переключатель клиники, фильтры). */
  document.addEventListener('change', function (e) {
    var form = e.target.closest('form[data-autosubmit]');
    if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
  });

  /* Строка таблицы целиком — ссылка. */
  document.addEventListener('click', function (e) {
    var row = e.target.closest('tr[data-href]');
    if (row && !e.target.closest('a, button, input, label, select')) window.location = row.dataset.href;
  });

  /* Отметить все уведомления прочитанными без перезагрузки страницы. */
  document.addEventListener('submit', function (e) {
    var form = e.target.closest('form[data-ajax]');
    if (!form) return;
    e.preventDefault();
    fetch(form.action, {method: 'POST', body: new FormData(form), headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(function () {
        var scope = document.getElementById(form.dataset.ajax);
        if (!scope) return;
        scope.querySelectorAll('.unread').forEach(function (el) { el.classList.remove('unread'); });
        scope.querySelectorAll('[data-unread-count]').forEach(function (el) { el.remove(); });
        document.querySelectorAll('[data-bell-count]').forEach(function (el) { el.remove(); });
        form.hidden = true;
      })
      .catch(function () { form.submit(); });
  });
})();
