/* HBA Viewer — Physical Bay Mapper tab.
 *
 * Draws user-defined enclosure grids and the drives mapped into them. The
 * helper (hbaviewer-ctl baymap …) is the authority: every change is a JSON
 * request carrying the revision this page loaded, and the helper refuses it if
 * someone else changed the map in between. Labels and every value from the
 * helper reach the DOM through textContent (h()/fill() from hbaviewer.js).
 *
 * Identities: bay = enclosure ID + bay ID (permanent); drive = WWN / serial;
 * controller link (PCI, PHY, /cN, HCTL, /dev) = evidence only.
 */
"use strict";

const BM_FIELDS = [
    ["model", "Model"], ["capacity", "Capacity"], ["serial", "Serial (last 6)"], ["type", "HDD/SSD"],
    ["link", "Controller · PHY"], ["dev", "/dev name"], ["role", "Pool / role"], ["state", "Status text"],
];
const BM_STATE = {
    connected: ["ok", "Connected"], empty: ["unknown", "Empty"], missing: ["critical", "Missing"],
    changed: ["warning", "Changed"], unknown: ["unknown", "Unknown"], conflict: ["critical", "Conflict"],
    nonexistent: ["unknown", "Not a bay"],
};
const BM_ICON = { "": "", hdd: "▭", ssd: "▯", server: "▤", jbod: "▦", cage: "▥" };

const bm = {
    pick: null,           // drive key picked for tap/keyboard placement
    filter: { q: "", ctl: "", type: "", show: "all" },
    prefs: (() => {
        const d = { mode: "expanded", fields: ["model", "capacity", "serial", "link", "role"] };
        try { return Object.assign(d, JSON.parse(localStorage.getItem("hbaviewer:baymap") || "{}")) } catch (e) { return d }
    })(),
    full: true,
};
function bmSavePrefs() { try { localStorage.setItem("hbaviewer:baymap", JSON.stringify(bm.prefs)) } catch (e) { /* storage blocked */ } }

LOADERS.baymap = () => { const full = bm.full; bm.full = false; return full ? ctl("baymap", "get") : ctl("baymap", "get", "--fast") };
RENDER.baymap = renderBaymap;

/* ── helpers ───────────────────────────────────────────────────────────── */
const bmData = () => state.data.baymap;
const driveByKey = k => ((bmData().inventory || {}).drives || []).find(d => d.key === k);
const serialTail = s => s ? "…" + String(s).slice(-6) : "";
const linkText = c => !c ? "" : [c.ctl_index !== undefined && c.ctl_index !== null ? `/c${c.ctl_index}` : (c.pci || ""),
    c.port || (c.phy !== null && c.phy !== undefined ? `PHY ${c.phy}` : "")].filter(Boolean).join(" · ");
const fmtSize = b => { if (!b) return ""; let u = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0; while (b >= 1000 && i < 5) { b /= 1000; i++ } return `${b < 10 ? b.toFixed(1) : Math.round(b)} ${u[i]}` };

function bayInfo(bayId) {
    const D = bmData();
    for (const e of D.enclosures) for (const b of e.bays) if (b.id === bayId) return { e, b, st: D.status.bays[bayId] || {}, a: D.assignments[bayId] };
    return null;
}

function bayOfDrive(key) {
    const D = bmData();
    for (const [bid, s] of Object.entries(D.status.bays)) if (s.drive === key) return bid;
    return null;
}

async function bmDo(op, req, quiet) {
    const r = await ctlIn(["baymap", op], Object.assign({ revision: bmData().revision }, req));
    if (r.code === "conflict") {
        toast("The bay map was changed elsewhere — reloaded. Try again.", true);
        await load("baymap", true);
        throw Object.assign(new Error(r.error), { handled: true });
    }
    if (r.error) throw Object.assign(new Error(r.error), { code: r.code, payload: r });
    if (!quiet) await load("baymap", true);
    return r;
}

function toast(msg, err) {
    let box = $("#bm-toasts");
    if (!box) { box = h("div", { id: "bm-toasts", role: "status", "aria-live": "polite" }); document.body.append(box) }
    const t = h("div", { class: "bm-toast" + (err ? " err" : "") }, msg);
    box.append(t);
    setTimeout(() => t.remove(), err ? 8000 : 3500);
}

/* A second, stacked dialog for confirmations so the bay dialog underneath keeps its state. */
function bmConfirm(title, body, okLabel, danger) {
    return new Promise(resolve => {
        const dlg = h("dialog", { class: "bm-confirm", "aria-label": title });
        const done = v => { dlg.close(); dlg.remove(); resolve(v) };
        dlg.append(h("div", { class: "hv-dlg-head" }, h("h2", {}, title)),
            h("div", { class: "bm-confirm-body" }, body),
            h("div", { class: "bm-confirm-foot" },
                h("button", { class: `hv-btn ${danger ? "hv-btn-danger" : ""}`, type: "button", onclick: () => done(true) }, okLabel || "OK"),
                h("button", { class: "hv-btn hv-btn-plain", type: "button", onclick: () => done(false) }, "Cancel")));
        dlg.addEventListener("cancel", e => { e.preventDefault(); done(false) });
        document.body.append(dlg);
        dlg.showModal();
    });
}

function errText(e) { return e && e.message ? e.message : String(e) }

