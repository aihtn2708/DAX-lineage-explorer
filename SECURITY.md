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
| **`run-local.bat` / `run-local.sh`** (`python -m dax_lineage serve`) | `127.0.0.1` only | the same, when a browser policy blocks pages opened from disk |
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

## The team workspace

`daxlin workspace` is different from the explorer in one way that matters: it
**keeps** what it reads. Each scan stores measure names, DAX, column names,
visual titles and the team's notes in a SQLite file (`pbi-workspace.db` unless
`--db` says otherwise). Treat that file like the models themselves: keep it on
a disk only the team can read, include it in your backups, and keep it out of
git (`.gitignore` already does).

What protects it:

- Folders are scanned read-only; no `.pbix` is ever written.
- `daxlin workspace serve` listens on 127.0.0.1 and refuses any other `Host`
  name unless you allow it, which defeats DNS rebinding.
- Changes (notes, decisions, starting a scan) are accepted only as `POST` with a
  JSON body, and an `Origin` header from another site is refused, so a web page
  elsewhere cannot make your browser change the workspace.
- The page loads its script and styles only from the workspace server and may
  call only it (`script-src 'self'; connect-src 'self'`). Everything read from a
  `.pbix` is shown as text, never as HTML.
- Beyond 127.0.0.1 it needs sign-in (`--auth iis`, `local` or `windows`) or
  `--read-only`, and refuses to start otherwise.
- **Sign-in.** With `--auth iis`, IIS signs people in with Windows and hands the
  workspace each request's Windows token, which it turns into DOMAIN\user and
  closes. With `--auth local`, passwords are stored as PBKDF2-SHA256 hashes
  (310,000 rounds); the session cookie is HttpOnly and SameSite=Strict (add
  `--secure-cookies` behind HTTPS); five failed sign-ins lock a name or an
  address out for 15 minutes. With `--auth windows`, the user header is
  believed only from `--trusted-proxy` addresses.
- **Roles** are checked by the server on every change, not by the page. Names
  recorded with notes, reviews and changes come from the sign-in. Nobody
  approves their own change unless `--allow-self-approval` is set.
- **API tokens** (for the companion and scripts) are stored as SHA-256 hashes,
  shown once, and can be revoked by their owner; `last used` shows when.
- **The audit log** records every change, sign-in, failed sign-in and refused
  attempt, with who, when and from which address. Passwords and tokens never
  appear in it.
- **Backups** (`daxlin workspace backup`) hold password and token hashes, not
  passwords or tokens, but they hold everything else: store them like the
  database.

The Desktop companion (`daxlin workspace companion`) is the only part that
changes a model, and only the one open in Power BI Desktop on that PC:

- it makes only edits from a change that someone approved in the workspace,
  each checked against the model's latest version when it was proposed;
- it shows every edit and changes nothing until the user types `APPLY`;
- it skips an edit whose object no longer has the value the change expects;
- it saves the model's definition to `%LOCALAPPDATA%\PowerBIWorkspace\backups`
  first, and if Desktop refuses any edit it undoes them all;
- nothing is saved to the .pbix until the user saves it in Desktop.

It calls the workspace's API with the person's own Windows sign-in (behind
IIS) or their personal token, so the server applies the same roles to it.

## Reporting a vulnerability

Please report it privately through GitHub: the repository's **Security** tab,
**Report a vulnerability**. Do not open a public issue for it.
