"""The members and their sessions in PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import timedelta

from .. import db
from . import ADMIN_EMAILS, ENABLED, SESSION_DAYS, User

log = logging.getLogger("pdft.auth")
_COLS = "id, email, name, picture, status, created_at, last_login_at"
TOUCH_EVERY = timedelta(hours=1)        # a session in use is pushed forward at most this often


def _user(r) -> User:
    # an administrator is an account of PDFT_ADMIN_EMAILS: decided by the server setting on every request, never stored
    return User(id=str(r["id"]), email=r["email"], name=r["name"], picture=r["picture"], status=r["status"],
                role="admin" if r["email"].lower() in ADMIN_EMAILS else "user",
                created_at=r["created_at"], last_login_at=r["last_login_at"])


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


# ----------------------------------------------------------------------------- pending logins
async def new_login(state: str, verifier: str, nonce: str, nxt: str) -> None:
    p = db.pool()
    await p.execute("DELETE FROM login_states WHERE created_at < now() - interval '15 minutes'")
    await p.execute("INSERT INTO login_states (state, code_verifier, nonce, next) VALUES ($1, $2, $3, $4)", state, verifier, nonce, nxt)


async def take_login(state: str) -> dict | None:
    """The pending login of this state, once: it is removed (a callback cannot be replayed)."""
    r = await db.pool().fetchrow(
        "DELETE FROM login_states WHERE state = $1 AND created_at > now() - interval '10 minutes' "
        "RETURNING code_verifier, nonce, next", state)
    return dict(r) if r else None


# ----------------------------------------------------------------------------- members
async def sign_in(claims: dict) -> User:
    """Create the member on their first login, refresh the profile on every later one."""
    r = await db.pool().fetchrow(
        f"""
        INSERT INTO users (google_sub, email, email_verified, name, picture, last_login_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (google_sub) DO UPDATE SET
            email = EXCLUDED.email, email_verified = EXCLUDED.email_verified, name = EXCLUDED.name, picture = EXCLUDED.picture,
            last_login_at = now()
        RETURNING {_COLS}
        """,
        str(claims["sub"]), claims["email"], bool(claims.get("email_verified")), claims.get("name") or "", claims.get("picture"))
    return _user(r)


async def get(user_id: str) -> User | None:
    try:
        r = await db.pool().fetchrow(f"SELECT {_COLS} FROM users WHERE id = $1::uuid", user_id)
    except (ValueError, db.asyncpg.DataError):      # not a uuid
        return None
    return _user(r) if r else None


async def all_users() -> list[User]:
    return [_user(r) for r in await db.pool().fetch(f"SELECT {_COLS} FROM users ORDER BY created_at")]


async def set_status(user_id: str, status: str) -> User | None:
    async with db.pool().acquire() as con, con.transaction():
        r = await con.fetchrow(f"UPDATE users SET status = $2 WHERE id = $1::uuid RETURNING {_COLS}", user_id, status)
        if r and status == "blocked":
            await con.execute("DELETE FROM sessions WHERE user_id = $1::uuid", user_id)
    return _user(r) if r else None


async def delete(user_id: str) -> bool:
    return (await db.pool().execute("DELETE FROM users WHERE id = $1::uuid", user_id)).endswith(" 1")     # sessions go with it


# ----------------------------------------------------------------------------- sessions
async def open_session(user: User, user_agent: str | None, ip: str | None) -> str:
    """A new session; returns the cookie value (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    p = db.pool()
    await p.execute("DELETE FROM sessions WHERE expires_at < now()")
    await p.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, user_agent, ip) VALUES ($1, $2::uuid, now() + $3::interval, $4, $5)",
        _hash(token), user.id, timedelta(days=SESSION_DAYS), (user_agent or "")[:300], ip)
    return token


async def session_user(token: str) -> User | None:
    """The active member of a live session; a session in use is kept alive."""
    r = await db.pool().fetchrow(
        f"SELECT u.{_COLS.replace(', ', ', u.')}, s.last_seen_at < now() - $2::interval AS stale "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = $1 AND s.expires_at > now()",
        _hash(token), TOUCH_EVERY)
    if not r or r["status"] != "active":
        return None
    if r["stale"]:
        await db.pool().execute("UPDATE sessions SET last_seen_at = now(), expires_at = now() + $2::interval WHERE token_hash = $1",
                                _hash(token), timedelta(days=SESSION_DAYS))
    return _user(r)


async def close_session(token: str) -> None:
    await db.pool().execute("DELETE FROM sessions WHERE token_hash = $1", _hash(token))


async def close_all(user_id: str) -> None:
    await db.pool().execute("DELETE FROM sessions WHERE user_id = $1::uuid", user_id)


# ----------------------------------------------------------------------------- activity log
async def record(user_id: str | None, kind: str, job=None, **detail) -> None:
    """Note what a member did (for the administrators' dashboard). Never fails the action it describes."""
    if not ENABLED or not user_id or user_id == "local":
        return
    try:
        await db.pool().execute(
            "INSERT INTO events (user_id, kind, job_id, filename, detail) VALUES ($1::uuid, $2, $3, $4, $5::jsonb)",
            user_id, kind, job.id if job else None, job.meta.get("filename") if job else None, json.dumps(detail, ensure_ascii=False))
    except Exception:  # noqa: BLE001  (the member was just removed, the database is restarting, ...)
        log.warning("activity not recorded: %s %s", kind, user_id, exc_info=True)


async def events(user_id: str | None = None, limit: int = 200) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT e.id, e.at, e.user_id, u.email, u.name, e.kind, e.job_id, e.filename, e.detail "
        "FROM events e JOIN users u ON u.id = e.user_id WHERE ($1::uuid IS NULL OR e.user_id = $1::uuid) "
        "ORDER BY e.at DESC, e.id DESC LIMIT $2", user_id, limit)
    return [{"id": r["id"], "at": r["at"].isoformat(), "user_id": str(r["user_id"]), "email": r["email"], "name": r["name"],
             "kind": r["kind"], "job_id": r["job_id"], "filename": r["filename"], "detail": json.loads(r["detail"])} for r in rows]
