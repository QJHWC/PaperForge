#!/bin/sh
set -eu

: "${PAPERFORGE_REMOTE_HOME:?PAPERFORGE_REMOTE_HOME is required}"
: "${PAPERFORGE_REMOTE_UID:?PAPERFORGE_REMOTE_UID is required}"
: "${PAPERFORGE_REMOTE_GID:?PAPERFORGE_REMOTE_GID is required}"

primary_group="$(getent group "$PAPERFORGE_REMOTE_GID" | cut -d: -f1 || true)"
if [ -z "$primary_group" ]; then
  groupmod --gid "$PAPERFORGE_REMOTE_GID" paperforge
  primary_group="paperforge"
fi
usermod \
  --uid "$PAPERFORGE_REMOTE_UID" \
  --gid "$primary_group" \
  --home "$PAPERFORGE_REMOTE_HOME" \
  paperforge

socket_gid="$(stat -c '%g' /var/run/docker.sock)"
if ! getent group "$socket_gid" >/dev/null 2>&1; then
  groupadd --gid "$socket_gid" docker-host
fi
socket_group="$(getent group "$socket_gid" | cut -d: -f1)"
usermod --append --groups "$socket_group" paperforge

chown "$PAPERFORGE_REMOTE_UID:$PAPERFORGE_REMOTE_GID" "$PAPERFORGE_REMOTE_HOME"
chmod 0700 "$PAPERFORGE_REMOTE_HOME"
chown -R "$PAPERFORGE_REMOTE_UID:$PAPERFORGE_REMOTE_GID" \
  "$PAPERFORGE_REMOTE_HOME/.ssh"
chmod 0700 "$PAPERFORGE_REMOTE_HOME/.ssh"
chmod 0600 "$PAPERFORGE_REMOTE_HOME/.ssh/authorized_keys"

ssh-keygen -A
cat >/etc/ssh/sshd_config.d/paperforge.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
X11Forwarding no
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers paperforge
EOF

exec /usr/sbin/sshd -D -e
