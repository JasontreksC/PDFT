"""Members: Google login, sessions, document ownership, member management — against a REAL PostgreSQL.

Needs a database: PDFT_TEST_DATABASE_URL=postgresql://…  or the pip package `pixeltable-pgserver` (a throw-away server is
started). Without either the tests are skipped. Google itself is not called: the token endpoint is replaced.
   cd backend && python -m unittest tests.test_auth"""
import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

CLIENT_ID = "test-client.apps.googleusercontent.com"
_PG = None


def _database_url() -> str | None:
    global _PG
    url = os.environ.get("PDFT_TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        try:
            import pgserver
        except ImportError:
            return None
    _PG = (tempfile.mkdtemp(prefix="pdft-pg-"), None)
    server = pgserver.get_server(_PG[0])
    _PG = (_PG[0], server)
    return server.get_uri()


def _id_token(**claims) -> str:
    body = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": time.time() + 600, "email_verified": True, **claims}
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(body)}.sig"


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = _database_url()
        if not url:
            raise unittest.SkipTest("no PostgreSQL (set PDFT_TEST_DATABASE_URL or pip install pixeltable-pgserver)")
        cls.tmp = tempfile.mkdtemp(prefix="pdft-auth-")
        os.environ.update(
            PDFT_AUTH="google", PDFT_DATABASE_URL=url, GOOGLE_CLIENT_ID=CLIENT_ID, GOOGLE_CLIENT_SECRET="test-secret",
            PDFT_ADMIN_EMAILS="boss@example.com", PDFT_PUBLIC_URL="", PDFT_ALLOWED_EMAILS="", PDFT_ALLOWED_DOMAINS="",
            PDFT_DATA_DIR=cls.tmp + "/data", PDFT_CONFIG_DIR=cls.tmp + "/config",
            PDFT_FRONTEND_DIR=str(Path(__file__).resolve().parents[2] / "frontend"),
            PDFT_EXAMPLE_MODELS=str(Path(__file__).resolve().parents[1] / "models.example.json"),
            PDFT_FONT_DIRS=os.environ.get("PDFT_TEST_FONT_DIR", cls.tmp + "/fonts"), PDFT_DOCLING_URL="http://127.0.0.1:1",
        )
        from fastapi.testclient import TestClient

        from app import config            # another test module may have imported it already, with the default paths
        config.DATA_DIR, config.CONFIG_DIR = Path(cls.tmp, "data"), Path(cls.tmp, "config")
        config.FRONTEND_DIR, config.EXAMPLE_MODELS = Path(os.environ["PDFT_FRONTEND_DIR"]), Path(os.environ["PDFT_EXAMPLE_MODELS"])
        config.FONT_DIRS = [Path(os.environ["PDFT_FONT_DIRS"])]
        from app import auth, main
        from app.auth import google, routes
        assert auth.ENABLED, "app.auth was imported before this test set PDFT_AUTH"
        cls.auth, cls.main, cls.google, cls.routes = auth, main, google, routes
        cls._fetch = google.fetch_tokens
        cls.ctx = TestClient(main.app, follow_redirects=False)
        cls.c = cls.ctx.__enter__()                       # one lifespan = one event loop for the connection pool
        cls.pdf = Path(cls.tmp) / "a.pdf"
        d = pymupdf.open()
        d.new_page(width=300, height=300).insert_text((20, 40), "Hello world, this is a page.", fontsize=11)
        d.save(cls.pdf)
        cls.cookies: dict[str, str] = {}

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "ctx"):
            return
        cls.google.fetch_tokens = cls._fetch
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        if _PG and _PG[1] is not None:
            _PG[1].cleanup()
            shutil.rmtree(_PG[0], ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def google_says(self, **claims):
        """What Google's token endpoint will answer for the next login."""
        async def fake(client, code, redirect_uri, verifier):
            self.assertEqual(redirect_uri, "http://testserver/auth/callback")
            self.assertEqual(self.google.challenge(verifier), self.sent["code_challenge"][0])      # PKCE holds
            return {"id_token": _id_token(**{"nonce": self.sent["nonce"][0], **claims})}
        self.google.fetch_tokens = fake

    def login(self, email: str, sub: str | None = None, nxt: str | None = None, **claims) -> str:
        """Run the whole login as a fresh browser; returns where the person lands."""
        self.c.cookies.clear()
        r = self.c.get("/auth/login", params={"next": nxt} if nxt else None)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("https://accounts.google.com/"))
        self.sent = parse_qs(urlparse(r.headers["location"]).query)
        self.assertEqual(self.sent["client_id"], [CLIENT_ID])
        self.google_says(**{"sub": sub or "sub-" + email, "email": email, "name": email.split("@")[0].title(), **claims})
        r = self.c.get("/auth/callback", params={"code": "c0de", "state": self.sent["state"][0]})
        self.assertEqual(r.status_code, 303)
        if self.c.cookies.get("pdft_session"):
            self.cookies[email] = self.c.cookies.get("pdft_session")
        return r.headers["location"]

    def be(self, email: str | None):
        self.c.cookies.clear()
        if email:
            self.c.cookies.set("pdft_session", self.cookies[email])

    def upload(self) -> str:
        with open(self.pdf, "rb") as f:
            r = self.c.post("/api/jobs", files={"file": ("a.pdf", f, "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    # ------------------------------------------------------------------ tests (in name order: they share the database)
    def test_01_without_a_session_the_api_is_closed_but_the_pages_open(self):
        self.be(None)
        self.assertEqual(self.c.get("/api/session").json(), {"auth": "google", "user": None})
        for path in ("/api/jobs", "/api/bootstrap", "/api/jobs/abc/pages/0/image", "/api/admin/users"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
        self.assertEqual(self.c.post("/api/jobs", files={"file": ("a.pdf", b"x")}).status_code, 401)
        self.assertEqual(self.c.get("/api/health").status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)
        self.c.cookies.set("pdft_session", "made-up")
        self.assertEqual(self.c.get("/api/jobs").status_code, 401)

    def test_02_first_login_creates_the_member(self):
        self.assertEqual(self.login("ann@example.com"), "http://testserver/#/docs")
        u = self.c.get("/api/session").json()["user"]
        self.assertEqual((u["email"], u["name"], u["role"], u["status"]), ("ann@example.com", "Ann", "user", "active"))
        self.assertEqual(self.c.get("/api/jobs").json(), {"jobs": []})
        ann = u["id"]
        self.login("ann@example.com", name="Ann Lee")                                 # again: the same member, profile refreshed
        u = self.c.get("/api/session").json()["user"]
        self.assertEqual((u["id"], u["name"]), (ann, "Ann Lee"))
        self.assertEqual(self.login("boss@example.com"), "http://testserver/#/docs")
        self.assertEqual(self.c.get("/api/session").json()["user"]["role"], "admin")     # PDFT_ADMIN_EMAILS

    def test_03_a_login_cannot_be_forged_or_replayed(self):
        self.c.cookies.clear()
        r = self.c.get("/auth/login")
        sent = parse_qs(urlparse(r.headers["location"]).query)
        self.sent = sent
        self.google_says(sub="x", email="eve@example.com")
        state = sent["state"][0]
        # another browser (no state cookie) / a made-up state
        mine = self.c.cookies.get("pdft_login")
        self.c.cookies.clear()
        self.assertEqual(self.c.get("/auth/callback", params={"code": "c", "state": state}).headers["location"], "http://testserver/?login=expired")
        self.c.cookies.set("pdft_login", "other", path="/auth")
        self.assertEqual(self.c.get("/auth/callback", params={"code": "c", "state": "other"}).headers["location"], "http://testserver/?login=expired")
        # the real one works once
        self.c.cookies.clear()
        self.c.cookies.set("pdft_login", mine, path="/auth")
        self.assertEqual(self.c.get("/auth/callback", params={"code": "c", "state": state}).headers["location"], "http://testserver/#/docs")
        self.c.cookies.set("pdft_login", mine, path="/auth")
        self.assertEqual(self.c.get("/auth/callback", params={"code": "c", "state": state}).headers["location"], "http://testserver/?login=expired")
        self.assertEqual(self.c.get("/auth/callback", params={"error": "access_denied"}).headers["location"], "http://testserver/?login=denied")

    def test_04_what_google_answers_is_checked(self):
        for claims, code in (({"nonce": "wrong"}, "failed"), ({"aud": "someone-else"}, "failed"), ({"iss": "https://evil.example"}, "failed"),
                             ({"exp": time.time() - 3600}, "failed"), ({"email_verified": False}, "unverified")):
            self.assertEqual(self.login("zed@example.com", **claims), f"http://testserver/?login={code}", claims)
            self.assertIsNone(self.c.cookies.get("pdft_session"))
        self.be("boss@example.com")
        self.assertNotIn("zed@example.com", [u["email"] for u in self.c.get("/api/admin/users").json()["users"]])

    def test_05_after_login_only_a_path_of_this_site(self):
        for nxt, want in (("/#/admin", "/#/admin"), ("//evil.example/x", "/#/docs"), ("https://evil.example", "/#/docs"), ("/\\evil", "/#/docs")):
            self.assertEqual(self.login("ann@example.com", nxt=nxt), "http://testserver" + want)

    def test_06_a_document_belongs_to_who_uploaded_it(self):
        self.login("bob@example.com")
        self.be("ann@example.com")
        jid = self.upload()
        self.assertEqual([j["id"] for j in self.c.get("/api/jobs").json()["jobs"]], [jid])
        self.assertEqual(self.c.get(f"/api/jobs/{jid}/pages/0/image?scale=0.25").status_code, 200)
        self.be("bob@example.com")                                                      # another member: it does not exist
        self.assertEqual(self.c.get("/api/jobs").json(), {"jobs": []})
        for method, path in (("GET", ""), ("GET", "/status"), ("GET", "/pages/0/image"), ("GET", "/export"), ("POST", "/reset"),
                             ("POST", "/cancel"), ("DELETE", "")):
            self.assertEqual(self.c.request(method, f"/api/jobs/{jid}{path}").status_code, 404, (method, path))
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/translate", json={"target_lang": "Korean"}).status_code, 404)
        for path in ("/api/admin/overview", f"/api/admin/jobs/{jid}", f"/api/admin/jobs/{jid}/pages/0", "/api/admin/events"):
            self.assertEqual(self.c.get(path).status_code, 403, path)
        self.be("ann@example.com")
        self.assertEqual(self.c.get(f"/api/jobs/{jid}").status_code, 200)

    def test_06b_an_administrator_reviews_but_cannot_change(self):
        self.be("ann@example.com")
        jid = self.c.get("/api/jobs").json()["jobs"][0]["id"]
        self.be("boss@example.com")
        self.assertEqual(self.c.get("/api/jobs").json(), {"jobs": []})                  # the workspace list stays one's own
        o = self.c.get("/api/admin/overview").json()
        row = next(j for j in o["jobs"] if j["id"] == jid)
        ann = next(u for u in o["users"] if u["email"] == "ann@example.com")
        self.assertEqual((row["owner"], row["filename"], row["pages"], row["status"]), (ann["id"], "a.pdf", 1, "ready"))
        self.assertEqual((ann["documents"], ann["pages"]), (1, 1))
        self.assertGreaterEqual(o["totals"]["users"], 3)
        d = self.c.get(f"/api/admin/jobs/{jid}").json()
        self.assertEqual(d["owner_user"]["email"], "ann@example.com")
        self.assertEqual([(p["n"], p["state"]) for p in d["page_list"]], [(0, 0)])
        pg = self.c.get(f"/api/admin/jobs/{jid}/pages/0").json()
        self.assertEqual((pg["w"], pg["frames"], pg["done"]), (300, [], False))
        self.assertEqual(self.c.get(f"/api/admin/jobs/{jid}/pages/9").status_code, 404)
        self.assertEqual(self.c.get("/api/admin/jobs/nope").status_code, 404)
        self.assertEqual(self.c.get(f"/api/jobs/{jid}/pages/0/image?scale=0.25").status_code, 200)       # looking is allowed
        for method, path in (("DELETE", ""), ("POST", "/reset"), ("POST", "/cancel"), ("PUT", "/selection"), ("GET", "/export?selected=true")):
            self.assertEqual(self.c.request(method, f"/api/jobs/{jid}{path}", json={"pages": [0]} if method == "PUT" else None).status_code,
                             404, (method, path))
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/translate", json={"target_lang": "Korean"}).status_code, 404)
        self.assertEqual(self.c.patch(f"/api/jobs/{jid}/blocks/x", json={"size": 20}).status_code, 404)
        ev = self.c.get("/api/admin/events").json()["events"]
        self.assertIn(("upload", "ann@example.com", "a.pdf"), [(e["kind"], e["email"], e["filename"]) for e in ev])
        self.assertIn("login", [e["kind"] for e in ev])
        mine = self.c.get("/api/admin/events", params={"user": ann["id"]}).json()["events"]
        self.assertTrue(mine and all(e["email"] == "ann@example.com" for e in mine))

    def test_07_documents_from_before_there_were_members_are_the_administrators(self):
        old = self.main.store.create("old.pdf")                                         # owner: None
        shutil.copy(self.pdf, old.source)
        old.meta["pages"] = [{"w": 300, "h": 300}]
        self.be("ann@example.com")
        self.assertEqual(self.c.get(f"/api/jobs/{old.id}").status_code, 404)
        self.be("boss@example.com")
        self.assertIn(old.id, [j["id"] for j in self.c.get("/api/jobs").json()["jobs"]])
        self.assertEqual(self.c.delete(f"/api/jobs/{old.id}").status_code, 200)

    def test_08_server_settings_are_the_administrators(self):
        self.be("ann@example.com")
        self.assertEqual(self.c.put("/api/settings", json={"concurrency": 9}).status_code, 403)
        self.assertEqual(self.c.post("/api/models/reload").status_code, 403)
        self.assertEqual(self.c.get("/api/admin/users").status_code, 403)
        self.be("boss@example.com")
        self.assertEqual(self.c.put("/api/settings", json={"concurrency": 3}).json()["concurrency"], 3)

    def test_08b_the_blanks_of_the_privacy_policy(self):
        self.be(None)
        page = self.c.get("/privacy_policy.html")                                       # public, blanks highlighted
        self.assertEqual(page.status_code, 200)
        self.assertIn("〔운영자 이름〕", page.text)
        self.assertNotIn("{{", page.text)
        self.assertEqual(self.c.get("/api/admin/policy").status_code, 401)
        self.be("ann@example.com")
        self.assertEqual(self.c.put("/api/admin/policy", json={"values": {"operator": "x"}}).status_code, 403)
        self.be("boss@example.com")
        before = self.c.get("/api/admin/policy").json()["missing"]
        r = self.c.put("/api/admin/policy", json={"values": {"operator": "PDFT <운영>", "dpo_name": "  김 책임  "}})
        self.assertEqual(r.json()["missing"], before - 2)
        self.assertEqual(self.c.put("/api/admin/policy", json={"values": {"nope": "x"}}).status_code, 400)
        page = self.c.get("/privacy_policy.html").text
        self.assertIn("PDFT &lt;운영&gt;", page)                                        # escaped
        self.assertIn("성명: 김 책임", page)
        self.assertNotIn("〔운영자 이름〕", page)

    def test_09_member_management(self):
        self.be("boss@example.com")
        users = {u["email"]: u for u in self.c.get("/api/admin/users").json()["users"]}
        self.assertEqual(users["ann@example.com"]["documents"], 1)
        self.assertEqual((users["boss@example.com"]["role"], users["ann@example.com"]["role"]), ("admin", "user"))
        bob, boss = users["bob@example.com"]["id"], users["boss@example.com"]["id"]
        self.assertEqual(self.c.patch(f"/api/admin/users/{boss}", json={"status": "blocked"}).status_code, 400)      # not yourself
        self.assertEqual(self.c.patch("/api/admin/users/nope", json={"status": "blocked"}).status_code, 404)
        self.assertEqual(self.c.patch(f"/api/admin/users/{bob}", json={"status": "blocked"}).json()["status"], "blocked")
        self.be("bob@example.com")                                                      # the open session ends at once
        self.assertEqual(self.c.get("/api/jobs").status_code, 401)
        self.assertEqual(self.login("bob@example.com"), "http://testserver/?login=blocked")
        self.be("boss@example.com")
        unblocked = self.c.patch(f"/api/admin/users/{bob}", json={"status": "active", "role": "admin"}).json()
        self.assertEqual((unblocked["status"], unblocked["role"]), ("active", "user"))   # nobody is MADE an administrator here
        self.login("bob@example.com")
        self.assertEqual(self.c.get("/api/admin/users").status_code, 403)
        self.be("boss@example.com")
        self.assertEqual(self.c.delete(f"/api/admin/users/{bob}").json(), {"ok": True, "documents": 0})
        self.be("bob@example.com")
        self.assertEqual(self.c.get("/api/session").json()["user"], None)

    def test_10_logout_ends_the_session(self):
        self.login("cat@example.com")
        self.assertEqual(self.c.get("/api/jobs").status_code, 200)
        token = self.cookies["cat@example.com"]
        self.assertEqual(self.c.post("/auth/logout").status_code, 200)
        self.c.cookies.clear()
        self.c.cookies.set("pdft_session", token)                                       # the old cookie is worth nothing now
        self.assertEqual(self.c.get("/api/jobs").status_code, 401)

    def test_11_leaving_removes_the_account_and_its_documents(self):
        self.be("ann@example.com")
        jid = self.c.get("/api/jobs").json()["jobs"][0]["id"]
        self.upload()
        self.assertEqual(self.c.delete("/api/me").json(), {"ok": True, "documents": 2})
        self.assertIsNone(self.main.store.get(jid))
        self.be("ann@example.com")
        self.assertEqual(self.c.get("/api/jobs").status_code, 401)
        self.be("boss@example.com")
        self.assertNotIn("ann@example.com", [u["email"] for u in self.c.get("/api/admin/users").json()["users"]])
        self.login("ann@example.com")                                                   # coming back = a new member
        self.assertEqual(self.c.get("/api/jobs").json(), {"jobs": []})

    def test_12_allow_list(self):
        a = self.auth
        keep = (a.ALLOWED_EMAILS, a.ALLOWED_DOMAINS)
        try:
            a.ALLOWED_EMAILS, a.ALLOWED_DOMAINS = frozenset({"dan@else.org"}), frozenset({"example.com"})
            self.assertTrue(a.may_sign_in("New@Example.com"))
            self.assertTrue(a.may_sign_in("dan@else.org"))
            self.assertFalse(a.may_sign_in("eve@else.org"))
            self.assertTrue(a.may_sign_in("boss@example.com"))
            self.assertEqual(self.login("eve@else.org"), "http://testserver/?login=not_allowed")
        finally:
            a.ALLOWED_EMAILS, a.ALLOWED_DOMAINS = keep


if __name__ == "__main__":
    unittest.main()