/* ── tab render ────────────────────────────────────────────────────────── */
function renderBaymap() {
    const D = bmData(), el = panel("baymap");
    const err = payloadError(D);
    if (err) { fill(el, errorBox(err)); return }
    if (D.corrupt) return renderCorrupt(D, el);
    const counts = D.status.counts || {};
    const order = ["missing", "changed", "conflict", "unknown", "connected", "empty"];
    const toolbar = h("div", { class: "hv-toolbar bm-toolbar" },
        h("button", { class: "hv-btn", type: "button", onclick: () => editEnclosure(null) }, "New enclosure"),
        h("div", { class: "bm-seg", role: "group", "aria-label": "Display mode" },
            ["compact", "expanded"].map(m => h("button", { type: "button", class: bm.prefs.mode === m ? "on" : null, "aria-pressed": String(bm.prefs.mode === m),
                onclick: () => { bm.prefs.mode = m; bmSavePrefs(); renderBaymap() } }, m === "compact" ? "Compact" : "Expanded"))),
        fieldsMenu(),
        h("span", { class: "grow" }),
        order.filter(k => counts[k]).map(k => h("span", { class: `bm-count s-${BM_STATE[k][0]}` }, `${counts[k]} ${BM_STATE[k][1].toLowerCase()}`)),
        h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: exportMap }, "Export"),
        h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: importMap }, "Import"),
        h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: backupMap, title: "Layout, mappings and history in one file" }, "Backup"),
        h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: printMap }, "Print"));
    const notes = [];
    if (D.migration_notes) notes.push(h("div", { class: "hv-alert info" }, "Bay map file upgraded: ", D.migration_notes.join("; "), "."));
    const ctlSrc = (D.inventory || {}).controller || {};
    if (ctlSrc.error) notes.push(h("div", { class: "hv-alert" }, "Controller listing unavailable (", ctlSrc.error, "). Drives are still identified from lsblk/sysfs; /cN and slot evidence is missing."));
    if (bm.pick) {
        const d = driveByKey(bm.pick);
        notes.push(h("div", { class: "hv-alert info bm-pickbar" }, "Placing ", h("b", {}, d ? `${d.model} ${serialTail(d.serial)}` : bm.pick),
            " — choose an empty bay (Esc to cancel). ",
            h("button", { class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", onclick: () => { bm.pick = null; renderBaymap() } }, "Cancel")));
    }
    const encs = D.enclosures.length ? D.enclosures.map(enclosureCard)
        : h("div", { class: "hv-card" }, h("div", { class: "hv-empty" },
            h("p", {}, "No enclosures yet. Describe your physical bays — an 8-bay cage, a hot-swap section, a JBOD — then drag drives into them."),
            h("button", { class: "hv-btn", type: "button", onclick: () => editEnclosure(null) }, "Create the first enclosure")));
    fill(el, toolbar, notes, h("div", { class: "bm-layout" }, h("div", { class: "bm-main" }, encs), inventoryPanel()));
    if (state.bmFocus) { const id = state.bmFocus; state.bmFocus = null; if (bayInfo(id)) setTimeout(() => openBay(id)) }
}

function fieldsMenu() {
    const box = h("details", { class: "bm-fields" }, h("summary", { class: "hv-btn hv-btn-secondary" }, "Show in bays…"),
        h("div", { class: "bm-fields-list" }, BM_FIELDS.map(([k, label]) => {
            const cb = h("input", { type: "checkbox", id: `bmf-${k}` });
            cb.checked = bm.prefs.fields.includes(k);
            cb.addEventListener("change", () => {
                bm.prefs.fields = BM_FIELDS.map(f => f[0]).filter(f => f === k ? cb.checked : bm.prefs.fields.includes(f));
                bmSavePrefs(); renderBaymap();
            });
            return h("label", { for: `bmf-${k}` }, cb, " ", label);
        })));
    return box;
}

function renderCorrupt(D, el) {
    fill(el, h("div", { class: "hv-alert err" },
        h("b", {}, "The bay map file is damaged. "), D.corrupt.reason,
        h("p", {}, "Nothing was overwritten. Restoring keeps the damaged file next to it (", h("code", {}, D.corrupt.path + ".corrupt-…"), ")."),
        D.backups.length ? h("table", { class: "hv-t" }, h("tbody", {}, D.backups.map(b => h("tr", {},
            h("td", { class: "mono" }, b.name), h("td", {}, b.valid ? `${b.enclosures} enclosures, ${b.assignments} mappings` : `unusable: ${b.error}`),
            h("td", {}, b.valid ? h("button", { class: "hv-btn hv-btn-sm", type: "button", onclick: () => recover("restore_backup", b.name) }, "Restore this") : null)))))
            : h("p", {}, "No backups are available."),
        h("button", { class: "hv-btn hv-btn-danger", type: "button", style: "margin-top:10px", onclick: () => recover("start_empty") }, "Start with an empty map")));
}

async function recover(action, backup) {
    if (action === "start_empty" && !await bmConfirm("Start empty?", h("p", {}, "The damaged file is kept aside, but the map starts with no enclosures."), "Start empty", true)) return;
    try {
        const r = await ctlIn(["baymap", "recover"], { action, backup });
        if (r.error) throw new Error(r.error);
        toast("Recovered. Damaged file kept as " + r.kept.split("/").pop());
        bm.full = true; load("baymap", true);
    } catch (e) { toast(errText(e), true) }
}

/* ── enclosure grid ────────────────────────────────────────────────────── */
function enclosureCard(e, idx) {
    const D = bmData();
    const vertical = e.orientation === "vertical";
    const grid = h("div", { class: `bm-grid ${vertical ? "vertical" : ""} ${bm.prefs.mode}`, role: "grid", "aria-label": e.name,
        style: `grid-template-columns: repeat(${e.cols}, minmax(${bm.prefs.mode === "compact" ? (vertical ? 46 : 76) : (vertical ? 92 : 150)}px, 1fr))` });
    const cells = e.bays.filter(b => b.row !== null).sort((a, b) => a.row - b.row || a.col - b.col);
    for (const b of cells) grid.append(bayCell(e, b));
    const parked = e.bays.filter(b => b.row === null);
    const head = h("div", { class: "hv-card-head bm-ehead", style: e.color ? `border-top: 4px solid ${e.color}` : null },
        h("div", {},
            h("h3", {}, BM_ICON[e.icon] ? h("span", { class: "bm-icon", "aria-hidden": "true" }, BM_ICON[e.icon] + " ") : null, e.name,
                e.face ? h("span", { class: "bm-face" }, e.face) : null),
            e.description || e.connection ? h("div", { class: "hv-muted" }, [e.description, e.connection ? "⇄ " + e.connection : ""].filter(Boolean).join(" · ")) : null),
        h("div", { class: "hv-toolbar", style: "margin:0" },
            h("span", { class: "hv-muted" }, `${e.rows}×${e.cols}`),
            idx > 0 ? h("button", { class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", title: "Move up", "aria-label": "Move enclosure up", onclick: () => reorder(e.id, -1) }, "↑") : null,
            idx < D.enclosures.length - 1 ? h("button", { class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", title: "Move down", "aria-label": "Move enclosure down", onclick: () => reorder(e.id, 1) }, "↓") : null,
            h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button", onclick: () => editEnclosure(e) }, "Edit"),
            h("button", { class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", onclick: () => deleteEnclosure(e) }, "Delete")));
    return h("div", { class: "hv-card bm-enc" }, head, h("div", { class: "hv-card-body" }, grid,
        parked.length ? h("div", { class: "bm-parked" }, h("b", {}, "Parked bays "), h("span", { class: "hv-muted" }, "(outside the grid after a resize; their mappings are kept)"),
            h("div", { class: "bm-parked-list" }, parked.map(b => bayCell(e, b)))) : null));
}

function bayCell(e, b) {
    const D = bmData(), st = D.status.bays[b.id] || { state: "unknown" }, a = D.assignments[b.id];
    if (b.absent) return h("div", { class: "bm-bay absent", role: "gridcell", "aria-label": "not a bay" });
    const d = st.drive ? driveByKey(st.drive) : null;
    const saved = a ? a.drive : null;
    const [tone, label] = BM_STATE[st.state] || ["unknown", st.state];
    const f = bm.prefs.fields, lines = [];
    const src = d || (saved ? { model: saved.model, size_bytes: saved.size_bytes, serial: saved.serial, type: saved.type } : null);
    if (src) {
        if (f.includes("model") && src.model) lines.push(h("div", { class: "m" }, src.model));
        const meta = [f.includes("capacity") ? fmtSize(src.size_bytes) : "", f.includes("type") ? src.type : "", f.includes("serial") ? serialTail(src.serial) : ""].filter(Boolean);
        if (meta.length) lines.push(h("div", { class: "x" }, meta.join(" · ")));
        if (d && f.includes("link") && linkText(d.conn)) lines.push(h("div", { class: "x" }, linkText(d.conn)));
        if (d && f.includes("dev")) lines.push(h("div", { class: "x mono" }, d.dev));
        if (d && f.includes("role") && (d.roles || []).length) lines.push(h("div", { class: "x role" }, d.roles.join(", ")));
    }
    if (st.state === "missing") lines.push(h("div", { class: "x why" }, st.last_seen ? `last seen ${ago(st.last_seen)}` : "not attached"));
    else if (f.includes("state") && st.state !== "connected" && st.state !== "empty") lines.push(h("div", { class: "x why" }, st.reason || ""));
    const cell = h("button", {
        type: "button", class: `bm-bay st-${st.state} tone-${tone}${st.locating ? " locating" : ""}${bm.pick && !a ? " droptarget" : ""}`,
        role: "gridcell", dataset: { bay: b.id, row: b.row ?? "", col: b.col ?? "", enc: e.id },
        "aria-label": `Bay ${st.label}: ${label}${src && src.model ? ", " + src.model : ""}`,
        title: st.reason || (st.moved || ""), draggable: a ? "true" : "false",
        onclick: () => bayClick(b.id), onkeydown: ev => gridKeys(ev, e, b),
    },
        h("div", { class: "bm-bay-top" }, h("span", { class: "lbl" }, st.label), h("span", { class: `dot s-${tone}`, title: label }),
            bm.prefs.mode === "expanded" ? h("span", { class: "st" }, st.locating ? "locating" : label) : null,
            st.moved ? h("span", { class: "moved", title: st.moved }, "⇄") : null),
        bm.prefs.mode === "expanded" ? lines : (src && src.model ? h("div", { class: "x" }, serialTail(src.serial) || src.model) : null));
    cell.addEventListener("dragstart", ev => { ev.dataTransfer.setData("application/x-bm-bay", b.id); ev.dataTransfer.effectAllowed = "move" });
    cell.addEventListener("dragover", ev => {
        if ([...ev.dataTransfer.types].some(t => t === "application/x-bm-drive" || t === "application/x-bm-bay")) { ev.preventDefault(); cell.classList.add("over") }
    });
    cell.addEventListener("dragleave", () => cell.classList.remove("over"));
    cell.addEventListener("drop", ev => {
        ev.preventDefault(); cell.classList.remove("over");
        const drive = ev.dataTransfer.getData("application/x-bm-drive"), from = ev.dataTransfer.getData("application/x-bm-bay");
        if (drive) placeDrive(drive, b.id); else if (from && from !== b.id) moveBay(from, b.id);
    });
    return cell;
}

function gridKeys(ev, e, b) {
    const moves = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };
    if (ev.key === "Escape" && bm.pick) { bm.pick = null; renderBaymap(); return }
    if (!moves[ev.key] || b.row === null) return;
    ev.preventDefault();
    const [dr, dc] = moves[ev.key];
    for (let r = b.row + dr, c = b.col + dc; r >= 0 && c >= 0 && r < e.rows && c < e.cols; r += dr, c += dc) {
        const t = document.querySelector(`.bm-bay[data-enc="${e.id}"][data-row="${r}"][data-col="${c}"]`);
        if (t) { t.focus(); return }
    }
}

function bayClick(bayId) {
    const info = bayInfo(bayId);
    if (bm.pick && info && !info.a) { const k = bm.pick; bm.pick = null; placeDrive(k, bayId); return }
    openBay(bayId);
}

async function placeDrive(key, bayId) {
    const info = bayInfo(bayId), d = driveByKey(key);
    if (!info) return;
    if (info.b.absent) return toast("That cell is not a bay.", true);
    const other = bayOfDrive(key);
    try {
        if (other && other !== bayId) {
            const o = bayInfo(other);
            if (info.a) return swapBays(other, bayId);
            if (!await bmConfirm("Move mapping?", h("p", {}, `${d ? d.model + " " + serialTail(d.serial) : "This drive"} is mapped to ${o.e.name} bay ${o.st.label}. Move it to ${info.e.name} bay ${info.st.label}?`), "Move")) return;
            await bmDo("move", { from: other, to: bayId });
        } else if (info.a) {
            return replaceFlow(bayId, key);
        } else {
            await bmDo("assign", { bay_id: bayId, drive: key });
            toast(`Mapped to ${info.e.name} bay ${info.st.label}`);
        }
    } catch (e) { if (!e.handled) toast(errText(e), true) }
}

async function moveBay(from, to) {
    const t = bayInfo(to);
    if (!t || t.b.absent) return;
    if (t.a) return swapBays(from, to);
    try { await bmDo("move", { from, to }); toast("Mapping moved") } catch (e) { if (!e.handled) toast(errText(e), true) }
}

async function swapBays(a, b) {
    const A = bayInfo(a), B = bayInfo(b);
    const desc = x => x.a ? `${x.a.drive.model || "drive"} ${serialTail(x.a.drive.serial)}` : "(empty)";
    if (!await bmConfirm("Swap these mappings?", h("div", {},
        h("p", {}, `${A.e.name} bay ${A.st.label}: ${desc(A)}`), h("p", {}, `${B.e.name} bay ${B.st.label}: ${desc(B)}`),
        h("p", { class: "hv-muted" }, "Only the map changes. Nothing is done to the disks.")), "Swap")) return;
    try { await bmDo("swap", { a, b, confirm: true }); toast("Mappings swapped") } catch (e) { if (!e.handled) toast(errText(e), true) }
}

/* ── inventory ─────────────────────────────────────────────────────────── */
function inventoryPanel() {
    const D = bmData(), inv = D.inventory || {}, drives = inv.drives || [];
    const unassigned = new Map((D.status.unassigned || []).map(u => [u.key, u]));
    const q = h("input", { type: "search", placeholder: "Search model, serial, WWN, /dev…", "aria-label": "Search drives", value: bm.filter.q });
    const ctlSel = h("select", { "aria-label": "Controller" }, h("option", { value: "" }, "All controllers"),
        [...new Set(drives.map(d => d.conn.pci).filter(Boolean))].sort().map(p => h("option", { value: p }, ctlLabel(p))));
    ctlSel.value = bm.filter.ctl;
    const typeSel = h("select", { "aria-label": "Type" }, [["", "All types"], ["HDD", "HDD"], ["SSD", "SSD"], ["NVMe", "NVMe"]].map(([v, t]) => h("option", { value: v }, t)));
    typeSel.value = bm.filter.type;
    const showSel = h("select", { "aria-label": "Show" }, [["all", "All drives"], ["unassigned", "Unassigned"], ["assigned", "Assigned"]].map(([v, t]) => h("option", { value: v }, t)));
    showSel.value = bm.filter.show;
    const list = h("div", { class: "bm-inv-list", role: "list" });
    const draw = () => {
        const s = bm.filter.q.trim().toLowerCase();
        const rows = drives.filter(d => (!bm.filter.ctl || d.conn.pci === bm.filter.ctl) && (!bm.filter.type || d.type === bm.filter.type)
            && (bm.filter.show === "all" || (bm.filter.show === "unassigned") === unassigned.has(d.key))
            && (!s || [d.model, d.serial, d.wwn, d.dev, d.vendor, linkText(d.conn), fmtSize(d.size_bytes), (d.roles || []).join(" ")].join(" ").toLowerCase().includes(s)));
        fill(list, rows.length ? rows.map(d => invItem(d, unassigned.get(d.key))) : h("div", { class: "hv-empty" }, "No drives match."));
    };
    q.addEventListener("input", () => { bm.filter.q = q.value; draw() });
    ctlSel.addEventListener("change", () => { bm.filter.ctl = ctlSel.value; draw() });
    typeSel.addEventListener("change", () => { bm.filter.type = typeSel.value; draw() });
    showSel.addEventListener("change", () => { bm.filter.show = showSel.value; draw() });
    draw();
    return h("aside", { class: "bm-side" },
        h("div", { class: "hv-card" }, h("div", { class: "hv-card-head" }, h("h3", {}, "Drives"),
            h("span", { class: "hv-muted" }, `${drives.length} found · ${unassigned.size} unassigned`)),
            h("div", { class: "hv-card-body" },
                h("div", { class: "bm-filters" }, q, ctlSel, typeSel, showSel),
                h("p", { class: "hv-muted small" }, "Drag a drive onto a bay, or select it and then click a bay. The map remembers drives by WWN and serial, never by /dev name."),
                list)),
        controllersCard(inv));
}

function ctlLabel(pci) {
    const inv = bmData().inventory || {};
    for (const g of inv.hbas || []) for (const f of g.functions) if (f.pci === pci) return `${f.ctl_index !== null ? "/c" + f.ctl_index + " · " : ""}${g.board_name || f.driver} · ${pci}`;
    return pci;
}

function invItem(d, un) {
    const bay = !un ? bayOfDrive(d.key) : null, bi = bay ? bayInfo(bay) : null;
    const picked = bm.pick === d.key;
    const it = h("div", { class: `bm-inv${un ? "" : " assigned"}${picked ? " picked" : ""}${d.key ? "" : " noid"}`, role: "listitem",
        draggable: d.key ? "true" : "false", tabindex: "0", "aria-label": `${d.model} ${d.serial}${bi ? ", in " + bi.e.name + " bay " + bi.st.label : ", unassigned"}` },
        h("div", { class: "bm-inv-top" }, h("b", {}, d.model || "(no model)"), h("span", { class: "hv-muted" }, fmtSize(d.size_bytes)),
            d.type ? h("span", { class: "bm-tag" }, d.type) : null, un && un.new ? h("span", { class: "bm-tag new" }, "new") : null),
        h("div", { class: "x mono" }, `SN ${d.serial || "?"}${d.wwn ? " · WWN " + d.wwn : ""}`),
        h("div", { class: "x" }, [d.dev, linkText(d.conn), (d.roles || []).join(", ")].filter(Boolean).join(" · ")),
        bi ? h("div", { class: "x inbay" }, `in ${bi.e.name} · bay ${bi.st.label}`) : null,
        (d.identity_issues || []).length ? h("div", { class: "x why" }, d.identity_issues.join("; ")) : null);
    if (!d.key) return it;
    it.addEventListener("dragstart", ev => { ev.dataTransfer.setData("application/x-bm-drive", d.key); ev.dataTransfer.effectAllowed = "copyMove" });
    const pick = () => {
        if (bi) { openBay(bay); return }
        bm.pick = picked ? null : d.key; renderBaymap();
        if (bm.pick) { const t = document.querySelector(".bm-bay.droptarget"); if (t) t.focus() }
    };
    it.addEventListener("click", pick);
    it.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick() } });
    return it;
}

function controllersCard(inv) {
    const groups = inv.hbas || [];
    if (!groups.length) return null;
    return h("div", { class: "hv-card", style: "margin-top:12px" }, h("div", { class: "hv-card-head" }, h("h3", {}, "Controllers")),
        h("div", { class: "hv-card-body" }, groups.map(g => h("div", { class: "bm-hba" },
            h("div", {}, h("b", {}, g.board_name || "HBA"), g.board_serial ? h("span", { class: "hv-muted" }, ` · SN ${g.board_serial}`) : null),
            h("div", { class: "hv-muted small" }, g.functions.length > 1 ? `${g.functions.length} controller functions on one card — grouped by ${g.grouped_by}` : "single controller",
                ` · ${g.phys_total} PHYs, ${g.phys_linked} linked, ${g.phys_total - g.phys_linked} idle · ${g.drives} drive${g.drives === 1 ? "" : "s"}`),
            h("ul", { class: "bm-funcs" }, g.functions.map(f => h("li", {},
                h("span", { class: "mono" }, f.ctl_index !== null ? `/c${f.ctl_index}` : "/c?"), ` ${f.pci} · SAS ${f.host_sas || "?"} · `,
                `${f.phys_linked}/${f.phys_total} PHYs linked · ${f.drives} drive${f.drives === 1 ? "" : "s"}`)))))),
        h("p", { class: "hv-muted small", style: "margin:0 16px 12px" }, "Idle PHYs are ports with nothing plugged in, not faults."));
}

/* ── bay detail ────────────────────────────────────────────────────────── */
function idRows(x, title) {
    if (!x) return h("div", { class: "bm-id" }, h("h4", {}, title), h("p", { class: "hv-muted" }, "—"));
    return h("div", { class: "bm-id" }, h("h4", {}, title), h("dl", { class: "hv-kv" },
        [["Model", x.model], ["Serial", x.serial], ["WWN", x.wwn], ["Capacity", fmtSize(x.size_bytes)], ["Type", x.type]]
            .filter(([, v]) => v).map(([k, v]) => [h("dt", {}, k), h("dd", { class: k === "Serial" || k === "WWN" ? "mono" : null }, String(v))])));
}

function linkRows(c, title, devLabel) {
    if (!c) return h("div", { class: "bm-id" }, h("h4", {}, title), h("p", { class: "hv-muted" }, "—"));
    return h("div", { class: "bm-id" }, h("h4", {}, title), h("dl", { class: "hv-kv" },
        [["Controller", c.ctl_index !== undefined && c.ctl_index !== null ? `/c${c.ctl_index}` : ""], ["PCI", c.pci], ["Port", c.port],
            ["Host SAS address", c.host_sas], ["Expander", c.expander], ["Controller slot", c.slot], ["HCTL", c.hctl], [devLabel || "/dev", c.dev]]
            .filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [h("dt", {}, k), h("dd", { class: "mono" }, String(v))])));
}

async function openBay(bayId) {
    const info = bayInfo(bayId);
    if (!info) return;
    const { e, b, st, a } = info, D = bmData();
    const d = st.drive ? driveByKey(st.drive) : null;
    const [tone, label] = BM_STATE[st.state] || ["unknown", st.state];
    const lbl = h("input", { type: "text", value: b.label, placeholder: st.label, maxlength: 32, "aria-label": "Bay label", style: "min-width:120px" });
    const note = h("input", { type: "text", value: b.note, maxlength: 500, "aria-label": "Bay note", placeholder: "e.g. top row, fan side" });
    const conn = h("input", { type: "text", value: b.connection || "", maxlength: 120, "aria-label": "Cable / connection label", placeholder: "e.g. breakout C, lead 3" });
    const saveBay = h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button", onclick: async () => {
        try { await bmDo("update", { id: e.id, cells: [{ id: b.id, label: lbl.value, note: note.value, connection: conn.value }] }); toast("Bay saved"); openBay(bayId) }
        catch (x) { if (!x.handled) toast(errText(x), true) }
    } }, "Save bay");

    const actions = [];
    if (!a && !b.absent && b.row !== null) {
        const sel = h("select", { "aria-label": "Drive to assign" }, h("option", { value: "" }, "Choose an unassigned drive…"),
            (D.status.unassigned || []).map(u => driveByKey(u.key)).filter(x => x && x.key)
                .map(x => h("option", { value: x.key }, `${x.model} · ${fmtSize(x.size_bytes)} · SN ${x.serial} · ${linkText(x.conn) || x.dev}`)));
        actions.push(h("div", { class: "hv-toolbar" }, sel, h("button", { class: "hv-btn", type: "button", onclick: async () => {
            if (!sel.value) return;
            try { await bmDo("assign", { bay_id: bayId, drive: sel.value }); toast("Mapped"); openBay(bayId) } catch (x) { if (!x.handled) toast(errText(x), true) }
        } }, "Assign")));
    }
    if (a) {
        const others = D.enclosures.flatMap(x => x.bays.filter(y => y.id !== bayId && !y.absent && y.row !== null).map(y => ({ e: x, b: y, st: D.status.bays[y.id], a: D.assignments[y.id] })));
        const tgt = h("select", { "aria-label": "Target bay" }, h("option", { value: "" }, "Move or swap with…"),
            others.map(o => h("option", { value: o.b.id }, `${o.e.name} · bay ${o.st.label}${o.a ? " (occupied → swap)" : ""}`)));
        actions.push(h("div", { class: "hv-toolbar" },
            tgt, h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: () => { if (tgt.value) { $("#hv-dialog").close(); moveBay(bayId, tgt.value) } } }, "Go"),
            h("button", { class: "hv-btn hv-btn-secondary", type: "button", onclick: () => replaceFlow(bayId, st.candidate || null) }, "Replace drive mapping…"),
            h("button", { class: "hv-btn hv-btn-plain", type: "button", onclick: async () => {
                if (!await bmConfirm("Unassign?", h("p", {}, `Remove ${a.drive.model || "the drive"} SN ${a.drive.serial || "?"} from bay ${st.label}? It stays in the history.`), "Unassign", true)) return;
                try { await bmDo("unassign", { bay_id: bayId }); toast("Unassigned"); openBay(bayId) } catch (x) { if (!x.handled) toast(errText(x), true) }
            } }, "Unassign")));
        if (st.moved) actions.push(h("div", { class: "hv-alert info" }, st.moved, ". ",
            h("button", { class: "hv-btn hv-btn-sm", type: "button", onclick: async () => {
                try { await bmDo("relink", { bay_id: bayId }); toast("Controller link updated"); openBay(bayId) } catch (x) { if (!x.handled) toast(errText(x), true) }
            } }, "Accept the new link")));
        if (st.state === "changed" && st.candidate) actions.push(h("div", { class: "hv-alert" }, st.reason, " ",
            h("button", { class: "hv-btn hv-btn-sm", type: "button", onclick: () => replaceFlow(bayId, st.candidate) }, "Review replacement…")));
        actions.push(locateBlock(bayId, st, d));
    }
    const hist = h("div", {}, h("span", { class: "hv-spin" }));
    openDialog(`${e.name} · bay ${st.label}`, h("div", { class: "bm-detail" },
        h("div", { class: "bm-status" }, pill(tone, st.locating ? "Locating" : label), st.reason ? h("span", {}, st.reason) : null,
            st.state === "missing" ? h("span", { class: "hv-muted" }, st.last_seen ? `Last seen ${ago(st.last_seen)}` : "Not seen since this map started tracking") : null),
        h("div", { class: "bm-ids" },
            h("div", { class: "bm-id" }, h("h4", {}, "Physical bay"), h("dl", { class: "hv-kv" },
                h("dt", {}, "Enclosure"), h("dd", {}, e.name + (e.face ? ` (${e.face})` : "")),
                h("dt", {}, "Position"), h("dd", {}, b.row === null ? "parked" : `row ${b.row + 1}, column ${b.col + 1}`),
                h("dt", {}, "Label"), h("dd", {}, lbl), h("dt", {}, "Cable"), h("dd", {}, conn), h("dt", {}, "Note"), h("dd", {}, note),
                h("dt", {}, "Bay ID"), h("dd", { class: "mono" }, b.id)), saveBay),
            a ? idRows(a.drive, d && st.state === "connected" ? "Drive (mapped, attached)" : "Drive (mapped)") : null,
            d && st.state !== "connected" ? idRows(Object.assign({}, d, { wwn: d.wwn }), "Drive attached that matches partly") : null,
            a ? linkRows(Object.assign({}, a.hint), "Controller link when mapped", "/dev then") : null,
            d ? linkRows(Object.assign({}, d.conn, { dev: d.dev }), "Controller link now", "/dev now") : null),
        a && a.note ? h("p", {}, h("b", {}, "Mapping note: "), a.note) : null,
        actions,
        h("h4", { style: "margin:16px 0 6px" }, "History"), hist));
    try {
        const r = await ctlIn(["baymap", "history"], { bay_id: bayId, limit: 50 });
        fill(hist, (r.history || []).length ? h("table", { class: "hv-t" }, h("tbody", {}, r.history.map(x => h("tr", {},
            h("td", {}, new Date(x.ts * 1000).toLocaleString()), h("td", {}, x.action),
            h("td", { class: "wrap" }, histText(x)))))) : h("p", { class: "hv-muted" }, "No changes recorded for this bay yet."));
    } catch (x) { fill(hist, errorBox(errText(x))) }
}

function histText(x) {
    const id = v => v ? `${v.model || "drive"} SN ${v.serial || "?"}${v.wwn ? " / " + v.wwn : ""}` : "";
    const parts = [];
    if (x.old) parts.push("old: " + id(x.old));
    if (x.new) parts.push("new: " + id(x.new));
    if (x.from_bay) parts.push(`from ${x.from_bay.enclosure_name} bay ${x.from_bay.bay_label}`);
    if (x.other_bay) parts.push(`with ${x.other_bay.enclosure_name} bay ${x.other_bay.bay_label}`);
    if (x.note) parts.push(`“${x.note}”`);
    return parts.join(" · ");
}

function locateBlock(bayId, st, d) {
    const D = bmData();
    const can = st.state === "connected" && d && /^\/dev\/sd/.test(d.dev) && d.conn.hctl;
    const why = !can ? (st.state !== "connected" ? "Locate needs the mapped drive to be attached." :
        d && d.type === "NVMe" ? "Locate is not available for NVMe drives here." : "This drive has no SCSI address to locate it by.") : null;
    const out = h("div", { class: "hv-muted small" });
    const go = async action => {
        out.textContent = action === "start" ? "Checking the drive's identity…" : "Stopping…";
        try {
            const r = await ctlIn(["baymap", "locate"], { bay_id: bayId, action });
            if (r.error) throw new Error(r.error);
            if (action === "start") out.textContent = `Verified ${r.verified.model} SN ${r.verified.serial} at ${r.verified.hctl} (${r.verified.dev}). ${r.explain} Auto-stop after ${r.secs} s.`;
            else out.textContent = "Stopped.";
            state.data.baymap.locating = r.active || [];
            load("baymap", true);
        } catch (x) { out.textContent = errText(x) }
    };
    return h("div", { class: "bm-locate" },
        h("h4", {}, "Locate"),
        h("p", { class: "hv-muted small" }, "Pulses read activity on the drive so its activity light flickers in a steady rhythm. No locate LED is driven: ",
            "with direct SATA breakout cabling there may be no per-bay LED to see, and the map above stays the reference. It wakes a sleeping drive. ",
            `Stops by itself after ${D.locate_max_secs || 300} s.`),
        h("div", { class: "hv-toolbar" },
            h("button", { class: "hv-btn hv-btn-secondary", type: "button", disabled: !can || st.locating, onclick: () => go("start") }, st.locating ? "Locating…" : "Locate drive"),
            h("button", { class: "hv-btn hv-btn-plain", type: "button", disabled: !can, onclick: () => go("stop") }, "Stop locate"),
            why ? h("span", { class: "hv-muted small" }, why) : null),
        out);
}

/* ── replacement workflow ──────────────────────────────────────────────── */
async function replaceFlow(bayId, proposedKey) {
    const info = bayInfo(bayId), D = bmData();
    if (!info || !info.a) return;
    const cands = (D.status.unassigned || []).map(u => driveByKey(u.key)).filter(x => x && x.key);
    const sel = h("select", { "aria-label": "New drive" }, h("option", { value: "" }, "Choose the new drive…"),
        cands.map(x => h("option", { value: x.key }, `${x.model} · ${fmtSize(x.size_bytes)} · SN ${x.serial} · ${linkText(x.conn) || x.dev}`)));
    if (proposedKey) sel.value = proposedKey;
    const right = h("div", {});
    const noteIn = h("input", { type: "text", maxlength: 1000, placeholder: "Note (optional), e.g. RMA number", "aria-label": "Note", style: "width:100%" });
    const drawRight = () => fill(right, sel.value ? idRows(Object.assign({}, driveByKey(sel.value), { wwn: driveByKey(sel.value).wwn }), "New drive") : h("p", { class: "hv-muted" }, "Pick the drive now in this bay."));
    sel.addEventListener("change", drawRight); drawRight();
    const ok = await bmConfirm(`Replace drive mapping · ${info.e.name} bay ${info.st.label}`, h("div", {},
        h("div", { class: "bm-ids two" }, idRows(info.a.drive, "Old drive (kept in history)"), h("div", {}, sel, right)),
        proposedKey && info.st.state === "changed" ? h("p", { class: "hv-alert info" }, "Suggested because a different drive appeared on the controller link this bay's drive used. Check the serial on the label before confirming.") : null,
        noteIn,
        h("p", { class: "hv-muted small" }, "This changes the map only. It does not run zpool replace, rebuild RAID, format, wipe or copy anything.")),
        "Confirm replacement");
    if (!ok) return;
    if (!sel.value) return toast("No new drive chosen.", true);
    try { await bmDo("replace", { bay_id: bayId, drive: sel.value, confirm: true, note: noteIn.value }); toast("Replacement recorded"); openBay(bayId) }
    catch (x) { if (!x.handled) toast(errText(x), true) }
}

/* ── enclosure editor ──────────────────────────────────────────────────── */
async function editEnclosure(e) {
    const isNew = !e;
    const v = e || { name: "", description: "", rows: 2, cols: 4, orientation: "horizontal", numbering: { order: "row", start: 1, prefix: "" }, color: "", icon: "", face: "", connection: "" };
    const f = {
        name: h("input", { type: "text", value: v.name, maxlength: 64, id: "bme-name" }),
        description: h("input", { type: "text", value: v.description, maxlength: 500, id: "bme-desc" }),
        rows: h("input", { type: "number", value: v.rows, min: 1, max: 64, id: "bme-rows" }),
        cols: h("input", { type: "number", value: v.cols, min: 1, max: 64, id: "bme-cols" }),
        orientation: h("select", { id: "bme-or" }, [["horizontal", "Horizontal (drives lie flat)"], ["vertical", "Vertical (drives stand up)"]].map(([a, b]) => h("option", { value: a }, b))),
        order: h("select", { id: "bme-ord" }, [["row", "Across rows (1 2 3 / 4 5 6)"], ["column", "Down columns (1 3 5 / 2 4 6)"]].map(([a, b]) => h("option", { value: a }, b))),
        start: h("input", { type: "number", value: v.numbering.start, min: 0, max: 9999, id: "bme-start" }),
        prefix: h("input", { type: "text", value: v.numbering.prefix, maxlength: 16, id: "bme-pre", placeholder: "e.g. B or Slot " }),
        face: h("select", { id: "bme-face" }, [["", "—"], ["front", "Front"], ["rear", "Rear"], ["top", "Top"], ["bottom", "Bottom"]].map(([a, b]) => h("option", { value: a }, b))),
        icon: h("select", { id: "bme-icon" }, [["", "None"], ["cage", "Drive cage"], ["hdd", "HDD"], ["ssd", "SSD"], ["server", "Server chassis"], ["jbod", "JBOD"]].map(([a, b]) => h("option", { value: a }, b))),
        color: h("input", { type: "color", value: v.color || "#0066cc", id: "bme-color" }),
        useColor: h("input", { type: "checkbox", id: "bme-usecolor" }),
        connection: h("input", { type: "text", value: v.connection, maxlength: 120, id: "bme-conn", placeholder: "e.g. /c0 ports 0-7 via 2× SFF-8643 → SATA breakout" }),
    };
    f.orientation.value = v.orientation; f.order.value = v.numbering.order; f.face.value = v.face; f.icon.value = v.icon; f.useColor.checked = !!v.color;
    // Cell editor: click a cell to toggle "not a bay".
    const absent = new Set((e ? e.bays.filter(b => b.absent && b.row !== null).map(b => `${b.row},${b.col}`) : []));
    const cellBox = h("div", {});
    const drawCells = () => {
        const R = Math.max(1, Math.min(64, +f.rows.value || 1)), C = Math.max(1, Math.min(64, +f.cols.value || 1));
        const g = h("div", { class: "bm-cellgrid", style: `grid-template-columns: repeat(${C}, 28px)` });
        for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) {
            const k = `${r},${c}`;
            g.append(h("button", { type: "button", class: absent.has(k) ? "off" : null, "aria-pressed": String(!absent.has(k)), title: absent.has(k) ? "not a bay" : "bay",
                onclick: () => { absent.has(k) ? absent.delete(k) : absent.add(k); drawCells() } }, absent.has(k) ? "×" : ""));
        }
        fill(cellBox, h("p", { class: "hv-muted small" }, "Click cells that are not real bays (a fan, a gap, a missing sled)."), g);
    };
    f.rows.addEventListener("input", drawCells); f.cols.addEventListener("input", drawCells); drawCells();
    const row = (label, inp, help) => [h("label", { for: inp.id }, label), inp, help ? h("div", { class: "help" }, help) : null];
    const ok = await bmConfirm(isNew ? "New enclosure" : `Edit ${e.name}`, h("div", { class: "bm-editor" },
        h("form", { class: "hv-form", onsubmit: ev => ev.preventDefault() },
            row("Name", f.name), row("Description", f.description), row("Rows", f.rows), row("Columns", f.cols),
            row("Orientation", f.orientation), row("Bay numbering", f.order), row("First number", f.start), row("Label prefix", f.prefix),
            row("Side", f.face, "Which side of the machine these bays face."), row("Icon", f.icon),
            [h("label", { for: "bme-usecolor" }, "Colour"), h("span", {}, f.useColor, " ", f.color)],
            row("Cabling", f.connection, "Optional: which controller / cable feeds this enclosure. Shown on the map only.")),
        cellBox), isNew ? "Create" : "Save");
    if (!ok) return;
    const R = +f.rows.value, C = +f.cols.value;
    const body = { name: f.name.value, description: f.description.value, rows: R, cols: C, orientation: f.orientation.value,
        numbering: { order: f.order.value, start: +f.start.value, prefix: f.prefix.value }, face: f.face.value, icon: f.icon.value,
        color: f.useColor.checked ? f.color.value : "", connection: f.connection.value };
    try {
        if (isNew) {
            await bmDo("create", { enclosure: Object.assign(body, { absent: [...absent].map(k => k.split(",").map(Number)) }) });
            toast(`Created ${body.name}`);
        } else {
            const cells = e.bays.filter(b => b.row !== null && b.row < R && b.col < C).map(b => ({ id: b.id, absent: absent.has(`${b.row},${b.col}`) }))
                .filter(c => c.absent !== !!e.bays.find(b => b.id === c.id).absent);
            const send = park => bmDo("update", { id: e.id, changes: body, cells, park });
            try { await send(false) }
            catch (x) {
                if (x.code !== "would_orphan") throw x;
                if (!await bmConfirm("Keep mapped bays?", h("div", {}, h("p", {}, x.message),
                    h("p", {}, "Keep them as parked bays? Their IDs and mappings stay; you can move the drives into the new grid afterwards.")), "Keep as parked")) return;
                await send(true);
            }
            // New cells created by growing the grid get their "not a bay" marks on a second pass.
            const fresh = bmData().enclosures.find(x => x.id === e.id);
            const more = fresh.bays.filter(b => b.row !== null && absent.has(`${b.row},${b.col}`) && !b.absent).map(b => ({ id: b.id, absent: true }));
            if (more.length) await bmDo("update", { id: e.id, changes: {}, cells: more });
            toast(`Saved ${body.name}`);
        }
    } catch (x) { if (!x.handled) toast(errText(x), true) }
}

