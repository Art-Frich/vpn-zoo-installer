# Тестовый «VPS»: Ubuntu с systemd в роли PID 1.
# Сборка: docker build -f docker/server.Dockerfile --build-arg UBUNTU=24.04 -t zoo-test-server:24.04 docker/
# Запуск только через docker/run-server.sh (privileged, cgroupns=host).
#
# Образ намеренно минимальный: всё, чем пользуется инсталлер сверх этого списка
# (jq, python3, sqlite3, ufw, ...), он обязан ставить сам — как на голом VPS.
ARG UBUNTU=24.04
FROM jrei/systemd-ubuntu:${UBUNTU}

ARG UBUNTU
ENV DEBIAN_FRONTEND=noninteractive \
    container=docker

# openssh-server — у любого VPS есть sshd, на него смотрят 01-firewall и fail2ban
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl ca-certificates iproute2 iptables sudo \
        openssh-server kmod procps \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl enable ssh \
 && mkdir -p /repo

LABEL zoo.role="server" zoo.ubuntu="${UBUNTU}"
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
