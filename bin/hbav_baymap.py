"""Physical Bay Mapper for HBA Viewer — backend model, discovery and persistence.

Loaded by hbaviewer-ctl for the `baymap` command family. Three identities are
kept strictly apart:

  * physical bay      — enclosure ID + bay ID (random, permanent) + a user label
  * controller link   — controller PCI address, host SAS address, PHY, expander,
                        storcli /cN + slot when known, HCTL; used only as EVIDENCE
  * drive             — WWN (when reliable), manufacturer serial, model, capacity

/dev/sdX is never an identifier: it is shown, and recorded as context, but no
decision is made from it. A storcli slot is never taken to be a front-panel bay.

Nothing here touches the disks. Discovery reads lsblk and sysfs, plus the same
read-only storcli/lsiutil drive listing the Drives tab already uses. The only
hardware action is Locate, and only when the user asks for it.

Files:
  /etc/hbaviewer/baymap.json              enclosures + assignments (authoritative)
  /var/lib/hbaviewer/baymap-history.json  assignment/replacement history
  /var/lib/hbaviewer/baymap-seen.json     first/last-seen per drive identity
  /var/lib/hbaviewer/baymap-backups/      the last copies of baymap.json
"""
import datetime
import json
import os
import re
import secrets
import socket
import time

SCHEMA = 1
FORMAT = "hbaviewer-baymap"
MAX_INPUT = 2 * 1024 * 1024
MAX_ENCLOSURES = 100
MAX_DIM = 64
MAX_BAYS_PER_ENCLOSURE = 1024
MAX_HISTORY = 10000
MAX_SEEN = 5000
KEEP_BACKUPS = 10
NEW_DRIVE_SECS = 86400

ICONS = ("", "hdd", "ssd", "server", "jbod", "cage")
FACES = ("", "front", "rear", "top", "bottom")
ORIENT = ("horizontal", "vertical")
ORDERS = ("row", "column")
ID_RE = re.compile(r"^[eb][0-9a-f]{10}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
HCTL_RE = re.compile(r"^\d+:\d+:\d+:\d+$")
BDF_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
SERIAL_PLACEHOLDERS = {"", "0", "00000000", "0000000000", "000000000000", "NONE", "N/A", "NA",
                       "TO BE FILLED BY O.E.M.", "DEFAULT STRING", "UNKNOWN", "XXXXXXXX", "123456789"}
# Boards that carry two controller chips. Used only for the fallback grouping
# (no board serial): two controllers behind one PCIe slot with one of these
# names and EXACTLY this count are one card. Anything else stays separate.
DUAL_IOC_BOARDS = {"9300-16i": 2, "9300-16e": 2, "9202-16e": 2}


class BayError(Exception):
    def __init__(self, msg, code="invalid", **extra):
        super().__init__(msg)
        self.code = code
        self.extra = extra


class Corrupt(Exception):
    pass


# ── context: paths and helpers handed over by hbaviewer-ctl ────────────────

class Ctx:
    def __init__(self, etc, state, sh, run_script, block_context, lock, locate_start, locate_stop,
                 locate_active, settings):
        self.etc, self.state = etc, state
        self.sh, self.run_script, self.block_context = sh, run_script, block_context
        self.lock, self.locate_start, self.locate_stop = lock, locate_start, locate_stop
        self.locate_active, self.settings = locate_active, settings
        self.conf = os.path.join(etc, "baymap.json")
        self.hist = os.path.join(state, "baymap-history.json")
        self.seen = os.path.join(state, "baymap-seen.json")
        self.backups = os.path.join(state, "baymap-backups")
        self.sysfs = os.environ.get("HBAV_SYSFS", "/sys")
        self.bsg = os.environ.get("HBAV_BSG_DIR", "/dev/bsg")


# ── small utilities ─────────────────────────────────────────────────────────

def now():
    return int(time.time())


def new_id(prefix):
    return prefix + secrets.token_hex(5)


def clean_str(v, limit, field):
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)):
        raise BayError(f"{field} must be text")
    v = str(v)
    v = re.sub(r"[\x00-\x1f\x7f]", " ", v).strip()
    if len(v) > limit:
        raise BayError(f"{field} is longer than {limit} characters")
    return v


def as_int(v, lo, hi, field):
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise BayError(f"{field} must be a whole number")
    if not lo <= i <= hi:
        raise BayError(f"{field} must be between {lo} and {hi}")
    return i


def norm_wwn(w):
    w = (w or "").strip().lower()
    w = re.sub(r"^(0x|naa\.|eui\.|wwn-0x)", "", w)
    return w if re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", w) else ""


def norm_serial(s):
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def wwn_plausible(w):
    return bool(w) and set(w) - {"0"} and set(w) - {"f"}


def serial_plausible(s):
    return norm_serial(s) not in SERIAL_PLACEHOLDERS and len(norm_serial(s)) >= 4


