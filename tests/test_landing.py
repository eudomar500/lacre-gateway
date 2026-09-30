"""What the landing reads without a key, and the account's job list.

GET /primitives is public and answers from a copy of the Router's reading at
most ten minutes old. GET /jobs lists the caller's jobs only. The Integrate
snippet on the page is the one in integrations/consumer_example.py, and the
genlayer-js the wallet path loads is the pinned self-hosted build.
"""

import html
import re
from html.parser import HTMLParser

import pytest

import support
from support import API_KEY
from lacre_gateway.app import JOB_FILTERS, PRIMITIVES_RETRY_S, PRIMITIVES_TTL_S
from lacre_gateway.web import CSP, REDIRECTS, WEB_DIR
from test_accounts import ADMIN, api, app_for, customer, upload  # noqa: F401

AUTH = {"X-API-Key": API_KEY}
LAYERS = ("router", "keycache", "verifier", "extractor_patterns", "extractor_llm")
CONSUMER = support.LACRE_SOURCE / "integrations" / "consumer_example.py"
BEGIN = "# integrate:begin\n"
END = "# integrate:end\n"
GENLAYER_JS = "genlayer-js-1.2.0.min.js"


def router_reads(api):
    return [r for r in api.chain.reads if r[0] == support.ROUTER and r[1] == "resolve"]


# ---- GET /primitives ----------------------------------------------------------------

def test_primitives_need_no_key(api):
    response = api.client.get("/primitives")
    assert response.status_code == 200
    body = response.json()
    assert body["addresses"] == {
        "router": support.ROUTER, "keycache": support.KEYCACHE, "verifier": support.VERIFIER,
        "extractor_patterns": support.EXTRACTOR, "extractor_llm": support.EXTRACTOR_LLM}
    assert body["layers"] == {name: True for name in LAYERS}
    assert body["network"] == api.settings.network
    assert body["read_at"].endswith("Z")


def test_primitives_are_read_at_finalized(api):
    api.client.get("/primitives")
    reads = router_reads(api)
    assert {r[2][0] for r in reads} == {"verifier", "keycache", "extractor", "extractor_llm"}
    assert all(r[3] is True for r in reads)


def test_primitives_are_cached_for_ten_minutes(api):
    first = api.client.get("/primitives")
    count = len(router_reads(api))
    api.chain.resolves["verifier"] = "0x" + "c7" * 20
    api.clock.advance(PRIMITIVES_TTL_S - 1)
    again = api.client.get("/primitives")
    # The copy, not the chain: nothing more was read and the old address stands.
    assert len(router_reads(api)) == count
    assert again.json() == first.json()
    assert again.headers["cache-control"] == "public, max-age=1"
    api.clock.advance(1)
    moved = api.client.get("/primitives").json()
    assert len(router_reads(api)) == 2 * count
    assert moved["addresses"]["verifier"] == "0x" + "c7" * 20
    assert first.headers["cache-control"] == "public, max-age=%d" % (PRIMITIVES_TTL_S,)


def test_a_reading_without_the_router_is_kept_briefly(api):
    api.chain.up = False
    down = api.client.get("/primitives")
    assert down.status_code == 200
    assert not any(down.json()["layers"].values())
    assert not any(down.json()["addresses"].values())
    api.chain.up = True
    api.clock.advance(PRIMITIVES_RETRY_S - 1)
    assert not api.client.get("/primitives").json()["layers"]["router"]
    api.clock.advance(1)
    assert api.client.get("/primitives").json()["layers"]["router"] is True


def test_an_extractor_the_router_does_not_name_is_empty(api):
    api.chain.resolves["extractor_llm"] = ""
    body = api.client.get("/primitives").json()
    assert body["addresses"]["extractor_llm"] == ""
    assert body["layers"]["extractor_llm"] is False
    assert body["layers"]["router"] is True


# ---- GET /jobs --------------------------------------------------------------------------

def test_jobs_lists_the_accounts_own_jobs_newest_first(api):
    auth, _ = customer(api, credits=10)
    other, _ = customer(api, credits=10, name="other")
    mine = []
    for _ in range(3):
        mine.append(upload(api, auth).json()["job_id"])
        api.clock.advance(1)
    theirs = upload(api, other).json()["job_id"]
    body = api.client.get("/jobs", headers=auth).json()
    assert [j["id"] for j in body["jobs"]] == mine[::-1]
    assert theirs not in [j["id"] for j in body["jobs"]]
    assert [j["id"] for j in api.client.get("/jobs", headers=other).json()["jobs"]] == [theirs]
    assert set(body["jobs"][0]) == {"id", "status", "stage", "created_at", "updated_at",
                                    "finished_at"}
    assert body["jobs"][0]["created_at"].endswith("Z")
    assert body["jobs"][0]["finished_at"] is None
    assert body["next"] is None and body["status"] is None


