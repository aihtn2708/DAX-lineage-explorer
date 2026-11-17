/*
 * The page around the viewer: pick files, read them, build the graph, show it.
 * Everything happens in the browser; no file leaves the machine.
 *
 * Two kinds of input, in any combination:
 *   the model   INFO.* exports (.xlsx/.csv/.tsv/.json), or a .pbit / model.bim
 *   the report  .pbix, .pbit, a PBIP folder (or its zip), a report layout .json
 */
(function () {
  "use strict";

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const SAMPLES = {
    "sample": { title: "Sample sales model",
      files: ["samples/info_sample.xlsx", "samples/sample_report.json"] },
    "sample-problems": { title: "Sample model with problems",
      files: ["samples/info_sample_with_problems.xlsx", "samples/sample_report_with_problems.json"] }
  };
  const REPORT_FILE = /\.(pbix|pbit|zip|bim)$/i;
  let current = { items: [], title: "" };

  function showLanding(message) {
    $("landing").hidden = false;
    $("app").hidden = true;
    $("stats").hidden = true;
    $("openAnother").hidden = true;
    $("title").textContent = "DAX lineage explorer";
    $("subtitle").textContent = "See what every measure reads, what reads it, and which visuals use it";
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

  /* -------- reading the model exports -------- */

  async function sourcesFromFile(name, blob) {
    const lower = name.toLowerCase();
    if (/\.(xlsx|xlsm)$/.test(lower)) {
      const sheets = await XlsxReader.read(blob);
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
    const textContent = new TextDecoder("utf-8").decode(await blob.arrayBuffer());
    if (/\.json$/.test(lower) || /^\s*[[{]/.test(textContent)) {
      return [{ label: name, display: name, rows: DaxLineage.rowsFromJson(JSON.parse(textContent)) }];
    }
    return [{ label: name, display: name, rows: DaxLineage.parseDelimited(textContent) }];
  }

  /* -------- one load: any mix of model exports and reports -------- */

  async function load(items, title) {
    const info = [], usages = [], found = [], notes = [];
    let jsonModel = null;
    const absorb = r => {
      usages.push(...r.usages);
      found.push(...r.found);
      if (r.note) notes.push(r.note);
      if (r.model && !jsonModel) jsonModel = r.model;
    };
    for (const item of items) {
      try {
        if (item.folder) {
          const r = await ReportReader.readFolder(item.files);
          if (!r.usages.length && !r.model) throw new Error("no Power BI report (.Report folder) or model.bim found in this folder");
          absorb(r);
          continue;
        }
        const blob = await item.blob();
        if (REPORT_FILE.test(item.name) || item.name.toLowerCase() === "layout") {
          const r = await ReportReader.readFile(item.name, blob);
          if (!r || (!r.usages.length && !r.model)) throw new Error("no Power BI report or model found inside");
          absorb(r);
          continue;
        }
        if (/\.json$/i.test(item.name)) {
          const r = await ReportReader.readFile(item.name, blob);
          if (r) { absorb(r); continue; }
        }
        info.push(...await sourcesFromFile(item.name, blob));
      } catch (err) {
        throw new Error(`<b>${esc(item.name)}</b>: ${esc(err.message)}`);
      }
    }

    // the INFO export wins; a .pbit or model.bim stands in when there is none
    const hasInfoMeasures = info.some(s => DaxLineage.detectKind(s.rows, s.label) === "measures");
    const sources = hasInfoMeasures || !jsonModel ? info : jsonModel;
    const displays = hasInfoMeasures || !jsonModel ? info.map(s => s.display) : jsonModel.map(s => s.label);
    let graph;
    try {
      graph = DaxLineage.lineageFromSources(sources, { usages });
    } catch (err) {
      if (!hasInfoMeasures && !jsonModel && usages.length) {
        throw new Error(`Found the report (${esc(found.join(", "))}) but no model to go with it. ` +
          (notes[0] ? esc(notes[0]) : "Add the INFO.MEASURES export, or use a .pbit file, which carries the model."));
      }
      throw new Error(esc(err.message));
    }

    const s = graph.stats, r = s.report;
    const read = graph.sources.map((x, i) =>
      `${displays[i] || x.label}${x.kind ? ` → ${x.kind} (${x.rows} rows)` : " → not recognised, skipped"}`)
      .concat(found.map(f => `report: ${f}`)).join("\n");
    const files = items.length === 1 ? items[0].name : `${items.length} files`;
    const subtitle = `${s.measures} measures · ${s.columns} columns · ${s.edges} dependencies · ${files}` +
      (r ? ` · ${r.visuals} visuals on ${r.pages} page${r.pages === 1 ? "" : "s"}` : "");
    $("title").textContent = title || items.map(i => i.name).join(", ");
    document.title = `${$("title").textContent} — DAX lineage explorer`;
    showApp();
    LineageViewer.mount(graph, { subtitle });
    $("subtitle").title = read;
    current = { items, title: $("title").textContent };
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
    if (!items.length) return;
    $("drop").classList.add("busy");
    try {
      await load(items, title);
    } catch (err) {
      showLanding(`<b>Could not read that.</b> ${err.message}`);
    } finally {
      $("drop").classList.remove("busy");
    }
  }

  /* -------- turning inputs into items -------- */

  const fromFiles = files => [...files].map(f => ({ name: f.name, blob: async () => f }));

  function fromFolderInput(files) {
    const list = [...files].filter(f => f.webkitRelativePath);
    if (!list.length) return fromFiles(files);
    const top = list[0].webkitRelativePath.split("/")[0];
    return [{ name: top, folder: true, files: list.map(f => ({ path: f.webkitRelativePath, blob: f })) }];
  }

  // a folder dropped on the page arrives as a FileSystemDirectoryEntry
  function readEntry(entry, prefix) {
    return new Promise(resolve => {
      if (entry.isFile) {
        entry.file(file => resolve([{ path: `${prefix}${entry.name}`, blob: file }]), () => resolve([]));
        return;
      }
      const reader = entry.createReader(), all = [];
      const batch = () => reader.readEntries(async entries => {
        if (!entries.length) {
          const nested = await Promise.all(all.map(e => readEntry(e, `${prefix}${entry.name}/`)));
          resolve(nested.flat());
          return;
        }
        all.push(...entries);
        batch();
      }, () => resolve([]));
      batch();
    });
  }

  async function fromDrop(dataTransfer) {
    const items = [];
    const entries = [...(dataTransfer.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (!entries.length) return fromFiles(dataTransfer.files);
    for (const entry of entries) {
      if (entry.isDirectory) items.push({ name: entry.name, folder: true, files: await readEntry(entry, "") });
      else {
        const file = await new Promise(res => entry.file(res, () => res(null)));
        if (file) items.push({ name: file.name, blob: async () => file });
      }
    }
    return items;
  }

  async function loadSample(key) {
    const sample = SAMPLES[key];
    if (!sample) return;
    await run(sample.files.map(file => ({
      name: file.split("/").pop(),
      blob: async () => {
        const res = await fetch(file);
        if (!res.ok) throw new Error(`could not fetch the sample (${res.status})`);
        return res.blob();
      }
    })), sample.title);
  }

  /* -------- wiring -------- */

  $("files").addEventListener("change", e => {
    if (e.target.files.length) run(fromFiles(e.target.files));
    e.target.value = "";
  });
  $("folder").addEventListener("change", e => {
    if (e.target.files.length) run(fromFolderInput(e.target.files));
    e.target.value = "";
  });
  $("addReportFile").addEventListener("change", e => {
    if (e.target.files.length) run([...current.items, ...fromFiles(e.target.files)], current.title);
    e.target.value = "";
  });
  window.onLineageAddReport = () => $("addReportFile").click();

  const drop = $("drop");
  ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, e => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach(type => drop.addEventListener(type, () => drop.classList.remove("over")));
  // anything dropped on the page loads; with the graph open, a dropped report is added to it
  addEventListener("dragover", e => e.preventDefault());
  addEventListener("drop", async e => {
    e.preventDefault();
    if (!e.dataTransfer) return;
    const items = await fromDrop(e.dataTransfer);
    if (!items.length) return;
    const onlyReports = items.every(i => i.folder || REPORT_FILE.test(i.name));
    if (!$("app").hidden && onlyReports && current.items.length) run([...current.items, ...items], current.title);
    else run(items);
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
    run([{ name: "pasted.tsv", blob: async () => new Blob([value]) }], "Pasted query result");
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