def fsync_write(path, obj, mode=0o640):
    """Atomic write: unique temp file in the same directory, fsync, rename, fsync dir."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass


def read_json_strict(path):
    with open(path) as f:
        return json.load(f)


# ── configuration model ─────────────────────────────────────────────────────

def empty_config():
    return {"schema": SCHEMA, "revision": 0, "updated": 0, "enclosures": [], "assignments": {}}


def migrate(cfg):
    """Bring an older document up to SCHEMA. Returns (cfg, list of notes)."""
    notes = []
    if not isinstance(cfg, dict):
        raise Corrupt("top level is not an object")
    ver = cfg.get("schema")
    if ver is None:
        # Schema 0: the pre-release draft layout — enclosures carried a "slots"
        # dict of "row,col" -> {"serial":..,"wwn":..} and no bay IDs.
        out = empty_config()
        out["revision"] = int(cfg.get("revision") or 0)
        for e in cfg.get("enclosures") or []:
            enc = {"id": new_id("e"), "name": str(e.get("name") or "Enclosure")[:64], "description": "",
                   "rows": int(e.get("rows") or 1), "cols": int(e.get("cols") or 1), "orientation": "horizontal",
                   "numbering": {"order": "row", "start": 1, "prefix": ""}, "color": "", "icon": "",
                   "face": "", "connection": "", "order": len(out["enclosures"]), "bays": []}
            for r in range(enc["rows"]):
                for c in range(enc["cols"]):
                    b = {"id": new_id("b"), "row": r, "col": c, "label": "", "absent": False, "note": "", "connection": ""}
                    enc["bays"].append(b)
                    slot = (e.get("slots") or {}).get(f"{r},{c}")
                    if isinstance(slot, dict) and (slot.get("serial") or slot.get("wwn")):
                        out["assignments"][b["id"]] = {
                            "drive": {"wwn": norm_wwn(slot.get("wwn")), "serial": norm_serial(slot.get("serial")),
                                      "model": str(slot.get("model") or ""), "size_bytes": None, "type": ""},
                            "hint": {}, "assigned_at": 0, "note": "migrated from schema 0"}
            out["enclosures"].append(enc)
        notes.append("migrated from schema 0 (draft layout); bay IDs were assigned")
        cfg = out
        ver = 1
    if not isinstance(ver, int) or ver > SCHEMA:
        raise Corrupt(f"schema {ver!r} is newer than this version of HBA Viewer understands ({SCHEMA})")
    # future: `if ver == 1: ... ver = 2`
    cfg["schema"] = SCHEMA
    return cfg, notes


def validate_config(cfg):
    """Structural check of a whole document (stored file or import). Raises Corrupt."""
    try:
        if not isinstance(cfg.get("enclosures"), list) or not isinstance(cfg.get("assignments"), dict):
            raise Corrupt("enclosures/assignments missing")
        if len(cfg["enclosures"]) > MAX_ENCLOSURES:
            raise Corrupt("too many enclosures")
        ids, bays = set(), {}
        for e in cfg["enclosures"]:
            if not ID_RE.match(str(e.get("id", ""))) or not e["id"].startswith("e") or e["id"] in ids:
                raise Corrupt(f"bad or duplicate enclosure id {e.get('id')!r}")
            ids.add(e["id"])
            rows, cols = int(e["rows"]), int(e["cols"])
            if not (1 <= rows <= MAX_DIM and 1 <= cols <= MAX_DIM and rows * cols <= MAX_BAYS_PER_ENCLOSURE):
                raise Corrupt(f"enclosure {e['id']} has an invalid size")
            cells = set()
            for b in e.get("bays") or []:
                if not ID_RE.match(str(b.get("id", ""))) or not b["id"].startswith("b") or b["id"] in bays:
                    raise Corrupt(f"bad or duplicate bay id {b.get('id')!r}")
                bays[b["id"]] = e["id"]
                if b.get("row") is None:          # parked bay (kept after a shrink)
                    continue
                cell = (int(b["row"]), int(b["col"]))
                if not (0 <= cell[0] < rows and 0 <= cell[1] < cols) or cell in cells:
                    raise Corrupt(f"bay {b['id']} is outside its grid or overlaps another")
                cells.add(cell)
        for bid, a in cfg["assignments"].items():
            if bid not in bays:
                raise Corrupt(f"assignment for unknown bay {bid}")
            if not isinstance(a, dict) or not isinstance(a.get("drive"), dict):
                raise Corrupt(f"assignment {bid} is malformed")
    except (KeyError, TypeError, ValueError) as e:
        raise Corrupt(f"malformed document: {e}")
    return cfg


def load_config(ctx):
    if not os.path.exists(ctx.conf):
        return empty_config(), []
    try:
        cfg = read_json_strict(ctx.conf)
    except ValueError as e:
        raise Corrupt(f"{ctx.conf} is not valid JSON ({e})")
    except OSError as e:
        raise Corrupt(f"{ctx.conf} cannot be read ({e.strerror})")
    cfg, notes = migrate(cfg)
    validate_config(cfg)
    return cfg, notes


def save_config(ctx, cfg):
    if os.path.exists(ctx.conf):
        os.makedirs(ctx.backups, mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        try:
            with open(ctx.conf, "rb") as src, open(os.path.join(ctx.backups, f"baymap-{stamp}.json"), "wb") as dst:
                dst.write(src.read())
            olds = sorted(f for f in os.listdir(ctx.backups) if f.startswith("baymap-") and f.endswith(".json"))
            for f in olds[:-KEEP_BACKUPS]:
                os.unlink(os.path.join(ctx.backups, f))
        except OSError:
            pass
    cfg["revision"] = int(cfg.get("revision") or 0) + 1
    cfg["updated"] = now()
    cfg["schema"] = SCHEMA
    validate_config(cfg)
    fsync_write(ctx.conf, cfg)
    return cfg


def list_backups(ctx):
    try:
        fs = sorted((f for f in os.listdir(ctx.backups) if f.startswith("baymap-") and f.endswith(".json")), reverse=True)
    except OSError:
        return []
    res = []
    for f in fs:
        p = os.path.join(ctx.backups, f)
        try:
            c, _ = migrate(read_json_strict(p))
            validate_config(c)
            res.append({"name": f, "valid": True, "revision": c.get("revision"), "mtime": int(os.path.getmtime(p)),
                        "enclosures": len(c["enclosures"]), "assignments": len(c["assignments"])})
        except (OSError, ValueError, Corrupt) as e:
            res.append({"name": f, "valid": False, "error": str(e)})
    return res


def load_history(ctx):
    try:
        h = read_json_strict(ctx.hist)
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def add_history(ctx, records):
    h = load_history(ctx)
    for r in records:
        r.setdefault("id", secrets.token_hex(6))
        r.setdefault("ts", now())
        h.append(r)
    fsync_write(ctx.hist, h[-MAX_HISTORY:])


# ── enclosure editing ───────────────────────────────────────────────────────

def enc_fields(src, base=None):
    """Validated enclosure attributes from user input (partial when base given)."""
    e = dict(base or {})
    if "name" in src or base is None:
        e["name"] = clean_str(src.get("name"), 64, "Name")
        if not e["name"]:
            raise BayError("The enclosure needs a name")
    for k, lim in (("description", 500), ("connection", 120)):
        if k in src or base is None:
            e[k] = clean_str(src.get(k), lim, k.capitalize())
    if "orientation" in src or base is None:
        o = src.get("orientation") or "horizontal"
        if o not in ORIENT:
            raise BayError("Orientation must be horizontal or vertical")
        e["orientation"] = o
    if "color" in src or base is None:
        c = src.get("color") or ""
        if c and not COLOR_RE.match(c):
            raise BayError("Color must look like #3366cc")
        e["color"] = c
    if "icon" in src or base is None:
        i = src.get("icon") or ""
        if i not in ICONS:
            raise BayError("Unknown icon")
        e["icon"] = i
    if "face" in src or base is None:
        f = src.get("face") or ""
        if f not in FACES:
            raise BayError("Face must be front, rear, top, bottom or empty")
        e["face"] = f
    if "numbering" in src or base is None:
        n = src.get("numbering") or {}
        if not isinstance(n, dict):
            raise BayError("numbering must be an object")
        order = n.get("order", "row")
        if order not in ORDERS:
            raise BayError("Numbering order must be row or column")
        e["numbering"] = {"order": order, "start": as_int(n.get("start", 1), 0, 9999, "First bay number"),
                          "prefix": clean_str(n.get("prefix"), 16, "Bay prefix")}
    return e


def bay_number_map(enc):
    """Bay ID -> display number, counting only real cells in the chosen order."""
    cells = [b for b in enc["bays"] if b.get("row") is not None and not b.get("absent")]
    if enc["numbering"]["order"] == "column":
        cells.sort(key=lambda b: (b["col"], b["row"]))
    else:
        cells.sort(key=lambda b: (b["row"], b["col"]))
    start = enc["numbering"]["start"]
    return {b["id"]: start + i for i, b in enumerate(cells)}


def bay_label(enc, bay, numbers=None):
    if bay.get("label"):
        return bay["label"]
    numbers = numbers or bay_number_map(enc)
    if bay["id"] in numbers:
        return f"{enc['numbering'].get('prefix', '')}{numbers[bay['id']]}"
    return "parked" if bay.get("row") is None else "—"


def find_bay(cfg, bay_id):
    if not isinstance(bay_id, str) or not ID_RE.match(bay_id):
        raise BayError("Bad bay id")
    for e in cfg["enclosures"]:
        for b in e["bays"]:
            if b["id"] == bay_id:
                return e, b
    raise BayError("No such bay", code="not_found")


def find_enc(cfg, enc_id):
    if not isinstance(enc_id, str) or not ID_RE.match(enc_id):
        raise BayError("Bad enclosure id")
    for e in cfg["enclosures"]:
        if e["id"] == enc_id:
            return e
    raise BayError("No such enclosure", code="not_found")


def op_create(cfg, req, _inv=None):
    if len(cfg["enclosures"]) >= MAX_ENCLOSURES:
        raise BayError(f"At most {MAX_ENCLOSURES} enclosures")
    src = req.get("enclosure") or {}
    e = enc_fields(src)
    rows = as_int(src.get("rows", 1), 1, MAX_DIM, "Rows")
    cols = as_int(src.get("cols", 1), 1, MAX_DIM, "Columns")
    if rows * cols > MAX_BAYS_PER_ENCLOSURE:
        raise BayError(f"At most {MAX_BAYS_PER_ENCLOSURE} cells per enclosure")
    absent = {(int(a[0]), int(a[1])) for a in (src.get("absent") or []) if isinstance(a, (list, tuple)) and len(a) == 2}
    e.update({"id": new_id("e"), "rows": rows, "cols": cols, "order": len(cfg["enclosures"]),
              "bays": [{"id": new_id("b"), "row": r, "col": c, "label": "", "absent": (r, c) in absent, "note": "",
                        "connection": ""} for r in range(rows) for c in range(cols)]})
    cfg["enclosures"].append(e)
    return {"enclosure_id": e["id"]}, []


def op_update(cfg, req, _inv=None):
    e = find_enc(cfg, req.get("id"))
    ch = req.get("changes") or {}
    upd = enc_fields(ch, base=e)
    hist = []
    rows = as_int(ch.get("rows", e["rows"]), 1, MAX_DIM, "Rows")
    cols = as_int(ch.get("cols", e["cols"]), 1, MAX_DIM, "Columns")
    if rows * cols > MAX_BAYS_PER_ENCLOSURE:
        raise BayError(f"At most {MAX_BAYS_PER_ENCLOSURE} cells per enclosure")
    # Per-cell edits: label / absent / note / connection, addressed by bay id.
    by_id = {b["id"]: b for b in e["bays"]}
    for cell in req.get("cells") or []:
        b = by_id.get(cell.get("id"))
        if not b:
            raise BayError("A cell edit names a bay that is not in this enclosure")
        if "label" in cell:
            b["label"] = clean_str(cell["label"], 32, "Bay label")
        if "note" in cell:
            b["note"] = clean_str(cell["note"], 500, "Bay note")
        if "connection" in cell:
            b["connection"] = clean_str(cell["connection"], 120, "Bay connection label")
        if "absent" in cell:
            want = bool(cell["absent"])
            if want and b["id"] in cfg["assignments"]:
                raise BayError(f"Bay {bay_label(e, b)} has a drive mapped to it — unassign or move it before "
                               "marking the cell as not a bay", code="occupied")
            b["absent"] = want
    # Resizing: a cell that falls outside the new grid must never take a mapping with it silently.
    removed = [b for b in e["bays"] if b.get("row") is not None and (b["row"] >= rows or b["col"] >= cols)]
    mapped = [b for b in removed if b["id"] in cfg["assignments"]]
    if mapped and not req.get("park"):
        nums = bay_number_map(e)
        raise BayError("Shrinking the grid would drop bays that have drives mapped: " +
                       ", ".join(bay_label(e, b, nums) for b in mapped) +
                       ". Move or unassign them first, or keep them as parked bays.",
                       code="would_orphan", bays=[b["id"] for b in mapped])
    nums = bay_number_map(e)
    for b in removed:
        if b["id"] in cfg["assignments"]:
            if not b.get("label"):
                b["label"] = bay_label(e, b, nums)   # keep the number people know it by
            b["row"] = b["col"] = None            # parked: keeps its ID and its mapping
            hist.append({"action": "park", "enclosure_id": e["id"], "bay_id": b["id"],
                         "note": "grid shrunk; bay kept as parked"})
        else:
            e["bays"].remove(b)
    have = {(b["row"], b["col"]) for b in e["bays"] if b.get("row") is not None}
    for r in range(rows):
        for c in range(cols):
            if (r, c) not in have:
                e["bays"].append({"id": new_id("b"), "row": r, "col": c, "label": "", "absent": False, "note": "",
                                  "connection": ""})
    upd["rows"], upd["cols"] = rows, cols
    e.update({k: v for k, v in upd.items() if k not in ("bays", "id", "order")})
    e["bays"].sort(key=lambda b: (b["row"] is None, b["row"] or 0, b["col"] or 0))
    return {"enclosure_id": e["id"]}, hist


def op_delete_parked(cfg, req, _inv=None):
    e, b = find_bay(cfg, req.get("bay_id"))
    if b.get("row") is not None:
        raise BayError("Only parked bays can be deleted on their own")
    if b["id"] in cfg["assignments"]:
        raise BayError("Unassign or move the drive in this parked bay first", code="occupied")
    e["bays"].remove(b)
    return {}, []


def op_delete(cfg, req, _inv=None):
    e = find_enc(cfg, req.get("id"))
    mapped = [b for b in e["bays"] if b["id"] in cfg["assignments"]]
    if mapped and not req.get("force"):
        raise BayError(f"{len(mapped)} bay(s) in {e['name']} have drives mapped. Confirm to delete the enclosure "
                       "and keep those mappings only in the history.", code="has_mappings", count=len(mapped))
    hist = []
    for b in mapped:
        a = cfg["assignments"].pop(b["id"])
        hist.append({"action": "unassign", "enclosure_id": e["id"], "bay_id": b["id"], "bay_label": bay_label(e, b),
                     "enclosure_name": e["name"], "old": a["drive"], "note": "enclosure deleted"})
    cfg["enclosures"].remove(e)
    for i, x in enumerate(sorted(cfg["enclosures"], key=lambda x: x["order"])):
        x["order"] = i
    return {}, hist


def op_reorder(cfg, req, _inv=None):
    order = req.get("order")
    have = {e["id"] for e in cfg["enclosures"]}
    if not isinstance(order, list) or set(order) != have or len(order) != len(have):
        raise BayError("order must list every enclosure ID exactly once")
    pos = {eid: i for i, eid in enumerate(order)}
    for e in cfg["enclosures"]:
        e["order"] = pos[e["id"]]
    cfg["enclosures"].sort(key=lambda e: e["order"])
    return {}, []


# ── discovery (read-only) ───────────────────────────────────────────────────

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _hosts(ctx):
    """SCSI hosts: driver, PCI function, and the mpt*sas board attributes."""
    hosts = {}
    base = os.path.join(ctx.sysfs, "class", "scsi_host")
    try:
        names = os.listdir(base)
    except OSError:
        return hosts
    for n in names:
        m = re.fullmatch(r"host(\d+)", n)
        if not m:
            continue
        d = os.path.join(base, n)
        real = os.path.realpath(d)
        bdfs = BDF_RE.findall(real)
        hosts[int(m.group(1))] = {
            "host": int(m.group(1)), "driver": _read(os.path.join(d, "proc_name")),
            "pci": bdfs[-1] if bdfs else "", "pci_path": bdfs,
            "host_sas": _read(os.path.join(d, "host_sas_address")).lower().replace("0x", ""),
            "board_name": _read(os.path.join(d, "board_name")),
            "board_serial": _read(os.path.join(d, "board_tracer")),
            "board_assembly": _read(os.path.join(d, "board_assembly")),
            "fw": _read(os.path.join(d, "version_fw")),
        }
    return hosts


def _phys(ctx):
    """HBA-owned SAS PHYs (phy-H:N), with link state. Expander PHYs (phy-H:N:M) are skipped."""
    out_ = {}
    base = os.path.join(ctx.sysfs, "class", "sas_phy")
    try:
        names = os.listdir(base)
    except OSError:
        return out_
    for n in names:
        m = re.fullmatch(r"phy-(\d+):(\d+)", n)
        if not m:
            continue
        rate = _read(os.path.join(base, n, "negotiated_linkrate"))
        out_.setdefault(int(m.group(1)), []).append({"phy": int(m.group(2)), "rate": rate,
                                                     "active": bool(re.search(r"\d", rate))})
    for v in out_.values():
        v.sort(key=lambda p: p["phy"])
    return out_


def _conn_from_sysfs(ctx, name, hosts):
    """Where a block device hangs: PCI function, host, PHY / expander / ATA port / NVMe."""
    dev = os.path.realpath(os.path.join(ctx.sysfs, "block", name, "device"))
    c = {"kind": "other", "pci": "", "host": None, "phy": None, "expander": "", "sas_address": "", "port": ""}
    bdfs = BDF_RE.findall(dev)
    c["pci"] = bdfs[-1] if bdfs else ""
    m = re.search(r"/host(\d+)/", dev + "/")
    if m:
        c["host"] = int(m.group(1))
    ed = re.findall(r"end_device-(\d+:\d+(?::\d+)?)", dev)
    if ed:
        c["kind"] = "sas"
        sd = os.path.join(ctx.sysfs, "class", "sas_device", "end_device-" + ed[-1])
        phy = _read(os.path.join(sd, "phy_identifier"))
        c["phy"] = int(phy) if phy.isdigit() else None
        c["sas_address"] = _read(os.path.join(sd, "sas_address")).lower().replace("0x", "")
        parts = ed[-1].split(":")
        if len(parts) == 3:
            c["expander"] = _read(os.path.join(ctx.sysfs, "class", "sas_device", f"expander-{parts[0]}:{parts[1]}",
                                               "sas_address")).lower().replace("0x", "")
        c["port"] = f"PHY {c['phy']}" if c["phy"] is not None and not c["expander"] else \
            (f"expander {c['expander'][-6:]} PHY {c['phy']}" if c["expander"] else "")
    elif re.search(r"/ata\d+/", dev):
        c["kind"] = "ata"
        c["port"] = re.findall(r"/(ata\d+)/", dev)[-1]
    elif "/nvme/" in dev or name.startswith("nvme"):
        c["kind"] = "nvme"
        c["port"] = (re.findall(r"/nvme/(nvme\d+)", dev) or [""])[-1]
    elif "/usb" in dev:
        c["kind"] = "usb"
    h = hosts.get(c["host"]) if c["host"] is not None else None
    if h:
        c["driver"] = h["driver"]
        c["host_sas"] = h["host_sas"]
        if not c["pci"]:
            c["pci"] = h["pci"]
    return c


def _lsblk(ctx):
    raw = ctx.sh(["lsblk", "-J", "-b", "-d", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,WWN,HCTL,TRAN,ROTA,VENDOR"])
    try:
        return [d for d in json.loads(raw).get("blockdevices", []) if d.get("type") == "disk"], True
    except (ValueError, AttributeError):
        return [], False


def _size_str(b):
    if not b:
        return ""
    b = float(b)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if b < 1000 or u == "PB":
            return f"{b:.1f} {u}" if u not in ("B",) else f"{int(b)} B"
        b /= 1000


def _drive_type(d, conn):
    if conn["kind"] == "nvme" or (d.get("tran") or "") == "nvme":
        return "NVMe"
    rota = d.get("rota")
    if rota in (True, "1", 1):
        return "HDD"
    if rota in (False, "0", 0):
        return "SSD"
    return ""


def discover(ctx, controller=True):
    """Everything known about the drives right now. Read-only."""
    blk, ok = _lsblk(ctx)
    hosts, phys = _hosts(ctx), _phys(ctx)
    try:
        roles_by_name = {k: v.get("roles", []) for k, v in ctx.block_context()[0].items()}
    except Exception:   # noqa: BLE001 — roles are decoration
        roles_by_name = {}
    drives = []
    for d in blk:
        name = d.get("name") or ""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name.startswith(("loop", "zram", "ram", "sr", "fd", "md", "dm-", "nbd", "rbd", "zd")):
            continue
        conn = _conn_from_sysfs(ctx, name, hosts)
        conn["hctl"] = d.get("hctl") or ""
        size = int(d["size"]) if str(d.get("size") or "").isdigit() else None
        drives.append({"dev": "/dev/" + name, "name": name, "model": (d.get("model") or "").strip(),
                       "vendor": (d.get("vendor") or "").strip(), "serial": (d.get("serial") or "").strip(),
                       "wwn": norm_wwn(d.get("wwn")), "size_bytes": size, "size": _size_str(size),
                       "tran": d.get("tran") or "", "type": _drive_type(d, conn), "conn": conn,
                       "roles": roles_by_name.get(name, [])})
    # Identity reliability across what is attached right now.
    wcount, scount = {}, {}
    for d in drives:
        if d["wwn"]:
            wcount[d["wwn"]] = wcount.get(d["wwn"], 0) + 1
        if serial_plausible(d["serial"]):
            scount[norm_serial(d["serial"])] = scount.get(norm_serial(d["serial"]), 0) + 1
    for d in drives:
        d["wwn_reliable"] = bool(d["wwn"] and wwn_plausible(d["wwn"]) and wcount[d["wwn"]] == 1)
        d["serial_reliable"] = bool(serial_plausible(d["serial"]) and scount[norm_serial(d["serial"])] == 1)
        issues = []
        if d["wwn"] and not d["wwn_reliable"]:
            issues.append("WWN shared with another device or a placeholder (bridge/enclosure WWN?)")
        if d["serial"] and not d["serial_reliable"]:
            issues.append("serial number duplicated or a placeholder")
        if d["wwn_reliable"]:
            d["key"] = "wwn:" + d["wwn"]
        elif d["serial_reliable"]:
            d["key"] = "sn:" + norm_serial(d["serial"])
        else:
            d["key"] = None
            issues.append("no reliable identity: this device cannot be mapped until it reports a unique serial or WWN")
        d["identity_issues"] = issues

    ctl_info = {"source": None, "error": None}
    if controller:
        data = ctx.run_script("get_attached_drives.sh")
        if isinstance(data, dict) and "error" not in data:
            ctl_info["source"] = data.get("backend")
            _join_controller(drives, data)
        else:
            ctl_info["error"] = (data or {}).get("error") if isinstance(data, dict) else "no data"

    functions = []
    for h in sorted(hosts.values(), key=lambda h: h["pci"]):
        if h["driver"] not in ("mpt3sas", "mpt2sas", "mptsas", "megaraid_sas", "mpi3mr"):
            continue
        ph = phys.get(h["host"], [])
        idx = sorted({d["conn"].get("ctl_index") for d in drives if d["conn"].get("host") == h["host"]
                      and d["conn"].get("ctl_index") is not None})
        functions.append({"pci": h["pci"], "host": h["host"], "driver": h["driver"], "host_sas": h["host_sas"],
                          "board_name": h["board_name"], "board_serial": h["board_serial"], "fw": h["fw"],
                          "ctl_index": idx[0] if len(idx) == 1 else None,
                          "ctl_index_note": "" if len(idx) <= 1 else "drives on this function map to several /cN — not trusted",
                          "pci_path": h["pci_path"], "phys": ph, "phys_total": len(ph),
                          "phys_linked": sum(1 for p in ph if p["active"]),
                          "drives": sum(1 for d in drives if d["conn"].get("host") == h["host"])})
    return {"discovered_at": now(), "lsblk_ok": ok, "sysfs_sas": os.path.isdir(os.path.join(ctx.sysfs, "class", "sas_device")),
            "controller": ctl_info, "drives": drives, "hbas": group_functions(functions)}


def _join_controller(drives, data):
    """Attach storcli/lsiutil evidence (/cN, slot, port, state). Serial first, then WWN/SAS prefix."""
    for ci, c in enumerate(data.get("controllers") or []):
        for sd in c.get("drives") or []:
            cand = []
            sn = norm_serial(sd.get("serial"))
            if serial_plausible(sn):
                cand = [d for d in drives if norm_serial(d["serial"]) == sn]
            if not cand and sd.get("os_name"):
                cand = [d for d in drives if d["dev"] == sd["os_name"]]
            if not cand and len(str(sd.get("sas_address") or "")) >= 15:
                pfx = str(sd["sas_address"]).lower()[:15]
                cand = [d for d in drives if d["wwn"].startswith(pfx) or d["conn"].get("sas_address", "").startswith(pfx)]
            if len(cand) == 1:
                cand[0]["conn"].update({"ctl_index": ci, "slot": sd.get("slot") or "", "ctl_port": str(sd.get("port") or ""),
                                        "ctl_state": sd.get("state") or ""})


def group_functions(functions):
    """Collapse controller functions into physical cards — only when it is safe.

    Strong: identical, plausible board serial AND identical board name.
    Fallback (no serials): same PCIe slot (shared upstream bridge), a board name
    known to carry exactly N controllers, and exactly N functions there.
    Everything else stays one card per function: merging two separate cards is
    worse than showing one card as two."""
    groups, used = [], set()
    by_serial = {}
    for i, f in enumerate(functions):
        s = norm_serial(f["board_serial"])
        if serial_plausible(s) and f["board_name"]:
            by_serial.setdefault((s, f["board_name"]), []).append(i)
    for (s, name), idxs in by_serial.items():
        if len(idxs) >= 2:
            groups.append({"board_name": name, "board_serial": f"{functions[idxs[0]]['board_serial']}",
                           "grouped_by": "board serial", "functions": [functions[i] for i in idxs]})
            used.update(idxs)
    rest = [i for i in range(len(functions)) if i not in used]
    by_slot = {}
    for i in rest:
        f = functions[i]
        # root port -> switch upstream port (on the card) -> downstream port -> function:
        # the upstream port is what two functions of one dual-chip card share.
        slot = f["pci_path"][-3] if len(f["pci_path"]) >= 4 else ""
        key = re.sub(r"^(LSI\s*)?SAS", "", f["board_name"] or "", flags=re.I).strip().lower()
        if slot and key in DUAL_IOC_BOARDS:
            by_slot.setdefault((slot, key), []).append(i)
    for (slot, key), idxs in by_slot.items():
        if len(idxs) == DUAL_IOC_BOARDS[key]:
            groups.append({"board_name": functions[idxs[0]]["board_name"], "board_serial": "",
                           "grouped_by": f"shared PCIe slot {slot}", "functions": [functions[i] for i in idxs]})
            used.update(idxs)
    for i, f in enumerate(functions):
        if i not in used:
            groups.append({"board_name": f["board_name"], "board_serial": f["board_serial"], "grouped_by": None,
                           "functions": [f]})
    for g in groups:
        g["functions"].sort(key=lambda f: f["pci"])
        g["phys_total"] = sum(f["phys_total"] for f in g["functions"])
        g["phys_linked"] = sum(f["phys_linked"] for f in g["functions"])
        g["drives"] = sum(f["drives"] for f in g["functions"])
    groups.sort(key=lambda g: g["functions"][0]["pci"])
    return groups


# ── reconciliation ──────────────────────────────────────────────────────────

def identity_of(d):
    return {"key": d.get("key"), "wwn": d.get("wwn") if d.get("wwn_reliable") else "",
            "serial": norm_serial(d.get("serial")) if serial_plausible(d.get("serial")) else "",
            "model": d.get("model") or "", "size_bytes": d.get("size_bytes"), "type": d.get("type") or ""}


def hint_of(d):
    c = d.get("conn") or {}
    return {k: c.get(k) for k in ("kind", "pci", "host_sas", "phy", "expander", "port", "hctl", "ctl_index", "slot",
                                  "ctl_port")} | {"dev": d.get("dev")}


def match_saved(saved, drives):
    """(state, drive or None, reason). state: connected | none | conflict."""
    w, s, model = saved.get("wwn") or "", saved.get("serial") or "", saved.get("model") or ""
    if not w and not s:
        return "unknown", None, "The saved mapping has neither a WWN nor a serial number to recognise the drive by"
    by_w = [d for d in drives if w and d["wwn"] == w]
    by_s = [d for d in drives if s and norm_serial(d["serial"]) == s]
    if by_w:
        if len(by_w) > 1:
            return "conflict", None, f"{len(by_w)} attached devices report WWN {w}"
        d = by_w[0]
        if s and serial_plausible(d["serial"]) and norm_serial(d["serial"]) != s:
            return "conflict", d, f"WWN matches {d['dev']} but its serial is {d['serial']}, not {s}"
        return "connected", d, ""
    if by_s:
        if len(by_s) > 1 and model:
            by_s = [d for d in by_s if d["model"] == model] or by_s
        if len(by_s) > 1:
            return "conflict", None, f"{len(by_s)} attached devices report serial {s}"
        d = by_s[0]
        if w and d["wwn"] and d["wwn"] != w:
            return "conflict", d, f"Serial matches {d['dev']} but its WWN is {d['wwn']}, not {w}"
        return "connected", d, ""
    return "none", None, ""


def saved_key(a):
    sv = a["drive"]
    return "wwn:" + sv["wwn"] if sv.get("wwn") else "sn:" + (sv.get("serial") or "")


def same_link(hint, d):
    """True when the saved controller link (PCI function + PHY [+ expander]) is where d is now."""
    c = d.get("conn") or {}
    if not hint or not hint.get("pci") or hint.get("phy") is None or c.get("phy") is None:
        return False
    return hint.get("pci") == c.get("pci") and hint.get("phy") == c.get("phy") and \
        (hint.get("expander") or "") == (c.get("expander") or "")


def reconcile(cfg, inv, seen, locating=()):
    drives = inv["drives"] if inv else []
    disc_ok = bool(inv and inv.get("lsblk_ok"))
    status, claimed = {}, {}
    for e in cfg["enclosures"]:
        nums = bay_number_map(e)
        for b in e["bays"]:
            st = {"enclosure_id": e["id"], "bay_id": b["id"], "label": bay_label(e, b, nums), "parked": b.get("row") is None}
            a = cfg["assignments"].get(b["id"])
            if b.get("absent"):
                st["state"] = "nonexistent"
            elif not a:
                st["state"] = "empty"
            elif not disc_ok:
                st.update(state="unknown", reason="Drive discovery failed, so the mapping cannot be checked")
            else:
                saved = a["drive"]
                state, d, why = match_saved(saved, drives)
                if state == "connected":
                    st.update(state="connected", drive=d["key"])
                    claimed.setdefault(d["key"], []).append(b["id"])
                    if a.get("hint") and a["hint"].get("pci") and not same_link(a["hint"], d) and d["conn"].get("phy") is not None:
                        st["moved"] = "This drive is now on a different controller link than when it was mapped " \
                                      f"({a['hint'].get('pci')} {a['hint'].get('port') or ''} → {d['conn'].get('pci')} {d['conn'].get('port') or ''})"
                elif state in ("conflict", "unknown"):
                    st.update(state=state, reason=why, drive=d["key"] if d else None)
                else:
                    occupant = [x for x in drives if same_link(a.get("hint") or {}, x)]
                    last = seen.get(saved_key(a), {})
                    st["last_seen"] = last.get("last_seen")
                    if len(occupant) == 1 and occupant[0].get("key"):
                        st.update(state="changed", candidate=occupant[0]["key"],
                                  reason=f"The mapped drive is gone and a different drive ({occupant[0]['model']} "
                                         f"SN {occupant[0]['serial'] or '?'}) is on the same controller link "
                                         f"({occupant[0]['conn'].get('pci')} {occupant[0]['conn'].get('port')}). "
                                         "Probably a replacement — confirm it before the map changes.")
                    else:
                        st.update(state="missing", reason="The mapped drive is not attached right now")
            if st.get("drive"):
                d = next((x for x in drives if x["key"] == st["drive"]), None)
                if d and d["conn"].get("hctl") in locating:
                    st["locating"] = True
            status[b["id"]] = st
    # The same physical drive must not appear in two bays.
    for key, bays in claimed.items():
        if len(bays) > 1:
            for bid in bays:
                status[bid].update(state="conflict", reason=f"This drive is mapped to {len(bays)} bays")
    t = now()
    unassigned = []
    for d in drives:
        if d["key"] in claimed:
            continue
        first = (seen.get(d["key"] or "") or {}).get("first_seen")
        unassigned.append({"key": d["key"], "new": bool(first and t - first < NEW_DRIVE_SECS)})
    counts = {}
    for s in status.values():
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    return {"bays": status, "unassigned": unassigned, "counts": counts}


def update_seen(ctx, inv):
    seen = {}
    try:
        seen = read_json_strict(ctx.seen)
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    if not inv or not inv.get("lsblk_ok"):
        return seen
    t = now()
    for d in inv["drives"]:
        if not d.get("key"):
            continue
        e = seen.setdefault(d["key"], {"first_seen": t})
        e.update(last_seen=t, last_dev=d["dev"], last_port=d["conn"].get("port"), last_pci=d["conn"].get("pci"),
                 model=d["model"], serial=d["serial"])
    if len(seen) > MAX_SEEN:
        for k in sorted(seen, key=lambda k: seen[k].get("last_seen", 0))[:len(seen) - MAX_SEEN]:
            del seen[k]
    try:
        fsync_write(ctx.seen, seen)
    except OSError:
        pass
    return seen


# ── assignment operations ───────────────────────────────────────────────────

def inv_drive(inv, key):
    if not isinstance(key, str) or not re.fullmatch(r"(wwn:[0-9a-f]{16,32}|sn:[ -~]{1,80})", key):
        raise BayError("Bad drive key")
    if not inv.get("lsblk_ok"):
        raise BayError("Drive discovery failed; cannot assign right now", code="discovery_failed")
    d = next((x for x in inv["drives"] if x["key"] == key), None)
    if not d:
        raise BayError("That drive is not attached right now (refresh the inventory)", code="not_found")
    return d


def bay_of_drive(cfg, d):
    for bid, a in cfg["assignments"].items():
        sv = a["drive"]
        if (sv.get("wwn") and d.get("wwn_reliable") and sv["wwn"] == d["wwn"]) or \
                (sv.get("serial") and d.get("serial_reliable") and sv["serial"] == norm_serial(d["serial"])):
            return bid
    return None


def _loc(cfg, bid):
    e, b = find_bay(cfg, bid)
    return {"enclosure_id": e["id"], "enclosure_name": e["name"], "bay_id": b["id"], "bay_label": bay_label(e, b)}


def _usable_bay(cfg, bid):
    e, b = find_bay(cfg, bid)
    if b.get("absent"):
        raise BayError("That cell is marked as not a bay")
    if b.get("row") is None:
        raise BayError("Parked bays cannot receive new drives")
    return e, b


def op_assign(cfg, req, inv):
    e, b = _usable_bay(cfg, req.get("bay_id"))
    d = inv_drive(inv, req.get("drive"))
    if not d.get("key"):
        raise BayError("This device has no reliable identity and cannot be mapped", code="ambiguous")
    if b["id"] in cfg["assignments"]:
        raise BayError(f"Bay {bay_label(e, b)} already has a drive. Swap or replace instead.", code="occupied")
    other = bay_of_drive(cfg, d)
    if other:
        loc = _loc(cfg, other)
        raise BayError(f"This drive is already mapped to {loc['enclosure_name']} bay {loc['bay_label']}. Move it instead.",
                       code="duplicate", bay=other)
    note = clean_str(req.get("note"), 1000, "Note")
    cfg["assignments"][b["id"]] = {"drive": identity_of(d), "hint": hint_of(d), "assigned_at": now(), "note": note}
    return {}, [dict(_loc(cfg, b["id"]), action="assign", new=identity_of(d), new_link=hint_of(d), note=note)]


def op_unassign(cfg, req, _inv):
    e, b = find_bay(cfg, req.get("bay_id"))
    a = cfg["assignments"].pop(b["id"], None)
    if not a:
        raise BayError("That bay has no drive mapped", code="empty")
    note = clean_str(req.get("note"), 1000, "Note")
    return {}, [dict(_loc(cfg, b["id"]), action="unassign", old=a["drive"], old_link=a.get("hint"), note=note)]


def op_move(cfg, req, _inv):
    _, src = find_bay(cfg, req.get("from"))
    _, dst = _usable_bay(cfg, req.get("to"))
    if src["id"] == dst["id"]:
        raise BayError("Source and destination are the same bay")
    if src["id"] not in cfg["assignments"]:
        raise BayError("The source bay has no drive mapped", code="empty")
    if dst["id"] in cfg["assignments"]:
        raise BayError("The destination bay is occupied — use swap (it needs confirming)", code="occupied")
    a = cfg["assignments"].pop(src["id"])
    cfg["assignments"][dst["id"]] = a
    note = clean_str(req.get("note"), 1000, "Note")
    return {}, [dict(_loc(cfg, dst["id"]), action="move", from_bay=_loc(cfg, src["id"]), new=a["drive"], note=note)]


def op_swap(cfg, req, _inv):
    if req.get("confirm") is not True:
        raise BayError("Swapping two mappings needs explicit confirmation", code="confirm_required")
    _, a_ = _usable_bay(cfg, req.get("a"))
    _, b_ = _usable_bay(cfg, req.get("b"))
    if a_["id"] == b_["id"]:
        raise BayError("Pick two different bays")
    A, B = cfg["assignments"].pop(a_["id"], None), cfg["assignments"].pop(b_["id"], None)
    if not A and not B:
        raise BayError("Neither bay has a drive mapped", code="empty")
    if A:
        cfg["assignments"][b_["id"]] = A
    if B:
        cfg["assignments"][a_["id"]] = B
    note = clean_str(req.get("note"), 1000, "Note")
    return {}, [dict(_loc(cfg, a_["id"]), action="swap", other_bay=_loc(cfg, b_["id"]),
                     new=(B or {}).get("drive"), old=(A or {}).get("drive"), note=note)]


def op_replace(cfg, req, inv):
    if req.get("confirm") is not True:
        raise BayError("Replacing a mapping needs explicit confirmation", code="confirm_required")
    e, b = _usable_bay(cfg, req.get("bay_id"))
    old = cfg["assignments"].get(b["id"])
    if not old:
        raise BayError("That bay has no drive mapped — assign instead", code="empty")
    d = inv_drive(inv, req.get("drive"))
    if not d.get("key"):
        raise BayError("This device has no reliable identity and cannot be mapped", code="ambiguous")
    other = bay_of_drive(cfg, d)
    if other and other != b["id"]:
        loc = _loc(cfg, other)
        raise BayError(f"The new drive is already mapped to {loc['enclosure_name']} bay {loc['bay_label']}",
                       code="duplicate", bay=other)
    if other == b["id"]:
        raise BayError("That is the drive already mapped here")
    note = clean_str(req.get("note"), 1000, "Note")
    cfg["assignments"][b["id"]] = {"drive": identity_of(d), "hint": hint_of(d), "assigned_at": now(), "note": note}
    return {}, [dict(_loc(cfg, b["id"]), action="replace", old=old["drive"], old_link=old.get("hint"),
                     new=identity_of(d), new_link=hint_of(d), note=note, confirmed=True)]


def op_note(cfg, req, _inv):
    _, b = find_bay(cfg, req.get("bay_id"))
    a = cfg["assignments"].get(b["id"])
    if not a:
        raise BayError("That bay has no drive mapped", code="empty")
    a["note"] = clean_str(req.get("note"), 1000, "Note")
    return {}, []


def op_relink(cfg, req, inv):
    """Accept the drive's current controller link as the new evidence (after a cable or port move)."""
    _, b = find_bay(cfg, req.get("bay_id"))
    a = cfg["assignments"].get(b["id"])
    if not a:
        raise BayError("That bay has no drive mapped", code="empty")
    state, d, why = match_saved(a["drive"], inv["drives"])
    if state != "connected":
        raise BayError(why or "The mapped drive is not attached right now")
    old = a.get("hint")
    a["hint"] = hint_of(d)
    return {}, [dict(_loc(cfg, b["id"]), action="relink", old_link=old, new_link=a["hint"], new=a["drive"])]


