#!/bin/bash
# Everything that can be checked without hardware.
#   tests/run_all.sh
# Needs the upstream release unpacked where tests/env.sh points (HBAV_SCRIPTS) for the
# regression checks of the existing tabs; the Bay Mapper suite runs on synthetic sysfs only.
cd "$(dirname "$0")/.." || exit 2
source tests/env.sh
export HBAV_ALLOW_NONROOT=1
rm -rf "$HBAV_ETC" "$HBAV_STATE" "$HBAV_RUN"
fail=0
step() { printf '\n== %s\n' "$1"; }

step "health rollup (existing)"
python3 tests/test_health.py || fail=1

step "existing commands still answer with their usual payloads (regression)"
check() { local name=$1 expect=$2; shift 2; if python3 "$C" "$@" | grep -q -- "$expect"; then echo "PASS $name"; else echo "FAIL $name"; fail=1; fi; }
check "env"               '"backend": "storcli"'   env
check "overview"          '"controllers": \['      overview
check "drives"            '"slot": "0/0"'          drives
check "phy"               '"phys": \['             phy
check "events"            '"entries": \['          events
check "health"            '"indicators"'           health
check "metrics"           '"controllers"'          metrics
check "smart status"      '"state"'                smart status
check "locate list"       '"active": \['           locate list
check "config get"        '"LOCATE_MAX_SECS"'      config get
check "locate rejects bad device" 'Expected a /dev/sdX' locate start /dev/../etc/passwd
check "drives tab gets bay locations" '"locations"'  baymap locations

step "bay mapper"
python3 tests/test_baymap.py || fail=1

step "JavaScript syntax"
for f in cockpit/hbaviewer/*.js; do node --check "$f" && echo "PASS $f" || { echo "FAIL $f"; fail=1; }; done
step "Python lint"
python3 -m pyflakes bin/hbav_baymap.py tests/*.py 2>/dev/null && echo "PASS pyflakes" || { python3 -m pyflakes bin/hbav_baymap.py tests/*.py; echo "(pyflakes warnings above)"; }

echo; [ $fail = 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $fail
