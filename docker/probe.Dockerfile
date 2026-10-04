# Клиентский пробник: запускается на машине пользователя (Docker), трафик идёт
# через его провайдера. Состав клиентов и раннер — в docker/probe/ (см. README там).
# Сборка (контекст — корень репо): docker build -f docker/probe.Dockerfile -t zoo-probe .
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl ca-certificates jq python3 iproute2 iptables \
        dnsutils netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

# versions.env берём из scripts/: клиенты закрепляются теми же версиями, что и сервер
COPY scripts/ /opt/zoo-probe/server-scripts/
COPY docker/probe/ /opt/zoo-probe/
RUN if [ -f /opt/zoo-probe/install-clients.sh ]; then bash /opt/zoo-probe/install-clients.sh; fi

LABEL zoo.role="probe"
WORKDIR /opt/zoo-probe
CMD ["bash"]