# ── import / export ─────────────────────────────────────────────────────────

def export_doc(cfg, history=None):
    doc = {"format": FORMAT, "schema": SCHEMA, "exported_at": now(), "host": socket.gethostname(),
           "config": {k: cfg[k] for k in ("enclosures", "assignments")}, "revision": cfg.get("revision", 0)}
    if history is not None:
        doc["history"] = history
    return doc


def parse_import(doc):
    if not isinstance(doc, dict):
        raise BayError("The file is not a JSON object")
    if doc.get("format") != FORMAT:
        raise BayError("Not an HBA Viewer bay map export (format field missing or different)")
    if not isinstance(doc.get("schema"), int) or doc["schema"] > SCHEMA:
        raise BayError(f"Export schema {doc.get('schema')!r} is newer than this version understands ({SCHEMA})")
    body = dict(doc.get("config") or {})
    body["schema"] = doc["schema"] if doc["schema"] >= 1 else None
    if body["schema"] is None:
        body.pop("schema")
    try:
        cfg, notes = migrate(body)
        cfg.setdefault("assignments", {})
        validate_config(cfg)
    except Corrupt as e:
        raise BayError(f"The file does not validate: {e}")
    # Re-validate every field the UI edits, as if typed in.
    for e in cfg["enclosures"]:
        enc_fields(e)
        for b in e["bays"]:
            clean_str(b.get("label"), 32, "Bay label")
            clean_str(b.get("note"), 500, "Bay note")
    hist = doc.get("history")
    if hist is not None and not isinstance(hist, list):
        raise BayError("history must be a list")
    return cfg, notes, hist or []


