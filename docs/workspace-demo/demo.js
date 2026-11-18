// Workspace demo: the real workspace page, with this script standing in for its server.
// Pages come from answers recorded from a workspace of made-up models (data.json). The main
// actions - comments, reviews, change decisions, notes - are acted out here in the browser and
// kept for this tab only (sessionStorage), so nothing is sent anywhere.
"use strict";

(function () {
  const realFetch = window.fetch.bind(window);
  const LOG = "workspace-demo.actions";
  let recorded = null;
  let state = null;
  let nextId = 100000;

  const ready = realFetch("data.json")
    .then((r) => r.json())
    .then((data) => {
      recorded = data;
      state = clone(recorded);
      for (const action of actions()) {
        try { act(action.path, action.body, action.at); } catch (err) { /* an old action that no longer fits */ }
      }
      tellDemo();
    });

  function clone(x) { return JSON.parse(JSON.stringify(x)); }
  function actions() { try { return JSON.parse(sessionStorage.getItem(LOG) || "[]"); } catch { return []; } }
  function remember(path, body, at) {
    try { sessionStorage.setItem(LOG, JSON.stringify([...actions(), { path, body, at }])); } catch { /* storage off */ }
  }
  function answer(status, body) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }
  class Refused extends Error { constructor(status, message) { super(message); this.status = status; } }
  const A = () => state.answers;
  const every = (prefix, fn) => Object.keys(A()).filter((k) => k === prefix || k.startsWith(prefix + "?")).forEach((k) => fn(A()[k], k));

  // -- reading ---------------------------------------------------------------------------------

  function read(url) {
    const path = url.pathname + url.search;
    if (url.pathname === "/api/search") return search(url.searchParams.get("q") || "", url.searchParams.get("dax") === "1");
    if (path in A()) return A()[path];
    if (url.pathname === "/api/session") return A()["/api/session"];
    throw new Refused(404, "This page is not part of the demo's sample workspace.");
  }

  function search(q, inDax) {
    const query = q.trim().toLowerCase();
    if (!query) return [];
    return state.search.filter((r) => r.name.toLowerCase().includes(query)
      || `${r.table_name}[${r.name}]`.toLowerCase().includes(query)
      || (inDax && r.expression.toLowerCase().includes(query)))
      .map((r) => {
        const { expression, ...row } = r;
        return { ...row, dax_match: inDax && expression.toLowerCase().includes(query) && !r.name.toLowerCase().includes(query) };
      })
      .sort((a, b) => (b.name.toLowerCase() === query) - (a.name.toLowerCase() === query));
  }

  // -- acting ----------------------------------------------------------------------------------

  const HANDLERS = [
    [/^\/api\/versions\/(\d+)\/comments$/, versionComment],
    [/^\/api\/versions\/(\d+)\/review$/, review],
    [/^\/api\/changes\/(\d+)\/decision$/, changeDecision],
    [/^\/api\/changes\/(\d+)\/comments$/, changeComment],
    [/^\/api\/models\/(\d+)$/, aboutModel],
    [/^\/api\/annotations$/, annotate],
    [/^\/api\/scan$/, scan],
  ];

  function act(path, body, at) {
    for (const [pattern, fn] of HANDLERS) {
      const match = path.match(pattern);
      if (match) return fn(match[1], body || {}, at || new Date().toISOString());
    }
    throw new Refused(403, "The demo only acts out reviews, comments, change decisions and notes. "
      + "To try everything else, run the workspace on your own PC: see the README on GitHub.");
  }

  const who = (body) => String(body.by || "").trim() || "You (demo)";

  function versionComment(id, body, at) {
    const text = String(body.body || "").trim();
    if (!text) throw new Refused(400, "write a comment first");
    const comment = { id: nextId++, version_id: Number(id), object_key: body.object_key || "", kind: "comment",
      author: who(body), body: text, created_at: at };
    every(`/api/versions/${id}`, (v) => v.comments.push(comment));
    for (const [k, rows] of Object.entries(A())) {
      if (k.startsWith("/api/reviews?")) rows.filter((r) => String(r.id) === id).forEach((r) => { r.comments = (r.comments || 0) + 1; });
    }
    return comment;
  }

  function review(id, body, at) {
    const v = A()[`/api/versions/${id}`];
    if (!v) throw new Refused(404, "no such version");
    const status = String(body.status || "");
    if (!["pending", "approved", "changes_requested"].includes(status)) throw new Refused(400, "unknown review status");
    if (v.baseline || v.version.status === "error") throw new Refused(400, "this version has nothing to review: it is the first readable one, or unreadable");
    let note = String(body.note || "").trim();
    const policy = v.policy;
    if (status === "approved" && policy && !policy.ok) {
      const failing = policy.checks.filter((c) => !c.ok);
      const reason = String(body.override || "").trim();
      if (!reason) {
        throw new Refused(409, `this save breaks the ${policy.tier} policy: `
          + failing.map((c) => `${c.label} (${c.detail})`).join("; ") + ". To approve it anyway, give a reason.");
      }
      note = `Approved despite the ${policy.tier} policy (${failing.map((c) => c.label).join("; ")}): ${reason}` + (note ? `\n${note}` : "");
    }
    const by = who(body);
    every(`/api/versions/${id}`, (x) => {
      Object.assign(x.version, { review: status, reviewer: status === "pending" ? "" : by, decided_at: status === "pending" ? "" : at });
      x.comments.push({ id: nextId++, version_id: Number(id), object_key: "", kind: status, author: by, body: note, created_at: at });
    });
    const all = A()["/api/reviews?status=all"];
    all.filter((r) => String(r.id) === id).forEach((r) => Object.assign(r, { review: status, reviewer: status === "pending" ? "" : by, decided_at: status === "pending" ? "" : at }));
    for (const s of ["pending", "changes_requested", "approved"]) A()[`/api/reviews?status=${s}`] = all.filter((r) => r.review === s);
    const model = A()[`/api/models/${v.version.model_id}`];
    if (model) model.history.filter((x) => String(x.id) === id).forEach((x) => { x.review = status; });
    A()["/api/summary"].pending_reviews = A()["/api/reviews?status=pending"].length;
    return { status, reviewer: by, decided_at: at };
  }

  const OPEN = ["proposed", "approved", "applied"];
  const MOVES = { proposed: ["approved", "rejected", "cancelled"], approved: ["cancelled", "proposed"], rejected: ["proposed"] };

  function changeDecision(id, body, at) {
    const c = A()[`/api/changes/${id}`];
    if (!c) throw new Refused(404, "no such change");
    const status = String(body.status || "");
    if (!(MOVES[c.status] || []).includes(status)) throw new Refused(409, `a ${c.status} change cannot become ${status}`);
    const by = who(body);
    Object.assign(c, { status, decided_by: status === "proposed" ? "" : by, decided_at: status === "proposed" ? "" : at });
    c.events.push({ id: nextId++, request_id: Number(id), kind: status, author: by, body: String(body.note || "").trim(), created_at: at });
    const all = A()["/api/changes?status=all"];
    all.filter((r) => String(r.id) === id).forEach((r) => Object.assign(r, { status, decided_by: c.decided_by, decided_at: c.decided_at }));
    A()["/api/changes?status=open"] = all.filter((r) => OPEN.includes(r.status));
    A()["/api/changes?status=confirmed"] = all.filter((r) => r.status === "confirmed");
    A()["/api/summary"].open_changes = A()["/api/changes?status=open"].length;
    return c;
  }

  function changeComment(id, body, at) {
    const c = A()[`/api/changes/${id}`];
    const text = String(body.body || "").trim();
    if (!c) throw new Refused(404, "no such change");
    if (!text) throw new Refused(400, "write a comment first");
    const event = { id: nextId++, request_id: Number(id), kind: "comment", author: who(body), body: text, created_at: at };
    c.events.push(event);
    return event;
  }

  function aboutModel(id, body) {
    const fields = {};
    for (const k of ["owner", "team", "notes"]) if (body[k] !== undefined && body[k] !== null) fields[k] = String(body[k]).trim();
    if (A()[`/api/models/${id}`]) Object.assign(A()[`/api/models/${id}`], fields);
    A()["/api/models"].filter((m) => String(m.id) === id).forEach((m) => Object.assign(m, fields));
    return { ok: true };
  }

  function annotate(_, body, at) {
    const decision = String(body.decision || "");
    if (!["", "keep", "remove"].includes(decision)) throw new Refused(400, "decision must be keep, remove or none");
    const note = { owner: String(body.owner || "").trim(), note: String(body.note || "").trim(), decision,
      updated_at: at, updated_by: who(body) };
    let sample = null;
    for (const [k, o] of Object.entries(A())) {
      if (k.startsWith("/api/objects/") && o.model_id === body.model_id && o.object_key === body.object_key) {
        o.annotation = { ...note };
        if (o.is_current) sample = o;
      }
    }
    for (const which of ["remove", "keep"]) {
      const key = `/api/decisions?decision=${which}`;
      A()[key] = A()[key].filter((r) => !(r.model_id === body.model_id && r.object_key === body.object_key));
      if (decision === which && sample) {
        A()[key].push({ model_id: sample.model_id, object_key: sample.object_key, ...note, model_name: sample.model_name,
          object_id: sample.id, kind: sample.kind, table_name: sample.table_name, name: sample.name, usage_status: sample.usage_status });
      }
    }
    A()["/api/summary"].decisions = { keep: A()["/api/decisions?decision=keep"].length, remove: A()["/api/decisions?decision=remove"].length };
    return note;
  }

  function scan() {
    const folder = (A()["/api/folders"].folders[0] || {}).path || "the folder";
    A()["/api/scan"] = { running: false, log: [`Scanning ${folder}`, "4 files: 0 new versions, 4 unchanged, 0 failed",
      "(Demo: the sample files do not change, so a scan finds nothing new. On your PC it reads every .pbix saved since the last scan.)"] };
    return A()["/api/scan"];
  }

  // -- the stand-in for the server --------------------------------------------------------------

  window.fetch = async function (input, options) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) {
      return realFetch(input, options);
    }
    await ready;
    const method = ((options && options.method) || "GET").toUpperCase();
    try {
      if (method === "GET") return answer(200, clone(read(url)));
      const body = options && options.body ? JSON.parse(options.body) : {};
      const at = new Date().toISOString();
      const result = act(url.pathname, body, at);
      remember(url.pathname, body, at);
      return answer(200, clone(result));
    } catch (err) {
      if (!(err instanceof Refused)) throw err;
      if (err.status === 403) toast(err.message);
      return answer(err.status, { error: err.message });
    }
  };

  // links to the lineage graph go to the demo's recorded graphs
  function graphLinks(root) {
    root.querySelectorAll('a[href^="/explorer/version/"]').forEach((a) => {
      const url = new URL(a.getAttribute("href"), location.origin);
      const id = url.pathname.split("/").pop();
      const p = url.searchParams;
      const key = p.get("change") ? `c${p.get("change")}` : p.get("against") ? `v${id}-a${p.get("against")}` : `v${id}`;
      a.setAttribute("href", `graph.html?g=${encodeURIComponent(key)}${p.get("focus") ? `&focus=${encodeURIComponent(p.get("focus"))}` : ""}`);
    });
  }

  function toast(message) {
    const box = document.getElementById("demoToast");
    if (!box) return;
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { box.hidden = true; }, 7000);
  }

  function tellDemo() {
    const count = actions().length;
    const status = document.getElementById("demoActions");
    if (status) status.textContent = count ? `${count} change${count === 1 ? "" : "s"} you made, kept in this tab.` : "";
  }

  document.addEventListener("DOMContentLoaded", () => {
    new MutationObserver(() => { graphLinks(document); tellDemo(); }).observe(document.getElementById("view"), { childList: true, subtree: true });
    const reset = document.getElementById("demoReset");
    if (reset) {
      reset.addEventListener("click", () => {
        try { sessionStorage.removeItem(LOG); } catch { /* storage off */ }
        location.hash = "#/";
        location.reload();
      });
    }
  });
})();
