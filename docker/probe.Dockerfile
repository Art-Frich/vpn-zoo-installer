# Клиентский пробник: запускается на машине пользователя (Docker), трафик идёт через его
# провайдера. Клиенты закреплены теми же версиями, что сервер (scripts/versions.env), и
# проверяются по sha256; zoo — из этого же репо. Инструкция: docker/probe/README.md.
#
# Сборка (контекст — корень репо): docker build -f docker/probe.Dockerfile -t zoo-probe .
# Запуск: docker run --rm --cap-add NET_ADMIN --device /dev/net/tun -v "$PWD/probe:/data" zoo-probe [--proto ID]
ARG UBUNTU=24.04

FROM ubuntu:${UBUNTU} AS clients
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gcc make libc6-dev \
 && rm -rf /var/lib/apt/lists/*
COPY scripts/versions.env /build/versions.env
COPY docker/probe/install-clients.sh /build/install-clients.sh
RUN bash /build/install-clients.sh /out

FROM ubuntu:${UBUNTU}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 curl ca-certificates jq iproute2 procps dnsutils netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*
COPY --from=clients /out/ /opt/zoo-probe/bin/
COPY zoo/ /opt/vpn-zoo/zoo/
COPY docker/probe/entrypoint.sh /usr/local/bin/zoo-probe
COPY docker/probe/expect.py /opt/zoo-probe/expect.py
RUN chmod 755 /usr/local/bin/zoo-probe /opt/vpn-zoo/zoo/zoo \
 && for b in /opt/zoo-probe/bin/*; do [ -x "$b" ] && ln -sf "$b" /usr/local/bin/; done \
 && ln -sf /opt/vpn-zoo/zoo/zoo /usr/local/bin/zoo \
 && zoo version

LABEL zoo.role="probe"
WORKDIR /data
# аргументы docker run после имени образа: «--…» — флаги пробника, иначе — команда вместо него
ENTRYPOINT ["zoo-probe"]