def import_plan(cur, new, mode):
    """What applying `new` would do. Never overwrites an enclosure just because names match."""
    plan = {"mode": mode, "add": [], "conflicts": [], "warnings": [], "assignments": 0}
    cur_ids = {e["id"]: e for e in cur["enclosures"]}
    cur_names = {e["name"].lower(): e for e in cur["enclosures"]}
    cur_drives = {}
    for bid, a in cur["assignments"].items():
        for k in ("wwn", "serial"):
            if a["drive"].get(k):
                cur_drives[(k, a["drive"][k])] = bid
    for e in new["enclosures"]:
        n_assign = sum(1 for b in e["bays"] if b["id"] in new["assignments"])
        item = {"id": e["id"], "name": e["name"], "bays": len(e["bays"]), "assignments": n_assign}
        if mode == "merge":
            if e["id"] in cur_ids:
                plan["conflicts"].append(dict(item, why="An enclosure with this ID already exists here (imported again?) — it will be skipped"))
                continue
            if e["name"].lower() in cur_names:
                plan["warnings"].append(f"“{e['name']}” has the same name as an existing enclosure; it will be added as a separate enclosure, not merged")
            for b in e["bays"]:
                a = new["assignments"].get(b["id"])
                if not a:
                    continue
                for k in ("wwn", "serial"):
                    if a["drive"].get(k) and (k, a["drive"][k]) in cur_drives:
                        plan["conflicts"].append(dict(item, why=f"Drive {a['drive'].get('serial') or a['drive'].get('wwn')} in imported bay is already mapped here — that mapping will be dropped from the import",
                                                      bay_id=b["id"]))
                        break
        plan["add"].append(item)
        plan["assignments"] += n_assign
    if mode == "replace":
        plan["warnings"].append(f"Replace mode discards the current {len(cur['enclosures'])} enclosure(s) and "
                                f"{len(cur['assignments'])} mapping(s) (a backup is kept)")
    return plan


