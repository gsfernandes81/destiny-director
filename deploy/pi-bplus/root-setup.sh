#!/bin/sh
# One-time ROOT setup for the Pi B+ test-bot host (Alpine Linux, sys install).
# Batches every operation that needs root; everything afterwards (image load,
# compose up, verification) runs unprivileged over SSH as $PI_USER.
# Idempotent: safe to re-run. Run as root: `sh root-setup.sh`.
set -eu

PI_USER="${PI_USER:-dd}"
SSH_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJDFSj3iGVI4W4HLDIoyDfyXtr3JdxlKAYd1iB8im8le claude-slim-stack-temp-20260805"
SUBID_START="${SUBID_START:-100000}"
SUBID_COUNT="${SUBID_COUNT:-65536}"
ZRAM_SIZE="${ZRAM_SIZE:-256M}"
APP_DIR="${APP_DIR:-/srv/dd}"

echo "== 1/9 apk repositories (enable community) + packages"
if grep -q '^#.*\/community$' /etc/apk/repositories; then
    sed -i 's|^#\(.*/community\)$|\1|' /etc/apk/repositories
fi
apk update
apk add --no-cache podman crun passt shadow-uidmap podman-compose zram-init

echo "== 2/9 user $PI_USER + SSH key"
if ! id "$PI_USER" >/dev/null 2>&1; then
    adduser -D "$PI_USER"
fi
HOME_DIR="$(getent passwd "$PI_USER" | cut -d: -f6)"
mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
grep -qF "$SSH_PUBKEY" "$HOME_DIR/.ssh/authorized_keys" \
    || echo "$SSH_PUBKEY" >> "$HOME_DIR/.ssh/authorized_keys"
chown -R "$PI_USER:$PI_USER" "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

echo "== 3/9 subuid/subgid ranges (rootless podman)"
grep -q "^$PI_USER:" /etc/subuid || echo "$PI_USER:$SUBID_START:$SUBID_COUNT" >> /etc/subuid
grep -q "^$PI_USER:" /etc/subgid || echo "$PI_USER:$SUBID_START:$SUBID_COUNT" >> /etc/subgid

echo "== 4/9 cgroups v2 (unified) + cgroups service"
if grep -q '^rc_cgroup_mode=' /etc/rc.conf; then
    sed -i 's/^rc_cgroup_mode=.*/rc_cgroup_mode="unified"/' /etc/rc.conf
elif grep -q '^#rc_cgroup_mode=' /etc/rc.conf; then
    sed -i 's/^#rc_cgroup_mode=.*/rc_cgroup_mode="unified"/' /etc/rc.conf
else
    echo 'rc_cgroup_mode="unified"' >> /etc/rc.conf
fi
rc-update add cgroups boot 2>/dev/null || true
rc-service cgroups start 2>/dev/null || true

echo "== 5/9 XDG_RUNTIME_DIR + boot autostart (local.d)"
PI_UID="$(id -u "$PI_USER")"
cat > /etc/local.d/podman-user.start <<EOF
#!/bin/sh
# Rootless podman prerequisites for $PI_USER (no logind on Alpine).
mkdir -p /run/user/$PI_UID
chown $PI_USER:$PI_USER /run/user/$PI_UID
chmod 700 /run/user/$PI_UID
# Start the bot stack at boot if a compose file is deployed.
if [ -f $APP_DIR/compose.yml ]; then
    su - $PI_USER -c "XDG_RUNTIME_DIR=/run/user/$PI_UID podman-compose -f $APP_DIR/compose.yml up -d" &
fi
EOF
chmod +x /etc/local.d/podman-user.start
rc-update add local default 2>/dev/null || true
# Make the runtime dir exist right now, not just after reboot:
mkdir -p "/run/user/$PI_UID"
chown "$PI_USER:$PI_USER" "/run/user/$PI_UID"
chmod 700 "/run/user/$PI_UID"
# Persist XDG_RUNTIME_DIR for SSH sessions:
grep -q XDG_RUNTIME_DIR "$HOME_DIR/.profile" 2>/dev/null \
    || echo "export XDG_RUNTIME_DIR=/run/user/$PI_UID" >> "$HOME_DIR/.profile"
chown "$PI_USER:$PI_USER" "$HOME_DIR/.profile"

echo "== 6/9 app dir $APP_DIR"
mkdir -p "$APP_DIR"
chown "$PI_USER:$PI_USER" "$APP_DIR"

echo "== 7/9 zram swap ($ZRAM_SIZE)"
cat > /etc/conf.d/zram-init <<EOF
load_on_start=yes
unload_on_stop=yes
num_devices=1
type0=swap
size0=$ZRAM_SIZE
EOF
rc-update add zram-init default 2>/dev/null || true
rc-service zram-init restart 2>/dev/null || rc-service zram-init start 2>/dev/null || true

echo "== 8/9 GPU memory -> 16MB (headless)"
for f in /boot/usercfg.txt /boot/config.txt; do
    if [ -f "$f" ]; then
        grep -q '^gpu_mem=' "$f" || echo "gpu_mem=16" >> "$f"
        break
    fi
done

echo "== 9/9 noatime on root filesystem"
# Conservative: only patch the common 'defaults'-style root entry; otherwise
# print a reminder instead of guessing at custom mount options.
if grep -qE '^\S+\s+/\s+\S+\s+\S*noatime' /etc/fstab; then
    echo "   root already mounted noatime"
elif grep -qE '^\S+\s+/\s+\S+\s+defaults\s' /etc/fstab; then
    sed -i -E 's|^(\S+\s+/\s+\S+\s+)defaults(\s)|\1defaults,noatime\2|' /etc/fstab
    echo "   added noatime to root fstab entry (takes effect on reboot)"
else
    echo "   NOTE: could not safely edit /etc/fstab - add 'noatime' to the / entry manually"
fi

echo
echo "Done. Reboot recommended (cgroup mode + gpu_mem + noatime need it):  reboot"
echo "After reboot, everything else is unprivileged as $PI_USER."
