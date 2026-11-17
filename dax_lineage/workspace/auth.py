r"""Who is asking: accounts, roles, sessions, API tokens and the audit log.

Four ways to run the server:

    none     no sign-in; everyone is the local admin. Only on 127.0.0.1, or with --read-only.
    local    accounts with passwords kept in the workspace (PBKDF2-SHA256), signed in with a
             cookie that scripts on other sites cannot read or send (HttpOnly, SameSite=Strict).
    iis      Windows sign-in by IIS, which starts the workspace through HttpPlatformHandler with
             forwardWindowsAuthToken: each request carries the signed-in user's Windows token
             (X-IIS-WindowsAuthToken), which the workspace turns into DOMAIN\user and closes.
    windows  any proxy that signs people in (Apache or nginx with Kerberos, for instance) and
             passes the user name in a header. The header is believed only from the proxy's address.

In every mode, a script such as the Desktop companion can send a personal API token
(``Authorization: Bearer pbiw_...``) instead.

Roles, each including the ones before it:

    viewer    reads everything
    author    notes, owners, comments, proposes changes, reports a change as applied
    reviewer  approves versions and changes, accepts exceptions, edits the rules
    admin     accounts, roles, scans, the audit log

Passwords and tokens are stored only as hashes. Every change anyone makes is written to the
audit log with who, when and from where.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .store import Store, _rows, now

ROLES = ("viewer", "author", "reviewer", "admin")
MODES = ("none", "local", "iis", "windows")
IIS_TOKEN_HEADER = "X-IIS-WindowsAuthToken"
ITERATIONS = 310_000
LOCAL_ADMIN = {"id": None, "username": "local", "display_name": "", "role": "admin"}


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def name_of(account: Optional[dict]) -> str:
    if not account:
        return ""
    return account.get("display_name") or account.get("username") or ""


def normalise(username: str) -> str:
    """One spelling per person: lower case, no surrounding spaces."""
    username = (username or "").strip().lower()
    if not re.fullmatch(r"[^\s/:*?\"<>|]{1,100}", username or " "):
        raise ValueError("a user name is 1 to 100 characters with no spaces")
    return username


# -- passwords ----------------------------------------------------------------------------

def hash_password(password: str) -> str:
    if len(password or "") < 10:
        raise ValueError("a password needs at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


_DUMMY = f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(bytes(16)).decode()}${base64.b64encode(bytes(32)).decode()}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- accounts -------------------------------------------------------------------------------

def accounts(store: Store) -> list:
    with store.connect() as con:
        rows = _rows(con.execute(
            "SELECT a.id, a.username, a.display_name, a.role, a.disabled, a.created_at, a.last_seen_at, "
            "a.password_hash != '' AS has_password, "
            "(SELECT COUNT(*) FROM api_token t WHERE t.account_id = a.id) AS tokens FROM account a ORDER BY a.username"))
    for r in rows:
        r["has_password"], r["disabled"] = bool(r["has_password"]), bool(r["disabled"])
    return rows


def account(store: Store, username: str) -> Optional[dict]:
    with store.connect() as con:
        row = con.execute("SELECT * FROM account WHERE username = ?", (normalise(username),)).fetchone()
        return dict(row) if row else None


def add_account(store: Store, username: str, role: str = "viewer", display_name: str = "",
                password: Optional[str] = None) -> dict:
    username = normalise(username)
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    hashed = hash_password(password) if password else ""
    with store.connect() as con:
        if con.execute("SELECT 1 FROM account WHERE username = ?", (username,)).fetchone():
            raise ValueError(f"{username} already has an account")
        con.execute("INSERT INTO account (username, display_name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                    (username, display_name.strip()[:100], role, hashed, now()))
    return account(store, username)


def update_account(store: Store, username: str, role: Optional[str] = None, display_name: Optional[str] = None,
                   password: Optional[str] = None, disabled: Optional[bool] = None) -> dict:
    found = account(store, username)
    if found is None:
        raise KeyError(f"no such account: {username}")
    if role is not None and role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if found["role"] == "admin" and (role not in (None, "admin") or disabled):
        with store.connect() as con:
            admins = con.execute("SELECT COUNT(*) FROM account WHERE role = 'admin' AND disabled = 0").fetchone()[0]
        if admins <= 1:
            raise ValueError("this is the last admin: make someone else admin first")
    with store.connect() as con:
        if role is not None:
            con.execute("UPDATE account SET role = ? WHERE id = ?", (role, found["id"]))
        if display_name is not None:
            con.execute("UPDATE account SET display_name = ? WHERE id = ?", (display_name.strip()[:100], found["id"]))
        if password is not None:
            con.execute("UPDATE account SET password_hash = ? WHERE id = ?", (hash_password(password), found["id"]))
            con.execute("DELETE FROM login_session WHERE account_id = ?", (found["id"],))
        if disabled is not None:
            con.execute("UPDATE account SET disabled = ? WHERE id = ?", (int(bool(disabled)), found["id"]))
            if disabled:
                con.execute("DELETE FROM login_session WHERE account_id = ?", (found["id"],))
    return account(store, username)


def _active(row) -> Optional[dict]:
    if row is None or row["disabled"]:
        return None
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "role": row["role"]}


def _seen(store: Store, account_id: int) -> None:
    with store.connect() as con:
        con.execute("UPDATE account SET last_seen_at = ? WHERE id = ?", (now(), account_id))


# -- sessions (local mode) ---------------------------------------------------------------------

def sign_in(store: Store, username: str, password: str, hours: int = 12) -> Optional[tuple]:
    """(session token, account) for the right password, else None. Takes as long either way."""
    try:
        found = account(store, username)
    except ValueError:
        found = None
    stored = found["password_hash"] if found else ""
    ok = check_password(password, stored or _DUMMY)  # the same work for a name that does not exist
    if not (found and stored and ok and not found["disabled"]):
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    with store.connect() as con:
        con.execute("DELETE FROM login_session WHERE expires_at < ?", (now(),))
        con.execute("INSERT INTO login_session (token_hash, account_id, created_at, expires_at) VALUES (?,?,?,?)",
                    (_hash_token(token), found["id"], now(), expires))
    _seen(store, found["id"])
    return token, _active(found)


def session_account(store: Store, token: str) -> Optional[dict]:
    if not token:
        return None
    with store.connect() as con:
        row = con.execute(
            "SELECT a.* FROM login_session s JOIN account a ON a.id = s.account_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?", (_hash_token(token), now())).fetchone()
    return _active(row)


def sign_out(store: Store, token: str) -> None:
    with store.connect() as con:
        con.execute("DELETE FROM login_session WHERE token_hash = ?", (_hash_token(token or ""),))


# -- Windows sign-in through a proxy --------------------------------------------------------------

def windows_account(store: Store, raw: str, default_role: str = "viewer", create: bool = True) -> Optional[dict]:
    """The account for a name the proxy signed in: DOMAIN\\user or user@domain, created on first visit."""
    try:
        username = normalise(raw)
    except ValueError:
        return None
    found = account(store, username)
    if found is None:
        if not create:
            return None
        found = add_account(store, username, default_role)
    if not found["disabled"]:
        _seen(store, found["id"])
    return _active(found)


def iis_token_user(hex_handle: str) -> str:
    """DOMAIN\\user for the Windows token handle IIS passed in X-IIS-WindowsAuthToken; closes the handle.

    Windows only. HttpPlatformHandler duplicates the signed-in user's token into this process for
    each request and leaves closing it to the process.
    """
    import ctypes
    from ctypes import wintypes
    handle = wintypes.HANDLE(int(hex_handle, 16))
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
    advapi32.LookupAccountSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(handle, 1, None, 0, ctypes.byref(needed))  # 1 = TokenUser
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(handle, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        sid = ctypes.c_void_p.from_buffer(buffer).value  # TOKEN_USER starts with the user's SID pointer
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        name_len, domain_len, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int(0)
        if not advapi32.LookupAccountSidW(None, sid, name, ctypes.byref(name_len), domain, ctypes.byref(domain_len),
                                          ctypes.byref(use)):
            raise OSError(ctypes.get_last_error(), "LookupAccountSid failed")
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        kernel32.CloseHandle(handle)


# -- API tokens ---------------------------------------------------------------------------------

def create_token(store: Store, username: str, name: str) -> str:
    found = account(store, username)
    if found is None:
        raise KeyError(f"no such account: {username}")
    token = "pbiw_" + secrets.token_urlsafe(30)
    with store.connect() as con:
        con.execute("INSERT INTO api_token (token_hash, prefix, account_id, name, created_at) VALUES (?,?,?,?,?)",
                    (_hash_token(token), token[:12], found["id"], (name or "token").strip()[:100], now()))
    return token


def tokens(store: Store, username: str) -> list:
    found = account(store, username)
    if found is None:
        return []
    with store.connect() as con:
        return _rows(con.execute("SELECT prefix, name, created_at, last_used_at FROM api_token WHERE account_id = ? "
                                 "ORDER BY created_at", (found["id"],)))


def revoke_token(store: Store, prefix: str, username: Optional[str] = None) -> bool:
    with store.connect() as con:
        if username:
            found = account(store, username)
            if not found:
                return False
            return con.execute("DELETE FROM api_token WHERE prefix = ? AND account_id = ?",
                               (prefix, found["id"])).rowcount > 0
        return con.execute("DELETE FROM api_token WHERE prefix = ?", (prefix,)).rowcount > 0


def token_account(store: Store, token: str) -> Optional[dict]:
    if not token or not token.startswith("pbiw_"):
        return None
    with store.connect() as con:
        row = con.execute("SELECT a.* FROM api_token t JOIN account a ON a.id = t.account_id WHERE t.token_hash = ?",
                          (_hash_token(token),)).fetchone()
        if row is not None:
            con.execute("UPDATE api_token SET last_used_at = ? WHERE token_hash = ?", (now(), _hash_token(token)))
    return _active(row)


# -- audit log ----------------------------------------------------------------------------------

SECRET_FIELDS = {"password", "old_password", "new_password", "token"}


def audit(store: Store, who: Optional[dict], action: str, target: str = "", detail=None, address: str = "") -> None:
    if isinstance(detail, dict):
        detail = {k: ("•••" if k in SECRET_FIELDS else v) for k, v in detail.items()}
    text = json.dumps(detail, ensure_ascii=False, default=str)[:4000] if detail not in (None, "") else ""
    with store.connect() as con:
        con.execute("INSERT INTO audit (at, username, action, target, detail, address) VALUES (?,?,?,?,?,?)",
                    (now(), (who or {}).get("username") or "", action[:100], str(target)[:200], text, address[:100]))


def audit_log(store: Store, limit: int = 200, username: str = "", action: str = "") -> list:
    with store.connect() as con:
        return _rows(con.execute(
            "SELECT * FROM audit WHERE (? = '' OR username = ?) AND (? = '' OR action LIKE ?) ORDER BY id DESC LIMIT ?",
            (username, username, action, action + "%", max(1, min(int(limit), 5000)))))


# -- failed sign-ins -------------------------------------------------------------------------------

class Throttle:
    """At most ``attempts`` failed sign-ins per name and per address in ``window`` seconds."""

    def __init__(self, attempts: int = 5, window: int = 900):
        self.attempts, self.window, self.failures = attempts, window, {}

    def _recent(self, key) -> list:
        cutoff = time.monotonic() - self.window
        self.failures[key] = [t for t in self.failures.get(key, []) if t > cutoff]
        return self.failures[key]

    def blocked(self, username: str, address: str) -> bool:
        return any(len(self._recent(k)) >= self.attempts for k in (("u", username.lower()), ("a", address)))

    def failed(self, username: str, address: str) -> None:
        for key in (("u", username.lower()), ("a", address)):
            self._recent(key).append(time.monotonic())

    def cleared(self, username: str) -> None:
        self.failures.pop(("u", username.lower()), None)
