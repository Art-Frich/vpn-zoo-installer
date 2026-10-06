"""Стили и скрипт админки. Отдаются как /static/app.css и /static/app.js (CSP: только 'self').

Цвета рядов графиков — проверенная категориальная палитра (порядок фиксирован, для тёмной
темы — свои ступени тех же оттенков).
"""

from __future__ import annotations

import hashlib

CSS = r"""
:root {
  color-scheme: light;
  --bg: #f5f5f3; --surface: #ffffff; --surface-2: #f0efec; --border: #e2e1dc;
  --text: #1a1a18; --text-2: #52514e; --muted: #7a7974;
  --accent: #2a78d6; --accent-ink: #ffffff; --accent-soft: #e3eefb;
  --ok: #1d7f45; --ok-soft: #e2f3e8; --warn: #9a6400; --warn-soft: #fbf0d9;
  --bad: #c0362f; --bad-soft: #fbe5e3; --info-soft: #e3eefb;
  --grid: #e9e8e4; --track: #ecebe7;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --series-5: #e87ba4; --series-6: #008300; --series-7: #4a3aa7; --series-8: #e34948;
  --radius: 10px; --shadow: 0 1px 2px rgba(0,0,0,.05);
  --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #121211; --surface: #1b1b1a; --surface-2: #232321; --border: #2f2f2c;
    --text: #f2f2ef; --text-2: #c3c2b7; --muted: #8f8e86;
    --accent: #3987e5; --accent-ink: #ffffff; --accent-soft: #18304d;
    --ok: #4cc27a; --ok-soft: #173322; --warn: #e2a93b; --warn-soft: #3a2c10;
    --bad: #f07a72; --bad-soft: #401c1a; --info-soft: #18304d;
    --grid: #2a2a28; --track: #2c2c29;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --series-5: #d55181; --series-6: #008300; --series-7: #9085e9; --series-8: #e66767;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { font-size: 1.45rem; margin: 0 0 4px; font-weight: 650; letter-spacing: -.01em; }
h2 { font-size: 1.1rem; margin: 28px 0 12px; font-weight: 600; }
h3 { font-size: .95rem; margin: 0; font-weight: 600; }
p { margin: 0 0 10px; }
code, pre, .mono { font-family: var(--mono); font-size: .86em; }
.muted { color: var(--muted); }
.small { font-size: .85rem; }
.nowrap { white-space: nowrap; }

/* шапка */
.top {
  position: sticky; top: 0; z-index: 10; background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.top-inner {
  max-width: 1200px; margin: 0 auto; padding: 0 16px;
  display: flex; align-items: center; gap: 16px; min-height: 54px;
}
.brand { font-weight: 700; color: var(--text); white-space: nowrap; }
.brand span { color: var(--muted); font-weight: 500; margin-left: 6px; }
.nav { display: flex; gap: 2px; overflow-x: auto; flex: 1; scrollbar-width: none; }
.nav::-webkit-scrollbar { display: none; }
.nav a {
  padding: 7px 11px; border-radius: 8px; color: var(--text-2); white-space: nowrap; font-size: .93rem;
}
.nav a:hover { background: var(--surface-2); text-decoration: none; }
.nav a.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.top form { margin: 0; }

main { max-width: 1200px; margin: 0 auto; padding: 20px 16px 48px; min-width: 0; }
body { overflow-x: hidden; }
.page-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 12px; margin-bottom: 4px; }
.page-head .sub { color: var(--muted); font-size: .9rem; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

/* карточки и сетки */
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px; box-shadow: var(--shadow); min-width: 0;
}
main > * + * { margin-top: 16px; }
main > h2 { margin-top: 28px; }
.stack > .card + .card { margin-top: 16px; }
.card-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(min(270px, 100%), 1fr)); }
.cols { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); align-items: start; }
.card h3.sub-h { margin: 16px 0 6px; }
@media (min-width: 960px) { .cols { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.stack > * + * { margin-top: 16px; }

/* показатели */
.tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(150px, 100%), 1fr)); }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; }
.tile .label { color: var(--muted); font-size: .82rem; }
.tile .value { font-size: 1.35rem; font-weight: 650; margin: 2px 0 4px; font-variant-numeric: tabular-nums; }
.tile .hint { color: var(--muted); font-size: .8rem; }

/* протокол */
/* карточка протокола: шапка — сетка «название | статус» (статус не переносится вниз),
   низ (сегодня, пользователи, график) прижат ко дну — в ряду всё на одной линии */
.card.proto { display: flex; flex-direction: column; }
.proto .card-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 8px; }
.proto .card-head h3 { margin: 0; overflow-wrap: anywhere; line-height: 1.3; }
.proto .meta { color: var(--text-2); font-size: .88rem; margin-bottom: 8px; }
.proto .chips { margin-bottom: 12px; }
.proto .row { display: flex; justify-content: space-between; align-items: flex-end; gap: 8px; margin-top: auto; }
/* «Подключение»: плитки протоколов и окно протокола */
.ptiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(220px, 100%), 1fr)); }
.ptile { display: grid; gap: 4px; align-content: start; text-align: left; cursor: pointer; font: inherit; color: inherit;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px;
  border-left: 4px solid var(--accent); transition: transform .08s, border-color .08s; }
.ptile:hover { border-color: var(--accent); transform: translateY(-1px); }
.ptile-name { font-weight: 600; font-size: 1rem; }
.ptile-sub { color: var(--muted); font-size: .85rem; }
.ptile .warnchip, .warnchip { justify-self: start; color: var(--warn); background: var(--warn-soft); }
.ptile.s1 { border-left-color: #2f8f5b; } .ptile.s2 { border-left-color: #b8860b; } .ptile.s3 { border-left-color: #8a63d2; }
.ptile.s4 { border-left-color: #c2410c; } .ptile.s5 { border-left-color: #0e7490; } .ptile.s6 { border-left-color: #be185d; }
.ptile.s7 { border-left-color: #4d7c0f; }
dialog.pdlg { width: min(460px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; box-shadow: 0 20px 60px rgba(0,0,0,.45); }
dialog.pdlg::backdrop { background: rgba(0,0,0,.55); backdrop-filter: blur(2px); }
.dlg-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; }
.dlg-head h3 { margin: 0; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.tab { font: inherit; font-size: .85rem; padding: 5px 12px; border-radius: 999px; cursor: pointer;
  background: var(--surface-2); color: var(--text-2); border: 1px solid var(--border); }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.variant { display: grid; gap: 12px; justify-items: center; margin-top: 8px; }
.variant .qr { background: #fff; padding: 10px; border-radius: 12px; }
.variant .qr svg { width: 260px; height: 260px; display: block; }
.variant .link-uri { width: 100%; }
.pdlg details { margin-top: 14px; color: var(--muted); font-size: .85rem; }
main.loading { opacity: .55; transition: opacity .15s; }
footer .live { display: inline-flex; align-items: center; gap: 6px; }
footer .live.on::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ok); }
footer .live-btn { margin-left: 10px; }
.proto .num-big { font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { font-size: .78rem; padding: 1px 7px; border-radius: 999px; background: var(--surface-2);
  color: var(--text-2); border: 1px solid var(--border); white-space: nowrap; }

/* значки */
.badge { display: inline-block; font-size: .78rem; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  white-space: nowrap; line-height: 1.5; }
.badge.ok { background: var(--ok-soft); color: var(--ok); }
.badge.warn { background: var(--warn-soft); color: var(--warn); }
.badge.bad { background: var(--bad-soft); color: var(--bad); }
.badge.info { background: var(--info-soft); color: var(--accent); }
.badge.muted { background: var(--surface-2); color: var(--muted); }
.badge::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; margin-right: 6px; vertical-align: 1px; }
.badge.muted::before { display: none; }

/* оповещения */
.alerts { list-style: none; margin: 0; padding: 0; }
.alerts li { display: flex; gap: 10px; align-items: flex-start; padding: 9px 12px; border-radius: 8px;
  margin-bottom: 6px; font-size: .92rem; }
.alerts li.bad { background: var(--bad-soft); }
.alerts li.warn { background: var(--warn-soft); }
.alerts li.ok { background: var(--ok-soft); }
.alerts li .ico { font-weight: 700; flex: none; width: 1.1em; text-align: center; }
.alerts li.bad .ico { color: var(--bad); }
.alerts li.warn .ico { color: var(--warn); }
.alerts li.ok .ico { color: var(--ok); }
.flash { margin-bottom: 16px; }

/* таблицы */
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 -4px; padding: 0 4px; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: .8rem; text-transform: uppercase;
  letter-spacing: .03em; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 9px 10px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--surface-2); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.empty { text-align: center; color: var(--muted); padding: 18px; }
td .sub { display: block; color: var(--muted); font-size: .82rem; }
td.bar-cell { width: 28%; min-width: 90px; }

/* кнопки и формы */
.btn, button.btn {
  display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); padding: 7px 13px; border-radius: 8px;
  font: inherit; font-size: .9rem; font-weight: 550; cursor: pointer; white-space: nowrap; text-decoration: none;
}
.btn:hover { background: var(--surface-2); text-decoration: none; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.primary:hover { filter: brightness(1.07); }
.btn.danger { color: var(--bad); border-color: var(--bad); }
.btn.danger:hover { background: var(--bad-soft); }
.btn.danger-solid { background: var(--bad); border-color: var(--bad); color: #fff; }
.btn.small { padding: 4px 9px; font-size: .82rem; }
.btn:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, a:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
form.inline { display: inline; margin: 0; }
.form-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.field label, .field .label { font-size: .82rem; color: var(--text-2); font-weight: 550; }
.field.grow { flex: 1 1 220px; }
input[type=text], input[type=password], textarea, select {
  font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px; width: 100%; min-width: 0;
}
textarea { font-family: var(--mono); font-size: .85rem; min-height: 160px; resize: vertical; }
.checks { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: .9rem; }
.checks label { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
.hint { color: var(--muted); font-size: .82rem; }

/* сегменты (период) */
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg a { padding: 6px 12px; color: var(--text-2); font-size: .88rem; border-left: 1px solid var(--border); }
.seg a:first-child { border-left: 0; }
.seg a:hover { background: var(--surface-2); text-decoration: none; }
.seg a.active { background: var(--accent); color: var(--accent-ink); font-weight: 600; }

/* графики */
.chart { width: 100%; height: auto; display: block; }
.chart .gridline { stroke: var(--grid); stroke-width: 1; }
.chart .baseline, .spark .baseline { stroke: var(--border); stroke-width: 1; }
.chart .axis { fill: var(--muted); font-size: 12px; font-family: inherit; font-variant-numeric: tabular-nums; }
.chart .hit { fill: transparent; }
.chart .hit:hover { fill: var(--text); fill-opacity: .05; }
.s1 { fill: var(--series-1); background: var(--series-1); } .s2 { fill: var(--series-2); background: var(--series-2); }
.s3 { fill: var(--series-3); background: var(--series-3); } .s4 { fill: var(--series-4); background: var(--series-4); }
.s5 { fill: var(--series-5); background: var(--series-5); } .s6 { fill: var(--series-6); background: var(--series-6); }
.s7 { fill: var(--series-7); background: var(--series-7); } .s8 { fill: var(--series-8); background: var(--series-8); }
.track { fill: var(--track); }
.fill-ok { fill: var(--ok); } .fill-warn { fill: var(--warn); } .fill-bad { fill: var(--bad); }
.legend { list-style: none; padding: 0; margin: 10px 0 0; display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: .86rem; }
.legend li { display: inline-flex; align-items: center; gap: 6px; }
.legend .muted { font-variant-numeric: tabular-nums; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; flex: none; }
.hbar { width: 100%; height: 8px; display: block; }
.meter { width: 100%; height: 6px; display: block; margin-top: 6px; }
.spark { width: 120px; height: 28px; display: block; }

/* ссылки и QR */
.link { border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; background: var(--surface); }
.link + .link { margin-top: 10px; }
.link-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.link-uri { display: flex; gap: 8px; align-items: stretch; margin-top: 8px; }
.link-uri input { font-family: var(--mono); font-size: .82rem; }
.link details { margin-top: 8px; }
summary { cursor: pointer; color: var(--accent); font-size: .9rem; }
.qr { background: #fff; padding: 10px; border-radius: 8px; width: min(320px, 100%); margin-top: 8px; }
.qr svg { display: block; width: 100%; height: auto; }
.notes { color: var(--text-2); font-size: .86rem; margin-top: 4px; }

/* журнал и вывод */
pre.log {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 12px;
  overflow: auto; max-height: 70vh; white-space: pre-wrap; word-break: break-word; font-size: .8rem; line-height: 1.45; margin: 0;
}
.side { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); align-items: start; }
@media (min-width: 900px) { .side { grid-template-columns: 260px minmax(0, 1fr); } }
.list { list-style: none; margin: 0; padding: 0; }
.list li a { display: block; padding: 6px 10px; border-radius: 6px; color: var(--text-2); font-size: .88rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.list li a:hover { background: var(--surface-2); text-decoration: none; }
.list li a.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.list .group { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
  padding: 10px 10px 4px; }

dl.kv { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 0; font-size: .92rem; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }

/* вход */
.login { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 16px; }
.login .card { width: 100%; max-width: 380px; }
.login h1 { margin-bottom: 6px; }
.login form { display: grid; gap: 12px; margin-top: 14px; }

.cmd { display: block; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; overflow-x: auto; white-space: pre; }
.danger-zone { border-color: var(--bad); }
footer { max-width: 1200px; margin: 0 auto; padding: 0 16px 24px; color: var(--muted); font-size: .8rem; }
@media (max-width: 600px) {
  body { font-size: 14px; }
  .top-inner { gap: 10px; }
  .brand span { display: none; }
  main { padding-top: 14px; }
  .card { padding: 13px; }
  th, td { padding: 7px 8px; }
}
"""