def apply_import(cur, new, mode):
    if mode == "replace":
        out_ = {"schema": SCHEMA, "revision": cur.get("revision", 0), "updated": cur.get("updated", 0),
                "enclosures": new["enclosures"], "assignments": new["assignments"]}
        return out_
    plan = import_plan(cur, new, mode)
    skip_enc = {c["id"] for c in plan["conflicts"] if "bay_id" not in c}
    skip_bay = {c["bay_id"] for c in plan["conflicts"] if "bay_id" in c}
    have_bays = {b["id"] for e in cur["enclosures"] for b in e["bays"]}
    for e in new["enclosures"]:
        if e["id"] in skip_enc:
            continue
        if len(cur["enclosures"]) >= MAX_ENCLOSURES:
            raise BayError(f"At most {MAX_ENCLOSURES} enclosures")
        e = json.loads(json.dumps(e))
        e["order"] = len(cur["enclosures"])
        for b in e["bays"]:
            if b["id"] in have_bays:            # never collide with an existing bay ID
                old = b["id"]
                b["id"] = new_id("b")
                if old in new["assignments"]:
                    new["assignments"][b["id"]] = new["assignments"].pop(old)
            a = new["assignments"].get(b["id"])
            if a and b["id"] not in skip_bay:
                cur["assignments"][b["id"]] = a
        cur["enclosures"].append(e)
    return cur


