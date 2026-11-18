#!/bin/bash
# Remove HBA Viewer for Cockpit.  --purge also deletes settings and history
# (health rings, PHY baselines, archived event log, SMART cache) AND the Bay Mapper's
# enclosure layouts, drive mappings, replacement history and backups.
# Without --purge all of that is kept, so a reinstall picks up where it left off.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
systemctl disable --now hbaviewer-sample.timer >/dev/null 2>&1 || true
systemctl stop 'hbaviewer-locate-*' 'hbaviewer-smart' >/dev/null 2>&1 || true
rm -f /etc/systemd/system/hbaviewer-sample.service /etc/systemd/system/hbaviewer-sample.timer
systemctl daemon-reload >/dev/null 2>&1 || true
rm -rf /usr/lib/hbaviewer /usr/share/cockpit/hbaviewer /run/hbaviewer
rm -f /usr/local/sbin/hbaviewer-ctl
if [ "${1:-}" = "--purge" ]; then
    rm -rf /etc/hbaviewer /var/lib/hbaviewer
    echo "Removed, including settings and history."
else
    echo "Removed. Settings and bay maps in /etc/hbaviewer and history in /var/lib/hbaviewer were kept (use --purge to delete)."
    echo "Tip: before purging, export a Bay Mapper backup (Bay Mapper → Backup) or run: hbaviewer-ctl baymap backup > baymap-backup.json"
fi
