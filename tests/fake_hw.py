"""Synthetic hardware for Bay Mapper tests: a fake /sys tree + an lsblk fixture.

build(root, topology) writes:
  root/sys/...            scsi_host, sas_device, sas_phy, block/<dev>/device symlinks,
                          laid out like the kernel does (PCI path / hostN / port / end_device / target)
  root/lsblk.json         what tests/stub/lsblk serves
  root/bsg/<hctl>         stand-ins for /dev/bsg nodes (only when bsg=True)

No real hardware is touched; point HBAV_SYSFS, HBAV_LSBLK_FIXTURE and HBAV_BSG_DIR at it.
"""
import json
import os

T = 1000 ** 4


def intel(n):
    return {"model": "INTEL SSDSC2KB960G8", "serial": f"BTYF8{n:05d}0Z960CGN", "wwn": f"0x55cd2e41{n:08x}",
            "size": 960197124096, "rota": False}


def seagate(n):
    return {"model": "ST16000NM001G-2KK103", "serial": f"ZL2{n:05d}", "wwn": f"0x5000c500{n:08x}",
            "size": 16 * T, "rota": True}


def lsi9300_16i():
    """The reference box: one SAS9300-16i = two SAS3008 functions with the same board serial."""
    sw = ["0000:00:03.0", "0000:03:00.0"]               # root port, PLX upstream port on the card
    c0 = {"host": 0, "pci_path": sw + ["0000:04:08.0", "0000:05:00.0"], "driver": "mpt3sas",
          "board_name": "SAS9300-16i", "serial": "SP73816973", "host_sas": "0x500062b202e76180", "fw": "16.00.12.00",
          "phys": [{"phy": p, "rate": "6.0 Gbit"} for p in range(8)],
          "drives": [dict(intel(i), name="sd" + "abcdefgh"[i], phy=i) for i in range(8)]}
    c1 = {"host": 1, "pci_path": sw + ["0000:04:10.0", "0000:07:00.0"], "driver": "mpt3sas",
          "board_name": "SAS9300-16i", "serial": "SP73816973", "host_sas": "0x500062b202e76900", "fw": "16.00.12.00",
          "phys": [{"phy": p, "rate": "6.0 Gbit" if p < 5 else "Unknown"} for p in range(8)],
          "drives": [dict(intel(8), name="sdi", phy=0)] + [dict(seagate(i), name="sd" + "jklm"[i], phy=i + 1) for i in range(4)]}
    return {"hosts": [c0, c1],
            "ata": [{"name": "sdn", "host": 4, "port": "ata1", "pci": ["0000:00:17.0"], "model": "Samsung SSD 870 EVO 500GB",
                     "serial": "S62ANJ0R123456", "wwn": "0x5002538f00000001", "size": 500107862016, "rota": False,
                     "parts": [{"fstype": "ext4", "mount": "/"}]}],
            "nvme": [{"name": "nvme0n1", "ctrl": "nvme0", "pci": ["0000:00:1d.0", "0000:01:00.0"],
                      "model": "Samsung SSD 980 PRO 1TB", "serial": "S5GXNX0T111111", "wwn": "eui.002538b111111111",
                      "size": 1000204886016}],
            "zfs": {"sdj": "tank", "sdk": "tank", "sdl": "tank", "sdm": "tank"}}


def two_unrelated():
    """Two separate cards: a 9207-8i (SAS2308) and a 9400-16i (SAS3416), different serials."""
    a = {"host": 0, "pci_path": ["0000:00:01.0", "0000:02:00.0"], "driver": "mpt3sas", "board_name": "SAS9207-8i",
         "serial": "SV41234567", "host_sas": "0x500605b001111111", "fw": "20.00.07.00",
         "phys": [{"phy": p, "rate": "6.0 Gbit" if p < 2 else "Unknown"} for p in range(8)],
         "drives": [dict(seagate(100), name="sda", phy=0), dict(seagate(101), name="sdb", phy=1)]}
    b = {"host": 1, "pci_path": ["0000:80:01.0", "0000:81:00.0"], "driver": "mpt3sas", "board_name": "HBA 9400-16i",
         "serial": "SP82222222", "host_sas": "0x500605b002222222", "fw": "24.00.00.00",
         "phys": [{"phy": p, "rate": "12.0 Gbit" if p < 1 else "Unknown"} for p in range(16)],
         "drives": [dict(intel(200), name="sdc", phy=0)]}
    return {"hosts": [a, b], "ata": [], "nvme": []}


def no_serial_dual():
    """A 9300-16i whose firmware reports no board serial: fallback grouping by shared PCIe slot."""
    t = lsi9300_16i()
    for h in t["hosts"]:
        h["serial"] = ""
    return t


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text) + "\n")


def _link(target, link):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(target, link)


