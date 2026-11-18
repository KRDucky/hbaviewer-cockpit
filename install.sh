#!/bin/bash
# HBA Viewer for Cockpit — installer for Ubuntu (tested target: 26.04 LTS).
#
#   sudo ./install.sh                         # install / upgrade
#   sudo ./install.sh --storcli-zip FILE.zip  # also install Broadcom storcli from its download zip
#   sudo ./install.sh --release-tarball F.txz # offline: use an already-downloaded upstream release
#   sudo ./install.sh --no-cockpit            # don't apt-install cockpit (you already run it elsewhere)
#
# The hardware readers (bash + gawk composers/parsers and the lsiutil binary for
# SAS2 cards) come straight from a pinned Unraid-HBAviewer release, checksum-
# verified, unmodified. This package only adds the Cockpit UI and a root helper.
set -euo pipefail

UPSTREAM_VER="2026.09.19"
UPSTREAM_URL="https://github.com/FugginOld/Unraid-HBAviewer/releases/download/${UPSTREAM_VER}/hbaviewer.txz"
UPSTREAM_MD5="508c735b3fd50a1171b3fcde39737a89"   # the md5 upstream's own .plg pins

LIB=/usr/lib/hbaviewer
COCKPIT_DIR=/usr/share/cockpit/hbaviewer
ETC=/etc/hbaviewer
STATE=/var/lib/hbaviewer
UNITDIR=/etc/systemd/system
HERE="$(cd "$(dirname "$0")" && pwd)"

TARBALL=""; STORCLI_ZIP=""; WITH_COCKPIT=1
while [ $# -gt 0 ]; do
    case "$1" in
        --release-tarball) TARBALL="$2"; shift 2 ;;
        --storcli-zip)     STORCLI_ZIP="$2"; shift 2 ;;
        --no-cockpit)      WITH_COCKPIT=0; shift ;;
        -h|--help)         sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
command -v apt-get >/dev/null || die "this installer expects an apt-based system (Ubuntu/Debian)"

# ── packages ────────────────────────────────────────────────────────────────
pkgs=(gawk smartmontools pciutils util-linux python3 curl xz-utils unzip sg3-utils)
[ "$WITH_COCKPIT" = 1 ] && ! dpkg -s cockpit-bridge >/dev/null 2>&1 && pkgs+=(cockpit)
missing=()
for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ "${#missing[@]}" -gt 0 ]; then
    say "Installing packages: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi
# Upstream's parsers need gawk (3-arg match()). hbaviewer-ctl puts a private
# awk->gawk shim first on PATH for its own children, so the system-wide awk
# alternative (mawk on Ubuntu) is left alone.

# ── upstream release ────────────────────────────────────────────────────────
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
if [ -z "$TARBALL" ]; then
    say "Downloading Unraid-HBAviewer ${UPSTREAM_VER}"
    curl -fsSL -o "$WORK/hbaviewer.txz" "$UPSTREAM_URL" || die "download failed: $UPSTREAM_URL (use --release-tarball for offline installs)"
    TARBALL="$WORK/hbaviewer.txz"
fi
got=$(md5sum "$TARBALL" | cut -d' ' -f1)
[ "$got" = "$UPSTREAM_MD5" ] || die "checksum mismatch for $TARBALL (got $got, want $UPSTREAM_MD5)"
mkdir "$WORK/x"
tar -xJf "$TARBALL" -C "$WORK/x"
SRC="$WORK/x/usr/local/emhttp/plugins/hbaviewer"
[ -f "$SRC/scripts/lib.sh" ] || die "release tarball does not contain the expected scripts"

# ── install files ───────────────────────────────────────────────────────────
say "Installing to $LIB and $COCKPIT_DIR"
install -d -m 0755 "$LIB" "$COCKPIT_DIR" "$ETC"
install -d -m 0700 "$STATE"
# Bay Mapper data is never touched by an upgrade: /etc/hbaviewer/baymap.json (layout + mappings)
# and /var/lib/hbaviewer/baymap-* (history, last-seen, backups) are only ever written by hbaviewer-ctl.
[ -f "$ETC/baymap.json" ] && chmod 0640 "$ETC/baymap.json"
rm -rf "$LIB/scripts"
cp -r "$SRC/scripts" "$LIB/scripts"
# Unraid-only pieces have no meaning here; drop them so nothing can call them.
rm -f "$LIB/scripts/flash_hba.sh" "$LIB/scripts/install_storcli2.sh" \
      "$LIB/scripts/notify_check.php" "$LIB/scripts/bundle_support.sh"
