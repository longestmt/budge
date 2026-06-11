#!/usr/bin/env bash
# budge bootstrap for Debian 13 — installs prerequisites (root part), then
# hands off to the interactive `budge setup` (PRD 7.1). Safe to re-run.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }

if [[ $(id -u) -ne 0 ]]; then
    echo "Run the prerequisite phase as root: sudo ./setup.sh" >&2
    echo "(then it drops to your user for the interactive part)" >&2
    exit 1
fi

REAL_USER="${SUDO_USER:-root}"

say "installing prerequisites (sudo, hledger, git, python3, pipx, podman)"
apt-get update -qq
apt-get install -y -qq sudo hledger hledger-ui hledger-web git python3 \
    python3-pip pipx podman

say "installing the budge CLI for ${REAL_USER}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# pipx gives an isolated, replaceable install; budge has zero runtime deps.
sudo -u "$REAL_USER" pipx install --force "$SCRIPT_DIR" >/dev/null
sudo -u "$REAL_USER" pipx ensurepath >/dev/null || true

# pipx installs into ~/.local/bin, which is often NOT on PATH (and never on
# sudo's or systemd's). Symlink into /usr/local/bin so `budge` works
# everywhere without shell-config changes.
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
ln -sf "$REAL_HOME/.local/bin/budge" /usr/local/bin/budge
# Some LXC consoles (pct enter, su without '-') have a PATH that omits
# /usr/local/bin entirely; verify the command actually resolves, and fall
# back to /usr/bin which is always on PATH.
if ! command -v budge >/dev/null 2>&1; then
    ln -sf "$REAL_HOME/.local/bin/budge" /usr/bin/budge
fi
BUDGE_CMD="$(command -v budge || echo /usr/bin/budge)"
say "budge available globally at ${BUDGE_CMD} (pipx's PATH note above is handled by this symlink)"

say "installing the budge man page"
install -D -m 0644 "$SCRIPT_DIR/budge.1" /usr/local/share/man/man1/budge.1
# LC_ALL set explicitly: minimal LXC images often have no locale configured,
# which makes mandb grumble.
command -v mandb >/dev/null && LC_ALL=C.UTF-8 mandb -q 2>/dev/null || true

say "prerequisites done — starting interactive setup as ${REAL_USER}"
# The interactive phase is idempotent and never needs root except for the
# systemd install step, which prints exact sudo commands if it can't write.
# Absolute path: sudo/login shells don't reliably see ~/.local/bin.
exec sudo -u "$REAL_USER" -i "$BUDGE_CMD" setup