def build(root, topo, bsg=True):
    sysd = os.path.join(root, "sys")
    blk = []
    parts = []
    for h in topo["hosts"]:
        H = h["host"]
        pci = os.path.join(sysd, "devices", "pci0000:00", *h["pci_path"])
        hostdir = os.path.join(pci, f"host{H}")
        sh = os.path.join(hostdir, "scsi_host", f"host{H}")
        for k, v in (("proc_name", h["driver"]), ("host_sas_address", h["host_sas"]), ("board_name", h["board_name"]),
                     ("board_tracer", h["serial"]), ("version_fw", h["fw"]), ("board_assembly", "")):
            _w(os.path.join(sh, k), v)
        _link(sh, os.path.join(sysd, "class", "scsi_host", f"host{H}"))
        for p in h["phys"]:
            _w(os.path.join(sysd, "class", "sas_phy", f"phy-{H}:{p['phy']}", "negotiated_linkrate"), p["rate"])
        for t, d in enumerate(h["drives"]):
            port = f"port-{H}:{d['phy']}"
            ed = f"end_device-{H}:{d['phy']}"
            lun = os.path.join(hostdir, port, ed, f"target{H}:0:{t}", f"{H}:0:{t}:0")
            os.makedirs(os.path.join(lun, "block", d["name"]), exist_ok=True)
            _link(lun, os.path.join(sysd, "block", d["name"], "device"))
            _w(os.path.join(sysd, "class", "sas_device", ed, "sas_address"), f"0x4433221{H:01x}{d['phy']:08x}")
            _w(os.path.join(sysd, "class", "sas_device", ed, "phy_identifier"), d["phy"])
            hctl = f"{H}:0:{t}:0"
            blk.append(dict(d, hctl=hctl, tran="sas"))
            if bsg:
                _w(os.path.join(root, "bsg", hctl), "")
    for a in topo.get("ata", []):
        H = a["host"]
        lun = os.path.join(sysd, "devices", "pci0000:00", *a["pci"], a["port"], f"host{H}", f"target{H}:0:0", f"{H}:0:0:0")
        os.makedirs(lun, exist_ok=True)
        _link(lun, os.path.join(sysd, "block", a["name"], "device"))
        sh = os.path.join(sysd, "devices", "pci0000:00", *a["pci"], a["port"], f"host{H}", "scsi_host", f"host{H}")
        _w(os.path.join(sh, "proc_name"), "ahci")
        _link(sh, os.path.join(sysd, "class", "scsi_host", f"host{H}"))
        blk.append(dict(a, hctl=f"{H}:0:0:0", tran="sata"))
        if bsg:
            _w(os.path.join(root, "bsg", f"{H}:0:0:0"), "")
    for n in topo.get("nvme", []):
        dev = os.path.join(sysd, "devices", "pci0000:00", *n["pci"], "nvme", n["ctrl"])
        os.makedirs(dev, exist_ok=True)
        _link(dev, os.path.join(sysd, "block", n["name"], "device"))
        blk.append(dict(n, hctl="", tran="nvme", rota=False))
    zfs = topo.get("zfs", {})
    for d in blk:
        if d["name"] in zfs:
            parts.append({"name": d["name"] + "1", "pkname": d["name"], "fstype": "zfs_member", "label": zfs[d["name"]]})
        for i, p in enumerate(d.get("parts", [])):
            parts.append({"name": d["name"] + str(i + 1), "pkname": d["name"], "fstype": p.get("fstype", ""),
                          "label": "", "mountpoint": p.get("mount", "")})
    fix = {"disks": [{"name": d["name"], "type": "disk", "size": d["size"], "model": d["model"], "serial": d["serial"],
                      "wwn": d.get("wwn"), "hctl": d["hctl"], "tran": d["tran"], "rota": d.get("rota"), "vendor": "ATA"}
                     for d in blk], "parts": parts}
    with open(os.path.join(root, "lsblk.json"), "w") as f:
        json.dump(fix, f)
    return fix


def unplug(root, name):
    """Simulate a drive pulled: gone from lsblk and from sysfs."""
    p = os.path.join(root, "lsblk.json")
    fix = json.load(open(p))
    fix["disks"] = [d for d in fix["disks"] if d["name"] != name]
    fix["parts"] = [x for x in fix["parts"] if x["pkname"] != name]
    json.dump(fix, open(p, "w"))
    link = os.path.join(root, "sys", "block", name)
    if os.path.lexists(os.path.join(link, "device")):
        os.unlink(os.path.join(link, "device"))


def swap_disk(root, name, new):
    """Same slot/link, different physical drive (a replacement)."""
    p = os.path.join(root, "lsblk.json")
    fix = json.load(open(p))
    for d in fix["disks"]:
        if d["name"] == name:
            d.update({k: new[k] for k in ("model", "serial", "wwn", "size")})
    json.dump(fix, open(p, "w"))


def rename_all(root, mapping):
    """Re-enumeration: every /dev name changes, hardware does not."""
    p = os.path.join(root, "lsblk.json")
    fix = json.load(open(p))
    for d in fix["disks"]:
        d["name"] = mapping.get(d["name"], d["name"])
    for x in fix["parts"]:
        x["pkname"] = mapping.get(x["pkname"], x["pkname"])
        x["name"] = mapping.get(x["name"][:-1], x["name"][:-1]) + x["name"][-1]
    json.dump(fix, open(p, "w"))
    sysb = os.path.join(root, "sys", "block")
    tmp = {}
    for old, new in mapping.items():
        if os.path.lexists(os.path.join(sysb, old)):
            tmp[new] = os.readlink(os.path.join(sysb, old, "device"))
            os.unlink(os.path.join(sysb, old, "device"))
    for new, target in tmp.items():
        _link(target, os.path.join(sysb, new, "device"))
