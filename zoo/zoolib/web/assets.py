"""Стили и скрипт админки. Отдаются как /static/app.css и /static/app.js (CSP: только 'self').

Цвета рядов графиков — проверенная категориальная палитра (порядок фиксирован, для тёмной
темы — свои ступени тех же оттенков).
"""

from __future__ import annotations

import gzip
import hashlib

CSS = r"""
[hidden] { display: none !important; }
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
html { -webkit-text-size-adjust: 100%; scrollbar-gutter: stable; }
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
.nav a { text-align: center; }
.top form { margin: 0; }

main { max-width: 1200px; margin: 0 auto; padding: 20px 16px 48px; min-width: 0; min-height: 70vh; }
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
.cols { display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); align-items: stretch; }
.cols > .card { display: flex; flex-direction: column; }
.card h3.sub-h { margin: 16px 0 6px; }
.head-end { display: flex; align-items: center; gap: 8px; }
details.help { position: relative; }
details.help > summary { list-style: none; width: 22px; height: 22px; border-radius: 50%; display: grid;
  place-content: center; font-size: .8rem; font-weight: 700; color: var(--muted); border: 1px solid var(--border);
  cursor: pointer; user-select: none; }
details.help > summary::-webkit-details-marker { display: none; }
details.help[open] > summary { color: var(--accent); border-color: var(--accent); }
details.help > .hint { position: absolute; right: 0; top: 28px; z-index: 5; width: min(360px, 80vw);
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  box-shadow: 0 8px 24px rgba(0,0,0,.18); color: var(--text-2); font-size: .85rem; }
details.help > .hint p { margin: 0 0 6px; } details.help > .hint ul { margin: 0 0 6px; padding-left: 18px; }
.empty-state { min-height: 96px; display: grid; place-content: center; justify-items: center; gap: 6px;
  text-align: center; color: var(--muted); }
.empty-state p { margin: 0; }
.btn-grid { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); }
.btn-grid form.inline { display: block; }
.btn-grid .btn { width: 100%; justify-content: center; }
@media (min-width: 960px) { .cols { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.stack > * + * { margin-top: 16px; }

/* показатели: плитка — сетка из строк, место под полоску занято всегда, подписи в одну строку */
.tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); }
.tile { display: grid; grid-template-rows: auto auto 14px auto; align-content: start; min-width: 0;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; }
.tile .label, .tile .hint { color: var(--muted); font-size: .82rem; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.tile .value { font-size: 1.35rem; font-weight: 650; margin: 2px 0 0; font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tile .meter-slot { align-self: center; }
.tile .hint { font-size: .8rem; }

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
.ptile { --acc: var(--accent); display: grid; gap: 4px; align-content: start; text-align: left; cursor: pointer;
  font: inherit; color: inherit; background: var(--surface-2); border: 1px solid var(--border);
  border-left: 4px solid var(--acc); border-radius: var(--radius); padding: 14px;
  transition: transform .08s, border-color .08s; }
.ptile:hover { border-color: var(--acc); transform: translateY(-1px); }
.ptile-name { font-weight: 600; font-size: 1rem; }
.ptile-sub { color: var(--muted); font-size: .85rem; }
.acc1 { --acc: var(--series-1); } .acc2 { --acc: var(--series-2); } .acc3 { --acc: var(--series-3); }
.acc4 { --acc: var(--series-4); } .acc5 { --acc: var(--series-5); } .acc6 { --acc: var(--series-6); }
.acc7 { --acc: var(--series-7); } .acc8 { --acc: var(--series-8); }
dialog.pdlg { width: min(460px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; box-shadow: 0 20px 60px rgba(0,0,0,.45); }
dialog.pdlg::backdrop { background: rgba(0,0,0,.55); backdrop-filter: blur(2px); }
.dlg-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; }
.dlg-head h3 { margin: 0; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.tab { font: inherit; font-size: .85rem; padding: 5px 12px; border-radius: 999px; cursor: pointer;
  background: var(--surface-2); color: var(--text-2); border: 1px solid var(--border); }
.tab.active { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.variant { display: grid; gap: 12px; justify-items: center; margin-top: 8px; }
.variant img.qr { background: #fff; padding: 10px; border-radius: 12px; width: 240px; height: 240px;
  box-sizing: content-box; display: block; max-width: 100%; }
.variant .link-uri { width: 100%; }
.pdlg details { margin-top: 14px; color: var(--muted); font-size: .85rem; }
body.busy::before { content: ""; position: fixed; top: 0; left: 0; width: 100%; height: 2px; z-index: 30;
  background: var(--accent); animation: busy 1s ease-in-out infinite; }
@keyframes busy { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
footer .live { display: inline-flex; align-items: center; gap: 6px; margin-left: 8px; }
footer .live.on::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ok); }
footer .live-btn { margin-left: 10px; }
.proto .num-big { font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { font-size: .78rem; padding: 1px 7px; border-radius: 999px; background: var(--surface-2);
  color: var(--text-2); border: 1px solid var(--border); white-space: nowrap; }

.chip.bad { color: var(--bad); background: var(--bad-soft); border-color: transparent; }
.chip.warn { color: var(--warn); background: var(--warn-soft); border-color: transparent; }
.chip.info { color: var(--accent); background: var(--info-soft); border-color: transparent; }
.chip.ok { color: var(--ok); background: var(--ok-soft); border-color: transparent; }
.quiet { color: var(--muted); font-size: .85rem; margin: 10px 0 0; }
.pack ul.steps { margin: 6px 0 10px; padding-left: 18px; }
.pack ul.steps li { margin: 3px 0; }
.pack + .pack { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--border); }
.quick { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 16px; align-items: center; }
.quick img.qr { background: #fff; padding: 8px; border-radius: 10px; width: 160px; height: 160px;
  box-sizing: content-box; display: block; }
.quick .actions { margin-top: 10px; }
tr.off td { opacity: .55; }
tr.off td:last-child, tr.off td:first-child { opacity: 1; }
details.more > summary { margin-top: 12px; }
details.more[open] > summary { margin-bottom: 10px; }
.top-inner .brand { flex: none; }

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
.alerts li .msg { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.alerts li .acts { margin-left: auto; display: inline-flex; gap: 10px; align-items: center; flex: none; }
.alerts li .acts form { margin: 0; }
.alerts li .acts button { font: inherit; font-size: .85rem; padding: 0; border: 0; background: none;
  color: var(--accent); cursor: pointer; text-decoration: underline; }
.alerts li.info { background: var(--info-soft); } .alerts li.info .ico { color: var(--accent); }
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
.btn.danger-solid { background: var(--bad); border-color: var(--bad); color: var(--accent-ink); }
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

/* приложения через VPN: переключатели и панель сохранения */
table.apps td, table.apps th { padding-left: 6px; padding-right: 6px; }
table.apps td.num, table.apps th.num { width: 76px; text-align: center; }
table.apps td:first-child { overflow-wrap: anywhere; }
table.apps tr.chg td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
.tgl { position: relative; display: inline-block; width: 38px; height: 22px; vertical-align: middle; }
.tgl input { position: absolute; inset: -8px -6px; width: calc(100% + 12px); height: calc(100% + 16px); margin: 0;
  opacity: 0; cursor: pointer; z-index: 1; }
.tgl i { position: absolute; inset: 0; border-radius: 999px; background: var(--track); border: 1px solid var(--border);
  transition: background .12s; }
.tgl i::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%;
  background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.3); transition: transform .12s; }
.tgl input:checked + i { background: var(--accent); border-color: var(--accent); }
.tgl input:checked + i::after { transform: translateX(16px); }
.tgl input:focus-visible + i { outline: 2px solid var(--accent); outline-offset: 2px; }
details.custom { margin-top: 14px; }
details.custom > .stack { margin-top: 10px; }
.hint.err { color: var(--bad); }
.savebar { position: sticky; bottom: 8px; z-index: 6; display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: 0 4px 16px rgba(0,0,0,.14); }
.savebar .count { flex: 1; min-width: 0; font-size: .9rem; color: var(--text-2); }
.btn:disabled { opacity: .5; cursor: default; }

/* сегменты (период) */
.seg { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
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
.chart .s1, .spark .s1, .hbar .s1 { fill: var(--series-1); } .chart .s2, .spark .s2, .hbar .s2 { fill: var(--series-2); }
.chart .s3, .spark .s3, .hbar .s3 { fill: var(--series-3); } .chart .s4, .spark .s4, .hbar .s4 { fill: var(--series-4); }
.chart .s5, .spark .s5, .hbar .s5 { fill: var(--series-5); } .chart .s6, .spark .s6, .hbar .s6 { fill: var(--series-6); }
.chart .s7, .spark .s7, .hbar .s7 { fill: var(--series-7); } .chart .s8, .spark .s8, .hbar .s8 { fill: var(--series-8); }
.swatch.s1 { background: var(--series-1); } .swatch.s2 { background: var(--series-2); }
.swatch.s3 { background: var(--series-3); } .swatch.s4 { background: var(--series-4); }
.swatch.s5 { background: var(--series-5); } .swatch.s6 { background: var(--series-6); }
.swatch.s7 { background: var(--series-7); } .swatch.s8 { background: var(--series-8); }
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
  .top-inner { gap: 6px 10px; flex-wrap: wrap; padding-top: 6px; padding-bottom: 6px; }
  .brand { flex: 1; }
  .brand span { display: none; }
  .nav { order: 3; flex: 1 1 100%; display: grid; grid-template-columns: repeat(4, 1fr); gap: 2px; overflow: visible; }
  .nav a { padding: 6px 0; font-size: .8rem; overflow: hidden; text-overflow: ellipsis; }
  main { padding-top: 14px; }
  .card { padding: 13px; }
  th, td { padding: 7px 8px; }
  table.stack thead { display: none; }
  table.stack, table.stack tbody { display: block; }
  table.stack tr { display: block; padding: 8px 0; border-bottom: 1px solid var(--border); }
  table.stack tbody tr:last-child { border-bottom: 0; }
  table.stack td { display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 0;
    padding: 3px 0; text-align: right; }
  table.stack td::before { content: attr(data-label); color: var(--muted); font-size: .8rem; text-align: left;
    flex: none; }
  table.stack td[data-label=""]::before, table.stack td:first-child::before { display: none; }
  table.stack td:first-child { justify-content: flex-start; text-align: left; font-weight: 600; }
  table.stack tbody tr:hover td { background: none; }
  td.bar-cell, table.stack td .hbar { width: 40%; }
  .quick { grid-template-columns: 1fr; justify-items: center; }
}
"""

