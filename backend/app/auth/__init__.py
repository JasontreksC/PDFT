"""Members: sign-in with a Google account, login sessions, and "who is asking" for every API call.

PDFT_AUTH = google (default) | off
  google  every /api call needs a login session; a document belongs to the member who uploaded it
  off     no login, one built-in local user owns everything (a single-user install, tests). Never use it on a public host.

Settings (environment):
  PDFT_PUBLIC_URL           the address people use, e.g. https://pdft.org (the Google redirect URI is <it>/auth/callback).
                            Empty: taken from the request (Host / X-Forwarded-Proto)
  PDFT_GOOGLE_CLIENT_FILE   the OAuth client JSON downloaded from Google Cloud Console (default /run/secrets/google_oauth.json),
                            or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET
  PDFT_ADMIN_EMAILS         comma separated: these members are administrators (member management, server settings)
  PDFT_ALLOWED_EMAILS / PDFT_ALLOWED_DOMAINS   comma separated; when either is set only those may sign in (default: anyone)
  PDFT_SESSION_DAYS         a session ends this many days after its last use (default 30)
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

MODE = os.environ.get("PDFT_AUTH", "google").strip().lower() or "google"
if MODE not in ("google", "off"):
    raise RuntimeError(f"PDFT_AUTH must be 'google' or 'off', not {MODE!r}")
ENABLED = MODE == "google"


def _csv(name: str) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip())


PUBLIC_URL = os.environ.get("PDFT_PUBLIC_URL", "").strip().rstrip("/")
ADMIN_EMAILS = _csv("PDFT_ADMIN_EMAILS")
ALLOWED_EMAILS = _csv("PDFT_ALLOWED_EMAILS")
ALLOWED_DOMAINS = frozenset(d.lstrip("@") for d in _csv("PDFT_ALLOWED_DOMAINS"))
SESSION_DAYS = max(1, int(os.environ.get("PDFT_SESSION_DAYS", "30") or 30))
COOKIE = "pdft_session"


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str = ""
    picture: str | None = None
    role: str = "user"
    status: str = "active"
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {
            "id": self.id, "email": self.email, "name": self.name, "picture": self.picture, "role": self.role,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


LOCAL = User(id="local", email="", name="로컬 사용자", role="admin")      # PDFT_AUTH=off

_current: ContextVar[User | None] = ContextVar("pdft_user", default=None)


def current() -> User | None:
    """The member this request belongs to (set by `routes.require_user`)."""
    return _current.get()


def may_sign_in(email: str) -> bool:
    email = email.lower()
    if email in ADMIN_EMAILS or not (ALLOWED_EMAILS or ALLOWED_DOMAINS):
        return True
    return email in ALLOWED_EMAILS or email.rpartition("@")[2] in ALLOWED_DOMAINS
