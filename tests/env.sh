# Source to point hbaviewer-ctl at upstream scripts + fixture storcli (no hardware).
T=/home/claude/hbaviewer-cockpit/tests
export HBAV_SCRIPTS=/tmp/claude-0/up/usr/local/emhttp/plugins/hbaviewer/scripts
export HBAV_ETC=/tmp/claude-0/t/etc HBAV_STATE=/tmp/claude-0/t/state HBAV_RUN=/tmp/claude-0/t/run
export HBAV_FIXTURES=/root/Unraid-HBAviewer/tests/fixtures HBAV_NO_SYSTEMD=1
export PATH=$T/stub:$PATH
C=/home/claude/hbaviewer-cockpit/bin/hbaviewer-ctl
