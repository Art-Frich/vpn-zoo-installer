# Эмулятор ТСПУ: маршрутизатор между zoo-net-client и zoo-net (см. README.md)
# Сборка: docker build -f docker/censor/censor.Dockerfile -t zoo-censor docker/censor
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends iptables iproute2 procps tcpdump \
 && rm -rf /var/lib/apt/lists/*

COPY profile.sh /usr/local/bin/zoo-censor-profile
RUN chmod 755 /usr/local/bin/zoo-censor-profile

LABEL zoo.role="censor"
CMD ["bash", "-c", "sysctl -w net.ipv4.ip_forward=1 >/dev/null; zoo-censor-profile clean; exec sleep infinity"]
