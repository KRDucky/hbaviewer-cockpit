/* HBA Viewer — Cockpit front end.
 *
 * Everything hardware-facing goes through one root helper, hbaviewer-ctl,
 * which prints one JSON document per call. This file only renders.
 * All text from the helper reaches the DOM through textContent (the h()
 * builder below), never innerHTML.
 */
"use strict";

const CTL = "/usr/lib/hbaviewer/hbaviewer-ctl";

/* ── tiny DOM helpers ─────────────────────────────────────────────────── */
const $ = (s, r = document) => r.querySelector(s);

function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
        if (kid === null || kid === undefined || kid === false) continue;
        el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
}

function svg(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
}

/* replaceChildren that accepts nested arrays and skips null, like h(). */
function fill(el, ...kids) {
    el.replaceChildren(...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false)
        .map(k => k instanceof Node ? k : document.createTextNode(String(k))));
    return el;
}

const dash = v => (v === null || v === undefined || v === "" ? "—" : v);

function ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
}

function span(s) {
    if (!s) return "—";
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
    return `${(s / 86400).toFixed(1)} d`;
}

function fmtTemp(c) {
    if (c === null || c === undefined || c === "" || isNaN(Number(c))) return "—";
    return state.settings.TEMP_UNIT === "F" ? `${Math.round(c * 9 / 5 + 32)}°F` : `${Math.round(c)}°C`;
}

function fmtRate(r) {
    if (!r) return "0/hr";
    if (r < 0.05) return "<0.1/hr";
    return (r < 10 ? r.toFixed(1) : Math.round(r).toLocaleString()) + "/hr";
}

function fmtBytes(b) {
    const u = ["B/s", "KB/s", "MB/s", "GB/s"];
    let i = 0;
    while (b >= 1000 && i < u.length - 1) { b /= 1000; i++ }
    return `${b < 10 && i ? b.toFixed(1) : Math.round(b)} ${u[i]}`;
}

function pill(st, label) {
    return h("span", { class: `hv-pill s-${st || "unknown"}` }, label || st || "unknown");
}

const STATE_LABEL = { ok: "Healthy", watch: "Watch", warning: "Warning", critical: "Critical", unknown: "Unknown", warn: "Warning", error: "Error" };

