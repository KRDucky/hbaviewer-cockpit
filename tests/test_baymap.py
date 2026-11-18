"""Bay Mapper tests — synthetic sysfs + lsblk fixtures, the real hbaviewer-ctl, no hardware.

    python3 tests/test_baymap.py        (needs tests/env.sh's upstream scripts only for the
                                         controller-join and regression checks)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_hw  # noqa: E402

CTL = os.path.join(HERE, "..", "bin", "hbaviewer-ctl")
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or not detail else f"  -- {detail}"))
    fails += not cond


class Box:
    """One fake machine: its own /etc, /var/lib, /sys, lsblk and bsg."""

    def __init__(self, topo, bsg=True):
        self.root = tempfile.mkdtemp(prefix="bm-")
        fake_hw.build(self.root, topo, bsg=bsg)
        self.env = dict(os.environ, HBAV_ALLOW_NONROOT="1", HBAV_ETC=os.path.join(self.root, "etc"),
                        HBAV_STATE=os.path.join(self.root, "state"), HBAV_RUN=os.path.join(self.root, "run"),
                        HBAV_SYSFS=os.path.join(self.root, "sys"), HBAV_LSBLK_FIXTURE=os.path.join(self.root, "lsblk.json"),
                        HBAV_BSG_DIR=os.path.join(self.root, "bsg"), HBAV_NO_SYSTEMD="1",
                        PATH=os.path.join(HERE, "stub") + ":" + os.environ.get("PATH", ""))
        self.env.setdefault("HBAV_SCRIPTS", os.path.join(self.root, "no-scripts"))

    def ctl(self, *args, req=None, env=None):
        p = subprocess.run([sys.executable, CTL, *args], input=json.dumps(req) if req is not None else "",
                           capture_output=True, text=True, env=dict(self.env, **(env or {})), timeout=60)
        try:
            return json.loads(p.stdout)
        except ValueError:
            raise AssertionError(f"non-JSON from {args}: {p.stdout!r} {p.stderr!r}")

    def bm(self, sub, req=None, **kw):
        return self.ctl("baymap", sub, req=req, **kw)

    def get(self):
        return self.bm("get", None) if False else self.ctl("baymap", "get", "--fast")

    def rev(self):
        return self.get()["revision"]

    def mut(self, sub, req):
        req = dict(req, revision=self.rev())
        return self.bm(sub, req)

    @property
    def conf(self):
        return os.path.join(self.root, "etc", "baymap.json")

    def done(self):
        shutil.rmtree(self.root, ignore_errors=True)


def drive_key(g, dev):
    return next(d["key"] for d in g["inventory"]["drives"] if d["dev"] == dev)


def bay_at(g, enc_id, r, c):
    e = next(e for e in g["enclosures"] if e["id"] == enc_id)
    return next(b["id"] for b in e["bays"] if b["row"] == r and b["col"] == c)


# ── reference box: one 9300-16i with two SAS3008 functions ─────────────────
box = Box(fake_hw.lsi9300_16i())
g = box.get()
inv = g["inventory"]
check("discovery is read-only and complete (13 HBA drives + SATA + NVMe)",
      len([d for d in inv["drives"] if d["conn"]["kind"] == "sas"]) == 13 and len(inv["drives"]) == 15)
hbas = inv["hbas"]
check("one physical card grouped from two SAS3008 functions by board serial",
      len(hbas) == 1 and hbas[0]["grouped_by"] == "board serial" and len(hbas[0]["functions"]) == 2
      and hbas[0]["board_serial"] == "SP73816973")
f0, f1 = hbas[0]["functions"]
check("functions keep their own PCI + SAS addresses",
      (f0["pci"], f0["host_sas"], f1["pci"], f1["host_sas"]) == ("0000:05:00.0", "500062b202e76180", "0000:07:00.0", "500062b202e76900"))
check("16 PHYs, 13 linked, 3 idle — idle PHYs are not failures",
      hbas[0]["phys_total"] == 16 and hbas[0]["phys_linked"] == 13 and g["status"]["counts"] == {})
check("no bays and no errors on an empty config", g["enclosures"] == [] and g["revision"] == 0 and "error" not in g)
check("dev names are context, keys are WWN-based", all(d["key"].startswith("wwn:") for d in inv["drives"]))

# create enclosures
r = box.mut("create", {"enclosure": {"name": "Icy Dock 8x2.5", "rows": 2, "cols": 4, "icon": "cage", "face": "front",
                                     "connection": "/c0 via 2x SFF-8643 breakout", "numbering": {"order": "row", "start": 1, "prefix": "S"}}})
check("create enclosure", r.get("ok") and r.get("enclosure_id", "").startswith("e"), r)
icy = r["enclosure_id"]
r = box.mut("create", {"enclosure": {"name": "Rosewill hot-swap", "rows": 4, "cols": 1, "orientation": "vertical",
                                     "absent": [[3, 0]], "color": "#336699"}})
rose = r["enclosure_id"]
g = box.get()
e_icy = next(e for e in g["enclosures"] if e["id"] == icy)
e_rose = next(e for e in g["enclosures"] if e["id"] == rose)
check("rows x cols cells with stable bay IDs", len(e_icy["bays"]) == 8 and len({b["id"] for b in e_icy["bays"]}) == 8)
check("nonexistent cell recorded", sum(b["absent"] for b in e_rose["bays"]) == 1
      and g["status"]["bays"][bay_at(g, rose, 3, 0)]["state"] == "nonexistent")
check("bay labels from numbering (prefix + row order)", g["status"]["bays"][bay_at(g, icy, 1, 0)]["label"] == "S5")

# invalid input
check("reject bad colour", "error" in box.mut("create", {"enclosure": {"name": "x", "rows": 1, "cols": 1, "color": "red;"}}))
check("reject oversized grid", "error" in box.mut("create", {"enclosure": {"name": "x", "rows": 65, "cols": 1}}))
check("reject empty name", "error" in box.mut("create", {"enclosure": {"name": "  ", "rows": 1, "cols": 1}}))
check("reject control chars beyond limits", "error" in box.mut("create", {"enclosure": {"name": "a" * 65, "rows": 1, "cols": 1}}))
check("reject unknown enclosure id / path-like id", box.mut("update", {"id": "../../etc/passwd", "changes": {}})["error"] == "Bad enclosure id")

# assign
k_a, k_b, k_j = drive_key(g, "/dev/sda"), drive_key(g, "/dev/sdb"), drive_key(g, "/dev/sdj")
s1, s2, s3 = bay_at(g, icy, 0, 0), bay_at(g, icy, 0, 1), bay_at(g, icy, 0, 2)
r1 = bay_at(g, rose, 0, 0)
check("assign drive to empty bay", box.mut("assign", {"bay_id": s1, "drive": k_a}).get("ok"))
dup = box.mut("assign", {"bay_id": s2, "drive": k_a})
check("duplicate identity rejected", dup.get("code") == "duplicate" and dup.get("bay") == s1, dup)
occ = box.mut("assign", {"bay_id": s1, "drive": k_b})
check("occupied bay rejected (swap/replace instead)", occ.get("code") == "occupied", occ)
check("cannot assign into a nonexistent cell", "error" in box.mut("assign", {"bay_id": bay_at(g, rose, 3, 0), "drive": k_b}))
box.mut("assign", {"bay_id": s2, "drive": k_b})
box.mut("assign", {"bay_id": r1, "drive": k_j, "note": "tank vdev 0"})
g = box.get()
st = g["status"]["bays"]
check("connected state for detected drives", st[s1]["state"] == st[s2]["state"] == st[r1]["state"] == "connected")
check("assignment stores WWN + serial + model + size, not dev", (lambda a: a["drive"]["wwn"] and a["drive"]["serial"].startswith("BTYF8")
      and a["drive"]["size_bytes"] == 960197124096 and "dev" not in a["drive"])(g["assignments"][s1]))
check("controller link kept as evidence", g["assignments"][s1]["hint"]["pci"] == "0000:05:00.0" and g["assignments"][s1]["hint"]["phy"] == 0)

# move / swap
check("move to empty bay", box.mut("move", {"from": s2, "to": s3}).get("ok") and box.get()["assignments"].get(s3))
check("move onto occupied bay refused", box.mut("move", {"from": s3, "to": s1}).get("code") == "occupied")
check("swap needs explicit confirmation", box.mut("swap", {"a": s1, "b": s3}).get("code") == "confirm_required")
check("swap with confirmation", box.mut("swap", {"a": s1, "b": s3, "confirm": True}).get("ok"))
g = box.get()
check("swap exchanged identities", g["assignments"][s1]["drive"]["wwn"] == k_b[4:] and g["assignments"][s3]["drive"]["wwn"] == k_a[4:])

# resize without silently losing mappings
r = box.mut("update", {"id": icy, "changes": {"rows": 1, "cols": 2}})
check("shrinking over a mapped bay is refused and names it", r.get("code") == "would_orphan" and s3 in r.get("bays", []), r)
r = box.mut("update", {"id": icy, "changes": {"rows": 1, "cols": 2}, "park": True})
g = box.get()
parked = [b for b in next(e for e in g["enclosures"] if e["id"] == icy)["bays"] if b["row"] is None]
check("park keeps the bay ID and its mapping", r.get("ok") and [b["id"] for b in parked] == [s3] and s3 in g["assignments"])
check("a parked bay keeps the label it had in the grid", parked[0]["label"] == "S3")
check("grow adds new bays, existing IDs unchanged", box.mut("update", {"id": icy, "changes": {"rows": 2, "cols": 4}}).get("ok")
      and bay_at(box.get(), icy, 0, 0) == s1)
check("marking a mapped cell nonexistent is refused", box.mut("update", {"id": icy, "cells": [{"id": s1, "absent": True}]}).get("code") == "occupied")
s3new = bay_at(box.get(), icy, 0, 2)
box.mut("move", {"from": s3, "to": s3new})
check("parked bay deletable once empty", box.mut("delete-parked", {"bay_id": s3}).get("ok"))
s3 = s3new
check("per-cell label + note", box.mut("update", {"id": icy, "cells": [{"id": s1, "label": "Top-left", "note": "fan side"}]}).get("ok")
      and box.get()["status"]["bays"][s1]["label"] == "Top-left")

# revision conflict / concurrency
stale = box.rev()
box.mut("update", {"id": rose, "changes": {"description": "lower cage"}})
r = box.bm("update", {"id": rose, "changes": {"description": "lost update"}, "revision": stale})
check("stale revision rejected (simultaneous editors)", r.get("code") == "conflict" and r.get("current") == stale + 1, r)
rev = box.rev()
results = []


def racer(i):
    results.append(box.bm("update", {"id": rose, "changes": {"description": f"racer {i}"}, "revision": rev}))


ts = [threading.Thread(target=racer, args=(i,)) for i in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
check("concurrent writers: exactly one wins, the rest get a conflict",
      sum(1 for x in results if x.get("ok")) == 1 and sum(1 for x in results if x.get("code") == "conflict") == 5, results)
check("file still valid JSON after the race", json.load(open(box.conf))["revision"] == rev + 1)
check("no temp files left behind", not [f for f in os.listdir(os.path.dirname(box.conf)) if ".tmp." in f])
check("config file is not world-readable", oct(os.stat(box.conf).st_mode & 0o777) == "0o640")

# persistence across re-enumeration: every /dev name changes
before = {b: s["state"] for b, s in box.get()["status"]["bays"].items()}
fake_hw.rename_all(box.root, {"sda": "sdx", "sdb": "sdy", "sdj": "sdz"})
g = box.get()
check("device renumbering does not break mappings", all(g["status"]["bays"][b]["state"] == before[b] for b in before)
      and next(d for d in g["inventory"]["drives"] if d["key"] == k_a)["dev"] == "/dev/sdx")

# controller identity change: same drive, different PHY/controller
fix = json.load(open(box.env["HBAV_LSBLK_FIXTURE"]))
link = os.path.join(box.root, "sys", "block", "sdz", "device")
tgt = os.readlink(link)
os.unlink(link)
newlun = tgt.replace("0000:07:00.0", "0000:05:00.0").replace("host1", "host0").replace("1:0:1:0", "0:0:9:0") \
    .replace("port-1:1", "port-0:6").replace("end_device-1:1", "end_device-0:6").replace("target1:0:1", "target0:0:9")
os.makedirs(newlun, exist_ok=True)
os.symlink(newlun, link)
g = box.get()
check("drive moved to another controller/PHY stays connected, flagged as moved",
      g["status"]["bays"][r1]["state"] == "connected" and "different controller link" in g["status"]["bays"][r1].get("moved", ""))
check("relink accepts the new link only when asked", box.mut("relink", {"bay_id": r1}).get("ok")
      and box.get()["assignments"][r1]["hint"]["pci"] == "0000:05:00.0" and "moved" not in box.get()["status"]["bays"][r1])

# missing vs empty
fake_hw.unplug(box.root, "sdy")
g = box.get()
check("missing drive distinguished from empty bay", g["status"]["bays"][s1]["state"] == "missing"
      and g["status"]["bays"][bay_at(g, icy, 1, 3)]["state"] == "empty")
check("missing bay shows last-seen", isinstance(g["status"]["bays"][s1].get("last_seen"), int))

# replacement: different drive on the same controller link
box2 = Box(fake_hw.lsi9300_16i())
box2.mut("create", {"enclosure": {"name": "Cage", "rows": 1, "cols": 4}})
g2 = box2.get()
e2 = g2["enclosures"][0]["id"]
b0 = bay_at(g2, e2, 0, 0)
old_key = drive_key(g2, "/dev/sdj")
box2.mut("assign", {"bay_id": b0, "drive": old_key})
fake_hw.swap_disk(box2.root, "sdj", fake_hw.seagate(77))
g2 = box2.get()
st = g2["status"]["bays"][b0]
new_key = "wwn:5000c5000000004d"
check("new drive on old link -> 'changed', not auto-replaced", st["state"] == "changed" and st["candidate"] == new_key
      and g2["assignments"][b0]["drive"]["wwn"] == old_key[4:], st)
check("replace needs confirmation", box2.mut("replace", {"bay_id": b0, "drive": new_key}).get("code") == "confirm_required")
r = box2.mut("replace", {"bay_id": b0, "drive": new_key, "confirm": True, "note": "RMA ZL200000"})
g2 = box2.get()
check("confirmed replacement updates the mapping", r.get("ok") and g2["assignments"][b0]["drive"]["wwn"] == new_key[4:]
      and g2["status"]["bays"][b0]["state"] == "connected")
h = box2.bm("history", {"bay_id": b0})["history"]
rep = next((x for x in h if x["action"] == "replace"), None)
check("history keeps old + new identity, timestamp, note", rep and rep["old"]["wwn"] == old_key[4:] and rep["new"]["wwn"] == new_key[4:]
      and rep["note"] == "RMA ZL200000" and rep["ts"] > 0 and rep["confirmed"] is True)
check("history lists the original assignment too", [x["action"] for x in h] == ["replace", "assign"])

# an unrelated new drive does not mark anything replaced
box3 = Box(fake_hw.lsi9300_16i())
box3.mut("create", {"enclosure": {"name": "Cage", "rows": 1, "cols": 2}})
g3 = box3.get()
e3 = g3["enclosures"][0]["id"]
b3 = bay_at(g3, e3, 0, 0)
box3.mut("assign", {"bay_id": b3, "drive": drive_key(g3, "/dev/sdc")})
fake_hw.unplug(box3.root, "sdc")
fix = json.load(open(box3.env["HBAV_LSBLK_FIXTURE"]))
fix["disks"].append({"name": "sdq", "type": "disk", "size": 4 * 10 ** 12, "model": "USB DISK", "serial": "NEWUSB0001",
                     "wwn": None, "hctl": "9:0:0:0", "tran": "usb", "rota": True, "vendor": "x"})
json.dump(fix, open(box3.env["HBAV_LSBLK_FIXTURE"], "w"))
g3 = box3.get()
check("unrelated new device: bay stays 'missing', newcomer listed as new + unassigned",
      g3["status"]["bays"][b3]["state"] == "missing" and any(u["key"] == "sn:NEWUSB0001" and u["new"] for u in g3["status"]["unassigned"]))

# serial-only and contradictory identities
box4 = Box(fake_hw.two_unrelated())
box4.mut("create", {"enclosure": {"name": "A", "rows": 1, "cols": 3}})
g4 = box4.get()
e4 = g4["enclosures"][0]["id"]
check("multiple unrelated HBAs stay separate cards", len(g4["inventory"]["hbas"]) == 2 and all(x["grouped_by"] is None for x in g4["inventory"]["hbas"]))
fix = json.load(open(box4.env["HBAV_LSBLK_FIXTURE"]))
fix["disks"][0]["wwn"] = None                       # this drive reports no WWN
fix["disks"][1]["wwn"] = fix["disks"][2]["wwn"]     # two devices report the same WWN (bridge)
json.dump(fix, open(box4.env["HBAV_LSBLK_FIXTURE"], "w"))
g4 = box4.get()
d0, d1 = g4["inventory"]["drives"][0], g4["inventory"]["drives"][1]
check("serial matching when no WWN", d0["key"].startswith("sn:"))
check("shared WWN is not trusted, falls back to serial", d1["key"].startswith("sn:") and not d1["wwn_reliable"] and d1["identity_issues"])
b40 = bay_at(g4, e4, 0, 0)
box4.mut("assign", {"bay_id": b40, "drive": d0["key"]})
check("serial-only mapping connected", box4.get()["status"]["bays"][b40]["state"] == "connected")
# make the stored mapping contradict: give the drive a WWN that belongs to another device's serial
cfg = json.load(open(box4.conf))
cfg["assignments"][b40]["drive"]["wwn"] = norm = g4["inventory"]["drives"][2]["wwn"]
json.dump(cfg, open(box4.conf, "w"))
check("WWN/serial contradiction -> conflict, not a guess", box4.get()["status"]["bays"][b40]["state"] == "conflict")
fix["disks"].append(dict(fix["disks"][0], name="sdz", hctl="5:0:0:0"))      # identical serial twice
json.dump(fix, open(box4.env["HBAV_LSBLK_FIXTURE"], "w"))
g4 = box4.get()
check("duplicate serial makes the device unmappable", all(d["key"] is None for d in g4["inventory"]["drives"] if d["serial"] == fix["disks"][0]["serial"]))

# fallback grouping without serials, and refusal cases
box5 = Box(fake_hw.no_serial_dual())
h5 = box5.get()["inventory"]["hbas"]
check("dual-IOC board without serial grouped by shared PCIe slot", len(h5) == 1 and h5[0]["grouped_by"].startswith("shared PCIe slot"))
sys.path.insert(0, os.path.join(HERE, "..", "bin"))
import hbav_baymap as bmod  # noqa: E402


def fn(name, serial, path):
    return {"pci": path[-1], "board_name": name, "board_serial": serial, "pci_path": path, "phys_total": 8, "phys_linked": 0, "drives": 0}


sw = ["0000:00:03.0", "0000:03:00.0"]
g8 = bmod.group_functions([fn("SAS9300-8i", "", sw + ["0000:04:08.0", "0000:05:00.0"]), fn("SAS9300-8i", "", sw + ["0000:04:10.0", "0000:07:00.0"])])
check("two single-IOC cards behind one switch are NOT grouped", len(g8) == 2)
g3x = bmod.group_functions([fn("SAS9300-16i", "", sw + [f"0000:04:0{i}.0", f"0000:0{5 + i}:00.0"]) for i in range(3)])
check("three functions on a board declaring two are NOT grouped", len(g3x) == 3)
gm = bmod.group_functions([fn("SAS9300-16i", "SP1", ["0000:00:01.0", "0000:05:00.0"]), fn("SAS9300-8i", "SP1", ["0000:00:02.0", "0000:06:00.0"])])
check("same serial but different board names are NOT grouped", len(gm) == 2)

# controller evidence join (storcli/lsiutil listing -> Linux disks): serial first, never by slot alone
drv = [{"dev": "/dev/sda", "serial": "ZL200001", "wwn": "5000c500aaaa0001", "conn": {}},
       {"dev": "/dev/sdb", "serial": "ZL200002", "wwn": "5000c500bbbb0002", "conn": {}},
       {"dev": "/dev/sdc", "serial": "ZL200003", "wwn": "5000c500cccc0003", "conn": {}},
       {"dev": "/dev/sdd", "serial": "ZL200004", "wwn": "5000c500cccc0004", "conn": {}}]
bmod._join_controller(drv, {"controllers": [{"drives": [{"serial": "ZL200002", "slot": "0/5", "port": "5", "state": "JBOD"}]},
                                            {"drives": [{"serial": "XXXXXXXX", "slot": "0/1", "sas_address": "5000c500aaaa0001"},
                                                        {"serial": "XXXXXXXX", "slot": "0/2", "sas_address": "5000c500cccc0009"}]}]})
check("controller join by serial gives /cN + slot as evidence", drv[1]["conn"].get("ctl_index") == 0 and drv[1]["conn"]["slot"] == "0/5")
check("placeholder serial falls back to WWN prefix, not to the slot", drv[0]["conn"].get("ctl_index") == 1)
check("an ambiguous WWN prefix joins nothing rather than guessing", "ctl_index" not in drv[2]["conn"] and "ctl_index" not in drv[3]["conn"])
ov = Box(fake_hw.lsi9300_16i())
o = ov.ctl("overview")
check("overview carries grouped physical cards without breaking its payload",
      "controllers" in o and isinstance(o.get("physical"), list) and len(o["physical"]) == 1 and len(o["physical"][0]["functions"]) == 2)
ov.done()

# locate integration
g = box2.get()
r = box2.bm("locate", {"bay_id": b0, "action": "start"})
check("locate verifies the bay's current drive and reuses the existing locate helper", r.get("ok") and r["verified"]["serial"] == "ZL200077"
      and r["verified"]["hctl"] == "1:0:1:0" and r["secs"] == 300, r)
check("locate says no LED is driven (activity pulses only)", r.get("mechanism") == "activity" and "No locate LED" in r.get("explain", ""))
os.unlink(os.path.join(box2.root, "bsg", g["inventory"]["drives"][9]["conn"]["hctl"]))
r = box2.bm("locate", {"bay_id": b0, "action": "start"})
check("missing /dev/bsg node -> accurate 'unsupported' error", r.get("code") == "unsupported" and "/bsg" in r.get("error", ""), r)
bn = bay_at(g, e2, 0, 1)
box2.mut("assign", {"bay_id": bn, "drive": drive_key(g, "/dev/nvme0n1")})
r = box2.bm("locate", {"bay_id": bn, "action": "start"})
check("NVMe locate is reported unsupported, never claimed", r.get("code") == "unsupported", r)
fake_hw.unplug(box2.root, "nvme0n1")
check("locate refuses a missing drive", box2.bm("locate", {"bay_id": bn, "action": "start"}).get("code") == "not_connected")
check("viewing never starts a locate", not os.path.exists(os.path.join(box.root, "run", "hbaviewer-locate")) and box.get()["locating"] == [])

# export / import
exp = box2.bm("export")
check("export has format + schema", exp["format"] == "hbaviewer-baymap" and exp["schema"] == 1 and exp["config"]["enclosures"])
prev = box2.bm("import-preview", {"data": exp, "mode": "merge"})
check("re-importing the same export conflicts on IDs instead of overwriting", prev["plan"]["conflicts"] and not prev["plan"]["add"], prev)
other = Box(fake_hw.lsi9300_16i())
other.mut("create", {"enclosure": {"name": "Cage", "rows": 2, "cols": 2}})
pv = other.bm("import-preview", {"data": exp, "mode": "merge"})["plan"]
check("same enclosure name elsewhere is a warning, added separately", pv["add"] and any("same name" in w for w in pv["warnings"]))
r = other.bm("import", {"data": exp, "mode": "merge", "revision": other.rev()})
og = other.get()
check("merge import adds enclosures + mappings, keeps existing", r.get("ok") and len(og["enclosures"]) == 2
      and sum(1 for e in og["enclosures"] if e["name"] == "Cage") == 2 and len(og["assignments"]) == 2)
bad = dict(exp, format="something-else")
check("import rejects foreign files", "error" in other.bm("import-preview", {"data": bad}))
evil = json.loads(json.dumps(exp))
evil["config"]["enclosures"][0]["id"] = "../../../etc/shadow"
check("import rejects path-like / malformed IDs", "error" in other.bm("import-preview", {"data": evil}))
newer = dict(exp, schema=99)
check("import rejects a newer schema", "newer" in other.bm("import-preview", {"data": newer}).get("error", ""))
check("replace import needs confirmation", other.bm("import", {"data": exp, "mode": "replace", "revision": other.rev()}).get("code") == "confirm_required")

# backup / restore with history
bk = box2.bm("backup")
check("backup includes history", bk.get("history") and any(x["action"] == "replace" for x in bk["history"]))
fresh = Box(fake_hw.lsi9300_16i())
r = fresh.bm("restore", {"data": bk, "confirm": True, "revision": fresh.rev()})
fg = fresh.get()
check("restore brings back layout, mappings and history", r.get("ok") and len(fg["enclosures"]) == 1 and len(fg["assignments"]) == 2
      and any(x["action"] == "replace" for x in fresh.bm("history", {})["history"]))

# corruption
open(box2.conf, "w").write('{"schema": 1, "enclosures": [ truncated')
g = box2.get()
check("corrupted config detected, never overwritten", g.get("corrupt") and g["backups"] and open(box2.conf).read().endswith("truncated"))
check("mutations refuse to run on a corrupt file", box2.bm("assign", {"bay_id": b0, "drive": new_key, "revision": 1}).get("code") == "corrupt")
good = next(b for b in g["backups"] if b["valid"])
r = box2.bm("recover", {"action": "restore_backup", "backup": good["name"]})
check("recover restores a backup and keeps the damaged file", r.get("ok") and os.path.exists(r["kept"]) and "error" not in box2.get())
check("recover rejects a path in the backup name", "error" in (lambda: (open(box2.conf, "w").write("{"),
      box2.bm("recover", {"action": "restore_backup", "backup": "../../etc/passwd"}))[1])())

# schema migration from the draft (schema 0) layout
mig = Box(fake_hw.lsi9300_16i())
os.makedirs(os.path.dirname(mig.conf), exist_ok=True)
json.dump({"enclosures": [{"name": "Old cage", "rows": 1, "cols": 2,
                           "slots": {"0,1": {"serial": "BTYF8000020Z960CGN", "wwn": "0x55cd2e4100000002"}}}]}, open(mig.conf, "w"))
gm = mig.get()
check("schema-0 file migrates on read (IDs assigned, mapping kept)", gm.get("migration_notes") and len(gm["enclosures"][0]["bays"]) == 2
      and list(gm["status"]["counts"].items()) and gm["status"]["counts"].get("connected") == 1, gm.get("status", {}).get("counts"))
check("migrated file is written as schema 1 on the next change", mig.mut("update", {"id": gm["enclosures"][0]["id"], "changes": {"name": "Old cage"}}).get("ok")
      and json.load(open(mig.conf))["schema"] == 1)

# discovery failure -> unknown, not missing
r = box.ctl("baymap", "get", "--fast", env={"HBAV_LSBLK_FAIL": "1"})
check("failed discovery marks mapped bays 'unknown', never 'missing'",
      all(s["state"] in ("unknown", "empty", "nonexistent") for s in r["status"]["bays"].values())
      and any(s["state"] == "unknown" for s in r["status"]["bays"].values()))

# security
p = subprocess.run([sys.executable, CTL, "baymap", "create"], input="x" * (2 * 1024 * 1024 + 10), capture_output=True, text=True, env=box.env)
check("request size limit enforced", "too large" in json.loads(p.stdout).get("error", ""))
check("non-object request rejected", "error" in json.loads(subprocess.run([sys.executable, CTL, "baymap", "create"], input="[1,2]",
                                                                           capture_output=True, text=True, env=box.env).stdout))
check("bad drive key rejected", box.mut("assign", {"bay_id": s1, "drive": "$(reboot)"}).get("error") == "Bad drive key")
r = box.mut("update", {"id": icy, "cells": [{"id": s1, "label": "<img src=x onerror=alert(1)>"}]})
check("labels stored as plain text (rendered via textContent in the UI)", r.get("ok"))
noroot = subprocess.run([sys.executable, CTL, "baymap", "get"], capture_output=True, text=True,
                        env={k: v for k, v in box.env.items() if k != "HBAV_ALLOW_NONROOT"})
check("non-root refused", os.geteuid() == 0 or "must run as root" in noroot.stdout)
locs = box.bm("locations")["locations"]
check("locations lookup for the Drives tab (dev -> enclosure/bay)", locs.get("/dev/sdx", {}).get("bay_id") == s3 and locs["/dev/sdx"]["enclosure"] == "Icy Dock 8x2.5", locs)

for b in (box, box2, box3, box4, box5, other, fresh, mig):
    b.done()
print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
