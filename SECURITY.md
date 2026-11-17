# Security and data privacy

DAX lineage explorer is built so that a model never has to leave the machine
it is opened on. This page says what it reads, where that happens, what stops
it from going anywhere else, and how to check all of that yourself.

## What it reads, and where

| You open | What is read | Where |
|---|---|---|
| INFO.* export (.xlsx, .csv, .json) | the rows in it | in your browser tab |
| .pbix | the report layout, and the model's *definitions*: tables, columns, measures with their DAX, relationships. Decompression stops at the model's metadata, so its data is never unpacked; Power Query code and connection details sit in that metadata but are not read, shown or exported | in your browser tab |
| .pbit, PBIP folder, model.bim | the same definitions, already as JSON | in your browser tab |

Files are never uploaded. There is no backend that could receive them: the
public site is static files on GitHub Pages, `daxlin serve` refuses anything
but `GET /`, and the offline page needs no server at all.

Nothing is stored. The page uses no cookies, local storage or IndexedDB;
closing the tab forgets everything. CSV exports go where your browser saves
downloads.

## Choosing how to run it

| | Network involved | Use it for |
|---|---|---|
| **Offline file** (`dax-lineage-explorer.html`) | none: open it from disk | sensitive models, air-gapped machines |
| **`daxlin serve`** | `127.0.0.1` only | the same, when a browser policy blocks pages opened from disk |
| **Docker / internal hosting** | your network | a team on your infrastructure |
| **Public site** | GitHub serves the page; your files stay in the tab | trying it out |

Even on the public site your files stay in the tab. Running locally also
takes GitHub out of the picture: it never learns that you used the tool.

## What enforces it

- **Content-Security-Policy on every page.** The offline page and the pages
  `daxlin build` writes allow inline code and nothing else (`connect-src
  'none'`, `default-src 'none'`); the site allows only its own files. The
  browser itself refuses any request to another host (fetch, XHR, images,
  scripts, WebSockets, beacons), whatever any code on the page tries.
- **No third-party code.** No CDN, no web fonts, no analytics, no npm
  packages in the page. The readers for .xlsx, zip, XPress9 and SQLite are
  part of the repository (`docs/js/`, about 3,000 lines). The XPress9
  decoder is a port of Microsoft's MIT-licensed library
  ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).
- **No dependencies in the CLI.** `daxlin` uses only the Python standard
  library.
- **`daxlin serve` hardening.** Binds to 127.0.0.1 unless told otherwise;
  answers only `GET`/`HEAD` for `/`; refuses requests under any host name it
  was not given (`--allow-host`), which defeats DNS rebinding; sends the
  policy above plus `frame-ancestors 'none'`, `X-Frame-Options: DENY`,
  `nosniff`, `no-referrer` and `no-store`.
- **Docker image** runs as an unprivileged user and serves one static page.

## Checking it yourself

1. **Watch the network.** Open the browser's developer tools, Network tab,
   then use the page with your own file. The offline page shows one request:
   the file itself.
2. **Run the audit.** `scripts/privacy_audit.js` drives a real browser
   through the sample, your file, the usage list and a CSV export, records
   every request, then tries to send data out by every route a page has. It
   fails if anything gets past the policy:

   ```
   npm install --no-save playwright && npx playwright install chromium
   node scripts/privacy_audit.js offline "path/to/your.pbix"
   ```

3. **Read the code.** Everything the page runs is in `docs/js/`. The offline
   file is those same files inlined by `scripts/build_offline.py`, and
   `tests/test_local.py` fails if it is ever out of step with them.

## Limits worth knowing

- Browser extensions can read any page you open. For very sensitive models,
  use a browser profile without extensions.
- The CSV exports contain measure names, DAX and where each is used. Treat
  them like the model.
- `daxlin serve --host 0.0.0.0` (and the Docker image) let other machines
  load the page. Files they open still stay in their own browsers, but put
  it behind your usual access controls.

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab,
**Report a vulnerability**. Do not open a public issue for it.
