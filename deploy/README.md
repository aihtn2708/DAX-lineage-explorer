# Running the workspace for a team

One shared workspace instead of one per PC: a Postgres database, sign-in, roles, an audit
log and backups. Everything still runs on your own servers.

```
            Windows sign-in                     reads .pbix, read-only
  browsers ───────────────► IIS ──► workspace ◄──────────────────────── synced SharePoint folder
  companion ──────────────► (or a proxy)  │
                                          ▼
                                     Postgres ──► nightly backup file
```

## 1. The database

Any Postgres 13 or later on your network. Create an empty database and a user for the workspace:

```sql
CREATE USER workspace WITH PASSWORD '...';
CREATE DATABASE powerbi OWNER workspace;
```

The workspace creates its own tables the first time it connects. Several teams can share one
database, each in its own schema: `postgresql://workspace@db/powerbi?schema=finance`.

**Moving a pilot over.** A team that started on one PC keeps everything (versions, notes, reviews,
rules, changes):

```bash
daxlin workspace --db pbi-workspace.db backup --out pilot.jsonl.gz
daxlin workspace --db postgresql://workspace@db.contoso.local/powerbi restore pilot.jsonl.gz
```

## 2. The server, and how people sign in

Pick one.

**A. Windows server with IIS: Windows sign-in, nobody types a password.** IIS signs people in and
starts the workspace through HttpPlatformHandler, which passes each request's Windows token to it.

1. Install IIS with *Windows Authentication*, then [HttpPlatformHandler](https://www.iis.net/downloads/microsoft/httpplatformhandler).
2. Install Python 3.9+ for all users, then from this repository: `pip install ".[postgres]"`.
3. Create a site (say `bi-workspace.contoso.local`) whose folder holds [`iis/web.config`](iis/web.config);
   edit its marked values. Allow the site's app pool identity to write the log folder.
4. In IIS Manager, unlock and set the site's authentication: Windows on, Anonymous off
   (web.config asks for this; the server's configuration must allow it).
5. Give the first admin their role, from the server:
   `daxlin workspace --db postgresql://... users add CONTOSO\linh --role admin`.
   Everyone else gets an account the first time they open the site, as a viewer
   (`--default-role` changes that); an admin raises their role on the Admin page.

**B. Linux server with Docker: workspace passwords.** [`docker/docker-compose.yml`](docker/docker-compose.yml)
runs Postgres and the workspace, with `--auth local`. Put your usual HTTPS reverse proxy in front.

**C. A proxy that already signs people in** (Apache `mod_auth_gssapi`, nginx with Kerberos, an SSO
gateway) and sets a header with the user name: `serve --auth windows --user-header X-Remote-User
--trusted-proxy <proxy address>`. The header is believed only from that address, so make sure the
workspace's port is reachable from nowhere else.

The server refuses to listen beyond 127.0.0.1 without one of these, unless it is `--read-only`.

## 3. Roles

| Role | Can |
|---|---|
| viewer | read everything |
| author | notes, owners, comments; propose changes; report a change as applied (the companion) |
| reviewer | approve or request changes on versions; approve or reject changes; accept exceptions; edit the rules |
| admin | accounts and roles; start scans; read the audit log |

Nobody approves their own change (`--allow-self-approval` turns that off for very small teams).
Names on notes, reviews and changes come from the sign-in, not from what someone types.

## 4. Scanning

The scan needs to read the .pbix files, so it runs where the synced SharePoint folder is:

- on the server itself, if it syncs the library: `serve --scan-every 60`, or
- on any PC that syncs it, writing to the shared database:
  `daxlin workspace --db postgresql://workspace@db/powerbi scan`, run by Task Scheduler.

## 5. The Desktop companion

With IIS (A), the companion signs in with the person's own Windows account: nothing to set up.
Otherwise each person creates a token on **My account** and stores it once, without admin rights:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-companion.ps1 -Workspace https://bi-workspace.contoso.local -Token pbiw_...
```

## 6. Backups and the audit log

```bash
daxlin workspace --db postgresql://... backup --out D:\backups\workspace-%DATE%.jsonl.gz   # nightly
daxlin workspace --db postgresql://... audit --limit 100
```

The backup is one compressed file that restores into SQLite or Postgres. Your usual `pg_dump` works
too. The audit log records every change, sign-in and refused attempt, with who, when and from where.
