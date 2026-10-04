# docker/censor — эмулятор ТСПУ

Маршрутизатор между пробником и сервером. Нужен, чтобы проверить, что пробник правильно классифицирует блокировки (ARCHITECTURE.md §8).

Схема сетей:

```
zoo-probe ── zoo-net-client ── zoo-censor ── zoo-net ── zoo-<сервер>
```

У пробника маршрут к подсети `zoo-net` идёт через `zoo-censor` (ip_forward=1). Цензор держит набор профилей, профиль переключается без пересоздания контейнера:

| Профиль | Правило | Ожидаемый вердикт пробника |
|---|---|---|
| `clean` | без правил | `OK` |
| `drop-udp` | `iptables -A FORWARD -p udp -d <сервер> -j DROP` | `UDP_BLOCKED` для Hy2/AWG/TUIC, `OK` для TCP |
| `ip-block` | DROP всего трафика к IP сервера | `IP_BLOCKED` |
| `freeze-16k` | после ~16 КБ от сервера к клиенту в одном TCP-потоке пакеты дропаются (`-m connbytes --connbytes 16384: --connbytes-dir reply --connbytes-mode bytes -j DROP`) | `FREEZE_16K` на большом запросе при успешном малом |
| `rst-tls` | RST на TLS ClientHello к порту (`-m string`) | `HANDSHAKE_FAIL` |

Файлы: `censor.Dockerfile` (ubuntu + iptables + iproute2) и `profile.sh <профиль>`. Контейнер и сети называются с префиксом `zoo-`.