# ── command entry ───────────────────────────────────────────────────────────

MUTATIONS = {"create": (op_create, False), "update": (op_update, False), "delete": (op_delete, False),
             "reorder": (op_reorder, False), "delete-parked": (op_delete_parked, False),
             "assign": (op_assign, True), "unassign": (op_unassign, False), "move": (op_move, False),
             "swap": (op_swap, False), "replace": (op_replace, True), "note": (op_note, False),
             "relink": (op_relink, True)}


def read_request(stdin):
    raw = stdin.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise BayError("Request too large")
    if not raw.strip():
        return {}
    try:
        req = json.loads(raw)
    except ValueError as e:
        raise BayError(f"Request is not JSON: {e}")
    if not isinstance(req, dict):
        raise BayError("Request must be a JSON object")
    return req


def describe(cfg, inv, seen, ctx):
    loc = set()
    try:
        loc = set(ctx.locate_active())
    except Exception:   # noqa: BLE001
        pass
    return {"schema": SCHEMA, "host": socket.gethostname(), "revision": cfg.get("revision", 0), "updated": cfg.get("updated", 0),
            "enclosures": sorted(cfg["enclosures"], key=lambda e: e["order"]), "assignments": cfg["assignments"],
            "inventory": inv, "status": reconcile(cfg, inv, seen, loc),
            "seen": {k: seen[k] for k in map(saved_key, cfg["assignments"].values()) if k in seen},
            "locating": sorted(loc), "locate_max_secs": ctx.settings().get("LOCATE_MAX_SECS", 300)}


