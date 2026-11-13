"""Login, logout, "my account" and member management.

  GET    /auth/login?next=/…      start the Google login (redirects to Google)
  GET    /auth/callback           Google comes back here; opens the session and redirects to `next`
  POST   /auth/logout             end this session
  GET    /api/session             who am I (never 401: the UI asks this first)
  DELETE /api/me                  leave: the account, its sessions and all its documents are removed
  GET    /api/admin/users         administrators: every member (+ how many documents)
  PATCH  /api/admin/users/{id}    administrators: block / unblock
  DELETE /api/admin/users/{id}    administrators: remove a member and their documents
  GET    /api/admin/events        administrators: the activity log (?user=<id>)

An administrator is an account whose e-mail is in PDFT_ADMIN_EMAILS — a server setting, nothing a member can be given
in the UI. (The dashboard and the review of documents are in main.py: they need the documents.)

The session is an opaque random cookie (HttpOnly, SameSite=Lax, Secure over https); only its hash is in the database.
SameSite=Lax keeps the cookie off cross-site POST/PUT/PATCH/DELETE, which is the CSRF protection of the JSON API.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import COOKIE, ENABLED, LOCAL, PUBLIC_URL, SESSION_DAYS, User, _current, current, google, may_sign_in, store

log = logging.getLogger("pdft.auth")
router = APIRouter()

STATE_COOKIE = "pdft_login"
OPEN_PATHS = {"/api/health", "/api/session"}          # /api paths that need no session
CACHE_SECONDS = 20.0                                   # a page of thumbnails is one database lookup, not fifty

_client: google.Client | None = None
_cache: dict[str, tuple[float, User]] = {}

# set by main.py: what else must happen when a member goes (their documents), and how many documents each one has
purge_documents: Callable[[str], Awaitable[int]] | None = None
count_documents: Callable[[], dict[str, int]] = dict


def client() -> google.Client:
    global _client
    if _client is None:
        _client = google.load_client()
    return _client


def forget_sessions() -> None:
    _cache.clear()


# ----------------------------------------------------------------------------- who is asking
def base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


async def session_user(request: Request) -> User | None:
    if not ENABLED:
        return LOCAL
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    hit = _cache.get(token)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    user = await store.session_user(token)
    if user:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[token] = (now, user)
    else:
        _cache.pop(token, None)
    return user


async def require_user(request: Request) -> None:
    """App-wide dependency: every /api call (but the open ones) belongs to a signed-in, active member."""
    path = request.url.path
    if not path.startswith("/api/") or path in OPEN_PATHS:
        return
    user = await session_user(request)
    if user is None:
        raise HTTPException(401, "로그인이 필요합니다.")
    _current.set(user)


def user() -> User:
    u = current()
    if u is None:
        raise HTTPException(401, "로그인이 필요합니다.")
    return u


def admin() -> User:
    u = user()
    if not u.is_admin:
        raise HTTPException(403, "관리자만 할 수 있습니다.")
    return u


# ----------------------------------------------------------------------------- login
def _safe_next(nxt: str | None) -> str:
    """Only a path of this site (never another host: no open redirect)."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or any(ord(c) < 32 for c in nxt):
        return "/#/docs"
    return nxt[:300]


def _back(request: Request, code: str) -> RedirectResponse:
    resp = RedirectResponse(f"{base_url(request)}/?login={quote(code)}", status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


def _secure(request: Request) -> bool:
    return base_url(request).startswith("https://")


@router.get("/auth/login")
async def login(request: Request, next: str | None = None):
    nxt = _safe_next(next)
    if not ENABLED or await session_user(request):
        return RedirectResponse(base_url(request) + nxt, status_code=303)
    state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(16)
    await store.new_login(state, verifier, nonce, nxt)
    url = google.auth_url(client(), base_url(request) + "/auth/callback", state, nonce, verifier)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, path="/auth", httponly=True, samesite="lax", secure=_secure(request))
    return resp


