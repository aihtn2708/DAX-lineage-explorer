/*
 * The page around the viewer: pick files, read them, build the graph, show it.
 * Everything happens in the browser; no file leaves the machine.
 */
(function () {
  "use strict";

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const SAMPLES = {
    "sample": { file: "samples/info_sample.xlsx", title: "Sample sales model" },
    "sample-problems": { file: "samples/info_sample_with_problems.xlsx", title: "Sample model with problems" }
  };

  function showLanding(message) {
    $("landing").hidden = false;
    $("app").hidden = true;
    $("stats").hidden = true;
    $("openAnother").hidden = true;
    $("title").textContent = "DAX lineage explorer";
    $("subtitle").textContent = "See what every measure reads, and what reads it";
    $("subtitle").title = "";
    const box = $("error");
    if (message) { box.innerHTML = message; box.hidden = false; } else { box.hidden = true; }
  }

  function showApp() {
    $("landing").hidden = true;
    $("app").hidden = false;
    $("stats").hidden = false;
    $("openAnother").hidden = false;
  }

  /* -------- reading -------- */

  async function sourcesFromFile(name, buffer) {
    const lower = name.toLowerCase();
    if (/\.(xlsx|xlsm)$/.test(lower)) {
      const sheets = await XlsxReader.read(buffer);
      const nonEmpty = sheets.filter(s => s.matrix.some(r => r && r.some(c => String(c).trim() !== "")));
      return nonEmpty.map(s => ({
        label: nonEmpty.length === 1 ? `${s.name} ${name}` : s.name,
        display: `${name} › ${s.name}`,
        rows: DaxLineage.rowsFromMatrix(s.matrix)
      }));
    }
    if (/\.xls$/.test(lower)) {
      throw new Error("Old .xls workbooks are not supported. Save it as .xlsx (File → Save As → Excel Workbook) or as CSV.");
    }
    const textContent = new TextDecoder("utf-8").decode(buffer);
    if (/\.json$/.test(lower) || /^\s*[[{]/.test(textContent)) {
      return [{ label: name, display: name, rows: DaxLineage.rowsFromJson(JSON.parse(textContent)) }];
    }
    return [{ label: name, display: name, rows: DaxLineage.parseDelimited(textContent) }];
  }

  async function load(items, title) {
    const sources = [];
    for (const item of items) {
      try {
        sources.push(...await sourcesFromFile(item.name, await item.read()));
      } catch (err) {
        throw new Error(`<b>${esc(item.name)}</b>: ${esc(err.message)}`);
      }
    }
    let graph;
    try {
      graph = DaxLineage.lineageFromSources(sources);
    } catch (err) {
      throw new Error(esc(err.message));
    }
    const read = graph.sources.map((s, i) =>
      `${sources[i].display}${s.kind ? ` → ${s.kind} (${s.rows} rows)` : " → not recognised, skipped"}`).join("\n");
    const s = graph.stats;
    const used = graph.sources.filter(x => x.kind).length;
    const skipped = graph.sources.length - used;
    const files = items.length === 1 ? items[0].name : `${items.length} files`;
    const subtitle = `${s.measures} measures · ${s.columns} columns · ${s.edges} dependencies · ` +
      `${files}, ${used} ${used === 1 ? "query" : "queries"} read` + (skipped ? `, ${skipped} skipped` : "");
    $("title").textContent = title || items.map(i => i.name).join(", ");
    document.title = `${$("title").textContent} — DAX lineage explorer`;
    showApp();
    LineageViewer.mount(graph, { subtitle });
    $("subtitle").title = read;
    if (!graph.sources.some(x => x.kind === "columns")) {
      flashNote("No INFO.COLUMNS sheet found: bare [references] that are columns will show as unresolved.");
    }
  }

  function flashNote(message) {
    const hint = $("hint");
    hint.textContent = message;
    setTimeout(() => { hint.textContent = "Click a node to trace what it reads and what reads it"; }, 6000);
  }

  async function run(items, title) {
    $("drop").classList.add("busy");
    try {
      await load(items, title);
    } catch (err) {
      showLanding(`<b>Could not read that.</b> ${err.message}`);
    } finally {
      $("drop").classList.remove("busy");
    }
  }

  const fromFiles = files => [...files].map(f => ({ name: f.name, read: () => f.arrayBuffer() }));

  async function loadSample(key) {
    const sample = SAMPLES[key];
    if (!sample) return;
    await run([{
      name: sample.file.split("/").pop(),
      read: async () => {
        const res = await fetch(sample.file);
        if (!res.ok) throw new Error(`could not fetch the sample (${res.status})`);
        return res.arrayBuffer();
      }
    }], sample.title);
  }

  /* -------- wiring -------- */

  $("files").addEventListener("change", e => {
    if (e.target.files.length) run(fromFiles(e.target.files));
    e.target.value = "";
  });

  const drop = $("drop");
  ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, e => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach(type => drop.addEventListener(type, () => drop.classList.remove("over")));
  // a file dropped anywhere on the page loads, even with the graph open
  addEventListener("dragover", e => e.preventDefault());
  addEventListener("drop", e => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files.length) run(fromFiles(e.dataTransfer.files));
  });

  for (const button of document.querySelectorAll("[data-sample]")) {
    button.addEventListener("click", () => {
      const key = button.dataset.sample;
      if (location.hash === `#${key}`) loadSample(key);
      else location.hash = key;
    });
  }

  $("pasteGo").addEventListener("click", () => {
    const value = $("pasteBox").value;
    if (!value.trim()) return;
    const bytes = new TextEncoder().encode(value);
    run([{ name: "pasted.tsv", read: async () => bytes.buffer }], "Pasted query result");
  });

  $("openAnother").addEventListener("click", () => {
    if (location.hash) history.pushState("", document.title, location.pathname + location.search);
    showLanding();
  });

  function route() {
    const key = location.hash.replace(/^#/, "");
    if (SAMPLES[key]) loadSample(key);
    else if (!key) showLanding();
  }
  addEventListener("hashchange", route);
  route();
})();