async function deleteEnclosure(e) {
    const mapped = e.bays.filter(b => bmData().assignments[b.id]).length;
    if (!await bmConfirm(`Delete ${e.name}?`, h("p", {}, mapped ? `${mapped} bay(s) have drives mapped. Their mappings are removed (they stay in the history).` : "The enclosure has no mappings."), "Delete", true)) return;
    try { await bmDo("delete", { id: e.id, force: true }); toast(`Deleted ${e.name}`) } catch (x) { if (!x.handled) toast(errText(x), true) }
}

async function reorder(id, dir) {
    const ids = bmData().enclosures.map(e => e.id), i = ids.indexOf(id), j = i + dir;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    try { await bmDo("reorder", { order: ids }) } catch (x) { if (!x.handled) toast(errText(x), true) }
}

/* ── export / import / backup / print ──────────────────────────────────── */
function download(name, obj) {
    const a = h("a", { href: URL.createObjectURL(new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" })), download: name });
    document.body.append(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 1000);
}
const stampName = p => `${p}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.json`;

async function exportMap() {
    try { const r = await ctl("baymap", "export"); if (r.error) throw new Error(r.error); download(stampName("baymap"), r) } catch (x) { toast(errText(x), true) }
}
async function backupMap() {
    try { const r = await ctl("baymap", "backup"); if (r.error) throw new Error(r.error); download(stampName("baymap-backup"), r) } catch (x) { toast(errText(x), true) }
}

function importMap() {
    const inp = h("input", { type: "file", accept: "application/json,.json", style: "display:none" });
    inp.addEventListener("change", () => {
        const file = inp.files[0];
        if (!file) return;
        if (file.size > 2 * 1024 * 1024) return toast("File too large (2 MB limit).", true);
        const rd = new FileReader();
        rd.onload = () => { let doc; try { doc = JSON.parse(rd.result) } catch (e) { return toast("Not a JSON file.", true) } importPreview(doc, file.name) };
        rd.readAsText(file);
    });
    document.body.append(inp); inp.click(); setTimeout(() => inp.remove(), 60000);
}

async function importPreview(doc, fname) {
    const isBackup = Array.isArray(doc.history);
    const mode = h("select", { "aria-label": "Import mode" }, h("option", { value: "merge" }, "Merge — add enclosures alongside the current ones"),
        h("option", { value: "replace" }, isBackup ? "Restore — replace everything with this backup (incl. history)" : "Replace — discard the current map"));
    const out = h("div", {});
    const show = async () => {
        fill(out, h("span", { class: "hv-spin" }), "Validating…");
        try {
            const r = await ctlIn(["baymap", "import-preview"], { data: doc, mode: mode.value });
            if (r.error) throw new Error(r.error);
            const p = r.plan;
            fill(out,
                h("p", {}, `${p.add.length} enclosure(s) and ${p.assignments} mapping(s) would be added.` + (p.history_records ? ` ${p.history_records} history records in the file.` : "")),
                p.add.length ? h("ul", {}, p.add.map(a => h("li", {}, `${a.name} — ${a.bays} bays, ${a.assignments} mapped`))) : null,
                p.conflicts.length ? h("div", { class: "hv-alert err" }, h("b", {}, "Conflicts: "), h("ul", {}, p.conflicts.map(c => h("li", {}, `${c.name}: ${c.why}`)))) : null,
                p.warnings.length ? h("div", { class: "hv-alert" }, h("ul", {}, p.warnings.map(w => h("li", {}, w)))) : null,
                (p.notes || []).length ? h("p", { class: "hv-muted" }, p.notes.join("; ")) : null);
        } catch (x) { fill(out, errorBox(errText(x))) }
    };
    mode.addEventListener("change", show);
    show();
    const ok = await bmConfirm(`Import ${fname}`, h("div", {}, mode, out), "Apply");
    if (!ok) return;
    const replace = mode.value === "replace";
    if (replace && !await bmConfirm("Replace the whole bay map?", h("p", {}, "The current layout and mappings are replaced. A copy is kept in the backups folder."), "Replace", true)) return;
    try {
        const r = await ctlIn(["baymap", replace && isBackup ? "restore" : "import"], { data: doc, mode: mode.value, confirm: replace, revision: bmData().revision });
        if (r.error) throw new Error(r.error);
        toast("Imported"); load("baymap", true);
    } catch (x) { toast(errText(x), true) }
}

function printMap() {
    const D = bmData(), box = $("#bm-print");
    fill(box, h("h1", {}, `Drive bay map — ${D.host || location.hostname}`), h("p", {}, `Printed ${new Date().toLocaleString()} · map revision ${D.revision}`),
        D.enclosures.map(e => {
            const nums = e.bays.filter(b => !b.absent).sort((a, b) => (a.row === null) - (b.row === null) || a.row - b.row || a.col - b.col);
            return h("section", {}, h("h2", {}, e.name + (e.face ? ` (${e.face})` : "")),
                e.description || e.connection ? h("p", {}, [e.description, e.connection].filter(Boolean).join(" · ")) : null,
                h("div", { class: "bm-pgrid", style: `grid-template-columns: repeat(${e.cols}, 1fr)` },
                    e.bays.filter(b => b.row !== null).sort((a, b) => a.row - b.row || a.col - b.col).map(b => {
                        if (b.absent) return h("div", { class: "pcell absent" });
                        const st = D.status.bays[b.id] || {}, a = D.assignments[b.id];
                        return h("div", { class: "pcell" }, h("b", {}, st.label), a ? [h("div", {}, a.drive.model), h("div", {}, "SN " + (a.drive.serial || "?")), h("div", {}, BM_STATE[st.state] ? BM_STATE[st.state][1] : st.state)] : h("div", {}, "empty"));
                    })),
                h("table", {}, h("thead", {}, h("tr", {}, ["Bay", "Status", "Model", "Serial", "WWN", "Capacity", "Controller link", "/dev"].map(x => h("th", {}, x)))),
                    h("tbody", {}, nums.map(b => {
                        const st = D.status.bays[b.id] || {}, a = D.assignments[b.id], d = st.drive ? driveByKey(st.drive) : null;
                        return h("tr", {}, h("td", {}, st.label + (b.row === null ? " (parked)" : "")), h("td", {}, BM_STATE[st.state] ? BM_STATE[st.state][1] : st.state),
                            h("td", {}, a ? a.drive.model : ""), h("td", {}, a ? a.drive.serial : ""), h("td", {}, a ? a.drive.wwn : ""),
                            h("td", {}, a ? fmtSize(a.drive.size_bytes) : ""), h("td", {}, d ? linkText(d.conn) : (a && a.hint ? linkText(a.hint) + " (when mapped)" : "")),
                            h("td", {}, d ? d.dev : ""));
                    }))));
        }));
    window.print();
}

/* ── periodic reconciliation while the tab is open ─────────────────────── */
document.addEventListener("keydown", e => { if (e.key === "Escape" && bm.pick && state.tab === "baymap") { bm.pick = null; renderBaymap() } });
setInterval(() => {
    if (state.tab === "baymap" && !document.hidden && !document.querySelector("dialog[open]") && !bm.pick) load("baymap", true);
}, 60000);
// Refresh on this tab re-reads the controller listing too (periodic refreshes use lsblk/sysfs only).
document.addEventListener("click", e => { if (state.tab === "baymap" && e.target.closest && e.target.closest("#hv-refresh")) bm.full = true }, true);
