#!/bin/bash
# One-time ROOT setup for the Pi 5 build host (Raspberry Pi OS / Debian-based).
# Batches every operation that needs root. The Pi 5 builds the linux/arm/v6
# images natively (Cortex-A76 executes 32-bit ARM userland) and ships them to
# the B+; building and shipping run unprivileged afterwards.
# Idempotent: safe to re-run. Run: `sudo bash root-setup.sh`.
set -euo pipefail

PI_USER="${PI_USER:-$(logname 2>/dev/null || echo pi)}"
# Deploy keys are per-session throwaways (the agent session that needs SSH access
# rotates every so often), so this default is just the current one -- it's appended to
# authorized_keys, never replacing what's already there (see the grep-before-append
# below), so old sessions' keys keep working until someone prunes them by hand. A
# future session rotating the key doesn't need a repo commit to do it: override with
# `SSH_PUBKEY="ssh-ed25519 AAAA... some-label" sudo -E bash deploy/pi5/root-setup.sh`.
SSH_PUBKEY="${SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOGaCeXcIrWB9SFTi/rwLx68HwPub3uP2TzsyPFKpbQj claude-cloud-two-20260806}"
SUBID_START="${SUBID_START:-200000}"
SUBID_COUNT="${SUBID_COUNT:-65536}"

echo "== 1/4 packages (podman + uidmap)"
apt-get update
apt-get install -y podman uidmap slirp4netns

echo "== 2/4 SSH key for $PI_USER"
HOME_DIR="$(getent passwd "$PI_USER" | cut -d: -f6)"
mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
grep -qF "$SSH_PUBKEY" "$HOME_DIR/.ssh/authorized_keys" \
    || echo "$SSH_PUBKEY" >> "$HOME_DIR/.ssh/authorized_keys"
chown -R "$PI_USER:$PI_USER" "$HOME_DIR/.ssh"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

echo "== 3/4 subuid/subgid ranges (rootless podman)"
grep -q "^$PI_USER:" /etc/subuid || echo "$PI_USER:$SUBID_START:$SUBID_COUNT" >> /etc/subuid
grep -q "^$PI_USER:" /etc/subgid || echo "$PI_USER:$SUBID_START:$SUBID_COUNT" >> /etc/subgid

echo "== 4/4 linger (user services/containers survive logout)"
loginctl enable-linger "$PI_USER"

echo
echo "Done. No reboot needed. Everything else runs unprivileged as $PI_USER."