def test_jobs_filters_by_status(api):
    auth, _ = customer(api, credits=10)
    ids = [upload(api, auth).json()["job_id"] for _ in range(4)]
    api.store.update_job(ids[0], status="finalized", stage="recorded")
    api.store.update_job(ids[1], status="refused", stage="stopped", refusal_reason="x")
    api.store.update_job(ids[2], status="failed", stage="stopped", error="x")
    listed = {status: [j["id"] for j in api.client.get(
        "/jobs?status=" + status, headers=auth).json()["jobs"]] for status in JOB_FILTERS}
    assert listed == {"open": [ids[3]], "finalized": [ids[0]], "refused": [ids[1]],
                      "failed": [ids[2]]}
    finished = api.client.get("/jobs?status=finalized", headers=auth).json()["jobs"][0]
    assert finished["status"] == "finalized" and finished["finished_at"].endswith("Z")


def test_jobs_pages(api):
    auth, _ = customer(api, credits=10)
    ids = []
    for _ in range(5):
        ids.append(upload(api, auth).json()["job_id"])
    first = api.client.get("/jobs?status=open&limit=2", headers=auth).json()
    assert [j["id"] for j in first["jobs"]] == ids[:-3:-1]
    assert first["next"] == "/jobs?status=open&limit=2&offset=2"
    second = api.client.get(first["next"], headers=auth).json()
    assert [j["id"] for j in second["jobs"]] == ids[2:0:-1]
    last = api.client.get(second["next"], headers=auth).json()
    assert [j["id"] for j in last["jobs"]] == ids[:1]
    assert last["next"] is None


@pytest.mark.parametrize("query", ["status=done", "status=OPEN", "limit=0", "limit=101",
                                   "offset=-1"])
def test_jobs_checks_its_query(api, query):
    auth, _ = customer(api)
    assert api.client.get("/jobs?" + query, headers=auth).status_code == 422


def test_jobs_needs_a_key(api):
    assert api.client.get("/jobs").status_code == 401
    assert api.client.get("/jobs", headers={"X-API-Key": "nope-" + "n" * 30}).status_code == 401


def test_a_job_from_before_accounts_is_listed_for_nobody(api):
    auth, _ = customer(api)
    job_id = upload(api, auth).json()["job_id"]
    api.store._run("UPDATE jobs SET account_id = NULL WHERE id = ?", (job_id,))
    assert api.client.get("/jobs", headers=auth).json()["jobs"] == []


# ---- HEAD --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", sorted(REDIRECTS))
def test_a_redirect_answers_head_as_it_answers_get(api, path):
    head = api.client.head(path, follow_redirects=False)
    get = api.client.get(path, follow_redirects=False)
    assert head.status_code == get.status_code == 301
    assert head.headers["location"] == get.headers["location"]
    assert head.content == b""


@pytest.mark.parametrize("path", ["/", "/docs.html", "/favicon.svg"])
def test_a_page_answers_head(api, path):
    head = api.client.head(path)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-security-policy"] == CSP


# ---- the nav ---------------------------------------------------------------------------------

class NavItems(HTMLParser):
    """The direct children of the desktop links and of the mobile drop, as
    (tag, attributes, text) in page order."""

    CONTAINERS = ("nav-links", "nav-drop")

    def __init__(self):
        super().__init__()
        self.lists = {name: [] for name in self.CONTAINERS}
        self.current = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.current is None:
            if attrs.get("class") in self.CONTAINERS:
                self.current, self.depth = attrs["class"], 0
            return
        self.depth += 1
        if self.depth == 1:
            self.lists[self.current].append([tag, attrs, ""])

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if self.depth == 0:
            self.current = None
        else:
            self.depth -= 1

    def handle_data(self, data):
        if self.current is not None and self.depth >= 1:
            self.lists[self.current][-1][2] += data


def nav_items():
    parser = NavItems()
    parser.feed((WEB_DIR / "index.html").read_text(encoding="ascii"))
    return parser.lists


@pytest.mark.parametrize("where", NavItems.CONTAINERS)
def test_the_nav_offers_the_wallet_right_before_access(where):
    items = nav_items()[where]
    names = [text.strip().upper() for _, _, text in items]
    assert names[-2:] == ["CONNECT WALLET", "ACCESS"], names
    tag, attrs, text = items[-2]
    assert tag == "button" and attrs.get("type") == "button"
    assert attrs.get("class") == "nav-wallet"
    assert text == "CONNECT WALLET"


def test_access_stays_the_only_pill():
    items = nav_items()["nav-links"]
    pills = [text for _, attrs, text in items if "nav-access" in (attrs.get("class") or "")]
    assert pills == ["Access"]