JS = r"""
(function () {
  'use strict';
  // копирование ссылки в буфер
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-copy]');
    if (!btn) return;
    var src = document.getElementById(btn.getAttribute('data-copy'));
    if (!src) return;
    var text = src.value !== undefined ? src.value : src.textContent;
    var done = function () {
      var old = btn.textContent;
      btn.textContent = 'Скопировано';
      setTimeout(function () { btn.textContent = old; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { src.select && src.select(); });
    } else if (src.select) {
      src.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* выделено — копируйте вручную */ }
    }
  });
  // подтверждение опасных действий
  document.addEventListener('submit', function (ev) {
    var msg = ev.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });
  // файл отчёта пробника → в поле формы
  document.querySelectorAll('input[type=file][data-fill]').forEach(function (inp) {
    inp.addEventListener('change', function () {
      var target = document.getElementById(inp.getAttribute('data-fill'));
      var f = inp.files && inp.files[0];
      if (!target || !f) return;
      if (f.size > 4 * 1024 * 1024) { alert('Файл больше 4 МБ'); return; }
      var r = new FileReader();
      r.onload = function () { target.value = r.result; };
      r.readAsText(f);
    });
  });
  // выделить содержимое поля ссылки по клику
  document.querySelectorAll('input[data-select]').forEach(function (inp) {
    inp.addEventListener('focus', function () { inp.select(); });
  });
  // окна протоколов: плитка [data-dialog] открывает <dialog>, ✕ или клик по фону — закрыть,
  // вкладки вариантов [data-tab] — показать один вариант
  document.addEventListener('click', function (ev) {
    var open = ev.target.closest('[data-dialog]');
    if (open) { var d = document.getElementById(open.getAttribute('data-dialog')); if (d && d.showModal) d.showModal(); return; }
    var close = ev.target.closest('[data-close]');
    if (close) { close.closest('dialog').close(); return; }
    if (ev.target.tagName === 'DIALOG') { ev.target.close(); return; }
    var tab = ev.target.closest('[data-tab]');
    if (tab) {
      var box = tab.closest('dialog') || document;
      box.querySelectorAll('[data-tab]').forEach(function (b) { b.classList.toggle('active', b === tab); });
      box.querySelectorAll('.variant').forEach(function (v) { v.hidden = v.id !== tab.getAttribute('data-tab'); });
    }
  });
  // переключатель периода — без перезагрузки: забрать страницу, подменить <main>, обновить адрес
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest('nav.seg a');
    if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    ev.preventDefault();
    var main = document.querySelector('main');
    if (main) main.classList.add('loading');
    fetch(a.href, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (r.redirected || !r.ok) throw new Error('nav'); return r.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('main');
        if (!fresh || !main) throw new Error('nav');
        main.innerHTML = fresh.innerHTML;
        history.replaceState(null, '', a.href);
      })
      .catch(function () { location.href = a.href; })
      .then(function () { if (main) main.classList.remove('loading'); });
  });
  // live: раз в N секунд забрать ту же страницу и подменить <main>; пауза — вкладка
  // скрыта, фокус в поле ввода, открыт <details> или выключено кнопкой (запоминается)
  var live = document.getElementById('live');
  if (live) {
    var every = (parseInt(live.getAttribute('data-live'), 10) || 10) * 1000;
    var paused = false;
    try { paused = localStorage.getItem('zoo-live') === 'off'; } catch (e) { /* без хранилища */ }
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'btn small live-btn';
    var label = function (extra) {
      live.textContent = paused ? 'live на паузе' : 'live · обновлено ' + (extra || new Date().toLocaleTimeString('ru-RU'));
      btn.textContent = paused ? 'Включить' : 'Пауза';
      live.className = paused ? 'live off' : 'live on';
    };
    btn.addEventListener('click', function () {
      paused = !paused;
      try { localStorage.setItem('zoo-live', paused ? 'off' : 'on'); } catch (e) { /* без хранилища */ }
      label();
    });
    live.after(btn);
    label();
    var busy = false;
    setInterval(function () {
      if (paused || busy || document.hidden) return;
      var a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
      var main = document.querySelector('main');
      if (!main || main.querySelector('details[open]')) return;
      busy = true;
      fetch(location.href, { credentials: 'same-origin', cache: 'no-store', headers: { 'X-Zoo-Live': '1' } })
        .then(function (r) {
          if (r.redirected || !r.ok) { paused = true; label(); throw new Error('сессия или сервер'); }
          return r.text();
        })
        .then(function (html) {
          var fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('main');
          if (fresh) { fresh.querySelectorAll('.alerts.flash').forEach(function (el) { el.remove(); }); main.innerHTML = fresh.innerHTML; }
          label();
        })
        .catch(function () { /* следующая попытка через интервал */ })
        .then(function () { busy = false; });
    }, every);
  }
})();
"""


def _ver(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:10]


CSS_VERSION = _ver(CSS)
JS_VERSION = _ver(JS)