def run(ctx, args, stdin):
    sub = args[0] if args else "get"
    fast = "--fast" in args
    try:
        if sub in ("get", "status", "inventory"):
            inv = discover(ctx, controller=not fast)
            if sub == "inventory":
                return inv
            seen = update_seen(ctx, inv)
            try:
                cfg, notes = load_config(ctx)
                if notes:
                    # Persist the migration at once: IDs assigned while migrating must be
                    # the same on the next read (the old file stays in the backups).
                    with ctx.lock("baymap"):
                        cfg, notes = load_config(ctx)
                        if notes:
                            cfg = save_config(ctx, cfg)
                            add_history(ctx, [{"action": "migrate", "note": "; ".join(notes)}])
            except Corrupt as e:
                return {"corrupt": {"path": ctx.conf, "reason": str(e)}, "backups": list_backups(ctx), "inventory": inv}
            d = describe(cfg, inv, seen, ctx)
            if notes:
                d["migration_notes"] = notes
            return d
        if sub == "locations":
            # Cheap dev -> bay lookup for the Drives tab (lsblk + sysfs only).
            try:
                cfg, _ = load_config(ctx)
            except Corrupt:
                return {"locations": {}}
            inv = discover(ctx, controller=False)
            res = {}
            for e in cfg["enclosures"]:
                nums = bay_number_map(e)
                for b in e["bays"]:
                    a = cfg["assignments"].get(b["id"])
                    if not a:
                        continue
                    st, d, _ = match_saved(a["drive"], inv["drives"])
                    if st == "connected":
                        res[d["dev"]] = {"enclosure": e["name"], "bay": bay_label(e, b, nums), "bay_id": b["id"]}
            return {"locations": res}
        if sub == "history":
            req = read_request(stdin)
            h = load_history(ctx)
            if req.get("bay_id"):
                h = [r for r in h if r.get("bay_id") == req["bay_id"] or (r.get("from_bay") or {}).get("bay_id") == req["bay_id"]
                     or (r.get("other_bay") or {}).get("bay_id") == req["bay_id"]]
            return {"history": list(reversed(h))[: int(req.get("limit") or 500)]}
        if sub in ("export", "backup"):
            cfg, _ = load_config(ctx)
            return export_doc(cfg, load_history(ctx) if sub == "backup" else None)
        if sub in ("import-preview", "import", "restore"):
            req = read_request(stdin)
            mode = "replace" if sub == "restore" else req.get("mode", "merge")
            if mode not in ("merge", "replace"):
                raise BayError("mode must be merge or replace")
            new, notes, hist = parse_import(req.get("data"))
            with ctx.lock("baymap"):
                cur, _ = load_config(ctx)
                plan = import_plan(cur, new, mode)
                plan["notes"] = notes
                plan["history_records"] = len(hist)
                if sub == "import-preview":
                    return {"plan": plan, "revision": cur.get("revision", 0)}
                if mode == "replace" and req.get("confirm") is not True:
                    raise BayError("Replacing the whole bay map needs explicit confirmation", code="confirm_required")
                check_revision(cur, req)
                cfg = save_config(ctx, apply_import(cur, new, mode))
                if hist and sub == "restore":
                    known = {r.get("id") for r in load_history(ctx)}
                    # History from a file is display-only data; keep plain records of sane size.
                    keep = [r for r in hist if isinstance(r, dict) and r.get("id") not in known
                            and len(json.dumps(r)) <= 20000 and isinstance(r.get("action", ""), str)]
                    add_history(ctx, keep[-MAX_HISTORY:])
                add_history(ctx, [{"action": "import" if sub == "import" else "restore", "note": f"{mode}: "
                                   f"{len(plan['add'])} enclosure(s), {plan['assignments']} mapping(s)"}])
                return {"ok": True, "revision": cfg["revision"], "plan": plan}
        if sub == "recover":
            req = read_request(stdin)
            with ctx.lock("baymap"):
                try:
                    load_config(ctx)
                    raise BayError("The bay map is not corrupt; nothing to recover")
                except Corrupt:
                    pass
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                keep = f"{ctx.conf}.corrupt-{stamp}"
                os.replace(ctx.conf, keep)
                if req.get("action") == "restore_backup":
                    name = req.get("backup")
                    if not isinstance(name, str) or not re.fullmatch(r"baymap-[0-9-]+\.json", name):
                        raise BayError("Pick a backup from the list")
                    cfg, _ = migrate(read_json_strict(os.path.join(ctx.backups, name)))
                    validate_config(cfg)
                    fsync_write(ctx.conf, cfg)
                elif req.get("action") != "start_empty":
                    os.replace(keep, ctx.conf)
                    raise BayError("action must be restore_backup or start_empty")
                add_history(ctx, [{"action": "recover", "note": f"{req['action']}; damaged file kept as {os.path.basename(keep)}"}])
                return {"ok": True, "kept": keep}
        if sub == "locate":
            return locate(ctx, read_request(stdin))
        if sub in MUTATIONS:
            fn, needs_inv = MUTATIONS[sub]
            req = read_request(stdin)
            inv = discover(ctx, controller=False) if needs_inv else None
            with ctx.lock("baymap"):
                cfg, _ = load_config(ctx)
                check_revision(cfg, req)
                res, hist = fn(cfg, req, inv)
                cfg = save_config(ctx, cfg)
                if hist:
                    add_history(ctx, hist)
            res.update(ok=True, revision=cfg["revision"])
            return res
        raise BayError("usage: baymap get|inventory|locations|history|export|backup|import-preview|import|restore|"
                       "recover|locate|" + "|".join(MUTATIONS))
    except BayError as e:
        return dict({"error": str(e), "code": e.code}, **e.extra)
    except Corrupt as e:
        return {"error": f"The bay map file is damaged: {e}. Open the Bay Mapper tab to recover it.", "code": "corrupt"}


