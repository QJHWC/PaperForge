FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        docker-cli \
        openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/sh paperforge \
    && mkdir -p /run/sshd

COPY entrypoint.sh /usr/local/bin/paperforge-openssh-entrypoint
RUN chmod 0755 /usr/local/bin/paperforge-openssh-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/paperforge-openssh-entrypoint"]
