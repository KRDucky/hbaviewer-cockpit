# HBA Viewer for Cockpit

A Cockpit page for monitoring LSI / Broadcom SAS HBAs on Ubuntu — a port of
[FugginOld/Unraid-HBAviewer](https://github.com/FugginOld/Unraid-HBAviewer)
(itself built on DevlinDelFuego's Unraid-LSIUtil). Shows up in Cockpit under
**Tools → HBA Viewer**.

| Tab | What it shows |
| --- | --- |
| **Overview** | Per-controller temperature gauge, chip, firmware/BIOS, IT/IR mode, drive count, PCIe link, health rollup |
| **Health** | Five indicators — thermal, link integrity, topology, host link, controller read — with a worst-of rollup. Unmeasured reads grey, never green |
| **PHY** | Per-PHY link state, speed, SAS address, the drive behind it, and the four error counters. *Set baseline* turns counters into growth + errors/hour, with a top-offenders line |
| **Drives** | Device, what it's used for (ZFS pool / md / LVM / mountpoint), slot, port, model, serial, state, size, link, firmware. Per-drive SMART and **Locate** (blinks the activity LED) |
| **SMART** | Background collection across every HBA drive; SATA drives in standby are skipped, not woken |
| **Bay Mapper** | Your own enclosure diagrams (cages, backplanes, chassis, JBODs) with drives dragged into physical bays; live connected / missing / changed status, replacement history, verified Locate, export/import, printable map |
| **Event log** | Controller firmware event log, archived so history survives reboots and ring wrap. Filter + copy |
| **Performance** | Live throughput / IOPS / util / latency per controller from `/proc/diskstats` (never touches storcli) |
| **Settings** | Alert threshold, °C/°F, locate timeout, PCIe expectation override, notify command; detected tools and PCI devices |

Backends are auto-detected per box, exactly as upstream does it:

| Generation | Cards | Tool |
| --- | --- | --- |
| SAS2 | 9207-8i, 9211-8i, H200/H310, M1015 | `lsiutil` (bundled from the upstream release) |
| SAS3 / SAS3.5 | 9300, 9305, 9400, 9500 | `storcli64` — install from Broadcom |
| SAS4 | 9600 | `storcli2` — install from Broadcom |

## Install

```bash
tar xzf hbaviewer-cockpit.tar.gz && cd hbaviewer-cockpit
sudo ./install.sh
```

The installer:

- apt-installs `cockpit` (if missing), `gawk`, `smartmontools`, `pciutils`, `sg3-utils`, `unzip`, `xz-utils`
- downloads the pinned upstream release (`2026.09.19`), checks it against the md5 upstream's own `.plg` pins, and installs its
  bash/gawk readers + the lsiutil binary unmodified into `/usr/lib/hbaviewer/`
- installs the Cockpit page to `/usr/share/cockpit/hbaviewer/`
- enables `hbaviewer-sample.timer` (a health sample every 5 min, so link-integrity rates build up even with the page closed)

Then open `https://<server>:9090`, turn on **Administrative access** in the top bar, and pick **HBA Viewer**.

### storcli (SAS3 and newer)

Broadcom doesn't allow redistribution, so grab **StorCLI** (9300/9400/9500) or
**StorCLI2** (9600) from broadcom.com → Support Documents and Downloads, then either
`dpkg -i` the `.deb` inside it yourself or let the installer dig it out:

```bash
sudo ./install.sh --storcli-zip ~/Downloads/STORCLI_SAS3.5_P36.zip
```

It lands in `/opt/MegaRAID/storcli/storcli64`, which the helper finds on its own.

Other options: `--release-tarball hbaviewer.txz` (offline install) and `--no-cockpit`.

## How it's put together

```
Cockpit page (HTML/JS, no build step)
   │  cockpit.spawn(..., superuser: "require")
   ▼
/usr/lib/hbaviewer/hbaviewer-ctl        Python, root — one JSON document per call
   │   env + PATH set for Ubuntu, cwd /run/hbaviewer
   ▼
/usr/lib/hbaviewer/scripts/*.sh          upstream composers + gawk parsers, unmodified
   ▼
storcli64 / storcli2 / lsiutil, sysfs, /proc, smartctl
```

What changed versus Unraid, and why:

- **No patches to upstream's readers.** Everything Unraid-specific in them is an env-var default (`LSIUTIL`, `LSI_CFG_PATH`, cache paths), so the helper just sets those. Upgrading to a newer upstream release is a matter of bumping the version + md5 in `install.sh`.
- **gawk shim.** Upstream's parsers call plain `awk` but need gawk (three-argument `match()`). Ubuntu's `awk` is mawk, which fails on them outright. The helper puts a private `awk → gawk` link first on its children's PATH instead of changing your system-wide alternative.
- **PHP → Python.** The health indicators/rollup, sample ring, PHY baseline maths and event archive were PHP. They're ported line-for-line into `hbaviewer-ctl` (see `tests/test_health.py`).
- **Storage.** `/boot/config/plugins/hbaviewer` → `/etc/hbaviewer` (settings) and `/var/lib/hbaviewer` (health rings, baselines, event archive, SMART cache). Volatile cache is `/run/hbaviewer`.
- **Unraid disk slots → Linux roles.** The "what Unraid calls this disk" column becomes ZFS pool / md array / LVM / mountpoint, from `lsblk`.
- **Background jobs** (SMART collection, locate blink) run as transient systemd units (`hbaviewer-smart`, `hbaviewer-locate-H-C-T-L`) so closing the browser tab can't orphan or kill them.
- **Notifications** → every health state change goes to the journal (`journalctl -t hbaviewer`), plus an optional *Notify command* you set in Settings (e.g. `curl -d "$HBAV_MESSAGE" ntfy.sh/your-topic`).

Deliberately left out: the **firmware/BIOS flasher** (its safety gates are built around stopping the Unraid array — flash from the CLI with `sas3flash`/`storcli` instead), the Unraid dashboard tile, and the support-bundle zipper. Upstream's PHP bay map is replaced by the Bay Mapper below.

## Bay Mapper

A drawing of where your drives physically sit, kept current from discovery. It never changes disk contents.

### Using it

1. **Bay Mapper → New enclosure.** Name it ("Icy Dock 8×2.5″", "Rosewill hot-swap", "Front JBOD"), set rows × columns, horizontal or vertical drives, numbering (across rows or down columns, first number, optional prefix), side (front/rear/top/bottom), optional colour/icon and a cabling note such as "/c0 via 2× SFF-8643 breakout". Click cells that aren't real bays (a fan, a gap).
2. **Map drives.** Drag a drive from the list on the right onto a bay. Without a mouse, select a drive (click/tap or Enter) and then click an empty bay, or open a bay and pick from its **Assign** list. Filter the list by controller, HDD/SSD/NVMe, assigned/unassigned, or search by model, serial, WWN or `/dev` name.
3. **Rearrange.** Drag a bay onto an empty bay to move the mapping, or onto an occupied one to swap (asks first). Open a bay to unassign, move/swap from a list, edit its label, cable note or note, see its history, or locate it.
4. **Compact / Expanded** changes the cell size; **Show in bays…** picks what cells display (model, capacity, serial suffix, HDD/SSD, controller · PHY, `/dev`, pool/role, status text). These view choices are remembered per browser; everything else lives on the server.

The **Drives** tab gains a *Bay* column that opens the drive's bay in the mapper. The **Overview** shows which controllers are one physical card.

### What the colours mean

| Status | Meaning |
| --- | --- |
| **Connected** | The mapped drive is attached now (matched by WWN/serial). A ⇄ mark means it is on a different controller link than when mapped — *Accept the new link* records that, nothing changes until you do |
| **Empty** | No drive is expected in this bay |
| **Missing** | The mapped drive isn't attached. The cell shows when it was last seen |
| **Changed** | The mapped drive is gone *and* a different drive is on the same controller PCI function + PHY. Probably a replacement — it is only suggested; **Review replacement…** shows old and new side by side and needs confirming |
| **Unknown** | Can't be checked (discovery failed, or the mapping has no WWN/serial) |
| **Conflict** | Identity evidence disagrees (WWN matches but serial differs, two attached devices share a serial, one drive mapped to two bays) — it is shown, never guessed |

Idle/unpopulated PHYs are not failures and never make a bay missing. A new drive appearing somewhere else never marks a missing one as replaced.

### Identities

- **Bay** = enclosure ID + bay ID, both random and permanent, plus your label. Resizing keeps IDs; shrinking over a mapped bay is refused unless you choose to keep it as a *parked* bay (mapping and label kept).
- **Drive** = WWN when it is unique and plausible, otherwise the manufacturer serial (unique, not a placeholder). Devices with neither can't be mapped, and say why.
- **Controller link** = controller PCI function, host SAS address, PHY or expander + PHY, `/cN` + slot from storcli when they can be joined by serial, HCTL. Recorded as *evidence* only. `/dev/sdX` is shown but never used to decide anything, and a storcli slot is never assumed to be a front-panel bay.

Controller functions are grouped into one physical card only when that is safe: same board serial **and** board name (the 9300-16i case: two SAS3008 functions, one serial), or — when firmware reports no serial — two functions behind the card's own PCIe switch with a board name known to carry exactly two controllers. Otherwise each function stays its own card.

### Replacement history

**Replace drive mapping…** (or *Review replacement…* on a Changed bay) records old drive, new drive, both WWNs/serials, time and your note, then updates the bay. Every assign, unassign, move, swap, relink, import and recovery is kept too (last 10 000 records). It is a mapping change only — no `zpool replace`, RAID rebuild, formatting, wiping or copying.

### Locate

*Locate drive* first re-checks that the drive attached now is the one mapped to the bay, then uses the existing locate helper: read activity twice a second so the drive's own activity light flickers, stopping after the Settings limit (default 300 s), with *Stop locate*. **No locate LED is driven**: with direct SATA breakout cabling there may be no per-bay light to see. It wakes a sleeping drive. NVMe drives and devices without `/dev/bsg/H:C:T:L` get an "unsupported" message instead.

### Export, import, backups

- **Export**: layout + mappings as JSON (`"format": "hbaviewer-baymap", "schema": 1`). **Backup**: the same plus the history.
- **Import** previews first: what would be added, conflicts and warnings. *Merge* adds enclosures alongside yours. It skips enclosures whose ID already exists (a re-import) and never merges by name; a mapped drive that's already mapped here is dropped from the import. *Replace* / *Restore* swaps the whole map (confirmed twice; the old file goes to the backups).
- **Print** gives a paper copy: each enclosure's grid plus a table of bay, status, model, serial, WWN, capacity, controller link and `/dev`.
- Every save keeps the previous file in `/var/lib/hbaviewer/baymap-backups/` (last 10). If `baymap.json` is ever damaged, the tab offers *Restore this* for a backup or *Start with an empty map*; the damaged file is kept as `baymap.json.corrupt-<time>`.

### Where it's stored

| File | What |
| --- | --- |
| `/etc/hbaviewer/baymap.json` (0640) | Enclosures, bays, mappings — the authority. Schema-versioned; older files are migrated on first read |
| `/var/lib/hbaviewer/baymap-history.json` | Assignment / replacement history |
| `/var/lib/hbaviewer/baymap-seen.json` | First / last seen per drive (for "last seen" and "new") |
| `/var/lib/hbaviewer/baymap-backups/` | Previous versions of `baymap.json` |

Every change carries the revision the page loaded; if another browser saved in between, the change is refused and the page reloads (no silent overwrites). Writes are atomic (temp file + fsync + rename) under a file lock. Install/upgrade never touch these files; `uninstall.sh` keeps them; `uninstall.sh --purge` deletes them.

### Command line

```bash
sudo hbaviewer-ctl baymap get | jq '.status.counts'
sudo hbaviewer-ctl baymap inventory --fast | jq '.drives[] | {dev, key, link: .conn.port}'
sudo hbaviewer-ctl baymap backup > baymap-backup.json
echo '{"bay_id":"b0123456789"}' | sudo hbaviewer-ctl baymap history | jq
```

Mutations (`create update delete reorder assign unassign move swap replace relink note delete-parked import restore recover locate`) take a JSON request on stdin that must include the current `revision`.

### Limits

- Discovery is Linux's view (lsblk + sysfs) plus the storcli/lsiutil listing the Drives tab already reads; behind SAS expanders the link evidence is expander + PHY. Bays are always yours to define — nothing is inferred from enclosure services.
- Drives that hide their identity (some USB bridges report the bridge's serial/WWN) show up as "no reliable identity" and can't be mapped.
- Up to 100 enclosures, 64×64 cells (1024 per enclosure); requests up to 2 MB.

## Command line

The helper works on its own too:

```bash
sudo hbaviewer-ctl env          # what was detected
sudo hbaviewer-ctl overview | jq
sudo hbaviewer-ctl health | jq '.controllers[] | {idx, state, reason}'
sudo hbaviewer-ctl baseline set 0
sudo hbaviewer-ctl config set ALERT_THRESHOLD=86 TEMP_UNIT=F
```

## Uninstall

```bash
sudo ./uninstall.sh            # keeps /etc/hbaviewer and /var/lib/hbaviewer (settings, bay maps, history)
sudo ./uninstall.sh --purge    # removes those too — export a Bay Mapper backup first
```

## Testing status

Exercised end-to-end through a real Cockpit (314) session — page → `cockpit.spawn` with superuser → helper → upstream scripts — against a fake `storcli64` that replays upstream's anonymised fixtures (dual 9400 cards), on Ubuntu with mawk as the system awk. The health port has its own unit tests (`python3 tests/test_health.py`).

The Bay Mapper is tested on synthetic hardware: `tests/fake_hw.py` builds a fake `/sys` (PCI path, scsi_host, sas_device, sas_phy, block links laid out like the kernel's) and an `lsblk` fixture for (a) a SAS9300-16i — two SAS3008 functions sharing board serial SP73816973, 8 Intel SSDs on /c0, an Intel SSD + 4 Seagates + 3 idle PHYs on /c1 — plus a SATA SSD and an NVMe drive, (b) two unrelated HBAs, and (c) a dual-chip card with no serial. `tests/test_baymap.py` (90+ checks) drives the real helper through those: layouts, moves/swaps, duplicates, WWN/serial matching, re-enumeration, controller moves, missing/changed/new drives, replacement history, concurrent writers, corruption recovery, migration, import/export and input validation. `tests/run_all.sh` runs everything, including the existing tabs' regression checks and JS syntax. The tab was also driven in a real Cockpit session against the fake 9300-16i: drag-assign, tap-assign, replace, park-on-shrink, import preview, locate, print, phone width.

It has **not** been run against a physical HBA yet; the reader scripts are upstream's, so their hardware coverage (and caveats, e.g. SAS4 being community-verified only) carries over unchanged.

## License

MIT, same as upstream. Upstream copyright DevlinDelFuego and FugginOld; see `LICENSE`.
