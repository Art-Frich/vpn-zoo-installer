# docker/censor — эмулятор ТСПУ

Маршрутизатор между пробником и сервером. Нужен, чтобы проверить, что пробник правильно классифицирует блокировки (ARCHITECTURE.md §8). Запускает его `docker/probe/run.sh`, имена у контейнера и сети свои на каждый прогон.

```
zoo-probe ── <префикс>-cnet ── <префикс>-censor ── zoo-net ── zoo-<сервер>
              (сеть пробника)    NAT + профиль
```

- Пробник получает маршрут `IP-сервера/32 via <цензор>` (`ZOO_PROBE_VIA`, нужен `NET_ADMIN`). Остальной его трафик (DNS, проверка своего IP) идёт мимо цензора.
- Цензор запущен с `--cap-add NET_ADMIN --sysctl net.ipv4.ip_forward=1` и делает маскарад в сторону сервера (`zoo-censor-profile nat <IP>`). Поэтому ответы сервера возвращаются через цензор, а сервер и его маршруты не трогаются.
- Правила профиля лежат в цепочке `ZOO_CENSOR`, она подключена первой в `FORWARD`. Профиль переключается без пересоздания контейнера: `zoo-censor-profile <профиль> <IP-сервера>`.

| Профиль | Правило | Ожидаемый вердикт (`docker/probe/expect.py`) |
|---|---|---|
| `clean` | без правил | все `OK` или `SLOW` |
| `drop-udp` | `-p udp -d <сервер> -j DROP` | UDP (Hysteria2, AWG, TUIC) — `UDP_BLOCKED`; TCP — `OK`/`SLOW` |
| `ip-block` | DROP всего к серверу и от него | все `IP_BLOCKED` |
| `freeze-16k` | `-p tcp -s <сервер> -m connbytes --connbytes 16384: --connbytes-dir reply --connbytes-mode bytes -j DROP` | TCP — `FREEZE_16K`; UDP — `OK`/`SLOW` |
| `rst-tls` | `-m u32`: в начале TCP-нагрузки запись TLS Handshake `16 03 00..03` с типом `01` (ClientHello) → `REJECT --reject-with tcp-reset` | TCP с TLS/REALITY (VLESS, XHTTP) — `HANDSHAKE_FAIL`; SS-2022 и UDP — `OK`/`SLOW` |
| `port-block` | `-p tcp -d <сервер> --dport ${ZOO_CENSOR_PORT:-443} -j DROP` (в прогон по умолчанию не входит: `ZOO_PROBE_PROFILES=port-block`) | TCP на 443 (VLESS-REALITY) — `IP_BLOCKED` с причиной «блокировка порта»; остальные — `OK`/`SLOW` |

`rst-tls` раньше искал `|1603|` где угодно в пакете (`-m string`). В зашифрованном потоке SS-2022 такая пара байтов изредка встречается случайно, поэтому теперь проверяется только начало нагрузки и тип сообщения (`u32`). Модули `connbytes`, `u32`, `string` и `MASQUERADE` проверены на ядре Docker Desktop (WSL2 6.6.87).

Вручную:

```bash
docker build -f docker/censor/censor.Dockerfile -t zoo-censor docker/censor
docker exec <цензор> zoo-censor-profile show
docker exec <цензор> zoo-censor-profile freeze-16k 172.22.0.2
```
