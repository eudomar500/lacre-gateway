"""The web app: the static files, the access form and its admin listing."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import support
from support import API_KEY
from lacre_gateway.app import create_app
from lacre_gateway.web import ACCESS_PER_IP, ACCESS_WINDOW_S, PAGES, WEB_DIR, RateLimit

ADMIN_TOKEN = "test-admin-" + "t" * 32
ADMIN = {"X-Admin-Token": ADMIN_TOKEN}
AUTH = {"X-API-Key": API_KEY}
# The only hosts a page may name: the public gateway and the chain explorer
# its job answers link to. Everything else the pages load is on /static.
ALLOWED_HOSTS = {"lacre.in-sidr.xyz", "explorer-bradbury.genlayer.com"}
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>)]*")
TEXT_SUFFIXES = {".html", ".css", ".js", ".txt"}


class AliveWorker(SimpleNamespace):
    def alive(self):
        return True


def app_for(gw, **changes):
    gw.settings = support.settings(gw.settings.data_dir, **changes)
    gw.client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                      AliveWorker(), clock=gw.clock))
    return gw


@pytest.fixture
def web(gw):
    return app_for(gw, admin_token=ADMIN_TOKEN)


def ask(web, ip="203.0.113.7", **fields):
    body = dict({"name": "Ada", "email": "ada@example.org", "what": "shipping notices"}, **fields)
    return web.client.post("/access-request", json=body, headers={"CF-Connecting-IP": ip})


def web_files():
    return sorted(p for p in WEB_DIR.rglob("*") if p.is_file())


# ---- the static files ----------------------------------------------------------

@pytest.mark.parametrize("path", sorted(PAGES))
def test_every_page_is_served_without_a_key(web, path):
    response = web.client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == (WEB_DIR / PAGES[path]).read_text(encoding="ascii")
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("path,kind", [
    ("/static/app.css", "text/css"), ("/static/app.js", "javascript"),
    ("/static/fonts/jost-latin.woff2", "font/woff2"),
    ("/static/fonts/jetbrains-mono-latin.woff2", "font/woff2")])
def test_the_assets_are_served_without_a_key(web, path, kind):
    response = web.client.get(path)
    assert response.status_code == 200
    assert kind in response.headers["content-type"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_static_does_not_leave_the_web_directory(web):
    assert web.client.get("/static/../lacre_gateway/app.py").status_code == 404
    assert web.client.get("/static/%2e%2e/README.md").status_code == 404
    assert web.client.get("/static/nothing.js").status_code == 404


def test_every_asset_a_page_names_exists(web):
    for name in PAGES.values():
        text = (WEB_DIR / name).read_text(encoding="ascii")
        for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', text):
            assert web.client.get(ref).status_code == 200, (name, ref)
    css = (WEB_DIR / "app.css").read_text(encoding="ascii")
    for ref in re.findall(r"url\('([^']+)'\)", css):
        assert (WEB_DIR / ref).is_file(), ref


def test_the_api_still_needs_its_key_next_to_the_pages(web):
    assert web.client.get("/health").status_code == 401
    assert web.client.get("/account").status_code == 401


def test_without_a_web_directory_only_the_api_is_routed(gw, tmp_path):
    client = TestClient(create_app(gw.settings, gw.store, gw.blobs, gw.contracts,
                                   AliveWorker(), clock=gw.clock, web_dir=tmp_path / "none"))
    gw.worker.tick()
    assert client.get("/").status_code == 404
    assert client.get("/static/app.js").status_code == 404
    assert client.get("/health", headers=AUTH).status_code == 200


def test_the_web_directory_names_no_host_but_the_two_allowed():
    seen = set()
    for path in web_files():
        if path.suffix not in TEXT_SUFFIXES:
            continue
        # The font licenses are kept verbatim, and the OFL names its own
        # home and the font project's; nothing loads from them.
        if path.name.startswith("OFL-"):
            continue
        for url in URL.findall(path.read_text(encoding="ascii")):
            host = url.split("://", 1)[1].split("/", 1)[0]
            assert url.startswith("https://") and host in ALLOWED_HOSTS, (path.name, url)
            seen.add(host)
    # The docs name the gateway; the explorer link comes from a job answer.
    assert "lacre.in-sidr.xyz" in seen


def test_the_web_directory_is_ascii():
    for path in web_files():
        if path.suffix in TEXT_SUFFIXES:
            path.read_bytes().decode("ascii")


def test_every_font_has_its_license_next_to_it():
    fonts = WEB_DIR / "fonts"
    names = {p.name for p in fonts.iterdir()}
    for family, license in (("jost-", "OFL-Jost.txt"), ("jetbrains-mono-", "OFL-JetBrainsMono.txt")):
        assert any(n.startswith(family) and n.endswith(".woff2") for n in names)
        text = (fonts / license).read_text(encoding="ascii")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text


def test_the_pages_keep_the_key_out_of_storage():
    script = (WEB_DIR / "app.js").read_text(encoding="ascii")
    for word in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert word not in script


# ---- /health layers ---------------------------------------------------------------

def test_health_reports_each_contract_layer(web):
    web.worker.tick()
    body = web.client.get("/health", headers=AUTH).json()
    assert body["layers"] == {"router": True, "keycache": True, "verifier": True,
                              "extractor_patterns": True, "extractor_llm": True}


def test_an_extractor_the_router_does_not_name_is_reported_off(web):
    web.worker.tick()
    web.chain.resolves["extractor_llm"] = ""
    response = web.client.get("/health", headers=AUTH)
    # Reported, not required: the status stays 200.
    assert response.status_code == 200
    assert response.json()["layers"]["extractor_llm"] is False
    assert response.json()["layers"]["extractor_patterns"] is True


def test_a_chain_that_is_down_reports_every_layer_down(web):
    web.chain.up = False
    body = web.client.get("/health", headers=AUTH).json()
    assert not any(body["layers"].values())


# ---- POST /access-request -------------------------------------------------------

def test_an_access_request_is_stored_without_a_key(web):
    response = ask(web, name="  Ada Lovelace ", what="invoices\nand receipts")
    assert response.status_code == 201
    assert response.json() == {"status": "received"}
    rows = web.store.access_requests(10, 0)
    assert len(rows) == 1
    assert (rows[0]["name"], rows[0]["email"], rows[0]["what"]) == \
        ("Ada Lovelace", "ada@example.org", "invoices\nand receipts")
    assert rows[0]["created_at"] == web.clock()


def test_what_may_be_left_empty(web):
    response = web.client.post("/access-request", json={"name": "Ada", "email": "a@b.org"},
                               headers={"CF-Connecting-IP": "203.0.113.9"})
    assert response.status_code == 201
    assert web.store.access_requests(10, 0)[0]["what"] == ""


@pytest.mark.parametrize("fields", [
    {"name": ""}, {"name": "   "}, {"email": "not-an-address"}, {"email": "a@b"},
    {"email": "a b@example.org"}, {"name": "x" * 101}, {"email": "a@" + "b" * 250 + ".org"},
    {"what": "w" * 2001}, {"name": "Ada\x00"}, {"what": "line\x1b[31m"},
    {"extra": "field"}])
def test_an_access_request_is_validated_and_capped(web, fields):
    assert ask(web, **fields).status_code == 422
    assert web.store.access_requests(10, 0) == []


def test_an_access_request_must_be_json(web):
    response = web.client.post("/access-request", data={"name": "Ada", "email": "a@b.org"})
    assert response.status_code == 422


def test_access_requests_are_rate_limited_per_address(web):
    for _ in range(ACCESS_PER_IP):
        assert ask(web).status_code == 201
    refused = ask(web)
    assert refused.status_code == 429
    assert len(web.store.access_requests(50, 0)) == ACCESS_PER_IP
    # Another address is not held back by the first.
    assert ask(web, ip="198.51.100.4").status_code == 201
    web.clock.advance(ACCESS_WINDOW_S + 1)
    assert ask(web).status_code == 201


def test_an_invalid_request_does_not_spend_the_limit(web):
    for _ in range(ACCESS_PER_IP + 2):
        assert ask(web, email="nope").status_code == 422
    assert ask(web).status_code == 201


def test_without_the_tunnel_header_the_socket_address_is_used(web):
    body = {"name": "Ada", "email": "ada@example.org"}
    for _ in range(ACCESS_PER_IP):
        assert web.client.post("/access-request", json=body).status_code == 201
    assert web.client.post("/access-request", json=body).status_code == 429


def test_the_rate_limit_caps_every_address_together():
    clock = support.Clock()
    limit = RateLimit(clock, window_s=60, per_key=2, total=3)
    assert [limit.allow(ip) for ip in ("a", "a", "a", "b", "c", "d")] == \
        [True, True, False, True, False, False]
    clock.advance(61)
    assert limit.allow("d") is True


def test_the_access_log_line_holds_no_personal_data(web, caplog):
    caplog.set_level("INFO", logger="lacre_gateway.app")
    ask(web, name="Ada Lovelace")
    text = caplog.text
    assert "access request" in text
    assert "Ada" not in text and "example.org" not in text


# ---- GET /admin/access-requests ---------------------------------------------------

def test_the_admin_lists_access_requests_newest_first(web):
    for i in range(3):
        ask(web, ip="203.0.113.%d" % (i,), name="n%d" % (i,))
        web.clock.advance(1)
    response = web.client.get("/admin/access-requests", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert [r["name"] for r in body["access_requests"]] == ["n2", "n1", "n0"]
    assert set(body["access_requests"][0]) == {"id", "name", "email", "what", "created_at"}
    assert body["access_requests"][0]["created_at"].endswith("Z")
    assert body["next"] is None


def test_the_admin_listing_pages(web):
    for i in range(3):
        ask(web, ip="203.0.113.%d" % (i,), name="n%d" % (i,))
    first = web.client.get("/admin/access-requests?limit=2", headers=ADMIN).json()
    assert [r["name"] for r in first["access_requests"]] == ["n2", "n1"]
    assert first["next"] == "/admin/access-requests?limit=2&offset=2"
    rest = web.client.get(first["next"], headers=ADMIN).json()
    assert [r["name"] for r in rest["access_requests"]] == ["n0"]
    assert rest["next"] is None


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong-" + "t" * 40}, AUTH])
def test_the_admin_listing_takes_the_admin_token_only(web, headers):
    assert web.client.get("/admin/access-requests", headers=headers).status_code == 401


def test_the_admin_listing_is_off_without_a_token(gw):
    app_for(gw, admin_token="")
    assert gw.client.get("/admin/access-requests", headers=ADMIN).status_code == 503


def test_the_access_table_survives_a_restart(web, tmp_path):
    ask(web)
    from lacre_gateway.store import Store
    again = Store(web.settings.db_path, clock=web.clock)
    assert len(again.access_requests(10, 0)) == 1


def test_the_web_directory_sits_at_the_repository_root():
    assert WEB_DIR == Path(support.ROOT) / "web"