def test_the_wallet_popover_is_in_the_page_and_hidden():
    page = (WEB_DIR / "index.html").read_text(encoding="ascii")
    tag = re.search(r'<div [^>]*id="walletPop"[^>]*>', page)
    assert tag, "no wallet popover"
    assert 'role="dialog"' in tag.group(0) and 'aria-label="' in tag.group(0)
    assert re.search(r"\shidden[\s>]", tag.group(0))
    for part in ('id="walletPopAddr"', 'id="walletPopGen"', "Not on Bradbury",
                 'id="walletPopOff"'):
        assert part in page, part


# ---- the footer --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["index.html", "docs.html"])
def test_the_page_ends_with_the_footer(name):
    page = (WEB_DIR / name).read_text(encoding="ascii")
    assert page.rstrip().endswith("</body>\n</html>")
    body = page[:page.rindex("</body>")].rstrip()
    assert body.endswith("</footer>")
    foot = body[body.rindex("<footer"):]
    assert re.findall(r'href="([^"]*)"', foot) == ["https://in-sidr.xyz", "https://genlayer.com"]
    assert foot.count('target="_blank" rel="noopener"') == 2
    assert re.sub(r"<[^>]+>", "", foot) == "Built by Insidr Labs on GenLayer"


# ---- the Integrate snippet -----------------------------------------------------------------

def snippet_in_file():
    if not CONSUMER.is_file():
        pytest.fail("%s is missing: move vendor/lacre to a commit that has it, or point "
                    "LACRE_SOURCE at one" % (CONSUMER,))
    text = CONSUMER.read_text(encoding="ascii")
    assert text.count(BEGIN) == 1 and text.count(END) == 1, \
        "consumer_example.py needs the integrate:begin and integrate:end markers"
    return text[text.index(BEGIN) + len(BEGIN):text.index(END)]


def test_the_page_serves_the_consumer_snippet_unchanged(api):
    page = api.client.get("/").text
    shown = re.search(r'<pre class="snippet" id="integrateCode">(.*?)</pre>', page, re.S)
    assert shown, "the Integrate block has no snippet"
    snippet = snippet_in_file()
    assert html.unescape(shown.group(1)) == snippet
    assert "def attested(router, record_id, domain, min_key_bits):" in snippet


def test_the_integrate_links_point_at_the_public_repository():
    page = (WEB_DIR / "index.html").read_text(encoding="ascii")
    assert ('href="https://github.com/eudomar500/lacre/blob/main/integrations/'
            'consumer_example.py"') in page
    assert 'href="https://github.com/eudomar500/lacre/blob/main/docs/direct-use.md"' in page


# ---- genlayer-js ------------------------------------------------------------------------------

def test_genlayer_js_is_self_hosted_and_pinned(api):
    response = api.client.get("/static/" + GENLAYER_JS)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.text.startswith("/* genlayer-js 1.2.0 with viem 2.56.3")
    assert (WEB_DIR / "genlayer-js-1.2.0.LICENSE.txt").is_file()
    # The first build sat one level deeper; that path must stay gone.
    assert api.client.get("/static/static/" + GENLAYER_JS).status_code == 404
    wallet = (WEB_DIR / "wallet.js").read_text(encoding="ascii")
    assert "'/static/" + GENLAYER_JS + "'" in wallet
    # Loaded on demand from this origin, never from a page's script tags.
    for name in ("index.html", "docs.html"):
        assert GENLAYER_JS not in (WEB_DIR / name).read_text(encoding="ascii")


def tunnel_rules():
    text = (support.ROOT / "deploy" / "cloudflared.yml.example").read_text(encoding="ascii")
    return [re.compile(p) for p in re.findall(r"^    path: (\S+)$", text, re.M)]


@pytest.mark.parametrize("path", ["/jobs", "/jobs/" + "a" * 32, "/primitives", "/",
                                  "/static/wallet.js", "/static/dkim.js",
                                  "/static/" + GENLAYER_JS])
def test_the_tunnel_routes_what_the_page_calls(path):
    assert any(rule.match(path) for rule in tunnel_rules()), path


@pytest.mark.parametrize("path", ["/jobs/", "/jobsx", "/primitives/x", "/admin/accounts"])
def test_the_tunnel_routes_nothing_more(path):
    assert not any(rule.match(path) for rule in tunnel_rules()), path


def test_the_page_may_reach_the_bradbury_rpc_and_nothing_else(api):
    csp = api.client.get("/").headers["content-security-policy"]
    assert "connect-src 'self' https://rpc-bradbury.genlayer.com;" in csp
    assert "script-src 'self';" in csp