/* ── theme: follow Cockpit's shell style setting ──────────────────────── */
function applyTheme() {
    let style = null;
    try { style = localStorage.getItem("shell:style") } catch (e) { /* storage blocked */ }
    const dark = style === "dark" ||
        ((!style || style === "auto") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("hv-dark", dark);
}
window.addEventListener("storage", e => { if (e.key === "shell:style") applyTheme() });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
applyTheme();

/* ── helper calls ─────────────────────────────────────────────────────── */
function ctl(...args) {
    return cockpit.spawn([CTL, ...args], { superuser: "require", err: "message" })
        .then(out => {
            try { return JSON.parse(out) } catch (e) { throw new Error("Helper returned non-JSON output: " + out.slice(0, 300)) }
        })
        .catch(ex => {
            const msg = ex && (ex.message || ex.problem) || String(ex);
            if (ex && (ex.problem === "access-denied" || ex.problem === "not-authorized" || /access.denied|not.authorized/i.test(msg)))
                $("#hv-noadmin").hidden = false;
            if (ex && ex.problem === "not-found")
                throw new Error(`${CTL} is not installed. Run install.sh on this server.`);
            throw ex instanceof Error ? ex : new Error(msg);
        });
}

/* Same as ctl(), with a JSON request on stdin (Bay Mapper's structured operations). */
function ctlIn(args, req) {
    const p = cockpit.spawn([CTL, ...args], { superuser: "require", err: "message" });
    p.input(JSON.stringify(req || {}));
    return new Promise((resolve, reject) => p.then(resolve, reject)).then(out => {
        try { return JSON.parse(out) } catch (e) { throw new Error("Helper returned non-JSON output: " + out.slice(0, 300)) }
    }, ex => {
        const msg = ex && (ex.message || ex.problem) || String(ex);
        if (ex && (ex.problem === "access-denied" || ex.problem === "not-authorized")) $("#hv-noadmin").hidden = false;
        throw ex instanceof Error ? ex : new Error(msg);
    });
}

/* ── app state ────────────────────────────────────────────────────────── */
const state = {
    tab: "overview",
    settings: { TEMP_UNIT: "C" },
    data: {},        // tab key -> payload
    at: {},          // tab key -> ms timestamp of last load
    busy: {},
};

const LOADERS = {
    overview: () => Promise.all([ctl("overview"), ctl("health").catch(e => ({ error: e.message }))])
        .then(([ov, hl]) => { state.data.health = hl; state.at.health = Date.now(); return ov }),
    health: () => ctl("health"),
    drives: () => Promise.all([ctl("drives"), ctl("baymap", "locations").catch(() => ({}))])
        .then(([d, l]) => { if (d && !d.error) d.bays = (l && l.locations) || {}; return d }),
    phy: () => Promise.all([ctl("phy"), state.data.drives ? Promise.resolve(state.data.drives) : ctl("drives").catch(() => null)])
        .then(([p, d]) => { if (d && !d.error) { state.data.drives = d; state.at.drives = Date.now() } return p }),
    events: () => ctl("events"),
    smart: () => ctl("smart", "status"),
    settings: () => Promise.all([ctl("env"), systemdTimer()]).then(([env, timer]) => ({ env, timer })),
    perf: () => Promise.resolve({}),
};

const RENDER = {
    overview: renderOverview, health: renderHealth, phy: renderPhy, drives: renderDrives,
    smart: renderSmart, events: renderEvents, perf: renderPerf, settings: renderSettings,
};

function panel(tab) { return $(`#tab-${tab}`) }

function loading(tab, what) {
    fill(panel(tab), h("div", { class: "hv-empty" }, h("span", { class: "hv-spin" }), what || "Reading controllers…"));
}

function errorBox(msg, detail) {
    return h("div", { class: "hv-alert err" }, h("b", {}, "Could not read this view. "), msg,
        detail ? h("pre", {}, detail) : null);
}

function payloadError(p) {
    if (!p) return "No data";
    if (p.error) return p.error;
    return null;
}

async function load(tab, force) {
    if (state.busy[tab]) return;
    const fresh = state.at[tab] && Date.now() - state.at[tab] < 30000;
    if (!force && fresh && state.data[tab]) { RENDER[tab](); return }
    state.busy[tab] = true;
    if (!state.data[tab]) loading(tab);
    $("#hv-refresh").disabled = true;
    try {
        state.data[tab] = await LOADERS[tab]();
        state.at[tab] = Date.now();
        if (tab === "overview" || tab === "health") updateBackendLine(state.data[tab]);
    } catch (e) {
        state.data[tab] = { error: e.message };
    } finally {
        state.busy[tab] = false;
        $("#hv-refresh").disabled = false;
    }
    if (state.tab === tab) RENDER[tab]();
    stamp();
}

function stamp() {
    const t = state.at[state.tab];
    $("#hv-stamp").textContent = t ? "Updated " + new Date(t).toLocaleTimeString() : "";
}

function updateBackendLine(p) {
    if (!p || p.error) return;
    const n = (p.controllers || []).length;
    const be = { storcli: "storcli (SAS3 / SAS3.5)", storcli2: "StorCLI2 (SAS4)", lsiutil: "lsiutil (SAS2)" }[p.backend] || p.backend;
    $("#hv-backend").textContent = `${n} controller${n === 1 ? "" : "s"} · via ${be}` + (p.driver ? ` · ${p.driver}` : "");
}

function selectTab(tab) {
    state.tab = tab;
    for (const b of document.querySelectorAll("#hv-tabs button"))
        b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    for (const s of document.querySelectorAll(".hv-panel"))
        s.hidden = s.id !== `tab-${tab}`;
    if (tab !== "perf") perfStop();
    if (tab === "perf") perfStart();
    load(tab, false);
    stamp();
    try { cockpit.location.go([tab]) } catch (e) { /* standalone preview */ }
}

/* ── controller naming (shared) ───────────────────────────────────────── */
function ctlName(i) {
    const ov = state.data.overview;
    const c = ov && !ov.error && ov.controllers ? ov.controllers[i] : null;
    const name = c ? (c.board_name || c.model || "") : "";
    return `/c${i}` + (name ? ` · ${name}` : "");
}

function sectionHead(i, extra) {
    return h("div", { class: "hv-card-head" },
        h("h3", {}, ctlName(i)), h("div", { class: "hv-toolbar", style: "margin:0" }, extra));
}

/* ── Overview ─────────────────────────────────────────────────────────── */
const BAND_VAR = { normal: "--b-normal", elevated: "--b-elevated", warning: "--b-warning", alert: "--b-alert", critical: "--b-critical" };

function gauge(temp, band) {
    const min = 25, max = 105;
    const frac = temp === null || temp === undefined || temp === "" ? 0 : Math.min(1, Math.max(0, (temp - min) / (max - min)));
    const r = 52, cx = 66, cy = 66, len = Math.PI * r;
    const s = svg("svg", { viewBox: "0 0 132 76", role: "img", "aria-label": `Controller temperature ${fmtTemp(temp)}` });
    const arc = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`;
    s.append(svg("path", { d: arc, fill: "none", stroke: "var(--border)", "stroke-width": 11, "stroke-linecap": "round" }));
    if (frac > 0)
        s.append(svg("path", {
            d: arc, fill: "none", stroke: `var(${BAND_VAR[band] || "--unknown"})`, "stroke-width": 11,
            "stroke-linecap": "round", "stroke-dasharray": `${len * frac} ${len}`,
        }));
    return h("div", { class: "hv-gauge" }, s,
        h("div", { class: "t" }, temp === null || temp === undefined ? "N/A" : fmtTemp(temp)),
        h("div", { class: "b" }, temp === null || temp === undefined ? "no sensor" : `${band || "?"} band`));
}

function renderOverview() {
    const p = state.data.overview, el = panel("overview");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err, p && (p.stderr || p.raw)), envHint()); return }
    const cs = p.controllers || [];
    if (!cs.length) { fill(el, h("div", { class: "hv-empty" }, "No LSI / Broadcom controllers found."), envHint()); return }
    const hl = state.data.health && !state.data.health.error ? state.data.health.controllers || [] : [];
    const cards = cs.map((c, i) => {
        if (c.error) return h("div", { class: "hv-card" }, sectionHead(i), h("div", { class: "hv-card-body" }, errorBox(c.error)));
        const hh = hl[i];
        const pcie = [c.pcie_width, c.pcie_speed].filter(Boolean).join(" ");
        const rows = [
            ["Chip", c.model], ["Firmware", c.firmware + (c.fw_old ? "  (pre-P20 — update recommended)" : "")],
            ["BIOS", c.bios], ["Mode", c.mode], ["Drives", c.drive_count],
            ["PCIe", pcie], ["PCI address", c.pci_location], ["Power", c.power_mode],
            ["Topology", c.topology === "internal" ? "Direct attach" : c.topology === "unknown" ? "Expander / unknown" : c.topology],
        ].filter(([, v]) => v !== undefined && v !== null && v !== "");
        return h("div", { class: "hv-card" },
            h("div", { class: "hv-card-head" },
                h("h3", {}, `/c${i} · ${c.board_name || c.model || "Controller"}`),
                hh ? pill(hh.state, STATE_LABEL[hh.state]) : pill(c.status, STATE_LABEL[c.status])),
            h("div", { class: "hv-card-body" },
                h("div", { class: "hv-ov" },
                    gauge(c.temp === "" ? null : c.temp, c.temp_band),
                    h("dl", { class: "hv-kv" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))]))),
                hh ? h("p", { class: "hv-muted", style: "margin:12px 0 0" }, hh.reason) : null,
                c.temp !== null && c.temp !== "" ? h("p", { class: "hv-muted", style: "margin:4px 0 0" },
                    `Alert threshold ${fmtTemp(c.alert_threshold)} · band ${c.cfg_band || "—"}`) : null));
    });
    const phys = (p.physical || []).filter(g => g.functions.length > 1 || g.functions.some(f => f.ctl_index !== null));
    const strip = phys.some(g => g.functions.length > 1) ? h("div", { class: "hv-alert info" },
        h("b", {}, "Physical cards: "),
        phys.map((g, n) => [n ? " · " : "", `${g.board_name || "HBA"}${g.board_serial ? " (SN " + g.board_serial + ")" : ""}: `,
            g.functions.map(f => f.ctl_index !== null ? `/c${f.ctl_index}` : f.pci).join(" + "),
            g.grouped_by ? ` — grouped by ${g.grouped_by}` : ""])) : null;
    fill(el, strip, h("div", { class: "hv-grid" }, cards));
}

function envHint() {
    return h("div", { class: "hv-alert info" },
        "SAS3/SAS3.5 cards (9300/9400/9500) need Broadcom ", h("code", {}, "storcli64"),
        ", SAS4 (9600) needs ", h("code", {}, "storcli2"),
        ", SAS2 (9207/9211/H310) uses the bundled lsiutil. See the Settings tab for what was detected.");
}

/* ── Health ───────────────────────────────────────────────────────────── */
const IND_NAMES = {
    thermal: "Thermal", link_integrity: "Link integrity", topology: "Topology",
    host_link: "Host link", controller: "Controller read",
};

function renderHealth() {
    const p = state.data.health, el = panel("health");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err)); return }
    const cs = p.controllers || [];
    if (!cs.length) { fill(el, h("div", { class: "hv-empty" }, "No controllers.")); return }
    fill(el, 
        h("p", { class: "hv-muted", style: "margin-top:0" },
            "Each read is added to a per-controller sample ring. Link integrity is a rate between the oldest and newest ",
            "sample, so it needs at least 30 minutes of history before it will call a link clean. ",
            "The hbaviewer-sample timer takes a reading every 5 minutes even when this page is closed."),
        h("div", { class: "hv-grid" }, cs.map(c => h("div", { class: "hv-card" },
            h("div", { class: "hv-card-head" }, h("h3", {}, ctlName(c.idx)), pill(c.state, STATE_LABEL[c.state])),
            h("div", { class: "hv-card-body" },
                h("div", { class: "hv-rollup" },
                    h("b", {}, `${c.ok} of ${c.total} indicators OK`),
                    h("span", { class: "reason" }, c.reason)),
                h("table", { class: "hv-ind" }, h("tbody", {},
                    Object.entries(c.indicators).map(([k, v]) => h("tr", {},
                        h("td", {}, h("span", { class: `dot s-${v.state}`, title: v.state })),
                        h("td", { class: "name" }, IND_NAMES[k] || k),
                        h("td", {}, v.reason),
                        h("td", { class: "val" }, v.value))))),
                h("p", { class: "hv-muted", style: "margin:10px 0 0" },
                    `${c.samples} sample${c.samples === 1 ? "" : "s"} · window ${span(c.span)}`))))));
}

/* ── Drives ───────────────────────────────────────────────────────────── */
const DRIVE_STATE = s => {
    if (!s) return "unknown";
    if (/^(JBOD|Onln|UGood|Optimal|OK|Ready|Online)/i.test(s)) return "ok";
    if (/fail|bad|offln|missing/i.test(s)) return "critical";
    return "watch";
};

async function locate(dev, start) {
    try {
        const r = await ctl("locate", start ? "start" : "stop", dev);
        if (r.error) throw new Error(r.error);
        if (state.data.drives) state.data.drives.locating = r.active || [];
        renderDrives();
    } catch (e) { alert(e.message) }
}

async function smartDialog(dev) {
    openDialog(`SMART · ${dev}`, h("div", { class: "hv-empty" }, h("span", { class: "hv-spin" }), "Running smartctl -x…"));
    try {
        const r = await ctl("smart-one", dev);
        if (r.error) throw new Error(r.error);
        const s = r.parsed || {};
        const rows = [["Health", s.health], ["Transport", s.transport], ["Temperature", s.temp ? fmtTemp(s.temp) : ""],
            ["Trip temperature", s.trip_temp ? fmtTemp(s.trip_temp) : ""], ["Power-on hours", s.power_on_hours],
            ["Grown defects / reallocated", s.defects], ["Pending sectors", s.pending], ["Non-medium errors", s.nonmedium]];
        fill($("#hv-dlg-body"), 
            h("dl", { class: "hv-kv", style: "margin-bottom:12px" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, dash(v))])),
            h("details", {}, h("summary", {}, "Full smartctl -x output"), h("pre", {}, r.raw || "(no output)")));
    } catch (e) { fill($("#hv-dlg-body"), errorBox(e.message)) }
}

function renderDrives() {
    const p = state.data.drives, el = panel("drives");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err, p && (p.stderr || p.raw))); return }
    const locating = new Set(p.locating || []);
    const cs = p.controllers || [];
    const showTemp = cs.some(c => (c.drives || []).some(d => d.temp !== undefined));
    fill(el, 
        h("p", { class: "hv-muted", style: "margin-top:0" },
            "Locate blinks a drive's activity light by reading it twice a second; it stops by itself after ",
            `${state.settings.LOCATE_MAX_SECS || 300} s.`),
        cs.map((c, i) => {
            if (c.error) return h("div", { class: "hv-section" }, sectionHead(i), errorBox(c.error));
            const drives = c.drives || [];
            const encl = (c.enclosures || []).map(e => `${e.product || e.type} (EID ${e.eid}, ${e.drives}/${e.slots} slots${e.direct ? ", direct attach" : ""})`).join(" · ");
            return h("div", { class: "hv-section" },
                sectionHead(i, h("span", { class: "hv-muted" }, `${drives.length} drive${drives.length === 1 ? "" : "s"}` + (encl ? ` · ${encl}` : ""))),
                h("div", { class: "hv-tablewrap" }, drives.length ? h("table", { class: "hv-t" },
                    h("thead", {}, h("tr", {}, ["Device", "Bay", "Used by", "Slot", "Port", "Model", "Serial", "State", "Size", "Link", "Firmware",
                        showTemp ? "Temp" : null, ""].filter(x => x !== null).map(x => h("th", {}, x)))),
                    h("tbody", {}, drives.map(d => {
                        const on = d.hctl && locating.has(d.hctl);
                        return h("tr", { class: DRIVE_STATE(d.state) === "critical" ? "bad" : null },
                            h("td", { class: "mono" }, dash(d.dev)),
                            h("td", {}, (p.bays || {})[d.dev] ? h("button", {
                                class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", title: "Open in Bay Mapper",
                                onclick: () => { state.bmFocus = p.bays[d.dev].bay_id; selectTab("baymap") },
                            }, `${p.bays[d.dev].enclosure} · ${p.bays[d.dev].bay}`) : h("span", { class: "hv-muted" }, "—")),
                            h("td", {}, (d.roles || []).join(", ") || "—"),
                            h("td", {}, dash(d.slot ?? (d.bus !== undefined ? `${d.bus}:${d.target}` : ""))),
                            h("td", {}, dash(d.port ?? d.phy)),
                            h("td", {}, dash(d.model)),
                            h("td", { class: "mono" }, dash(d.serial)),
                            h("td", {}, d.state ? pill(DRIVE_STATE(d.state), d.state) : "—"),
                            h("td", { class: "num" }, dash(d.size)),
                            h("td", {}, dash(d.link)),
                            h("td", {}, dash(d.firmware)),
                            showTemp ? h("td", { class: "num" }, d.temp !== undefined ? fmtTemp(d.temp) : "—") : null,
                            h("td", {}, d.dev ? [
                                h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button", onclick: () => smartDialog(d.dev) }, "SMART"), " ",
                                d.hctl ? h("button", {
                                    class: `hv-btn hv-btn-sm ${on ? "hv-btn-danger" : "hv-btn-secondary"}`, type: "button",
                                    onclick: () => locate(d.dev, !on),
                                }, on ? "Stop blink" : "Locate") : null,
                            ] : null));
                    }))) : h("div", { class: "hv-empty" }, "No drives reported on this controller.")));
        }));
}

/* ── PHY ──────────────────────────────────────────────────────────────── */
function phyDrive(ci, phy) {
    const dp = state.data.drives;
    if (!dp || dp.error || !dp.controllers || !dp.controllers[ci]) return null;
    const drives = dp.controllers[ci].drives || [];
    if (phy.sas_addr) {
        const pfx = String(phy.sas_addr).toUpperCase().slice(0, 15);
        if (pfx.length < 15) return null;
        const m = drives.filter(d => String(d.sas_address || "").toUpperCase().slice(0, 15) === pfx);
        return m.length === 1 ? m[0] : null;
    }
    const m = drives.filter(d => d.phy !== undefined && Number(d.phy) === Number(phy.phy));
    return m.length === 1 ? m[0] : null;
}

function driveLabel(d) {
    if (!d) return "—";
    const parts = [d.slot, d.dev].filter(Boolean);
    return parts.join(" · ") || "—";
}

async function baseline(ci, act) {
    if (act === "clear" && !confirm(`Clear the error baseline for /c${ci}?`)) return;
    try {
        const r = await ctl("baseline", act, String(ci));
        if (r.error) throw new Error(r.error);
        load("phy", true);
    } catch (e) { alert(e.message) }
}

const COUNTERS = [["inv", "Invalid DWord"], ["disp", "Disparity"], ["sync", "Loss of sync"], ["reset", "Reset problem"]];

function renderPhy() {
    const p = state.data.phy, el = panel("phy");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err, p && (p.stderr || p.raw))); return }
    fill(el, 
        h("p", { class: "hv-muted", style: "margin-top:0" },
            "Counters are cumulative since the driver loaded. Set a baseline and each counter is then shown as growth since then, ",
            "with an errors-per-hour rate — that is what tells an old burst apart from a cable failing now. ",
            "Baselines are kept in /var/lib/hbaviewer and are discarded automatically after a reboot or driver reload."),
        (p.controllers || []).map((c, i) => {
            if (c.error) return h("div", { class: "hv-section" }, sectionHead(i), errorBox(c.error));
            if (c.unsupported) return h("div", { class: "hv-section" }, sectionHead(i), h("div", { class: "hv-alert info" }, "This controller does not report PHY counters."));
            const phys = c.phys || [];
            const hasBase = !!c.baseline_ts;
            const anyReset = phys.some(x => x.base && x.base.reset);
            const offenders = phys.filter(x => x.base && !x.base.reset)
                .map(x => ({ x, r: COUNTERS.reduce((a, [k]) => a + (x.base.rate[k] || 0), 0) }))
                .filter(o => o.r > 0).sort((a, b) => b.r - a.r).slice(0, 5);
            return h("div", { class: "hv-section" },
                sectionHead(i, [
                    h("span", { class: "hv-muted" }, hasBase ? `Baseline set ${ago(c.baseline_ts)}` : "No baseline"),
                    h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button", onclick: () => baseline(i, "set") }, hasBase ? "Reset baseline" : "Set baseline"),
                    hasBase ? h("button", { class: "hv-btn hv-btn-plain hv-btn-sm", type: "button", onclick: () => baseline(i, "clear") }, "Clear") : null,
                ]),
                anyReset ? h("div", { class: "hv-alert", style: "margin:0;border-radius:0" }, "Counters restarted since the baseline was set (reboot or driver reload). Set a new baseline.") : null,
                offenders.length ? h("div", { class: "hv-alert", style: "margin:0;border-radius:0" },
                    h("b", {}, "Top offenders since baseline: "),
                    offenders.map((o, n) => [n ? " · " : "", `PHY ${o.x.phy} (${driveLabel(phyDrive(i, o.x))}) ${fmtRate(o.r)}`])) : null,
                h("div", { class: "hv-tablewrap" }, phys.length ? h("table", { class: "hv-t" },
                    h("thead", {}, h("tr", {}, h("th", {}, "PHY"), h("th", {}, "Link"), h("th", {}, "Speed"), h("th", {}, "SAS address"),
                        h("th", {}, "Drive"), COUNTERS.map(([, n]) => h("th", { class: "num" }, n)),
                        hasBase ? h("th", { class: "num" }, "Rate since baseline") : null)),
                    h("tbody", {}, phys.map(x => {
                        const total = COUNTERS.reduce((a, [k]) => a + (Number(x[k]) || 0), 0);
                        const grow = x.base && !x.base.reset ? COUNTERS.reduce((a, [k]) => a + x.base.delta[k], 0) : 0;
                        const rate = x.base && !x.base.reset ? COUNTERS.reduce((a, [k]) => a + x.base.rate[k], 0) : null;
                        return h("tr", { class: grow > 0 ? "bad" : (!hasBase && total > 0 ? "warnrow" : null) },
                            h("td", {}, String(x.phy)),
                            h("td", {}, pill(x.link === "up" ? "ok" : "unknown", x.link || "?")),
                            h("td", {}, dash(x.speed)),
                            h("td", { class: "mono" }, dash(x.sas_addr)),
                            h("td", {}, driveLabel(phyDrive(i, x))),
                            COUNTERS.map(([k]) => h("td", { class: "num" }, (Number(x[k]) || 0).toLocaleString(),
                                x.base && !x.base.reset ? h("span", { class: `delta${x.base.delta[k] ? " up" : ""}` }, `+${x.base.delta[k]}`) : null)),
                            hasBase ? h("td", { class: "num" }, rate === null ? "—" : fmtRate(rate)) : null);
                    }))) : h("div", { class: "hv-empty" }, "No PHYs reported.")));
        }));
}

/* ── SMART ────────────────────────────────────────────────────────────── */
let smartPoll = null;

async function smartRefresh() {
    try {
        state.data.smart = await ctl("smart", "start");
        state.at.smart = Date.now();
    } catch (e) { state.data.smart = { error: e.message } }
    renderSmart();
}

function renderSmart() {
    const p = state.data.smart, el = panel("smart");
    clearTimeout(smartPoll);
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err)); return }
    const running = p.state === "running";
    if (running && state.tab === "smart")
        smartPoll = setTimeout(async () => {
            try { state.data.smart = await ctl("smart", "status"); state.at.smart = Date.now() } catch (e) { /* keep last */ }
            if (state.tab === "smart") renderSmart();
        }, 1500);
    let prog = null;
    if (running) {
        const [a, b] = String(p.progress || "0/?").split("/").map(Number);
        prog = h("span", { class: "hv-toolbar", style: "margin:0" }, h("span", { class: "hv-spin" }),
            `Reading drive ${a || 0} of ${b || "?"}`,
            b ? h("div", { class: "hv-progress" }, h("div", { style: `width:${Math.round(100 * a / b)}%` })) : null);
    }
    const drives = (p.data && p.data.drives) || [];
    const good = v => !v || /^(OK|PASSED)$/i.test(v);
    fill(el, 
        h("div", { class: "hv-toolbar" },
            h("button", { class: "hv-btn", type: "button", disabled: running, onclick: smartRefresh }, running ? "Collecting…" : "Collect SMART now"),
            prog || h("span", { class: "hv-muted" }, p.collected_at ? `Collected ${ago(p.collected_at)} — kept until you collect again` : "Not collected yet"),
            h("span", { class: "grow" }),
            h("span", { class: "hv-muted" }, "Runs in the background (≈1 s per drive). SATA drives in standby are skipped, never woken.")),
        drives.length ? h("div", { class: "hv-tablewrap", style: "border-radius:3px" }, h("table", { class: "hv-t" },
            h("thead", {}, h("tr", {}, ["Device", "Used by", "Model", "Serial", "Size", "Type", "Health", "Temp", "Power-on", "Defects", "Pending", "Non-medium", ""]
                .map((x, n) => h("th", { class: n >= 7 && n <= 11 ? "num" : null }, x)))),
            h("tbody", {}, drives.map(d => {
                const s = d.smart || {};
                const bad = !good(s.health) || Number(s.defects) > 0 || Number(s.pending) > 0;
                const poh = Number(s.power_on_hours);
                return h("tr", { class: bad ? "bad" : null },
                    h("td", { class: "mono" }, d.dev),
                    h("td", {}, (d.roles || []).join(", ") || "—"),
                    h("td", {}, dash(d.model)), h("td", { class: "mono" }, dash(d.serial)), h("td", { class: "num" }, dash(d.size)),
                    h("td", {}, dash((s.transport || "").toUpperCase())),
                    h("td", {}, s.health ? pill(good(s.health) ? "ok" : "critical", s.health) : h("span", { class: "hv-muted" }, Object.keys(s).length ? "standby / n/a" : "—")),
                    h("td", { class: "num" }, s.temp ? fmtTemp(Number(s.temp)) : "—"),
                    h("td", { class: "num" }, poh ? `${poh.toLocaleString()} h (${(poh / 8766).toFixed(1)} y)` : "—"),
                    h("td", { class: "num" }, dash(s.defects)), h("td", { class: "num" }, dash(s.pending)), h("td", { class: "num" }, dash(s.nonmedium)),
                    h("td", {}, h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button", onclick: () => smartDialog(d.dev) }, "Details")));
            })))) : (running ? null : h("div", { class: "hv-empty" }, p.collected_at ? "No HBA drives found (drives with a WWN on a SCSI bus)." : "Press “Collect SMART now” to read every HBA-attached drive.")));
}

/* ── Events ───────────────────────────────────────────────────────────── */
function copyText(txt, btn) {
    const done = () => { const o = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = o }, 1500) };
    if (navigator.clipboard && window.isSecureContext)
        navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done));
    else fallbackCopy(txt, done);
}
function fallbackCopy(txt, done) {
    const ta = h("textarea", { style: "position:fixed;opacity:0" }, txt);
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); done() } catch (e) { /* nothing more to try */ }
    ta.remove();
}

function renderEvents() {
    const p = state.data.events, el = panel("events");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err, p && (p.stderr || p.raw))); return }
    const filter = h("input", { type: "text", placeholder: "Filter events…", "aria-label": "Filter events" });
    const body = h("div", {});
    const draw = () => {
        const q = filter.value.trim().toLowerCase();
        fill(body, ...(p.controllers || []).map((c, i) => {
            if (c.error) return h("div", { class: "hv-section" }, sectionHead(i), errorBox(c.error));
            const all = c.entries || [];
            const rows = q ? all.filter(e => `${e.seq} ${e.time} ${e.code} ${e.description}`.toLowerCase().includes(q)) : all;
            const copyBtn = h("button", { class: "hv-btn hv-btn-secondary hv-btn-sm", type: "button" }, "Copy");
            copyBtn.addEventListener("click", () => copyText(rows.map(e => `${e.seq}\t${e.time}\t${e.code}\t${e.description}`).join("\n"), copyBtn));
            return h("div", { class: "hv-section" },
                sectionHead(i, [h("span", { class: "hv-muted" }, `${c.archived || all.length} archived · ${c.live_count ?? "?"} in firmware ring`), copyBtn]),
                h("div", { class: "hv-tablewrap" }, rows.length ? h("table", { class: "hv-t" },
                    h("thead", {}, h("tr", {}, ["Seq", "Time", "Code", "Description"].map(x => h("th", {}, x)))),
                    h("tbody", {}, rows.slice(0, 1000).map(e => h("tr", {},
                        h("td", { class: "mono" }, dash(e.seq)), h("td", {}, dash(e.time)), h("td", { class: "mono" }, dash(e.code)),
                        h("td", { class: "wrap" }, dash(e.description)))))) : h("div", { class: "hv-empty" }, q ? "No matching events." : "No events.")));
        }));
    };
    filter.addEventListener("input", draw);
    draw();
    fill(el, 
        h("div", { class: "hv-toolbar" }, filter, h("span", { class: "hv-muted" },
            "Entries are archived to /var/lib/hbaviewer, so history survives reboots and the firmware ring wrapping.")),
        body);
}

/* ── Performance ──────────────────────────────────────────────────────── */
const perf = { timer: null, prev: null, series: {}, phy0: {}, latest: {} };
const PERF_POINTS = 150, PERF_MS = 2000;

function perfStart() {
    if (perf.timer) return;
    perfTick();
    perf.timer = setInterval(perfTick, PERF_MS);
}
function perfStop() { clearInterval(perf.timer); perf.timer = null; perf.prev = null }

async function perfTick() {
    let m;
    try { m = await ctl("metrics") } catch (e) { perf.error = e.message; renderPerf(); return }
    if (m.error) { perf.error = m.error; renderPerf(); return }
    perf.error = null;
    const now = performance.now();
    if (perf.prev) {
        const dt = (now - perf.prev.at) / 1000;
        for (const c of m.controllers || []) {
            const pc = (perf.prev.m.controllers || []).find(x => x.idx === c.idx);
            if (!pc) continue;
            const pd = Object.fromEntries((pc.drives || []).map(d => [d.dev, d]));
            let rb = 0, wb = 0, ri = 0, wi = 0, busiest = 0, latSum = 0, ioSum = 0;
            const per = [];
            for (const d of c.drives || []) {
                const q = pd[d.dev];
                if (!q) continue;
                const dr = Math.max(0, d.r_sect - q.r_sect) * 512, dw = Math.max(0, d.w_sect - q.w_sect) * 512;
                const dri = Math.max(0, d.r_io - q.r_io), dwi = Math.max(0, d.w_io - q.w_io);
                const util = Math.min(100, Math.max(0, d.io_ticks - q.io_ticks) / (dt * 10));
                const wt = Math.max(0, d.weighted - q.weighted);
                rb += dr; wb += dw; ri += dri; wi += dwi; busiest = Math.max(busiest, util);
                latSum += wt; ioSum += dri + dwi;
                per.push({ dev: d.dev, r: dr / dt, w: dw / dt, iops: (dri + dwi) / dt, util, lat: dri + dwi ? wt / (dri + dwi) : null });
            }
            const s = perf.series[c.idx] || (perf.series[c.idx] = { r: [], w: [], ri: [], wi: [] });
            s.r.push(rb / dt); s.w.push(wb / dt); s.ri.push(ri / dt); s.wi.push(wi / dt);
            for (const k of Object.keys(s)) if (s[k].length > PERF_POINTS) s[k].shift();
            if (!perf.phy0[c.idx]) perf.phy0[c.idx] = c.phy;
            const p0 = perf.phy0[c.idx];
            const phyNew = c.phy ? Object.keys(c.phy).reduce((a, k) => a + Math.max(0, c.phy[k] - (p0[k] || 0)), 0) : null;
            perf.latest[c.idx] = { busiest, lat: ioSum ? latSum / ioSum : null, temp: c.temp, phyNew, per: per.sort((a, b) => b.util - a.util) };
        }
    }
    perf.prev = { at: now, m };
    state.at.perf = Date.now();
    if (state.tab === "perf") { renderPerf(); stamp() }
}

function chart(title, a, b, fmt) {
    const cv = h("canvas", { width: 600, height: 140, role: "img", "aria-label": title });
    const last = arr => arr.length ? arr[arr.length - 1] : 0;
    const box = h("div", { class: "hv-chart" },
        h("div", { class: "lbl" }, h("b", {}, title),
            h("span", { class: "now" }, h("span", { class: "legend-r" }, "● read " + fmt(last(a))), "  ", h("span", { class: "legend-w" }, "● write " + fmt(last(b))))),
        cv);
    requestAnimationFrame(() => drawChart(cv, a, b, fmt));
    return box;
}

function drawChart(cv, a, b, fmt) {
    const dpr = window.devicePixelRatio || 1, wCss = cv.clientWidth || 600, hCss = 140;
    cv.width = wCss * dpr; cv.height = hCss * dpr;
    const g = cv.getContext("2d"); g.scale(dpr, dpr);
    const css = getComputedStyle(document.documentElement);
    const col = n => css.getPropertyValue(n).trim();
    const max = Math.max(1, ...a, ...b) * 1.15, padL = 0, padB = 16, ch = hCss - padB;
    g.strokeStyle = col("--border"); g.lineWidth = 1; g.fillStyle = col("--muted"); g.font = "11px sans-serif";
    for (let i = 0; i <= 3; i++) {
        const y = Math.round(ch - ch * i / 3) + .5;
        g.beginPath(); g.moveTo(padL, y); g.lineTo(wCss, y); g.stroke();
        if (i) g.fillText(fmt(max * i / 3), 4, y + 12);
    }
    g.fillText(`last ${Math.round(PERF_POINTS * PERF_MS / 60000)} min`, 4, hCss - 3);
    const line = (arr, color) => {
        if (arr.length < 2) return;
        g.strokeStyle = color; g.lineWidth = 1.75; g.beginPath();
        arr.forEach((v, i) => {
            const x = wCss - (arr.length - 1 - i) * (wCss / (PERF_POINTS - 1)), y = ch - ch * v / max;
            i ? g.lineTo(x, y) : g.moveTo(x, y);
        });
        g.stroke();
    };
    line(a, col("--chart-r")); line(b, col("--chart-w"));
}

function renderPerf() {
    const el = panel("perf");
    if (perf.error) { fill(el, errorBox(perf.error)); return }
    const ids = Object.keys(perf.series).map(Number);
    if (!ids.length) {
        fill(el, h("div", { class: "hv-empty" }, h("span", { class: "hv-spin" }),
            perf.prev && !(perf.prev.m.controllers || []).length ? "No SAS controllers with attached block devices found." : "Sampling… first rates appear after two polls."));
        return;
    }
    fill(el, 
        h("p", { class: "hv-muted", style: "margin-top:0" },
            "Live from /proc/diskstats and sysfs every 2 s — never touches storcli/lsiutil. Only runs while this tab is open."),
        ids.map(i => {
            const s = perf.series[i], L = perf.latest[i] || {};
            const top = (L.per || []).slice(0, 8);
            return h("div", { class: "hv-section" },
                sectionHead(i, h("span", { class: "hv-muted" },
                    `Busiest drive ${Math.round(L.busiest || 0)}% · avg latency ${L.lat == null ? "—" : L.lat.toFixed(1) + " ms"} · ` +
                    `temp ${fmtTemp(L.temp)} · PHY errors since opened ${L.phyNew == null ? "n/a" : L.phyNew}`)),
                h("div", { class: "hv-card", style: "border-top:0;border-radius:0 0 3px 3px" }, h("div", { class: "hv-card-body" },
                    h("div", { class: "hv-perf-row" },
                        chart("Throughput", s.r, s.w, fmtBytes),
                        chart("IOPS", s.ri, s.wi, v => `${v < 10 ? v.toFixed(1) : Math.round(v)}`)),
                    top.length ? h("table", { class: "hv-t", style: "margin-top:12px" },
                        h("thead", {}, h("tr", {}, ["Drive", "Read", "Write", "IOPS", "Util", "Latency"].map((x, n) => h("th", { class: n ? "num" : null }, x)))),
                        h("tbody", {}, top.map(d => h("tr", {},
                            h("td", { class: "mono" }, "/dev/" + d.dev), h("td", { class: "num" }, fmtBytes(d.r)), h("td", { class: "num" }, fmtBytes(d.w)),
                            h("td", { class: "num" }, Math.round(d.iops)), h("td", { class: "num" }, `${Math.round(d.util)}%`),
                            h("td", { class: "num" }, d.lat == null ? "—" : d.lat.toFixed(1) + " ms"))))) : null)));
        }));
}

/* ── Settings ─────────────────────────────────────────────────────────── */
function systemdTimer() {
    return cockpit.spawn(["systemctl", "show", "hbaviewer-sample.timer", "-p", "ActiveState", "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime"], { err: "ignore" })
        .then(o => Object.fromEntries(o.trim().split("\n").map(l => l.split("=", 2))))
        .catch(() => ({}));
}

const FIELDS = [
    ["ALERT_THRESHOLD", "Temperature alert", "select", [[66, "66 °C"], [76, "76 °C (default)"], [86, "86 °C"], [96, "96 °C"]],
        "Controllers above this read as warn on the overview. Most LSI chips are rated to ~105 °C junction."],
    ["TEMP_UNIT", "Temperature unit", "select", [["C", "Celsius"], ["F", "Fahrenheit"]]],
    ["LOCATE_MAX_SECS", "Locate auto-stop (s)", "number", null, "30–1800. A forgotten blink cannot keep a disk awake longer than this."],
    ["EVENT_LIMIT", "Event read size", "number", null, "StorCLI2 only: how many latest events to read per refresh (10–500)."],
    ["PCIE_EXPECT_WIDTH", "Expected PCIe width", "select", [[0, "Auto (slot / card maximum)"], [1, "x1"], [2, "x2"], [4, "x4"], [8, "x8"], [16, "x16"]],
        "Override what the Host link indicator compares against."],
    ["PCIE_EXPECT_GEN", "Expected PCIe gen", "select", [[0, "Auto"], [1, "Gen1"], [2, "Gen2"], [3, "Gen3"], [4, "Gen4"], [5, "Gen5"]]],
    ["HBA_PORT", "lsiutil fallback port", "number", null, "SAS2 only, used when lsiutil's port table cannot be parsed."],
    ["NOTIFY_CMD", "Notify command", "text", null,
        "Optional. Run by /bin/sh when a controller's health changes state. Gets HBAV_CTL, HBAV_PREV, HBAV_STATE, HBAV_REASON, HBAV_MESSAGE. Every change is also written to the journal (journalctl -t hbaviewer)."],
];

function renderSettings() {
    const p = state.data.settings, el = panel("settings");
    const err = payloadError(p);
    if (err) { fill(el, errorBox(err)); return }
    const env = p.env || {};
    if (env.error) { fill(el, errorBox(env.error)); return }
    const cur = env.settings || state.settings;
    const inputs = {};
    const form = h("form", { class: "hv-form" }, FIELDS.map(([k, label, kind, opts, help]) => {
        let inp;
        if (kind === "select") {
            inp = h("select", { id: `f-${k}` }, opts.map(([v, t]) => h("option", { value: v }, t)));
            inp.value = String(cur[k]);
        } else {
            inp = h("input", { id: `f-${k}`, type: kind === "number" ? "number" : "text", value: cur[k] ?? "" });
        }
        inputs[k] = inp;
        return [h("label", { for: `f-${k}` }, label), inp, help ? h("div", { class: "help" }, help) : null];
    }));
    const msg = h("span", { class: "hv-muted" });
    const save = h("button", { class: "hv-btn", type: "button" }, "Save");
    save.addEventListener("click", async () => {
        save.disabled = true; msg.textContent = "Saving…";
        try {
            const r = await ctl("config", "set", ...Object.entries(inputs).map(([k, i]) => `${k}=${i.value}`));
            if (r.error) throw new Error(r.error);
            state.settings = r;
            state.at = {};       // bands and units changed: re-read everything on next view
            msg.textContent = "Saved.";
        } catch (e) { msg.textContent = e.message }
        save.disabled = false;
    });

    const t = p.timer || {};
    const diag = [
        ["Backend in use", env.backend || "none — no readable controller found"],
        ["storcli", env.storcli ? `${env.storcli} (${env.storcli_flavor}, ${env.storcli_count} controller${env.storcli_count === "1" ? "" : "s"})` : "not found"],
        ["lsiutil (bundled)", env.lsiutil || "not installed"],
        ["Driver", env.driver || "—"], ["SCSI host personalities", env.personalities || "—"],
        ["smartctl", env.smartctl || "not installed (apt install smartmontools)"],
        ["gawk", env.gawk ? "yes" : "MISSING — apt install gawk"],
        ["Sample timer", t.ActiveState ? `${t.ActiveState}${t.NextElapseUSecRealtime ? ", next " + t.NextElapseUSecRealtime : ""}` : "unknown"],
        ["System", `${env.os} · kernel ${env.kernel}`],
    ];
    fill(el, h("div", { class: "hv-grid", style: "grid-template-columns:repeat(auto-fill,minmax(460px,1fr))" },
        h("div", { class: "hv-card" }, h("div", { class: "hv-card-head" }, h("h3", {}, "Settings")),
            h("div", { class: "hv-card-body" }, form, h("div", { class: "hv-toolbar", style: "margin:16px 0 0" }, save, msg))),
        h("div", { class: "hv-card" }, h("div", { class: "hv-card-head" }, h("h3", {}, "Detected hardware & tools")),
            h("div", { class: "hv-card-body" },
                h("dl", { class: "hv-kv" }, diag.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
                h("h3", { style: "margin:14px 0 6px" }, "LSI / Broadcom PCI devices"),
                env.pci && env.pci.length ? h("pre", { class: "mono", style: "white-space:pre-wrap;margin:0" }, env.pci.join("\n")) : h("p", { class: "hv-muted" }, "None found by lspci.")))));
}

/* ── dialog ───────────────────────────────────────────────────────────── */
function openDialog(title, body) {
    $("#hv-dlg-title").textContent = title;
    fill($("#hv-dlg-body"), body);
    const d = $("#hv-dialog");
    if (!d.open) d.showModal();
}

/* ── boot ─────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
    for (const b of document.querySelectorAll("#hv-tabs button"))
        b.addEventListener("click", () => selectTab(b.dataset.tab));
    $("#hv-refresh").addEventListener("click", () => {
        if (state.tab === "perf") { perf.series = {}; perf.phy0 = {}; perf.prev = null; return }
        if (state.tab === "phy") state.at.drives = 0, state.data.drives = null;
        load(state.tab, true);
    });
    $("#hv-dlg-close").addEventListener("click", () => $("#hv-dialog").close());
    window.addEventListener("resize", () => { if (state.tab === "perf") renderPerf() });

    ctl("config", "get").then(s => { if (!s.error) state.settings = s }).catch(() => {})
        .finally(() => {
            let first = "overview";
            try { const p = cockpit.location.path[0]; if (RENDER[p]) first = p } catch (e) { /* standalone */ }
            selectTab(first);
        });
    // Keep the overview + health live while the page is open.
    setInterval(() => { if (state.tab === "overview" || state.tab === "health") load(state.tab, true) }, 60000);
});
