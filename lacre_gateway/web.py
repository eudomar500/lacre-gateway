"""The web app in web/ and the access request form behind it.

The pages are static files: no template, no build step, nothing rendered
per request. They call the API on the same origin with the key the visitor
pastes, which the page keeps in memory or, if the visitor lets it, in the
tab's sessionStorage, never anywhere longer lived; so serving them needs no
key and they read nothing a key does not already read.

A visitor with a wallet and no key attests without the gateway: the page
cuts the signed headers itself (web/dkim.js), the wallet signs attest_inline
and the page follows the transaction on the Bradbury RPC with genlayer-js,
which is served under /static like everything else. No job is created.
"""

import collections
import re
import threading
from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
# Path -> file. The docs keep their .html name because /docs would read as
# an API explorer; everything else is one scrolling page at /.
PAGES = {
    "/": "index.html",
    "/docs.html": "docs.html",
}
# The pages the landing absorbed, sent to their section so old links and
# bookmarks still land where they meant to.
REDIRECTS = {
    "/how.html": "/#how",
    "/why.html": "/#why",
    "/mcp.html": "/#mcp",
    "/access.html": "/#access",
}
FAVICON = "favicon.svg"
# The chain the wallet path reads and follows its transaction on. The wallet
# signs; every read, and the fee and gas estimates, go to this RPC.
BRADBURY_RPC = "https://rpc-bradbury.genlayer.com"
# The pages load their own script, style and fonts only, and talk to this
# origin and the Bradbury RPC only, so an injected script or a framing page
# gets nowhere. A key pasted into the page is sent to this origin and
# nowhere else: the page never puts it on a call to the RPC.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
       "img-src 'self' data:; connect-src 'self' " + BRADBURY_RPC + "; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")
PAGE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Revalidated each time, so a deploy shows at once.
    "Cache-Control": "no-cache",
}

NAME_MAX = 100
# RFC 5321 caps a path at 256 octets, the brackets included.
EMAIL_MAX = 254
WHAT_MAX = 2000
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Tab and newline are kept in the free text; every other control character
# is refused, so what an operator lists is what the visitor typed.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

ACCESS_WINDOW_S = 3600
ACCESS_PER_IP = 5
# The per address limit alone lets a visitor with many addresses fill the
# table; this caps every address together.
ACCESS_TOTAL = 100


class AccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX)
    email: str = Field(max_length=EMAIL_MAX)
    what: str = Field("", max_length=WHAT_MAX)

    @field_validator("name", "email", "what")
    @classmethod
    def printable(cls, value):
        value = value.strip()
        if CONTROL.search(value):
            raise ValueError("control characters are not allowed")
        return value

    @field_validator("name")
    @classmethod
    def named(cls, value):
        if not value:
            raise ValueError("a name is required")
        return value

    @field_validator("email")
    @classmethod
    def address(cls, value):
        if not EMAIL.match(value):
            raise ValueError("not an email address")
        return value


class RateLimit:
    """At most per_key hits per key and total hits overall in window_s.

    Kept in memory: the gateway is one process, and a restart forgetting
    who asked costs at most one more window of requests.
    """

    def __init__(self, clock, window_s=ACCESS_WINDOW_S, per_key=ACCESS_PER_IP,
                 total=ACCESS_TOTAL):
        self.clock = clock
        self.window_s = window_s
        self.per_key = per_key
        self.total = total
        self._hits = collections.defaultdict(collections.deque)
        self._all = collections.deque()
        self._lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        start = now - self.window_s
        with self._lock:
            while self._all and self._all[0] <= start:
                self._all.popleft()
            hits = self._hits[key]
            while hits and hits[0] <= start:
                hits.popleft()
            if len(hits) >= self.per_key or len(self._all) >= self.total:
                if not hits:
                    del self._hits[key]
                return False
            hits.append(now)
            self._all.append(now)
            # Keys whose hits all expired are dropped, so the table holds
            # only the addresses of the current window.
            for stale in [k for k, v in self._hits.items() if not v or v[-1] <= start]:
                del self._hits[stale]
            return True


def client_address(request):
    """The visitor's address as the tunnel reports it.

    Behind cloudflared every connection comes from 127.0.0.1, so the socket
    address says nothing; Cloudflare sets CF-Connecting-IP and overwrites
    one a client sends. The gateway listens on localhost only, so nothing
    else can set the header, and if something did the total cap still holds.
    """
    given = request.headers.get("cf-connecting-ip", "").strip()
    if given:
        return given[:64]
    return request.client.host if request.client else "unknown"


class WebFiles(StaticFiles):
    """/static: the same headers as the pages."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(PAGE_HEADERS)
        return response


def add_pages(app, web_dir=WEB_DIR):
    """Route the pages and /static. Nothing is routed when web_dir is
    missing, so the API runs from a checkout without the web app."""
    web_dir = Path(web_dir)
    if not web_dir.is_dir():
        return False
    # HEAD too: a link checker or a crawler asks with HEAD, and FastAPI,
    # unlike /static, does not answer it for a GET route by itself.
    for path, name in PAGES.items():
        app.add_api_route(path, page_endpoint(web_dir / name), methods=["GET", "HEAD"],
                          include_in_schema=False, name="page:" + name)
    for path, target in REDIRECTS.items():
        app.add_api_route(path, redirect_endpoint(target), methods=["GET", "HEAD"],
                          include_in_schema=False, name="redirect:" + path)
    app.add_api_route("/" + FAVICON, favicon_endpoint(web_dir / FAVICON),
                      methods=["GET", "HEAD"], include_in_schema=False, name="favicon")
    app.mount("/static", WebFiles(directory=web_dir), name="static")
    return True


def page_endpoint(file):
    def page():
        return FileResponse(file, media_type="text/html; charset=utf-8", headers=PAGE_HEADERS)
    return page


def redirect_endpoint(target):
    def redirect():
        # Permanent: the page is gone for good, and a browser may remember it.
        return RedirectResponse(target, status_code=301, headers=PAGE_HEADERS)
    return redirect


def favicon_endpoint(file):
    # Browsers ask for it at the root, so it is routed there as well as
    # being reachable under /static like every other file.
    def favicon():
        return FileResponse(file, media_type="image/svg+xml", headers=PAGE_HEADERS)
    return favicon