JS = r"""
(function () {
  'use strict';
  // копирование в буфер: data-copy="id" — поле, значение которого берём
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
  // подтверждение опасных действий; форма с data-swap уходит в фоне, <main> подменяется ответом
  document.addEventListener('submit', function (ev) {
    var f = ev.target, msg = f.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) { ev.preventDefault(); return; }
    if (f.hasAttribute('data-swap')) {
      ev.preventDefault();
      go(f.getAttribute('action') || location.pathname, { method: 'POST', body: new URLSearchParams(new FormData(f)) });
    }
  });
  // обработчики делегированы: страница подменяется целиком (live, переключатель периода)
  document.addEventListener('focusin', function (ev) {
    var inp = ev.target;
    if (inp.matches && inp.matches('input[data-select]')) inp.select();
  });
  document.addEventListener('change', function (ev) {
    var inp = ev.target;
    if (!inp.matches || !inp.matches('input[type=file][data-fill]')) return;
    var target = document.getElementById(inp.getAttribute('data-fill'));
    var f = inp.files && inp.files[0];
    if (!target || !f) return;
    if (f.size > 4 * 1024 * 1024) { alert('Файл больше 4 МБ'); return; }
    var r = new FileReader();
    r.onload = function () { target.value = r.result; };
    r.readAsText(f);
  });
  // QR берётся с сервера по требованию: только у варианта, который виден в окне
  function showQr(box) {
    var v = box.querySelector('.variant:not([hidden])');
    var img = v && v.querySelector('img[data-src]');
    if (img) { img.src = img.getAttribute('data-src'); img.removeAttribute('data-src'); }
  }
  document.addEventListener('error', function (ev) {
    var img = ev.target;
    if (img.tagName !== 'IMG' || !img.classList.contains('qr')) return;
    var p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'QR не построился';
    img.replaceWith(p);
  }, true);
  // окна протоколов: плитка [data-dialog] открывает <dialog>, ✕ или клик по фону — закрыть,
  // вкладки вариантов [data-tab] — показать один вариант
  document.addEventListener('click', function (ev) {
    var open = ev.target.closest('[data-dialog]');
    if (open) {
      var d = document.getElementById(open.getAttribute('data-dialog'));
      if (d && d.showModal) { d.showModal(); showQr(d); }
      return;
    }
    var close = ev.target.closest('[data-close]');
    if (close) { close.closest('dialog').close(); return; }
    if (ev.target.tagName === 'DIALOG') {
      // закрывает только клик по фону: по полям окна (padding) target тоже DIALOG
      var r = ev.target.getBoundingClientRect();
      if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) ev.target.close();
      return;
    }
    var tab = ev.target.closest('[data-tab]');
    if (tab) {
      var box = tab.closest('dialog') || document;
      box.querySelectorAll('[data-tab]').forEach(function (b) { b.classList.toggle('active', b === tab); });
      box.querySelectorAll('.variant').forEach(function (v) { v.hidden = v.id !== tab.getAttribute('data-tab'); });
      showQr(box);
    }
  });
  // форма входа по одноразовой ссылке отправляется сама
  document.querySelectorAll('form[data-autosubmit]').forEach(function (f) { f.submit(); });

  // подмена <main> новой страницей; при фоновом обновлении (keep) сообщение (flash) остаётся,
  // пока человек не уйдёт со страницы
  function swapMain(main, html, keep) {
    var fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('main');
    if (!fresh) return false;
    var flash = keep && main.querySelector(':scope > .alerts.flash');
    main.innerHTML = fresh.innerHTML;
    if (flash) main.insertBefore(flash, main.firstChild);
    return true;
  }
  function isHtml(r) { return (r.headers.get('content-type') || '').indexOf('text/html') === 0; }
  function toast(msg) {
    var main = document.querySelector('main');
    if (!main) return;
    var old = main.querySelector(':scope > .alerts.flash');
    if (old) old.remove();
    var ul = document.createElement('ul'), li = document.createElement('li'), ico = document.createElement('span'),
        txt = document.createElement('span');
    ul.className = 'alerts flash'; li.className = 'bad'; ico.className = 'ico'; ico.textContent = '✕';
    txt.className = 'msg'; txt.textContent = msg;
    li.appendChild(ico); li.appendChild(txt); ul.appendChild(li);
    main.insertBefore(ul, main.firstChild);
  }

  // переход без перезагрузки (ссылки-сегменты, ссылки и формы с data-swap): забрать страницу,
  // подменить <main>, обновить адрес; прокрутка и фокус остаются, сверху — тонкая полоса загрузки
  function go(url, init) {
    var main = document.querySelector('main');
    if (!main) { location.href = url; return; }
    var post = !!(init && init.method === 'POST'), y = window.scrollY,
        keep = document.activeElement && document.activeElement.id;
    document.body.classList.add('busy');
    // переход по ссылке — как фоновый запрос (сообщения не тратит), отправка формы — как обычный
    fetch(url, Object.assign({ credentials: 'same-origin', cache: 'no-store' }, init || { headers: { 'X-Zoo-Live': '1' } }))
      .then(function (r) {
        if (r.status === 401 || (r.redirected && new URL(r.url).pathname === '/login')) {
          location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
          throw new Error('auth');
        }
        if (!isHtml(r)) throw new Error('nav');
        return r.text().then(function (html) { return { html: html, url: r.url, moved: r.redirected }; });
      })
      .then(function (x) {
        if (!swapMain(main, x.html, !post)) throw new Error('nav');
        if (!post || x.moved) history.replaceState(null, '', x.url);
        window.scrollTo(0, y);
        var el = keep && document.getElementById(keep);
        if (el) el.focus({ preventScroll: true });
        initDrafts();
      })
      .catch(function (e) {
        if (e.message === 'auth') return;
        if (post) toast('Нет связи с админкой — повторите'); else location.href = url;
      })
      .then(function () { document.body.classList.remove('busy'); });
  }
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest('nav.seg a, a[data-swap]');
    if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey || a.origin !== location.origin) return;
    ev.preventDefault();
    if (dirty() && !window.confirm('Изменения не сохранены. Уйти без сохранения?')) return;
    go(a.href);
  });

  // черновик списка приложений: переключатели меняют только форму, «Сохранить» — один POST
  function was(i) { return i.getAttribute('data-was') === '1'; }
  function boxes(form) { return form.querySelectorAll('input[type=checkbox][data-was]'); }
  function refreshDraft(form) {
    var n = 0, pend = false;
    form.querySelectorAll('tbody tr').forEach(function (tr) {
      var d = false;
      tr.querySelectorAll('input[data-was]').forEach(function (i) { if (i.checked !== was(i)) d = true; });
      tr.classList.toggle('chg', d);
    });
    boxes(form).forEach(function (i) { if (i.checked !== was(i)) n++; });
    form.querySelectorAll('[data-pend]').forEach(function (i) { if (i.value.trim()) pend = true; });
    var c = form.querySelector('[data-count]'), s = form.querySelector('[data-save]');
    if (c) c.textContent = n ? 'Изменено: ' + n : (pend ? 'Своё приложение ждёт сохранения' : 'Без изменений');
    if (s) s.disabled = !n && !pend;
  }
  function dirty() {
    var f = document.querySelector('form[data-draft]'), n = 0;
    if (f) boxes(f).forEach(function (i) { if (i.checked !== was(i)) n++; });
    return n > 0;
  }
  function initDrafts() {
    document.querySelectorAll('form[data-draft]').forEach(function (f) {
      f.querySelectorAll('[data-add]').forEach(function (b) { b.hidden = false; });
      refreshDraft(f);
    });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  // «Своё приложение» → строка таблицы (id, уже есть в таблице, — просто включается)
  function addCustom(form) {
    var inp = function (p) { return form.querySelector('[name="custom_' + p + '"]'); },
        err = form.querySelector('[data-cu-err]'), title = inp('title').value.trim(), ids = {}, msg = '';
    ['android', 'windows'].forEach(function (p) {
      var v = inp(p).value.trim();
      if (!v) return;
      if (!new RegExp('^(?:' + form.getAttribute('data-re-' + p) + ')$').test(v))
        msg = p === 'android' ? 'Пакет Android вида com.example.app' : 'Процесс Windows вида program.exe';
      else ids[p] = v;
    });
    if (!msg && !ids.android && !ids.windows) msg = 'Укажите пакет Android или процесс Windows';
    err.textContent = msg; err.hidden = !msg;
    if (msg) return;
    var fresh = {};
    ['android', 'windows'].forEach(function (p) {
      if (!ids[p]) return;
      var same = Array.prototype.filter.call(form.querySelectorAll('input[type=checkbox][name=' + p + ']'),
        function (i) { return i.value.toLowerCase() === ids[p].toLowerCase(); })[0];
      if (same) same.checked = true; else fresh[p] = ids[p];
    });
    if (fresh.android || fresh.windows) {
      var label = title || fresh.android || fresh.windows, tr = el('tr', 'added'), td = el('td', '');
      td.appendChild(el('span', '', label));
      if (title) td.appendChild(el('span', 'sub', [fresh.android, fresh.windows].filter(Boolean).join(' · ')));
      tr.appendChild(td);
      ['android', 'windows'].forEach(function (p) {
        var c = el('td', 'num');
        if (fresh[p]) {
          var lab = el('label', 'tgl'), box = el('input', '');
          box.type = 'checkbox'; box.name = p; box.value = fresh[p]; box.checked = true;
          box.setAttribute('data-was', '0');
          box.setAttribute('aria-label', (p === 'android' ? 'Android: ' : 'Windows: ') + label);
          lab.appendChild(box); lab.appendChild(el('i', '')); c.appendChild(lab);
          if (title) {
            var h = el('input', '');
            h.type = 'hidden'; h.name = 'title:' + fresh[p]; h.value = title; td.appendChild(h);
          }
        } else {
          c.className = 'num muted'; c.textContent = '—';
        }
        tr.appendChild(c);
      });
      form.querySelector('tbody').appendChild(tr);
    }
    ['title', 'android', 'windows'].forEach(function (p) { inp(p).value = ''; });
    refreshDraft(form);
  }
  document.addEventListener('change', function (ev) {
    var i = ev.target, form = i.closest && i.closest('form[data-draft]');
    if (!form || i.type !== 'checkbox') return;
    if (!i.checked && !form.querySelector('input[type=checkbox][name=' + i.name + ']:checked')) {
      i.checked = true;  // пустой список платформы не допускается
      form.querySelector('[data-count]').textContent =
        'Нельзя выключить последнее приложение ' + (i.name === 'android' ? 'Android' : 'Windows');
      return;
    }
    refreshDraft(form);
  });
  document.addEventListener('input', function (ev) {
    var form = ev.target.closest && ev.target.closest('form[data-draft]');
    if (form && ev.target.hasAttribute('data-pend')) refreshDraft(form);
  });
  document.addEventListener('keydown', function (ev) {
    var form = ev.target.closest && ev.target.closest('form[data-draft]');
    if (form && ev.key === 'Enter' && ev.target.hasAttribute('data-pend')) {
      ev.preventDefault();
      addCustom(form);
    }
  });
  document.addEventListener('click', function (ev) {
    var add = ev.target.closest('[data-add]'), cancel = ev.target.closest('a[data-cancel]'), form;
    if (add && (form = add.closest('form[data-draft]'))) { addCustom(form); return; }
    if (cancel && (form = cancel.closest('form[data-draft]'))) {
      ev.preventDefault();
      form.querySelectorAll('tr.added').forEach(function (tr) { tr.remove(); });
      boxes(form).forEach(function (i) { i.checked = was(i); });
      form.querySelectorAll('[data-pend]').forEach(function (i) { i.value = ''; });
      var err = form.querySelector('[data-cu-err]');
      if (err) err.hidden = true;
      refreshDraft(form);
    }
  });
  window.addEventListener('beforeunload', function (ev) {
    if (dirty()) { ev.preventDefault(); ev.returnValue = ''; }
  });
  initDrafts();

  // live: раз в N секунд забрать ту же страницу и подменить <main>; пауза — вкладка скрыта,
  // фокус в поле ввода, открыт <details> или окно, либо выключено кнопкой (запоминается).
  // Запрос с If-None-Match: страница не изменилась — сервер отвечает 304, разметку не трогаем
  var live = document.getElementById('live');
  if (live) {
    var every = (parseInt(live.getAttribute('data-live'), 10) || 10) * 1000;
    var paused = false, dead = false, etag = '', etagUrl = '';
    try { paused = localStorage.getItem('zoo-live') === 'off'; } catch (e) { /* без хранилища */ }
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'btn small live-btn';
    var label = function (extra) {
      if (dead) return;
      live.textContent = paused ? 'live на паузе' : 'live · обновлено ' + (extra || new Date().toLocaleTimeString('ru-RU'));
      btn.textContent = paused ? 'Включить' : 'Пауза';
      live.className = paused ? 'live off' : 'live on';
    };
    var expired = function (text) {
      dead = true; paused = true; btn.hidden = true;
      var a = document.createElement('a');
      a.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
      a.textContent = text;
      if (text === 'Войти') a.className = 'btn small';
      live.className = 'live off';
      live.textContent = '';
      live.appendChild(a);
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
      if (!main || main.querySelector('details[open]') || document.querySelector('dialog[open]')) return;
      busy = true;
      var headers = { 'X-Zoo-Live': '1' };
      if (etag && etagUrl === location.href) headers['If-None-Match'] = etag;
      fetch(location.href, { credentials: 'same-origin', cache: 'no-store', headers: headers })
        .then(function (r) {
          if (r.status === 401) { expired('Войти'); throw new Error('сессия'); }
          if (r.redirected && new URL(r.url).pathname === '/login') { expired('сессия истекла — войти'); throw new Error('сессия'); }
          if (r.status === 304) { label(); return null; }
          if (!r.ok || !isHtml(r)) { paused = true; label(); throw new Error('сервер'); }
          etag = r.headers.get('ETag') || ''; etagUrl = location.href;
          return r.text();
        })
        .then(function (html) {
          if (html !== null && html !== undefined) { swapMain(main, html, true); label(); }
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
# статика сжимается один раз: сервер отдаёт готовый gzip тем, кто его принимает
CSS_GZ = gzip.compress(CSS.encode(), 9, mtime=0)
JS_GZ = gzip.compress(JS.encode(), 9, mtime=0)