@router.get("/auth/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if not ENABLED:
        return RedirectResponse(base_url(request) + "/", status_code=303)
    if error or not code:
        return _back(request, "denied")
    # the state must be the one this browser started with (cookie) and still pending on our side (once)
    if not state or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE) or ""):
        return _back(request, "expired")
    pending = await store.take_login(state)
    if not pending:
        return _back(request, "expired")
    try:
        claims = await google.finish(client(), code, base_url(request) + "/auth/callback", pending["code_verifier"], pending["nonce"])
    except google.LoginError as e:
        log.warning("google login failed: %s", e)
        return _back(request, e.code)
    except Exception:  # noqa: BLE001  (network trouble talking to Google)
        log.exception("google login failed")
        return _back(request, "failed")
    email = claims["email"].lower()
    if not may_sign_in(email):
        log.info("sign-in refused (not on the allow list): %s", email)
        return _back(request, "not_allowed")
    member = await store.sign_in(claims)
    if member.status != "active":
        return _back(request, "blocked")
    token = await store.open_session(member, request.headers.get("user-agent"), request.client.host if request.client else None)
    await store.record(member.id, "login")
    resp = RedirectResponse(base_url(request) + pending["next"], status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/", httponly=True, samesite="lax", secure=_secure(request))
    log.info("signed in: %s (%s)", member.email, member.id)
    return resp


@router.post("/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if ENABLED and token:
        await store.close_session(token)
        _cache.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ----------------------------------------------------------------------------- my account
@router.get("/api/session")
async def session(request: Request):
    u = await session_user(request)
    return {"auth": "google" if ENABLED else "off", "user": u.public() if u else None}


@router.delete("/api/me")
async def leave():
    u = user()
    if not ENABLED:
        raise HTTPException(400, "로그인을 쓰지 않는 설치에서는 탈퇴할 계정이 없습니다.")
    n = await purge_documents(u.id) if purge_documents else 0
    await store.delete(u.id)
    forget_sessions()
    log.info("member left: %s (%d documents removed)", u.email, n)
    resp = JSONResponse({"ok": True, "documents": n})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ----------------------------------------------------------------------------- member management (administrators)
def _uid(uid: str) -> str:
    try:
        return str(uuid.UUID(uid))
    except ValueError:
        raise HTTPException(404, "회원을 찾을 수 없습니다.") from None


@router.get("/api/admin/users")
async def list_users():
    admin()
    if not ENABLED:
        return {"users": []}
    docs = count_documents()
    return {"users": [{**u.public(), "documents": docs.get(u.id, 0)} for u in await store.all_users()]}


class UserPatch(BaseModel):
    status: Literal["active", "blocked"]


@router.patch("/api/admin/users/{uid}")
async def patch_user(uid: str, p: UserPatch):
    me = admin()
    uid = _uid(uid)
    if uid == me.id:
        raise HTTPException(400, "자기 자신은 차단할 수 없습니다.")
    target = await store.get(uid)
    if not target:
        raise HTTPException(404, "회원을 찾을 수 없습니다.")
    if p.status == "blocked" and target.is_admin:
        raise HTTPException(400, "관리자(PDFT_ADMIN_EMAILS)는 차단할 수 없습니다. 먼저 서버 설정에서 빼세요.")
    u = await store.set_status(uid, p.status)
    forget_sessions()
    log.info("member %s set to %s by %s", target.email, p.status, me.email)
    return u.public()


@router.delete("/api/admin/users/{uid}")
async def delete_user(uid: str):
    me = admin()
    uid = _uid(uid)
    if uid == me.id:
        raise HTTPException(400, "자기 계정은 여기서 지울 수 없습니다 (계정 메뉴의 회원 탈퇴를 쓰세요).")
    target = await store.get(uid)
    if not target:
        raise HTTPException(404, "회원을 찾을 수 없습니다.")
    n = await purge_documents(uid) if purge_documents else 0
    await store.delete(uid)
    forget_sessions()
    log.info("member %s removed by %s (%d documents)", target.email, me.email, n)
    return {"ok": True, "documents": n}


@router.get("/api/admin/events")
async def list_events(user: str | None = None, limit: int = 200):
    admin()
    if not ENABLED:
        return {"events": []}
    return {"events": await store.events(_uid(user) if user else None, max(1, min(1000, limit)))}
