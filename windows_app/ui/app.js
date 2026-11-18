/* CopyClip window logic. Vanilla JS, no dependencies, no innerHTML: every string reaches the DOM as text. */
"use strict";
(() => {
  // ------------------------------------------------------------------ helpers
  const SVGNS = "http://www.w3.org/2000/svg";
  const $ = (sel, root = document) => root.querySelector(sel);

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    }
    const add = (kid) => {
      if (kid === null || kid === undefined || kid === false) return;
      if (Array.isArray(kid)) kid.forEach(add);
      else node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    };
    kids.forEach(add);
    return node;
  }


  // Append children, skipping null/false and flattening arrays (Element.append would print "null").
  function add(el, ...kids) {
    el.append(...h("div", {}, ...kids).childNodes);
    return el;
  }

  // Replace an element's children. Unlike replaceChildren(), flattens arrays and skips null/false.
  function fill(el, ...kids) {
    const tmp = h("div", {}, ...kids);
    el.replaceChildren(...tmp.childNodes);
    return el;
  }

  function icon(name, cls = "") {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "i " + cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVGNS, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }

  async function api(method, path, body) {
    const opt = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) {
      const err = new Error((data && data.error) || r.statusText || "request failed");
      err.status = r.status;
      err.problems = (data && data.problems) || [];
      throw err;
    }
    return data;
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  function ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 5) return "just now";
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function duration(sec) {
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.floor(sec / 60)} min`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
    return `${Math.floor(sec / 86400)} d ${Math.floor((sec % 86400) / 3600)} h`;
  }
  function size(n, kind) {
    if (kind === "text" || kind === undefined) return `${n.toLocaleString()} characters`;
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function dayLabel(ts) {
    const d = new Date(ts * 1000), now = new Date();
    const same = (a, b) => a.toDateString() === b.toDateString();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    return same(d, now) ? "Today" : same(d, y) ? "Yesterday" : d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  }

  function toast(message, kind = "ok") {
    const t = h("div", { class: "toast " + kind, role: kind === "er" ? "alert" : "status" },
      icon(kind === "er" ? "alert" : "check"), h("span", { text: message }));
    $("#toasts").append(t);
    setTimeout(() => t.remove(), kind === "er" ? 7000 : 3500);
  }

  function dialog(build) {
    const dlg = h("dialog");
    document.body.append(dlg);
    dlg.addEventListener("close", () => dlg.remove());
    build(dlg);
    dlg.showModal();
    return dlg;
  }

  function confirmBox({ title, body, confirm = "Confirm", danger = false }) {
    return new Promise((resolve) => {
      dialog((dlg) => {
        let result = false;
        dlg.append(
          h("div", { class: "dlg-h" }, h("h1", { text: title })),
          h("div", { class: "dlg-b" }, [].concat(body).map((p) => h("p", { class: "sub", style: "margin:0", text: p }))),
          h("div", { class: "dlg-f" },
            h("button", { class: "btn", text: "Cancel", onclick: () => dlg.close() }),
            h("button", { class: "btn pri" + (danger ? " danger" : ""), text: confirm, onclick: () => { result = true; dlg.close(); } })));
        dlg.addEventListener("close", () => resolve(result));
      });
    });
  }

  let openMenu = null;
  function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
  function menu(anchor, items) {
    closeMenu();
    const r = anchor.getBoundingClientRect();
    const m = h("div", { class: "menu", role: "menu", style: `left:${Math.max(8, r.right - 200)}px;top:${r.bottom + 4}px` },
      items.map(([label, fn]) => h("button", { role: "menuitem", text: label, onclick: () => { closeMenu(); fn(); } })));
    document.body.append(m);
    openMenu = m;
    m.querySelector("button").focus();
  }
  document.addEventListener("click", (e) => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

  const guard = (fn, ok) => async (...a) => {
    try { const r = await fn(...a); if (ok) toast(typeof ok === "function" ? ok(r) : ok); return r; }
    catch (e) { toast(e.message, "er"); return null; }
  };

  // -------------------------------------------------------------------- state
  const S = {
    status: null, statusKey: "", config: null, events: [], eventsKey: "", actTotal: 0,
    selectedDevice: null, selectedEvent: null, actFilter: "all", actQuery: "",
    logLevel: "ALL", logDevice: "", logQuery: "", logFollow: true, logs: null, logsKey: "",
    restart: new Set(), screen: null, route: "overview", online: true, checks: {},
  };
  const NAV = [["overview", "Overview", "home"], ["devices", "Devices", "phone"], ["activity", "Activity", "pulse"], ["logs", "Logs", "term"]];

  // ------------------------------------------------------------- derived data
  const quietSet = () => { try { return new Set(JSON.parse(localStorage.getItem("cc_quiet") || "[]")); } catch (_) { return new Set(); } };
  const isQuiet = (n) => quietSet().has(n);
  function setQuiet(n, on) {
    const q = quietSet(); on ? q.add(n) : q.delete(n);
    try { localStorage.setItem("cc_quiet", JSON.stringify([...q])); } catch (_) { /* storage blocked */ }
    S.statusKey = ""; refreshScreen(); buildNav();
  }
  const waitingNames = () => ((S.status && S.status.outbox_targets) || []).filter((n) => {
    const i = S.status.nodes[n]; return !isQuiet(n) && i && !i.paused && i.enabled !== false;
  });
  const waitingCount = () => waitingNames().length;
  // Relative times are refreshed in place so the layout never re-mounts or jumps.
  setInterval(() => document.querySelectorAll("[data-ago]").forEach((el) => {
    const ts = Number(el.getAttribute("data-ago")); if (!ts) return;
    const t = ago(ts); el.textContent = el.hasAttribute("data-strip") ? t.replace(" ago", "") : t;
  }), 5000);
  const deviceNodes = () => Object.entries((S.status && S.status.nodes) || {}).filter(([n]) => n !== "laptop");
  function deviceState(info) {
    if (!info) return { tone: "mu", text: "Disabled" };
    if (info.paused) return { tone: "mu", text: "Paused" };
    if (info.available) return { tone: "ok", text: info.locked ? "Connected, screen locked" : "Connected" };
    return { tone: "er", text: "Offline" };
  }
  function summary() {
    const st = S.status;
    // Devices the person switched off, or marked as occasional while offline, are not a problem.
    const quiet = deviceNodes().filter(([n, i]) => isQuiet(n) && !i.available && !i.paused);
    const nodes = deviceNodes().filter(([n, i]) => !i.paused && i.enabled !== false && !(isQuiet(n) && !i.available));
    const online = nodes.filter(([, i]) => i.available && !i.paused).length;
    const total = nodes.length;
    if (st.paused) {
      const until = st.paused_until ? ` until ${clock(st.paused_until)}` : "";
      return { tone: "mu", icon: "pause", title: "Sync is paused", sub: `Nothing is being copied${until}. Resume to continue.`, label: "Paused", online, total };
    }
    if (total === 0) return { tone: "mu", icon: "info", title: "No devices yet", sub: "Add a phone or tablet to start sharing your clipboard.", label: "No devices", online, total };
    if (online === total) return { tone: "ok", icon: "check", title: "All devices connected", sub: `Clipboard is in sync across this PC and ${plural(total, "device", "devices")}.${quiet.length ? ` ${quiet.map(([n, i]) => deviceLabel(n, i)).join(", ")} is marked occasional and offline.` : ""}`, label: "All online", online, total };
    if (online > 0) return { tone: "wn", icon: "alert", title: `${online} of ${total} devices connected`, sub: "Clips for offline devices wait safely and send when they return.", label: "Degraded", online, total };
    return { tone: "er", icon: "alert", title: "No devices connected", sub: "CopyClip keeps retrying in the background.", label: "Offline", online, total };
  }
  function deviceIcon(name, info) {
    const s = `${name} ${(info && info.label) || ""}`.toLowerCase();
    return s.includes("tab") ? "tablet" : s.includes("laptop") || s.includes("pc") ? "laptop" : "phone";
  }
  const deviceLabel = (name, info) => (info && info.label && info.label !== name ? info.label : name);

  // ------------------------------------------------------------------- shared
  function pill(tone, text) { return h("span", { class: "pl " + tone, text }); }
  function pageHeader(title, subtitle, ...actions) {
    return h("div", { class: "hd" }, h("div", {}, h("h1", { text: title }), subtitle && h("div", { class: "sub", text: subtitle })),
      actions.length ? h("div", { class: "row" }, actions) : null);
  }
  function switchBtn(on, label, onToggle, disabled = false) {
    const b = h("button", { class: "sw", role: "switch", "aria-checked": String(!!on), "aria-label": label, disabled });
    b.addEventListener("click", () => { const next = b.getAttribute("aria-checked") !== "true"; b.setAttribute("aria-checked", String(next)); onToggle(next); });
    return b;
  }
  function restartBanner() {
    if (!S.restart.size) return null;
    return h("div", { class: "banner", role: "status" }, icon("refresh"),
      h("div", { class: "grow", text: `Restart CopyClip to apply: ${[...S.restart].join(", ")}.` }),
      h("button", { class: "btn pri", text: "Restart now", onclick: doRestart }));
  }
  async function doRestart() {
    if (!await confirmBox({ title: "Restart CopyClip?", body: "Syncing pauses for a few seconds. A new window opens when it is back.", confirm: "Restart" })) return;
    try { await api("POST", "/api/restart", {}); } catch (e) { return toast(e.message, "er"); }
    fill(document.body, h("p", { style: "padding:32px", text: "CopyClip is restarting. A new window will open in a moment. You can close this one." }));
    setTimeout(() => window.close(), 1500);
  }

  function pauseMenu(anchor) {
    const st = S.status;
    if (st.paused) return post("/api/pause", { paused: false }, "Sync resumed");
    menu(anchor, [
      ["Pause for 15 minutes", () => post("/api/pause", { paused: true, minutes: 15 }, "Paused for 15 minutes")],
      ["Pause for 1 hour", () => post("/api/pause", { paused: true, minutes: 60 }, "Paused for 1 hour")],
      ["Pause until I resume", () => post("/api/pause", { paused: true }, "Sync paused")],
    ]);
  }
  async function post(path, body, okMsg) {
    const r = await guard(() => api("POST", path, body || {}), okMsg)();
    await pollStatus(true);
    return r;
  }
  const openFolder = (which) => guard(() => api("POST", "/api/folders/open", { which }))();
  const sendClipboard = guard(() => api("POST", "/api/send-clipboard", {}), (r) => `Sent ${r.sent.toLocaleString()} characters to your devices`);

  function deviceRow(name, info, { compact = false } = {}) {
    const st = deviceState(info);
    return h("div", { class: compact ? "dv" : "card dv" },
      h("div", { class: "tile" }, icon(deviceIcon(name, info))),
      h("div", { class: "dm" },
        h("div", { class: "dn" }, deviceLabel(name, info), pill(st.tone, st.text)),
        h("div", { class: "ds", text: [name !== deviceLabel(name, info) ? name : null, (info.transport || "adb").toUpperCase(), info.address].filter(Boolean).join(" · ") })),
      h("div", { class: "row" },
        !compact && h("button", { class: "btn sub ib", "aria-label": `Reconnect ${deviceLabel(name, info)}`, title: "Reconnect",
          onclick: () => post(`/api/devices/${name}/reconnect`, {}, `Reconnecting ${deviceLabel(name, info)}`) }, icon("refresh")),
        switchBtn(!info.paused, `Sync with ${deviceLabel(name, info)}`, (on) => post(`/api/devices/${name}/${on ? "resume" : "pause"}`, {}))));
  }

  const copyClip = (e) => guard(() => api("POST", `/api/activity/${e.id}/copy`, {}), e.has_image ? "Image copied. Paste it anywhere." : "Text copied. Paste it anywhere.")();
  const copyImage = copyClip;
  function imageStrip(limit = 8) {
    const imgs = S.events.filter((e) => e.has_image).slice(0, limit);
    if (!imgs.length) return null;
    return h("div", { class: "strip", role: "list", "aria-label": "Recent pictures. Select one to copy the full image." },
      imgs.map((e) => h("button", { class: "th", role: "listitem", title: `${e.origin === "laptop" ? "This PC" : e.origin}, ${ago(e.ts)}. Copy the full image.`,
        "aria-label": `Copy the full image from ${e.origin === "laptop" ? "this PC" : e.origin}, ${ago(e.ts)}`, onclick: () => copyImage(e) },
        h("img", { src: `/api/activity/${e.id}/image`, alt: "", loading: "lazy", decoding: "async" }), h("span", { text: "Copy" }))));
  }

  function activityRow(e, { onclick, selected } = {}) {
    const kindIcon = e.sensitive ? "lock" : e.kind === "image" ? "image" : e.kind === "camera" ? "camera" : "text";
    const shown = e.targets.filter((t) => t.status !== "paused" && t.status !== "superseded" && !(e.status === "delivered" && t.status === "queued"));
    const targets = shown.map((t) => (t.status === "mirrored" ? `${t.node} (sync)` : t.node)).join(", ")
      || e.targets.map((t) => t.node).join(", ");
    const waitingOn = e.status === "delivered" ? e.targets.filter((t) => t.status === "queued").map((t) => t.node) : [];
    const route = e.targets.length ? `${e.origin === "laptop" ? "This PC" : e.origin} to ${targets}${waitingOn.length ? `. ${waitingOn.join(", ")} waiting` : ""}` : e.origin === "laptop" ? "This PC" : e.origin;
    const tone = { delivered: "ok", queued: "wn", skipped: "wn", paused: "mu", partial: "wn", superseded: "mu", insync: "mu" }[e.status] || "mu";
    const label = { delivered: "Delivered", queued: "Queued", skipped: "Skipped", paused: "Paused", partial: "Partial", insync: "In sync" }[e.status] || e.status;
    const body = [
      e.has_image
        ? h("img", { class: "rt", src: `/api/activity/${e.id}/image`, alt: "", loading: "lazy", decoding: "async" })
        : h("div", { class: "at" }, icon(kindIcon, "sm")),
      h("div", { class: "ab" },
        h("b", { text: e.sensitive ? "Sensitive clip" : e.kind === "image" ? `Image, ${size(e.size, "bytes")}` : e.kind === "camera" ? `Camera photo, ${size(e.size, "bytes")}` : `Text, ${size(e.size)}` }),
        h("div", { class: "l2", text: [route, e.reason].filter(Boolean).join(" · ") })),
    ];
    if (onclick) body.push(pill(tone, label));
    body.push(h("span", { class: "when", "data-ago": String(e.ts), text: ago(e.ts) }));
    if (!onclick) return h("div", { class: "ar" }, body);
    // One stripe: the copy button lives inside the row, so hover and selection cover it too.
    body.push((e.has_image || e.has_text)
      ? h("button", { class: "cp", title: e.has_image ? "Copy the full image" : "Copy the text", "aria-label": e.has_image ? "Copy the full image" : "Copy the text",
          onclick: (ev) => { ev.stopPropagation(); copyClip(e); } }, icon("copy"))
      : h("span", { class: "cp ph0", "aria-hidden": "true" }));
    return h("div", { class: "ar click" + (selected ? " sel" : ""), role: "button", tabindex: "0", "aria-pressed": String(!!selected), onclick,
      onkeydown: (ev) => { if ((ev.key === "Enter" || ev.key === " ") && ev.target === ev.currentTarget) { ev.preventDefault(); onclick(); } } }, body);
  }

  // ----------------------------------------------------------------- overview
  function overviewScreen() {
    const st = S.status;
    const page = h("div", { class: "page" });
    if (!st) { add(page, h("div", { class: "skeleton" }), h("div", { class: "skeleton" })); return page; }
    const sm = summary();
    const nodes = deviceNodes();
    const offline = nodes.filter(([n, i]) => !i.available && !i.paused && i.enabled !== false && !isQuiet(n));
    const waiting = waitingNames();
    const lastClip = Math.max(0, ...Object.values(st.nodes).map((n) => n.last_clip_ts || 0));

    add(page, restartBanner(),
      pageHeader("Overview", "Your clipboard, shared across this PC and your devices.",
        h("button", { class: "btn", onclick: (e) => pauseMenu(e.currentTarget) }, icon(st.paused ? "play" : "pause"), st.paused ? "Resume sync" : "Pause sync"),
        h("button", { class: "btn pri", onclick: () => post("/api/devices/all/reconnect", {}, "Reconnecting devices") }, icon("refresh"), "Reconnect devices")));

    if (offline.length && !st.paused) {
      const [name, info] = offline[0];
      add(page, h("div", { class: "banner", role: "status" }, icon("alert"),
        h("div", { class: "grow" }, h("b", { text: `${deviceLabel(name, info)} is unreachable. ` }),
          info.last_error ? `${info.last_error.replace(/\.$/, "")}.` : "Wireless ADB may need re-arming after a restart.",
          waiting.length ? ` ${plural(waiting.length, "clip is", "clips are")} waiting and will send when it reconnects.` : ""),
        h("button", { class: "btn", text: "Dismiss", title: "Mark as an occasional device. It stops counting as a problem while offline.", onclick: () => setQuiet(name, true) }),
        h("button", { class: "btn", text: "Details", onclick: () => { S.selectedDevice = name; location.hash = "#/devices"; } }),
        h("button", { class: "btn pri", text: "Retry now", onclick: () => post(`/api/devices/${name}/reconnect`, {}, `Retrying ${deviceLabel(name, info)}`) })));
    }

    const hero = h("section", { class: "card hero", "aria-label": "Status" },
      h("div", { class: "hr" }, h("div", { class: "ht " + sm.tone }, icon(sm.icon)),
        h("div", {}, h("div", { class: "hh", text: sm.title }), h("div", { class: "sub", text: sm.sub }))),
      h("div", { class: "ms" },
        h("div", { class: "m" }, h("b", { text: String(st.stats.delivered) }), h("span", { text: "Delivered", title: "Clips delivered since CopyClip started" })),
        h("div", { class: "m" }, h("b", { text: String(waiting.length) }), h("span", { text: "Waiting", title: waiting.length ? `Queued for ${waiting.join(", ")}` : "Nothing is waiting for a device" })),
        h("div", { class: "m" }, h("b", { text: String(st.stats.skipped) }), h("span", { text: "Skipped", title: "Clips blocked by your size, type or sensitivity settings" })),
        h("div", { class: "m" }, h("b", { "data-ago": lastClip ? String(lastClip) : "", "data-strip": "1", text: lastClip ? ago(lastClip).replace(" ago", "") : "none" }), h("span", { text: "Last clip" }))));

    const devicesCard = h("section", { class: "card pn", "aria-label": "Devices" },
      h("div", { class: "ph" }, h("h2", { text: "Devices" }), h("button", { class: "btn sub", text: "Manage devices", onclick: () => { location.hash = "#/devices"; } })),
      h("div", { class: "pb" },
        nodes.length ? nodes.map(([n, i]) => deviceRow(n, i, { compact: true })) : h("div", { class: "empty" }, "No devices configured.",
          h("div", {}, h("button", { class: "btn pri", style: "margin-top:12px", text: "Add a device", onclick: () => { location.hash = "#/devices"; setTimeout(() => editDeviceDialog(null), 50); } }))),
        (st.domains || []).map((d) => h("div", { class: "dv" },
          h("div", { class: "tile" }, icon("pulse")),
          h("div", { class: "dm" }, h("div", { class: "dn" }, `${d.domain} sync group`, pill(d.healthy ? "ok" : "wn", d.healthy ? "Healthy" : "Degraded")),
            h("div", { class: "ds", text: `Samsung Continuity mirroring. Representative: ${d.representative || "none"}` }))))));

    const activityCard = h("section", { class: "card pn", "aria-label": "Recent activity" },
      h("div", { class: "ph" }, h("h2", { text: "Recent activity" }), h("button", { class: "btn sub", text: "View all", onclick: () => { location.hash = "#/activity"; } })),
      h("div", { class: "pb" }, S.events.length ? S.events.slice(0, 4).map((e) => activityRow(e)) : h("div", { class: "empty", text: "Nothing copied yet." })));

    const actions = h("section", { class: "card qs", "aria-label": "Quick actions" },
      h("h2", { text: "Quick actions" }),
      h("div", { class: "chips" },
        h("button", { class: "btn pri", onclick: sendClipboard }, icon("send"), "Send clipboard to all devices"),
        h("button", { class: "btn", onclick: () => openFolder("screenshots") }, icon("folder"), "Screenshots"),
        h("button", { class: "btn", onclick: () => openFolder("camera") }, icon("camera"), "Camera photos"),
        nodes.map(([n, i]) => h("button", { class: "btn", onclick: () => openFolder("device:" + n) }, icon("folder"), deviceLabel(n, i)))));
    add(page, hero, h("div", { class: "cols" }, devicesCard, activityCard), actions);
    return page;
  }

  // ------------------------------------------------------------------ devices
  function configDevices() {
    return (S.config && S.config.config.devices) || [];
  }
  function groupOf(name) {
    const d = ((S.config && S.config.config.domains) || []).find((x) => x.members.includes(name));
    return d ? d.name : "";
  }

  function devicesScreen() {
    const page = h("div", { class: "page" });
    if (!S.status || !S.config) { add(page, h("div", { class: "skeleton" })); return page; }
    const list = configDevices();
    if (!S.selectedDevice || !list.some((d) => d.name === S.selectedDevice)) S.selectedDevice = list[0] ? list[0].name : null;
    add(page, restartBanner(),
      pageHeader("Devices", "Add, test and manage the devices that share this PC's clipboard.",
        h("button", { class: "btn pri", onclick: () => editDeviceDialog(null) }, icon("plus"), "Add device")));
    if (!list.length) {
      add(page, h("div", { class: "card empty" }, "No devices yet. Turn on wireless debugging on your phone, then add it here."));
      return page;
    }
    const left = h("div", { class: "lst", role: "list" }, list.map((d) => {
      const info = S.status.nodes[d.name];
      const stt = d.enabled === false ? { tone: "mu", text: "Disabled" } : deviceState(info);
      return h("button", { class: "it", role: "listitem", "aria-current": String(d.name === S.selectedDevice),
        onclick: () => { S.selectedDevice = d.name; refreshScreen(); } },
        h("div", { class: "tile" }, icon(deviceIcon(d.name, d))),
        h("div", { class: "im" }, h("b", { text: deviceLabel(d.name, d) }), h("span", { text: `${d.name} · ${groupOf(d.name) || "standalone"}` })),
        pill(stt.tone, stt.text));
    }));
    add(page, h("div", { class: "split" }, left, deviceDetail(list.find((d) => d.name === S.selectedDevice))));
    return page;
  }

  function deviceDetail(dev) {
    const info = S.status.nodes[dev.name];
    const stt = dev.enabled === false ? { tone: "mu", text: "Disabled" } : deviceState(info);
    const checks = h("div", { id: "checks" });
    const showChecks = (r) => fill(checks, r.steps.map((s) => h("div", { class: "ck " + (s.ok ? "ok" : "er") }, icon(s.ok ? "check" : "alert"),
      h("span", { text: `${s.step}: ${s.detail}` }), h("span", { class: "ms2", text: `${s.ms} ms` }))));
    if (S.checks[dev.name]) showChecks(S.checks[dev.name]);
    const runChecks = async () => {
      fill(checks, h("div", { class: "ck" }, "Running checks..."));
      const r = await guard(() => api("POST", "/api/devices/test", { address: dev.device, transport: dev.transport || "adb" }))();
      if (!r) return fill(checks);
      S.checks[dev.name] = r;
      showChecks(r);
    };
    return h("div", { class: "det" },
      h("section", { class: "card dh" },
        h("div", { class: "tile" }, icon(deviceIcon(dev.name, dev), "")),
        h("div", { style: "flex:1;min-width:160px" }, h("div", { class: "hh", text: deviceLabel(dev.name, dev) }),
          h("div", { class: "sub" }, pill(stt.tone, stt.text), info ? [" last clip ", h("span", { "data-ago": info.last_clip_ts ? String(info.last_clip_ts) : "", text: ago(info.last_clip_ts) })] : "")),
        info && h("button", { class: "btn", onclick: () => post(`/api/devices/${dev.name}/reconnect`, {}, "Reconnecting") }, icon("refresh"), "Reconnect"),
        h("button", { class: "btn", title: "An occasional device is not reported as a problem while it is offline.", onclick: () => setQuiet(dev.name, !isQuiet(dev.name)) }, isQuiet(dev.name) ? "Expect always online" : "Mark as occasional"),
        info && switchBtn(!info.paused, `Sync with ${deviceLabel(dev.name, dev)}`, (on) => post(`/api/devices/${dev.name}/${on ? "resume" : "pause"}`, {}))),
      h("section", { class: "card", "aria-label": "Connection details" }, h("div", { class: "kv" },
        h("div", { text: "Name" }), h("div", { class: "mono", text: dev.name }),
        h("div", { text: "Address" }), h("div", { class: "mono", text: dev.device }),
        h("div", { text: "Transport" }), h("div", { text: (dev.transport || "adb") === "adb" ? "ADB over Tailscale (no app on the device)" : "TCP with shared key" }),
        h("div", { text: "Sync group" }), h("div", { text: groupOf(dev.name) ? `${groupOf(dev.name)} (mirrors by itself)` : "Standalone" }),
        info && h("div", { text: "Screen" }), info && h("div", { text: info.locked ? "Locked, clips are held until unlock" : "Unlocked" }),
        info && info.last_error && h("div", { text: "Last error" }), info && info.last_error && h("div", { text: info.last_error }))),
      h("section", { class: "card", "aria-label": "Health checks" },
        h("div", { class: "row", style: "justify-content:space-between;padding:14px 20px 8px" }, h("h2", { text: "Health checks" }),
          h("button", { class: "btn sub", text: "Run checks", onclick: runChecks })), checks),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: () => editDeviceDialog(dev) }, icon("edit"), "Edit"),
        h("button", { class: "btn sub danger", onclick: () => removeDevice(dev) }, icon("trash"), "Remove")));
  }

  async function removeDevice(dev) {
    if (!await confirmBox({ title: `Remove ${deviceLabel(dev.name, dev)}?`, body: "It stops syncing after the next restart. You can add it again any time.", confirm: "Remove", danger: true })) return;
    const r = await guard(() => api("DELETE", `/api/devices/${dev.name}`))();
    if (r) { r.restart_required.forEach((k) => S.restart.add(k)); await loadConfig(); refreshScreen(); toast("Device removed"); }
  }

  function editDeviceDialog(existing) {
    const groups = [...new Set(((S.config && S.config.config.domains) || []).map((d) => d.name))];
    dialog((dlg) => {
      const f = {
        name: h("input", { class: "in", value: existing ? existing.name : "", "aria-label": "Name", maxlength: "32", autocomplete: "off" }),
        label: h("input", { class: "in", value: (existing && existing.label) || "", "aria-label": "Display name", maxlength: "60", placeholder: "Optional, for example Galaxy S24 Ultra" }),
        addr: h("input", { class: "in mono", value: existing ? existing.device : "", "aria-label": "Address", placeholder: "100.x.x.x:5555", autocomplete: "off" }),
        transport: h("select", { class: "in", "aria-label": "Connection" }, h("option", { value: "adb", text: "ADB over Tailscale (recommended)" }), h("option", { value: "tcp", text: "TCP with shared key" })),
        group: h("select", { class: "in", "aria-label": "Sync group" }, h("option", { value: "none", text: "None, standalone device" }),
          groups.map((g) => h("option", { value: g, text: `${g} (devices that already mirror each other)` })), h("option", { value: "__new", text: "New group..." })),
        newGroup: h("input", { class: "in", placeholder: "Group name", "aria-label": "New group name", hidden: true, maxlength: "32" }),
      };
      f.transport.value = (existing && existing.transport) || "adb";
      f.group.value = existing ? groupOf(existing.name) || "none" : "none";
      let enabled = existing ? existing.enabled !== false : true;
      const errBox = h("div", { class: "err", role: "alert", hidden: true });
      const tests = h("div", { class: "tst", role: "status", "aria-label": "Connection test", hidden: true });
      f.group.addEventListener("change", () => { f.newGroup.hidden = f.group.value !== "__new"; });
      const test = async () => {
        tests.hidden = false; fill(tests, h("div", { class: "tr" }, "Testing..."));
        try {
          const r = await api("POST", "/api/devices/test", { address: f.addr.value, transport: f.transport.value });
          fill(tests, r.steps.map((s) => h("div", { class: "tr " + (s.ok ? "ok" : "er") }, icon(s.ok ? "check" : "alert"), h("span", { text: `${s.step}: ${s.detail}` }), h("span", { class: "ms2", text: `${s.ms} ms` }))));
        } catch (e) { fill(tests, h("div", { class: "tr er" }, icon("alert"), e.message)); }
      };
      const save = async () => {
        errBox.hidden = true;
        const grp = f.group.value === "__new" ? f.newGroup.value.trim() : f.group.value;
        const device = { name: f.name.value.trim(), device: f.addr.value.trim(), transport: f.transport.value, enabled, label: f.label.value.trim() };
        try {
          const r = existing ? await api("PUT", `/api/devices/${existing.name}`, { device, group: grp }) : await api("POST", "/api/devices", { device, group: grp });
          r.restart_required.forEach((k) => S.restart.add(k));
          S.selectedDevice = device.name;
          dlg.close(); await loadConfig(); refreshScreen();
          toast(existing ? "Device saved" : "Device added. Restart CopyClip to start syncing with it.");
        } catch (e) {
          errBox.hidden = false; fill(errBox, icon("alert", "sm"), (e.problems.length ? e.problems : [e.message]).join(". "));
        }
      };
      dlg.append(
        h("div", { class: "dlg-h" }, h("h1", { text: existing ? "Edit device" : "Add a device" }), h("div", { class: "hint", text: "Wireless debugging must be on. No app is installed on the device." })),
        h("div", { class: "dlg-b" },
          h("label", { class: "f" }, "Name", f.name, h("span", { class: "hint", text: "Short id used in logs and settings. Letters, digits, - and _." })),
          h("label", { class: "f" }, "Display name", f.label),
          h("label", { class: "f" }, "Address", f.addr, h("span", { class: "hint", text: "Tailscale IP and port, for example 100.x.x.x:5555." })),
          h("label", { class: "f" }, "Connection", f.transport),
          h("label", { class: "f" }, "Sync group", f.group, f.newGroup),
          h("div", { class: "row", style: "justify-content:space-between" },
            h("div", {}, h("b", { style: "font-weight:500;font-size:13px", text: "Enabled" }), h("div", { class: "hint", text: "Disabled devices stay saved but are not contacted." })),
            switchBtn(enabled, "Enabled", (v) => { enabled = v; })),
          tests, errBox),
        h("div", { class: "dlg-f" },
          h("button", { class: "btn", style: "margin-right:auto", text: "Test connection", onclick: test }),
          h("button", { class: "btn", text: "Cancel", onclick: () => dlg.close() }),
          h("button", { class: "btn pri", text: existing ? "Save" : "Add device", onclick: save })));
      f.name.focus();
    });
  }

  // ----------------------------------------------------------------- activity
  const ACT_FILTERS = [["all", "All"], ["text", "Text"], ["image", "Images"], ["camera", "Camera"], ["sensitive", "Sensitive"], ["skipped", "Skipped"]];
  function activityScreen() {
    const page = h("div", { class: "page" });
    const feed = h("section", { class: "card list", "aria-label": "Clip history", style: "min-height:240px" });
    const detail = h("aside", { class: "card", style: "width:320px;flex:none;padding:20px;display:flex;flex-direction:column;gap:14px", "aria-label": "Clip details" });
    const render = () => {
      const evs = S.events;
      fill(feed, );
      if (!evs.length) add(feed, h("div", { class: "empty", text: S.actFilter === "all" && !S.actQuery ? "Nothing copied yet. Copy something on any device." : "No clips match this filter." }));
      let lastDay = "";
      evs.forEach((e) => {
        const d = dayLabel(e.ts);
        if (d !== lastDay) { add(feed, h("div", { class: "day", text: d })); lastDay = d; }
        add(feed, activityRow(e, { selected: e.id === S.selectedEvent, onclick: () => { S.selectedEvent = e.id; render(); } }));
      });
      const sel = evs.find((e) => e.id === S.selectedEvent) || evs[0];
      fill(detail, );
      if (!sel) { add(detail, h("div", { class: "empty", text: "Select a clip to see its details." })); return; }
      add(detail, 
        h("div", { class: "row", style: "justify-content:space-between" }, h("h2", { text: sel.sensitive ? "Sensitive clip" : `${sel.kind[0].toUpperCase()}${sel.kind.slice(1)} clip` }), pill({ delivered: "ok" }[sel.status] || "wn", sel.status)),
        sel.has_image
          ? h("div", { class: "imgbox" }, h("img", { src: `/api/activity/${sel.id}/image`, alt: "Picture from this clip", decoding: "async" }),
              h("button", { class: "btn pri", onclick: () => copyImage(sel) }, icon("copy"), "Copy full image"))
          : h("div", { class: "imgbox" },
              h("div", { class: "card", style: "padding:14px;min-height:64px;font-size:13px;color:var(--t2);overflow-wrap:anywhere;background:var(--layer2)",
                text: sel.preview !== null ? sel.preview : sel.sensitive ? "Hidden. Sensitive clips never show a preview." : "No preview for this type." }),
              sel.has_text ? h("button", { class: "btn pri", onclick: () => copyClip(sel) }, icon("copy"), "Copy text") : null),
        h("div", { class: "kv", style: "grid-template-columns:116px 1fr" },
          h("div", { text: "From" }), h("div", { text: sel.origin === "laptop" ? "This PC" : sel.origin }),
          h("div", { text: "To" }), h("div", { text: sel.targets.map((t) => `${t.node} (${({ mirrored: "copied by Samsung sync", superseded: "replaced by a newer clip", paused: "sync is paused" })[t.status] || t.status})`).join(", ") || "nobody" }),
          h("div", { text: "Time" }), h("div", { text: `${clock(sel.ts)}${sel.latency_ms !== null ? `, ${sel.latency_ms} ms` : ""}` }),
          sel.fingerprint && h("div", { text: "Fingerprint" }), sel.fingerprint && h("div", { class: "mono", text: sel.fingerprint }),
          sel.reason && h("div", { text: "Note" }), sel.reason && h("div", { text: sel.reason })));
    };
    S.screen.update = render;
    const search = h("input", { placeholder: "Search activity", "aria-label": "Search activity", value: S.actQuery });
    search.addEventListener("input", () => { S.actQuery = search.value; S.eventsKey = ""; pollActivity(true); });
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Filter by type" });
    const buildSeg = () => fill(seg, ACT_FILTERS.map(([k, l]) => h("button", { "aria-pressed": String(S.actFilter === k), text: l,
      onclick: () => { S.actFilter = k; buildSeg(); S.eventsKey = ""; pollActivity(true); } })));
    buildSeg();
    add(page, restartBanner(),
      pageHeader("Activity", "Every clip that moved, where it went, and why anything was skipped. Previews stay on this PC.",
        h("button", { class: "btn", onclick: guard(async () => { await api("POST", "/api/activity/clear", {}); S.eventsKey = ""; await pollActivity(true); }, "History cleared") }, icon("trash"), "Clear history")),
      h("div", { class: "row", style: "justify-content:space-between" }, seg, h("label", { class: "srch" }, icon("search", "sm"), search)),
      h("div", { class: "split" }, h("div", { style: "flex:1;min-width:0" }, feed), detail));
    render();
    return page;
  }

  // --------------------------------------------------------------------- logs
  function logsScreen() {
    const page = h("div", { class: "page" });
    const box = h("section", { class: "card logwrap", "aria-label": "Log lines", tabindex: "0" });
    const foot = h("div", { class: "row t3", style: "justify-content:space-between;font-size:12px" });
    const expanded = new Set();
    const render = () => {
      const L = S.logs;
      fill(box, );
      if (!L || !L.entries.length) add(box, h("div", { class: "empty", text: L ? "No log lines match." : "Loading..." }));
      else L.entries.forEach((e, idx) => {
        const key = e.ts + e.message;
        add(box, h("div", { class: "ll" }, h("span", { class: "ts", text: e.ts.slice(11) }), h("span", { class: "lv " + e.level, text: e.level }),
          h("span", { class: "nm", text: e.name.replace("copyclip.", "") }),
          h("span", { class: "lm" }, e.message, e.detail && h("button", { class: "tg", text: expanded.has(key) ? "hide details" : "details",
            onclick: () => { expanded.has(key) ? expanded.delete(key) : expanded.add(key); render(); } }), expanded.has(key) && h("pre", { text: e.detail })),
          e.count > 1 && h("span", { class: "rp", text: `x ${e.count.toLocaleString()}` })));
      });
      fill(foot, h("span", { text: L ? `${L.entries.length} shown of ${L.matched.toLocaleString()} matching, ${L.total_lines.toLocaleString()} lines total, ${L.folded_groups} repeated groups folded` : "" }),
        h("label", { class: "row" }, h("input", { type: "checkbox", checked: S.logFollow, onchange: (ev) => { S.logFollow = ev.target.checked; } }), "Follow newest"));
      if (S.logFollow) box.scrollTop = box.scrollHeight;
    };
    S.screen.update = render;
    // render() runs before the page is attached, where scrollTop cannot move yet. Scroll again once it is on screen.
    S.screen.after = () => { if (S.logFollow) box.scrollTop = box.scrollHeight; };
    const seg = (get, set, items, label) => {
      const el = h("div", { class: "seg", role: "group", "aria-label": label });
      const build = () => fill(el, items.map(([k, l]) => h("button", { "aria-pressed": String(get() === k), text: l, onclick: () => { set(k); build(); S.logsKey = ""; pollLogs(true); } })));
      build(); return el;
    };
    const search = h("input", { placeholder: "Filter log lines", "aria-label": "Filter log lines", value: S.logQuery });
    search.addEventListener("input", () => { S.logQuery = search.value; S.logsKey = ""; pollLogs(true); });
    const text = () => (S.logs ? S.logs.entries.map((e) => `${e.ts} ${e.level} ${e.name}: ${e.message}${e.count > 1 ? ` (x${e.count})` : ""}${e.detail ? "\n" + e.detail : ""}`).join("\n") : "");
    const devs = [["", "All devices"], ...configDevices().map((d) => [d.name, d.name])];
    add(page, restartBanner(),
      pageHeader("Logs", "Live diagnostics. Identical repeated errors fold into one line with a count.",
        h("button", { class: "btn", onclick: guard(() => navigator.clipboard.writeText(text()), "Copied") }, icon("copy"), "Copy"),
        h("button", { class: "btn", onclick: () => { const a = h("a", { href: URL.createObjectURL(new Blob([text()], { type: "text/plain" })), download: "copyclip-log.txt" }); a.click(); } }, icon("down"), "Export"),
        h("button", { class: "btn", onclick: () => openFolder("logs") }, icon("folder"), "Open folder"),
        h("button", { class: "btn", onclick: async () => { if (await confirmBox({ title: "Clear the log?", body: "The current log is archived as copyclip.log.1 and a fresh one starts.", confirm: "Clear" })) { await guard(() => api("POST", "/api/logs/clear", {}), "Log cleared")(); S.logsKey = ""; pollLogs(true); } } }, icon("trash"), "Clear")),
      h("div", { class: "row", style: "justify-content:space-between" },
        h("div", { class: "row" }, seg(() => S.logLevel, (k) => { S.logLevel = k; }, [["ALL", "All"], ["INFO", "Info"], ["WARNING", "Warnings"], ["ERROR", "Errors"]], "Minimum level"),
          seg(() => S.logDevice, (k) => { S.logDevice = k; }, devs, "Device")),
        h("label", { class: "srch" }, icon("search", "sm"), search)),
      box, foot);
    render();
    return page;
  }

  // ----------------------------------------------------------------- settings
  async function pickFolder(input, key, set) {
    const r = await guard(() => api("POST", "/api/pick-folder", { initial: input.value }))();
    if (r && r.path) { input.value = r.path; set(r.path); }
  }

  function settingsScreen() {
    const cfg = S.config.config;
    const restartKeys = new Set(S.config.restart_keys);
    const draft = {};
    const bad = {};
    const rows = {};
    const get = (k) => (k in draft ? draft[k] : cfg[k]);
    const set = (k, v) => {
      if (JSON.stringify(v) === JSON.stringify(cfg[k])) delete draft[k]; else draft[k] = v;
      refresh();
    };
    const bar = h("div", { class: "savebar", role: "status", hidden: true });
    const problemsBox = h("div", { class: "banner er", role: "alert", hidden: true });
    const tag = (k) => restartKeys.has(k) ? h("span", { class: "pl mu", style: "margin-left:8px", text: "Needs restart" }) : null;
    const row = (k, title, desc, ...controls) => {
      const r = h("div", { class: "sr", "data-key": k }, h("div", { class: "tx" }, h("b", {}, title, tag(k)), desc && h("span", { text: desc })), controls);
      rows[k] = r; return r;
    };
    const swRow = (k, title, desc, confirmOn) => {
      const b = switchBtn(get(k), title, async (on) => {
        if (on && confirmOn && !await confirmBox(confirmOn)) { b.setAttribute("aria-checked", "false"); return; }
        set(k, on);
      });
      return row(k, title, desc, b);
    };
    const numRow = (k, title, desc, { unit, scale = 1, width = 90, int = true, min = 0 } = {}) => {
      const inp = h("input", { class: "in mono", style: `width:${width}px`, inputmode: "decimal", "aria-label": title, value: String(+(get(k) / scale).toFixed(2)) });
      inp.addEventListener("input", () => {
        const n = Number(inp.value.trim());
        if (inp.value.trim() === "" || !Number.isFinite(n) || n < min) { bad[k] = true; inp.setAttribute("aria-invalid", "true"); refresh(); return; }
        delete bad[k]; inp.removeAttribute("aria-invalid");
        set(k, int ? Math.round(n * scale) : n * scale);
      });
      return row(k, title, desc, inp, unit && h("span", { class: "unit", text: unit }));
    };
    const textRow = (k, title, desc, placeholder = "", width = 300) => {
      const inp = h("input", { class: "in", style: `width:${width}px`, "aria-label": title, value: get(k) || "", placeholder });
      inp.addEventListener("input", () => set(k, inp.value.trim()));
      return row(k, title, desc, inp);
    };
    const pathRow = (k, title, desc, placeholder = "") => {
      const inp = h("input", { class: "in", style: "width:300px", "aria-label": title, value: get(k) || "", placeholder });
      inp.addEventListener("input", () => set(k, inp.value.trim()));
      return row(k, title, desc, inp, h("button", { class: "btn", text: "Browse", onclick: () => pickFolder(inp, k, (v) => set(k, v)) }));
    };
    const selRow = (k, title, desc, options) => {
      const sel = h("select", { class: "in", "aria-label": title }, options.map(([v, l]) => h("option", { value: v, text: l })));
      sel.value = get(k); sel.addEventListener("change", () => set(k, sel.value));
      return row(k, title, desc, sel);
    };

    // sensitive rows need cross-dependencies
    const sens = swRow("allow_sensitive", "Sync one-time codes and password-manager copies",
      "Hidden from Win+V history and cloud clipboard. Never saved to disk. Auto-clears after the timer below.", {
        title: "Sync sensitive clips?",
        body: ["This syncs 2FA codes, Authenticator tiles and password-manager copies across your devices.",
          "Protections: they auto-clear after the timer, stay out of Win+V history, are blocked from Microsoft cloud clipboard, and are never saved to disk.",
          "Caution: any app running as you can read the Windows clipboard while a code is on it."], confirm: "Enable" });
    const wipe = numRow("sensitive_wipe_s", "Clear the clipboard after", "On every device, once the clip has been delivered.", { unit: "seconds", width: 70, int: false, min: 1 });
    const direct = swRow("direct_password_sync", "Keep passwords in history instead (direct mode)", "Turns off auto-clear and shows passwords in Win+V as plain text.", {
      title: "Enable direct password mode?",
      body: ["Passwords will appear in Windows Clipboard History (Win+V) as plain text and will not be cleared automatically.", "Passwords copied from your password manager will stay on the clipboard until you replace them."], confirm: "Enable direct mode" });
    const deps = () => {
      const on = !!get("allow_sensitive");
      [wipe, direct].forEach((r) => { r.classList.toggle("off", !on); r.querySelectorAll("input,button").forEach((c) => { c.disabled = !on; }); });
      if (!on && get("direct_password_sync")) { set("direct_password_sync", false); direct.querySelector(".sw").setAttribute("aria-checked", "false"); }
    };
    sens.querySelector(".sw").addEventListener("click", () => setTimeout(deps, 0));

    // key row
    const keyBox = h("div", { class: "mono t3", style: "overflow-wrap:anywhere;max-width:420px" });
    const keyAge = h("span", { text: "" });
    const keyRow = row("_key", "Shared key", "", h("button", { class: "btn", text: "Reveal", onclick: async (e) => {
      if (keyBox.textContent) { keyBox.textContent = ""; e.target.textContent = "Reveal"; return; }
      const r = await guard(() => api("GET", "/api/key?reveal=1"))(); if (r) { keyBox.textContent = r.key; e.target.textContent = "Hide"; }
    } }), h("button", { class: "btn", text: "Rotate key", onclick: async () => {
      if (!await confirmBox({ title: "Rotate the shared key?", body: "A new key is generated and sent to connected devices right away. Devices that are offline get it when they reconnect.", confirm: "Rotate" })) return;
      const r = await guard(() => api("POST", "/api/key/rotate", {}), (x) => `Key rotated. ${plural(x.updated_devices.length, "device", "devices")} updated.`)();
      if (r) loadKey();
    } }));
    keyRow.querySelector(".tx").append(keyAge, keyBox);
    const loadKey = async () => { const k = await guard(() => api("GET", "/api/key"))(); if (k) keyAge.textContent = k.exists ? `Created ${k.age_days} days ago. Use Reveal to copy it into a new device.` : "No key yet. Rotate to create one."; keyBox.textContent = ""; };
    loadKey();

    const themeSel = h("select", { class: "in", "aria-label": "App theme" }, [["system", "Use system setting"], ["light", "Light"], ["dark", "Dark"]].map(([v, l]) => h("option", { value: v, text: l })));
    try { themeSel.value = localStorage.getItem("cc_theme") || "system"; } catch (_) { /* storage blocked */ }
    themeSel.addEventListener("change", () => { applyTheme(themeSel.value); });

    const pathsDraft = () => ({ ...(get("device_screenshot_paths") || {}) });
    const devicePaths = configDevices().map((d) => {
      const inp = h("input", { class: "in", style: "width:300px", "aria-label": `Screenshot folder for ${d.name}`, value: (get("device_screenshot_paths") || {})[d.name] || "", placeholder: "Use the default folder" });
      inp.addEventListener("input", () => { const p = pathsDraft(); p[d.name] = inp.value.trim(); set("device_screenshot_paths", p); });
      return h("div", { class: "sr" }, h("div", { class: "tx" }, h("b", { text: `${deviceLabel(d.name, d)} screenshots` }), h("span", { text: "Optional override for this device." })), inp,
        h("button", { class: "btn", text: "Browse", onclick: () => pickFolder(inp, "device_screenshot_paths", (v) => { const p = pathsDraft(); p[d.name] = v; set("device_screenshot_paths", p); }) }));
    });

    const group = (title, ...kids) => h("div", { class: "setgroup" }, h("h2", { text: title }), h("section", { class: "card" }, kids));

    function refresh() {
      const n = Object.keys(draft).length;
      bar.hidden = n === 0;
      fill(bar, h("div", { class: "tx" }, h("b", { text: plural(n, "unsaved change", "unsaved changes") }), " ", h("span", { class: "t3", text: "Saved changes apply live unless a setting is tagged Needs restart." })),
        h("button", { class: "btn", text: "Discard", onclick: () => { settingsRoute(); } }),
        h("button", { class: "btn pri", text: "Save changes", disabled: Object.keys(bad).length > 0, onclick: save }));
    }
    async function save() {
      problemsBox.hidden = true;
      document.querySelectorAll(".sr .err").forEach((x) => x.remove());
      try {
        const r = await api("PUT", "/api/config", draft);
        r.restart_required.forEach((k) => S.restart.add(k));
        await loadConfig();
        toast(r.saved ? (r.restart_required.length ? "Saved. Some changes need a restart." : "Saved and applied") : "Nothing to save");
        settingsRoute();
      } catch (e) {
        const unmatched = [];
        (e.problems.length ? e.problems : [e.message]).forEach((p) => {
          const key = p.split(" ")[0].replace(/\[.*$/, "");
          const r = rows[key];
          if (r) { r.append(h("div", { class: "err", style: "flex-basis:100%" }, icon("alert", "sm"), p)); const i = r.querySelector("input"); if (i) i.setAttribute("aria-invalid", "true"); }
          else unmatched.push(p);
        });
        if (unmatched.length) { problemsBox.hidden = false; fill(problemsBox, icon("alert"), h("div", { class: "grow", text: unmatched.join(". ") })); }
        toast("Some settings are not valid", "er");
      }
    }

    const page = h("div", { class: "page" }, restartBanner(), pageHeader("Settings"), bar, problemsBox,
      group("General",
        row("_theme", "App theme", "Follows your Windows setting by default.", themeSel),
        swRow("autostart", "Start CopyClip when I sign in", "Adds a per-user startup entry. No administrator rights needed."),
        selRow("tray_left_click", "Clicking the tray icon opens", "Right-click always shows the full menu.", [["window", "The CopyClip window"], ["flyout", "A quick flyout"]]),
        swRow("activity_previews", "Show clip previews in Activity", "Previews stay in memory on this PC. Sensitive clips never get one. Pictures are shown from the file saved on this PC.")),
      group("Sync",
        swRow("allow_images", "Sync images and screenshots", "Turn off to send text only."),
        numRow("max_image_bytes", "Largest image to send", "Bigger images are skipped, never cut short.", { unit: "MB", scale: 1048576, width: 80, int: true, min: 1 }),
        numRow("max_chars", "Largest text clip", "Longer text is skipped and logged.", { unit: "characters", width: 110, min: 1 }),
        swRow("allow_recopy", "Copy again to refresh", "Copying the same text twice resends it to every device."),
        swRow("adopt_on_connect", "Take a device's clipboard when it connects", "Off keeps what you just copied on this PC."),
        swRow("filter_screenshot_previews", "Ignore screenshot preview images", "Samsung adds a small preview next to each screenshot. Images under 60 KB wait 2.5 seconds so the preview can be dropped. Turn off to sync every small image at once.")),
      group("Sensitive clips", sens, wipe, direct,
        h("div", { class: "warnbox" }, icon("alert"), h("span", { text: "Any app running as you can read the clipboard while a code is on it. Direct mode makes this permanent, so a confirmation appears when you turn it on." }))),
      group("Storage",
        swRow("screenshot_backup_include_devices", "Save synced screenshots to this PC", "Screenshots taken on devices are saved to the folder below."),
        pathRow("screenshot_backup_dir", "Screenshot folder", "Empty turns off saving. Where synced and local screenshots go.", "C:\\Users\\you\\Pictures\\Screenshots"),
        swRow("screenshot_device_subfolders", "One subfolder per device", "Saves into the folder above, under each device's name."),
        devicePaths,
        swRow("screenshot_exclude_from_history", "Keep synced images out of Win+V history", "Stops a large image evicting other history entries."),
        swRow("camera_backup", "Back up camera photos", "DCIM/Camera from your devices, into the folder below."),
        pathRow("camera_backup_dir", "Camera photos folder", "Empty uses a Camera subfolder of the screenshot folder."),
        swRow("camera_device_subfolders", "One camera subfolder per device")),
      group("Network and security",
        numRow("push_port", "Listening port for Send Clip", "Devices dial in here when this PC cannot reach them. 1024 to 65535.", { width: 90, min: 1024 }),
        numRow("ack_timeout_s", "Wait for a device to confirm a write", "Longer is more forgiving on a slow network.", { unit: "seconds", width: 80, int: false, min: 0.5 }),
        textRow("hub_ip", "This PC's address for devices", "Leave empty to detect the Tailscale address automatically.", "100.x.x.x", 180),
        selRow("log_level", "Log detail", "Debug shows every routing step and grows the log fast.", [["DEBUG", "Debug"], ["INFO", "Info"], ["WARNING", "Warnings"], ["ERROR", "Errors only"]]),
        keyRow));
    refresh(); deps();
    return page;
  }

  // ------------------------------------------------------------------- flyout
  function flyoutScreen() {
    const st = S.status;
    const page = h("div", { class: "page" });
    if (!st) { add(page, h("div", { class: "skeleton" })); return page; }
    const sm = summary();
    const nodes = deviceNodes();
    const act = (iconName, label, fn) => h("button", { class: "qa", onclick: fn }, icon(iconName), label);
    add(page, 
      h("div", { class: "row", style: "gap:12px;padding:2px 2px 6px" }, h("div", { class: "ht " + sm.tone }),
        h("div", {}, h("b", { text: sm.title }), h("div", { class: "t3", style: "font-size:12px", text: (st.outbox_targets || []).length ? `${plural(st.outbox_targets.length, "clip", "clips")} waiting` : `Delivered ${st.stats.delivered} this session` }))),
      nodes.map(([n, i]) => deviceRow(n, i, { compact: true })),
      imageStrip(4),
      h("div", { style: "height:1px;background:var(--stroke);margin:6px 4px" }),
      act(st.paused ? "play" : "pause", st.paused ? "Resume sync" : "Pause all sync", (e) => pauseMenu(e.currentTarget)),
      act("send", "Send current clipboard to all", sendClipboard),
      act("folder", "Open screenshots folder", () => openFolder("screenshots")),
      h("div", { style: "height:1px;background:var(--stroke);margin:6px 4px" }),
      act("clip", "Open CopyClip", () => guard(() => api("POST", "/api/ui/open", { route: "overview" }))()),
      act("gear", "Settings", () => guard(() => api("POST", "/api/ui/open", { route: "settings" }))()),
      act("x", "Quit CopyClip", async () => { if (await confirmBox({ title: "Quit CopyClip?", body: "Clipboard sync stops until you start it again.", confirm: "Quit", danger: true })) guard(() => api("POST", "/api/quit", {}))(); }));
    return page;
  }

  // ------------------------------------------------------------------ routing
  const main = () => $("#main");
  function mount(node) {
    const m = main(), top = m.scrollTop;
    fill(m, node);
    m.scrollTop = top;
  }
  function refreshScreen() {
    if (!S.screen) return;
    if (S.screen.update) S.screen.update(); else if (S.screen.build) mount(S.screen.build());
  }
  function buildNav() {
    const nav = $("#nav");
    const link = (id, label, ic) => h("a", { class: "ni", href: "#/" + id, "aria-current": S.route === id ? "page" : null, title: label },
      icon(ic), h("span", { class: "lbl", text: label }),
      id === "activity" && S.status && waitingCount() ? h("span", { class: "badge", title: `${waitingCount()} clip(s) waiting for an offline device`, text: String(waitingCount()) }) : null);
    const sm = S.status ? summary() : null;
    const brand = h("div", { class: "brand" }, h("img", { src: "/icon.png", alt: "", width: "30", height: "30" }),
      h("div", {}, h("b", { text: "CopyClip" }), h("span", { text: S.status ? `Version ${S.status.version}` : "" })));
    fill(nav, brand, NAV.map(([id, l, ic]) => link(id, l, ic)), link("settings", "Settings", "gear"), h("div", { class: "sp" }),
      sm && h("div", { class: "ns" }, h("span", { class: "pl " + sm.tone, style: "align-self:flex-start", text: sm.label }),
        h("span", { style: "margin-top:6px", text: "Tray service running" }), h("span", { class: "t3", text: S.status.uptime_s ? `Up ${duration(S.status.uptime_s)}` : "" })));
  }
  async function loadConfig() { try { S.config = await api("GET", "/api/config"); } catch (e) { toast(e.message, "er"); } }
  function settingsRoute() { if (S.route === "settings") mount(settingsScreen()); }

  async function route() {
    closeMenu();
    const name = (location.hash.replace(/^#\/?/, "") || "overview").split("?")[0];
    S.route = ["overview", "devices", "activity", "logs", "settings", "flyout"].includes(name) ? name : "overview";
    document.body.classList.toggle("flyout", S.route === "flyout");
    document.title = S.route === "flyout" ? "CopyClip quick menu" : "CopyClip";
    buildNav();
    S.screen = {};
    if (["devices", "logs", "settings"].includes(S.route) && !S.config) await loadConfig();
    if (S.route === "settings") { await loadConfig(); }
    const builders = { overview: overviewScreen, devices: devicesScreen, activity: activityScreen, logs: logsScreen, settings: settingsScreen, flyout: flyoutScreen };
    S.screen.build = builders[S.route];
    S.eventsKey = ""; S.logsKey = "";
    const node = S.screen.build();   // may set S.screen.update (activity/logs)
    mount(node);
    main().scrollTop = 0;   // a new screen starts at the top, it must not inherit the previous screen's scroll offset
    if (S.screen.after) S.screen.after();
    if (S.route === "activity") pollActivity(true);
    if (S.route === "logs") pollLogs(true);
    if (S.route === "overview" || S.route === "flyout") pollActivity(true);
    const m = main();
    m.setAttribute("data-anim", "");
    clearTimeout(route.animTimer);
    route.animTimer = setTimeout(() => m.removeAttribute("data-anim"), 700);
    m.scrollTop = 0;
  }

  // ------------------------------------------------------------------ polling
  // If the tray this window belongs to is gone (restarted, quit, crashed), the window can never
  // reconnect: the next tray has a new port and key. Leaving it open would make "click the tray
  // icon" focus a dead window, so it closes itself after a grace period.
  const ORPHAN_AFTER_MS = 25000;
  let offlineSince = 0;
  function setConn(ok) {
    S.online = ok;
    // Wait a moment before showing it, so a slow first poll at startup does not flash a warning.
    clearTimeout(setConn.t);
    if (ok) $("#conn").hidden = true; else setConn.t = setTimeout(() => { if (!S.online) $("#conn").hidden = false; }, 1500);
    if (ok) { offlineSince = 0; return; }
    offlineSince = offlineSince || Date.now();
    if (Date.now() - offlineSince > ORPHAN_AFTER_MS) {
      document.body.replaceChildren(h("p", { style: "padding:32px;font-size:15px", text: "CopyClip was restarted or closed. This window is no longer connected, so it is closing." }));
      setTimeout(() => window.close(), 1200);
    }
  }
  let failures = 0;
  async function pollStatus(now = false) {
    try {
      const s = await api("GET", "/api/status");
      failures = 0; setConn(true);
      // A short ADB reconnect blip must not flip the whole app to "Degraded": a device that was up
      // within the last 20 s still counts as up. A real outage shows once the grace runs out.
      S.upAt = S.upAt || {};
      for (const [n, i] of Object.entries(s.nodes || {})) {
        if (i.available) S.upAt[n] = Date.now();
        else if (S.upAt[n] && Date.now() - S.upAt[n] < 20000) { i.available = true; i.grace = true; }
      }
      // Relative times ("12 s ago") only need a refresh on the screens that show them.
      const bucket = 0;
      const key = JSON.stringify(s, (k, v) => (k === "server_time" || k === "uptime_s" ? undefined : v)) + bucket;
      const changed = key !== S.statusKey;
      S.status = s; S.statusKey = key;
      if (s.nav) {   // the tray asked this window to show another screen
        if (S.navSeen !== undefined && s.nav.id !== S.navSeen && s.nav.route) location.hash = "#/" + s.nav.route;
        S.navSeen = s.nav.id;
      }
      if (changed) {
        buildNav();
        if (!S.screen || !S.screen.build) return;
        if (["overview", "flyout"].includes(S.route) || (S.route === "devices" && S.config)) mount(S.screen.build());
      }
    } catch (e) {
      failures += 1; if (failures >= 2) setConn(false);
    } finally {
      if (!now) setTimeout(pollStatus, document.hidden ? 5000 : 1500);
    }
  }
  async function pollActivity(force = false) {
    if (!["overview", "activity", "flyout"].includes(S.route)) return;
    try {
      const q = new URLSearchParams({ limit: S.route === "activity" ? "200" : S.route === "flyout" ? "20" : "6" });
      if (S.route === "activity") {
        if (S.actFilter === "sensitive") q.set("kind", "sensitive");
        else if (["text", "image", "camera"].includes(S.actFilter)) q.set("kind", S.actFilter);
        else if (S.actFilter === "skipped") q.set("status", "skipped");
        if (S.actQuery) q.set("q", S.actQuery);
      }
      const r = await api("GET", "/api/activity?" + q);
      const key = JSON.stringify(r.events.map((e) => [e.id, e.status, e.has_image]));
      if (force || key !== S.eventsKey) {
        S.eventsKey = key; S.events = r.events; S.actTotal = r.total;
        if (S.route === "activity" && S.screen.update) S.screen.update();
        else if (S.route === "overview" && S.screen.build) mount(S.screen.build());
      }
    } catch (_) { /* status poll reports connectivity */ }
  }
  async function pollLogs(force = false) {
    if (S.route !== "logs") return;
    try {
      const q = new URLSearchParams({ level: S.logLevel, limit: "400" });
      if (S.logDevice) q.set("device", S.logDevice);
      if (S.logQuery) q.set("q", S.logQuery);
      const r = await api("GET", "/api/logs?" + q);
      const key = JSON.stringify([r.size, r.matched, r.total_lines]);
      if (force || key !== S.logsKey) { S.logsKey = key; S.logs = r; if (S.screen.update) S.screen.update(); }
    } catch (_) { /* ignore */ }
  }
  function applyTheme(v) {
    if (v === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", v);
    try { localStorage.setItem("cc_theme", v); } catch (_) { /* storage blocked */ }
  }

  // --------------------------------------------------------------------- boot
  async function boot() {
    try { const t = localStorage.getItem("cc_theme"); if (t && t !== "system") applyTheme(t); } catch (_) { /* storage blocked */ }
    if (location.search.includes("t=")) history.replaceState(null, "", location.pathname + location.hash);  // drop the token from the address
    $("#app").hidden = false;
    window.addEventListener("hashchange", route);
    await pollStatus(true);
    if (!S.status) setConn(false);
    await route();
    setTimeout(pollStatus, 1500);
    setInterval(() => { if (!document.hidden) { pollActivity(); pollLogs(); } }, 2000);
    const ping = () => api("POST", "/api/ping", {}).catch(() => {});
    ping(); setInterval(ping, 5000);
  }
  boot();
})();
