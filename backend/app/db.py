"""PostgreSQL: the members and their login sessions (the documents themselves stay in files under /data/jobs).

Connection: PDFT_DATABASE_URL, or the parts PDFT_DB_HOST / PDFT_DB_PORT / PDFT_DB_USER / PDFT_DB_PASSWORD / PDFT_DB_NAME
(the parts need no URL escaping of the password).

The schema is a list of numbered migrations applied in order, once each, under an advisory lock.
"""
from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

log = logging.getLogger("pdft.db")

MIGRATIONS: list[str] = [
    # 1 — members, sessions, pending Google logins
    """
    CREATE TABLE users (
        id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        google_sub     TEXT NOT NULL UNIQUE,                 -- Google's stable account id (the e-mail may change)
        email          TEXT NOT NULL,
        email_verified BOOLEAN NOT NULL DEFAULT FALSE,
        name           TEXT NOT NULL DEFAULT '',
        picture        TEXT,
        role           TEXT NOT NULL DEFAULT 'user'   CHECK (role IN ('user', 'admin')),
        status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'blocked')),
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at  TIMESTAMPTZ
    );
    CREATE INDEX users_email_idx ON users (lower(email));

    CREATE TABLE sessions (
        token_hash   BYTEA PRIMARY KEY,                      -- sha256 of the cookie value: a leaked table opens no session
        user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at   TIMESTAMPTZ NOT NULL,
        user_agent   TEXT,
        ip           TEXT
    );
    CREATE INDEX sessions_user_idx ON sessions (user_id);
    CREATE INDEX sessions_expires_idx ON sessions (expires_at);

    CREATE TABLE login_states (                              -- a Google login that was started and not finished yet
        state         TEXT PRIMARY KEY,
        code_verifier TEXT NOT NULL,
        nonce         TEXT NOT NULL,
        next          TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    # 2 — administrators are the accounts in PDFT_ADMIN_EMAILS (server setting), not a stored role;
    #     the activity log: what each member did, for the administrators' dashboard (goes with the member)
    """
    ALTER TABLE users DROP COLUMN role;

    CREATE TABLE events (
        id       BIGSERIAL PRIMARY KEY,
        at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        user_id  UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        kind     TEXT NOT NULL,          -- login | upload | translate | finish | export | reset | delete
        job_id   TEXT,
        filename TEXT,
        detail   JSONB NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_at_idx ON events (at DESC);
    CREATE INDEX events_user_idx ON events (user_id, at DESC);
    """,
]

_pool: asyncpg.Pool | None = None


def _connect_args() -> dict:
    url = os.environ.get("PDFT_DATABASE_URL", "").strip()
    if url:
        return {"dsn": url}
    return {
        "host": os.environ.get("PDFT_DB_HOST", "db"),
        "port": int(os.environ.get("PDFT_DB_PORT", "5432")),
        "user": os.environ.get("PDFT_DB_USER", "pdft"),
        "password": os.environ.get("PDFT_DB_PASSWORD", ""),
        "database": os.environ.get("PDFT_DB_NAME", "pdft"),
    }


async def connect(retries: int = 30) -> asyncpg.Pool:
    """Open the pool (waiting for a database that is still starting) and bring the schema up to date."""
    global _pool
    if _pool is not None:
        return _pool
    last: Exception | None = None
    for attempt in range(retries):
        try:
            _pool = await asyncpg.create_pool(min_size=1, max_size=8, command_timeout=30, **_connect_args())
            break
        except (OSError, asyncpg.PostgresError) as e:       # refused / still starting up / wrong password
            last = e
            if isinstance(e, asyncpg.InvalidPasswordError | asyncpg.InvalidAuthorizationSpecificationError):
                break
            log.info("database not ready (%s), retrying %d/%d", e, attempt + 1, retries)
            await asyncio.sleep(1.0)
    if _pool is None:
        raise RuntimeError(f"PostgreSQL에 연결할 수 없습니다: {last}") from last
    await migrate(_pool)
    return _pool


async def migrate(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as con:
        await con.execute("SELECT pg_advisory_lock(7306411)")
        try:
            await con.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                              "(version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            done = {r["version"] for r in await con.fetch("SELECT version FROM schema_migrations")}
            for n, sql in enumerate(MIGRATIONS, start=1):
                if n in done:
                    continue
                async with con.transaction():
                    await con.execute(sql)
                    await con.execute("INSERT INTO schema_migrations (version) VALUES ($1)", n)
                log.info("database migration %d applied", n)
        finally:
            await con.execute("SELECT pg_advisory_unlock(7306411)")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("데이터베이스에 연결되어 있지 않습니다.")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
