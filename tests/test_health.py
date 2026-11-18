"""Health-rollup port vs. upstream health.php semantics, on synthetic rings."""
import importlib.machinery, importlib.util, os, sys
os.environ["HBAV_ALLOW_NONROOT"] = "1"
here = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("ctl", os.path.join(here, "..", "bin", "hbaviewer-ctl"))
spec = importlib.util.spec_from_loader("ctl", loader); ctl = importlib.util.module_from_spec(spec); loader.exec_module(ctl)
CFG = dict(ctl.DEFAULTS)

def sample(t, up, inv=0, sync=0, drives=8, temp=50, band="normal", w=8, mw=8, sw=16, ok=True):
    return {"t": t, "uptime": up, "temp": temp, "temp_band": band, "fw": "x", "drives": drives, "read_ok": ok,
            "link": {"width": w, "max_width": mw, "speed": "8.0 GT/s", "max_speed": "8.0 GT/s", "slot_width": sw, "slot_speed": "8.0 GT/s"},
            "phys": [{"idx": 0, "inv": inv, "disp": 0, "sync": sync, "rst": 0}, {"idx": 1, "inv": 0, "disp": 0, "sync": 0, "rst": 0}]}

def run(ring, now=None):
    now = now or ring[-1]["t"]
    ind = ctl.health_indicators(ring, ctl.health_rates(ring), now, CFG)
    return ind, ctl.health_rollup(ind)

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name); fails += not cond

# clean for 1 h -> ok
r = []
for i in range(13): r = ctl.health_ingest(r, sample(1000 + i * 300, 500 + i * 300))
ind, (st, why) = run(r)
check("clean 1h is ok", st == "ok" and ind["link_integrity"]["state"] == "ok")
# clean for only 10 min -> unknown (all-clear floor)
ind, (st, _) = run(r[:3])
check("clean 10min is unknown", ind["link_integrity"]["state"] == "unknown" and st == "unknown")
# invalid dwords rising 100/hr -> warning naming PHY 0
r2 = [sample(0, 100, inv=0), sample(3600, 3700, inv=100)]
ind, (st, why) = run(r2)
check("inv 100/hr is warning", ind["link_integrity"]["state"] == "warning" and "PHY 0" in why)
# any loss-of-sync -> warning even at <1/hr, printed with a decimal
r3 = [sample(0, 100), sample(7200, 7300, sync=1)]
ind, _ = run(r3)
check("sync 0.5/hr is warning", ind["link_integrity"]["state"] == "warning" and "0.5/hr" in ind["link_integrity"]["reason"])
# reboot (uptime goes down) wipes the ring
r4 = ctl.health_ingest([sample(0, 9000, inv=50)], sample(60, 30, inv=0))
check("reboot resets ring", len(r4) == 1)
# counter decrease without reboot (driver reload) wipes the ring
r5 = ctl.health_ingest([sample(0, 100, inv=50)], sample(60, 160, inv=3))
check("counter decrease resets ring", len(r5) == 1)
# drive missing -> critical
r6 = [sample(0, 100, drives=8), sample(3600, 3700, drives=7)]
ind, (st, why) = run(r6)
check("missing drive critical", st == "critical" and "7 of the 8" in why)
# x4 in an x16 slot with an x8 card -> downtrained warning
ind, _ = run([sample(0, 100, w=4)])
check("x4 of x8 downtrained", ind["host_link"]["state"] == "warning" and "x4" in ind["host_link"]["reason"])
# x8 card in x4 slot running x4 -> ok, slot's maximum
ind, _ = run([sample(0, 100, w=4, sw=4)])
check("slot-limited x4 is ok", ind["host_link"]["state"] == "ok" and "slot" in ind["host_link"]["reason"])
# stale sample -> controller unknown
ind, _ = run([sample(0, 100)], now=5000)
check("stale is unknown", ind["controller"]["state"] == "unknown")
# hot -> critical thermal; F conversion
CFG["TEMP_UNIT"] = "F"; ind, _ = run([sample(0, 100, temp=90, band="alert")]); CFG["TEMP_UNIT"] = "C"
check("alert band critical in F", ind["thermal"]["state"] == "critical" and ind["thermal"]["value"] == "194°F")
# baseline delta
d = ctl.baseline_delta({"ts": 0, "up": 100, "inv": 5, "disp": 0, "sync": 0, "reset": 0}, {"inv": 15, "disp": 0, "sync": 0, "reset": 0}, 3600, 3700)
check("baseline 10/hr", d and d["rate"]["inv"] == 10 and d["delta"]["inv"] == 10)
check("baseline reset on reboot", ctl.baseline_delta({"ts": 0, "up": 9999}, {}, 10, 50) == {"reset": True})
sys.exit(1 if fails else 0)