def check_revision(cfg, req):
    rev = req.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise BayError("Missing revision (reload the page)", code="conflict", current=cfg.get("revision", 0))
    if rev != cfg.get("revision", 0):
        raise BayError("The bay map was changed elsewhere since you loaded it. Reload and try again.",
                       code="conflict", current=cfg.get("revision", 0))


# ── locate ──────────────────────────────────────────────────────────────────

def locate(ctx, req):
    """Verify the bay's drive is the one attached right now, then reuse the existing locate."""
    action = req.get("action")
    if action not in ("start", "stop"):
        raise BayError("action must be start or stop")
    cfg, _ = load_config(ctx)
    e, b = find_bay(cfg, req.get("bay_id"))
    a = cfg["assignments"].get(b["id"])
    if not a:
        raise BayError("No drive is mapped to this bay", code="empty")
    inv = discover(ctx, controller=False)
    state, d, why = match_saved(a["drive"], inv["drives"])
    if state != "connected":
        raise BayError(why or "The mapped drive is not attached right now, so it cannot be located", code="not_connected")
    hctl = d["conn"].get("hctl") or ""
    verified = {"dev": d["dev"], "serial": d["serial"], "wwn": d["wwn"], "hctl": hctl, "model": d["model"]}
    if action == "stop":
        r = ctx.locate_stop(d["dev"])
        if r.get("error"):
            raise BayError(r["error"])
        return {"ok": True, "active": r.get("active", []), "verified": verified}
    if d["conn"]["kind"] == "nvme" or not d["dev"].startswith("/dev/sd"):
        raise BayError("Locate is not supported for this device: it works by generating read activity on a SCSI/SATA "
                       "disk through /dev/bsg, and this device has no such path.", code="unsupported")
    if not HCTL_RE.match(hctl):
        raise BayError("This drive has no SCSI address (HCTL), so the locate helper cannot address it.", code="unsupported")
    if not os.path.exists(os.path.join(ctx.bsg, hctl)):
        raise BayError(f"{ctx.bsg}/{hctl} is missing (sg/bsg support not loaded?), so the drive cannot be pulsed.",
                       code="unsupported")
    r = ctx.locate_start(d["dev"])
    if r.get("error"):
        raise BayError(r["error"])
    return {"ok": True, "secs": r.get("secs"), "active": r.get("active", []), "verified": verified,
            "mechanism": "activity",
            "explain": "No locate LED is driven. The drive is read twice a second so its own activity light flickers "
                       "in a steady rhythm — visible only if the bay or drive has an activity light wired to it. It "
                       "wakes a sleeping drive and stops by itself after the time limit."}
