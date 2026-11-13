"""Sign-in with Google: OpenID Connect, authorization-code flow with PKCE.

The client (id + secret) is the JSON downloaded from Google Cloud Console → APIs & Services → Credentials → OAuth client
("Web application"). Its redirect URI must be registered there: <PDFT_PUBLIC_URL>/auth/callback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid email profile"


class LoginError(Exception):
    """The login cannot be completed; `code` is shown to the person as a plain message by the UI."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str
    auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"


def load_client() -> Client:
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID", "").strip(), os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if cid and secret:
        return Client(cid, secret)
    path = Path(os.environ.get("PDFT_GOOGLE_CLIENT_FILE", "/run/secrets/google_oauth.json"))
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(f"Google OAuth 클라이언트 파일이 없습니다: {path} (PDFT_GOOGLE_CLIENT_FILE)") from e
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Google OAuth 클라이언트 파일을 읽을 수 없습니다: {path}: {e}") from e
    d = raw.get("web") or raw.get("installed") or raw
    if not d.get("client_id") or not d.get("client_secret"):
        raise RuntimeError(f"{path}에 client_id / client_secret이 없습니다.")
    return Client(d["client_id"], d["client_secret"], token_uri=d.get("token_uri") or Client.token_uri)


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def auth_url(client: Client, redirect_uri: str, state: str, nonce: str, verifier: str) -> str:
    return client.auth_uri + "?" + urlencode({
        "client_id": client.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "state": state, "nonce": nonce, "code_challenge": challenge(verifier), "code_challenge_method": "S256",
        "prompt": "select_account",
    })


async def fetch_tokens(client: Client, code: str, redirect_uri: str, verifier: str) -> dict:
    """Trade the authorization code for tokens at Google's token endpoint."""
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(client.token_uri, data={
            "code": code, "client_id": client.client_id, "client_secret": client.client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        })
    if r.status_code != 200:
        try:
            why = r.json().get("error_description") or r.json().get("error")
        except ValueError:
            why = r.text[:200]
        raise LoginError("failed", f"token endpoint {r.status_code}: {why}")
    return r.json()


def id_claims(id_token: str, client: Client, nonce: str) -> dict:
    """The claims of an ID token that came straight from Google's token endpoint.

    The signature is not checked: the token arrived over TLS from Google itself in answer to our client secret, which
    OpenID Connect Core §3.1.3.7 accepts in place of signature validation. Issuer, audience, expiry and nonce are checked."""
    try:
        body = id_token.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError) as e:
        raise LoginError("failed", "malformed id_token") from e
    if c.get("iss") not in ISSUERS:
        raise LoginError("failed", "wrong issuer")
    aud = c.get("aud")
    if client.client_id not in (aud if isinstance(aud, list) else [aud]):
        raise LoginError("failed", "wrong audience")
    if float(c.get("exp", 0)) < time.time() - 60:
        raise LoginError("failed", "expired id_token")
    if c.get("nonce") != nonce:
        raise LoginError("failed", "nonce mismatch")
    if not c.get("sub") or not c.get("email"):
        raise LoginError("failed", "no account id / e-mail")
    if not c.get("email_verified"):
        raise LoginError("unverified")
    return c


async def finish(client: Client, code: str, redirect_uri: str, verifier: str, nonce: str) -> dict:
    tokens = await fetch_tokens(client, code, redirect_uri, verifier)
    if "id_token" not in tokens:
        raise LoginError("failed", "no id_token")
    return id_claims(tokens["id_token"], client, nonce)
