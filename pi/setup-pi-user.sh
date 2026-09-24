#!/usr/bin/env bash
# Creates the SFTP-only `wigle-sync` user on the Pi and grants it access to Kismet's logs.
# Idempotent. Run on the Pi as root, with the sync key's public half as the argument:
#
#   ssh zaphod@<pi> 'sudo bash -s' < pi/setup-pi-user.sh "$(cat ~/.ssh/wigle_sync_ed25519.pub)"
#
# Least privilege: the user can't get a shell or forward ports (sshd ForceCommand +
# authorized_keys `restrict`), and its only extra permission is an ACL on the Kismet
# log dir — not membership in the `kismet` group, which can run Kismet's setuid
# capture helpers.
set -euo pipefail

SYNC_USER=wigle-sync
LOG_DIR=/var/log/kismet
PUBKEY="${1:?usage: setup-pi-user.sh '<ssh public key>'}"

if ! id "$SYNC_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SYNC_USER"
fi

install -d -m 700 -o "$SYNC_USER" -g "$SYNC_USER" "/home/$SYNC_USER/.ssh"
echo "restrict $PUBKEY" > "/home/$SYNC_USER/.ssh/authorized_keys"
chown "$SYNC_USER:$SYNC_USER" "/home/$SYNC_USER/.ssh/authorized_keys"
chmod 600 "/home/$SYNC_USER/.ssh/authorized_keys"

# rwx on the dir lets it list, read and rename (archive) files; the default ACL
# covers files Kismet creates later, and the uploaded/ archive dir.
setfacl -m "u:$SYNC_USER:rwx" "$LOG_DIR"
setfacl -d -m "u:$SYNC_USER:rwx" "$LOG_DIR"
setfacl -m "u:$SYNC_USER:rw" "$LOG_DIR"/* 2>/dev/null || true

cat > /etc/ssh/sshd_config.d/60-wigle-sync.conf <<EOF
Match User $SYNC_USER
    ForceCommand internal-sftp
    PasswordAuthentication no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
EOF

sshd -t
systemctl reload ssh
echo "wigle-sync user ready"