chown -R root:root "$LIB/scripts"
find "$LIB/scripts" -type f -name '*.sh' -exec chmod 0755 {} +
if [ "$(uname -m)" = x86_64 ]; then
    install -m 0755 "$SRC/hbaviewer.x86_64" "$LIB/lsiutil"
else
    warn "lsiutil is x86_64-only; SAS2 (9207/9211) cards will not be readable on $(uname -m)"
fi
echo "$UPSTREAM_VER" > "$LIB/UPSTREAM_VERSION"
install -m 0755 "$HERE/bin/hbaviewer-ctl" "$LIB/hbaviewer-ctl"
install -m 0644 "$HERE/bin/hbav_baymap.py" "$LIB/hbav_baymap.py"
ln -sf "$LIB/hbaviewer-ctl" /usr/local/sbin/hbaviewer-ctl
install -m 0644 "$HERE"/cockpit/hbaviewer/* "$COCKPIT_DIR/"
install -m 0644 "$SRC/icon.png" "$COCKPIT_DIR/icon.png" 2>/dev/null || true

# Settings: keep an existing file; (re)generate the shell cfg from it.
"$LIB/hbaviewer-ctl" config set >/dev/null

# ── optional: storcli from Broadcom's zip ───────────────────────────────────
if [ -n "$STORCLI_ZIP" ]; then
    say "Installing storcli from $STORCLI_ZIP"
    mkdir "$WORK/sc"; unzip -q -o "$STORCLI_ZIP" -d "$WORK/sc"
    # The download nests zips (Unified_storcli_all_os.zip inside the outer one).
    while IFS= read -r -d '' z; do unzip -q -o "$z" -d "${z%.zip}"; done < <(find "$WORK/sc" -name '*.zip' -print0)
    deb=$(find "$WORK/sc" -iname 'storcli*_all.deb' -o -iname 'storcli*amd64.deb' | grep -iv arm | head -1)
    [ -n "$deb" ] || die "no storcli .deb found inside $STORCLI_ZIP"
    dpkg -i "$deb"
fi

# ── sampling timer ──────────────────────────────────────────────────────────
say "Installing hbaviewer-sample.timer (health sample every 5 min)"
cat > "$UNITDIR/hbaviewer-sample.service" <<'EOF'
[Unit]
Description=HBA Viewer health sample (LSI/Broadcom HBA)
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/lib/hbaviewer/hbaviewer-ctl sample
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=180
StandardOutput=null
EOF
cat > "$UNITDIR/hbaviewer-sample.timer" <<'EOF'
[Unit]
Description=Sample LSI/Broadcom HBA health every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable --now hbaviewer-sample.timer >/dev/null
    [ "$WITH_COCKPIT" = 1 ] && systemctl enable --now cockpit.socket >/dev/null 2>&1 || true
else
    warn "systemd is not running here; enable hbaviewer-sample.timer and cockpit.socket yourself"
fi

# ── report ──────────────────────────────────────────────────────────────────
say "Detected:"
"$LIB/hbaviewer-ctl" env | python3 -c '
import json, sys
e = json.load(sys.stdin)
print("    backend   :", e.get("backend") or "none")
print("    storcli   :", e.get("storcli") or "not found")
print("    lsiutil   :", e.get("lsiutil") or "not installed")
print("    driver    :", e.get("driver") or "-")
for l in e.get("pci") or []: print("    pci       :", l)
'
if ! "$LIB/hbaviewer-ctl" env | grep -q '"storcli": "/'; then
    if lspci -nn -d 1000: 2>/dev/null | grep -qiE 'SAS3|SAS38|SAS39|SAS4|SAS35|Fusion-MPT 12GSAS|24GSAS'; then
        warn "A SAS3/SAS3.5/SAS4 controller is present but no storcli was found."
        warn "Download StorCLI (SAS3/3.5) or StorCLI2 (9600/SAS4) from broadcom.com, then:"
        warn "    sudo $0 --storcli-zip ~/Downloads/<storcli download>.zip"
    fi
fi
port=$(systemctl show cockpit.socket -p Listen 2>/dev/null | grep -oE '[0-9]+ ' | head -1 | tr -d ' ')
say "Done. Open Cockpit (https://$(hostname -f 2>/dev/null || hostname):${port:-9090}) → HBA Viewer, with Administrative access on."
