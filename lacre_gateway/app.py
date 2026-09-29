"""The HTTP API.

Every endpoint takes an API key in the X-API-Key header. The two paths that
do not, /h/{token} and /b/{name}.bin, are not part of the API: they are
where the validators fetch the signed headers of an attest call and the body
of an extract call in flight, from the URL in that call's public calldata,
and they serve nothing once the call is over.

POST /inbound takes no API key either. Only the Cloudflare Email Worker
calls it, and it authenticates with an HMAC over the delivery made with
LACRE_INBOUND_SECRET, which no agent holds (see inbound_mac).

/mcp serves the lacre_mcp tools over streamable HTTP, behind the same key
check as the API (see McpEndpoint).

Every API key is an account (accounts.py). /admin/* manages accounts and
takes LACRE_ADMIN_TOKEN in X-Admin-Token, never an API key, so a leaked
API key cannot mint credits or read other accounts.
"""

import datetime
import hashlib
import hmac
import logging
import re

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.routing import Route

from lacre_mcp.gateway import Gateway
from lacre_mcp.server import build_server

from . import accounts, headers
from .blobs import BODY, BlobStore
from .chainio import ChainUnavailable
from .config import EXTRACT_MODES
from .contracts import LANES, confirm_after, sender_state
from .store import InsufficientCredits, TopUpConflict
from .topups import ManualTopUp
from .vendor import attest

log = logging.getLogger("lacre_gateway.app")

API_KEY_HEADER = "X-API-Key"
ADMIN_TOKEN_HEADER = "X-Admin-Token"
SIGNATURE_HEADER = "X-Lacre-Signature"
RECIPIENT_HEADER = "X-Lacre-Recipient"
TIMESTAMP_HEADER = "X-Lacre-Timestamp"
# How far a delivery's timestamp may be from the gateway clock, either way.
INBOUND_SKEW_S = 300
MAILBOX_ID = re.compile(r"^[a-z2-7]{12}$")
# Account ids are drawn as mailbox ids are (store.random_id).
ACCOUNT_ID = MAILBOX_ID
SIGNATURE = re.compile(r"^[0-9a-f]{64}$")
PAGE_MAX = 100
LEDGER_MAX = 500


class PaymentRequired(HTTPException):
    """402, with the numbers an agent needs to ask for a top-up."""

    def __init__(self, short):
        super().__init__(status_code=402, detail=(
            "not enough credits: this job holds %d, the balance is %d, %d short"
            % (short.needed, short.credits, short.shortfall)))
        self.short = short


class NewAccount(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    credits: int = Field(0, ge=0)


class NewTopUp(BaseModel):
    credits: int = Field(ge=1)
    external_ref: str = Field(min_length=1, max_length=200)
    note: str | None = Field(None, max_length=500)


def inbound_mac(secret, timestamp, recipient, raw):
    """The hex HMAC-SHA256 the Worker sends in X-Lacre-Signature.

    The timestamp and the recipient are signed with the body. A MAC over the
    body alone would let whoever saw one request send it again with a fresh
    timestamp once the replay window had passed, or to another mailbox.
    """
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(("%s\n%s\n" % (timestamp, recipient)).encode("ascii"))
    mac.update(raw)
    return mac.hexdigest()


def mailbox_address(mailbox_id, domain):
    return "lacre-%s@%s" % (mailbox_id, domain)


def mailbox_view(row, domain):
    return {
        "id": row["id"],
        "address": mailbox_address(row["id"], domain),
        "extract": row["extract_mode"],
        "enabled": bool(row["enabled"]),
        "received": row["received"],
        "dropped": row["dropped"],
        "last_received_at": iso(row["last_received_at"]),
        "created_at": iso(row["created_at"]),
        "disabled_at": iso(row["disabled_at"]),
        "jobs": "/mailboxes/%s/jobs" % (row["id"],),
    }


def cost_view(job):
    """What a job holds and, once it ended, what of that was charged and
    what released. Both are 0 while the job is open."""
    return {"held": job["held_attest"] + job["held_extract"],
            "charged": job["charged"] or 0, "released": job["released"] or 0}


def account_view(account, usage, settings):
    jobs = usage["jobs"]
    return {
        "id": account["id"],
        "name": account["name"],
        "enabled": bool(account["enabled"]),
        "unlimited": bool(account["unlimited"]),
        # Available now: what open jobs hold is already taken out.
        "credits": account["credits"],
        "held": usage["held"],
        "charged": usage["charged"],
        "prices": {"attest": settings.price_attest, "extract": settings.price_extract},
        "counts": {
            "jobs": sum(jobs.values()),
            "open": jobs["pending"] + jobs["attesting"] + jobs["extracting"],
            "finalized": jobs["finalized"],
            "refused": jobs["refused"],
            "failed": jobs["failed"],
            "mailboxes": usage["mailboxes"],
        },
        "created_at": iso(account["created_at"]),
        "disabled_at": iso(account["disabled_at"]),
    }


def ledger_view(row):
    return {"id": row["id"], "delta": row["delta"], "kind": row["kind"],
            "job_id": row["job_id"], "topup_id": row["topup_id"], "note": row["note"],
            "created_at": iso(row["created_at"])}


def topup_view(row):
    return {"id": row["id"], "account_id": row["account_id"], "credits": row["credits"],
            "source": row["source"], "external_ref": row["external_ref"], "note": row["note"],
            "created_at": iso(row["created_at"])}


def extraction_view(job, chain):
    """The "extraction" object of a job, or None when none was asked for."""
    mode = job["extract_mode"] or "none"
    if mode == "none":
        return None
    if job["ext_status"]:
        status = job["ext_status"]
    elif job["status"] == "extracting":
        status = "in progress"
    elif job["status"] in ("pending", "attesting"):
        status = "waiting for the attestation"
    else:
        # The attestation was refused or failed, so nothing was extracted.
        status = "not run"
    last_tx = job["ext_tx_ids"][-1] if job["ext_tx_ids"] else None
    body = {
        "requested": mode,
        "status": status,
        "lane": job["ext_lane"],
        "extractor": job["extractor"],
        "attempts": job["ext_attempts"],
        "consensus_tx": last_tx,
        "consensus_txs": [{"tx": tx, "explorer": chain.explorer(tx)}
                          for tx in job["ext_tx_ids"]],
        "tx_status": job["ext_tx_status"],
        "record_id": job["ext_record_id"],
    }
    if job["ext_record_id"] is not None:
        body["record"] = "/extractions/%s/%s" % (job["ext_lane"], job["ext_record_id"])
        body.update(job["ext_record"] or {})
    # "reason" is a stored field of the record; why the gateway stopped is
    # kept under other names so the two never collide.
    note = {"skipped": "skipped_reason", "refused": "refusal_reason",
            "failed": "error", "no match": "error"}.get(status)
    if note:
        body[note] = job["ext_note"]
    return body


def iso(seconds):
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def key_refusal(account):
    """(status, detail) when a request with this account is refused, else None."""
    if account is None:
        return 401, "missing or unknown API key"
    if not account["enabled"]:
        return 403, "this account is disabled"
    return None


class McpEndpoint:
    """/mcp: the key check of the API, then the MCP session manager.

    The tools reach the API through the app itself, in process, with the
    key of the request they answer, so a key sees over MCP exactly what it
    sees over REST and a remote agent needs nothing configured but its key.
    """

    def __init__(self, store, session_manager):
        self.store = store
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        given = Request(scope).headers.get(API_KEY_HEADER, "")
        refused = key_refusal(accounts.authenticate(self.store, given))
        if refused:
            response = JSONResponse(status_code=refused[0], content={"detail": refused[1]})
            await response(scope, receive, send)
            return
        await self.session_manager.handle_request(scope, receive, send)


def create_app(settings, store, blobs, contracts, worker=None, clock=None, bodies=None):
    import time

    clock = clock or time.time
    bodies = bodies or BlobStore(settings.body_dir, settings.body_url, BODY)
    accounts.bootstrap(store, settings.api_keys, settings.bootstrap_credits)
    manual = ManualTopUp(store)

    def in_process(ctx):
        # McpEndpoint has checked this key; the API checks it again on every
        # call the tool makes.
        given = (ctx.headers or {}).get(API_KEY_HEADER, "")
        return Gateway("http://gateway", given,
                       transport=httpx.ASGITransport(app=app, raise_app_exceptions=False))

    mcp = build_server(in_process)
    mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # No session state: each request carries its key, and a long
        # lacre_wait_job holds only its own request open.
        stateless_http=True,
        # The Host is lacre.in-sidr.xyz behind the tunnel. Rebinding
        # protection guards servers that answer without credentials; this
        # one answers nothing without X-API-Key, which a page in a browser
        # cannot send to another origin.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        # A message as base64 inside JSON is about 4/3 of its size.
        max_request_body_size=2 * settings.max_eml_bytes + 65536)
    session_manager = mcp.session_manager

    app = FastAPI(title="Lacre gateway", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lambda app: session_manager.run())
    endpoint = McpEndpoint(store, session_manager)
    # Both spellings are routed so that neither is answered with a redirect,
    # which some MCP clients do not follow on POST.
    app.router.routes.append(Route("/mcp", endpoint=endpoint, name="mcp"))
    app.router.routes.append(Route("/mcp/", endpoint=endpoint, name="mcp_slash"))
    chain = contracts.chain

    def require_key(request: Request):
        """The caller's account; 401 for an unknown key, 403 when disabled."""
        account = accounts.authenticate(store, request.headers.get(API_KEY_HEADER, ""))
        refused = key_refusal(account)
        if refused:
            raise HTTPException(status_code=refused[0], detail=refused[1])
        return account

    def require_admin(request: Request):
        if not settings.admin_token:
            raise HTTPException(status_code=503, detail="the admin API is not configured")
        given = request.headers.get(ADMIN_TOKEN_HEADER, "")
        if not hmac.compare_digest(given.encode(), settings.admin_token.encode()):
            raise HTTPException(status_code=401, detail="missing or wrong admin token")

    def extract_mode(extract):
        mode = (extract or settings.extract_default).strip().lower()
        if mode not in EXTRACT_MODES:
            raise HTTPException(status_code=422, detail="extract must be one of %s"
                                % (", ".join(EXTRACT_MODES),))
        return mode

    @app.exception_handler(ChainUnavailable)
    async def chain_down(request, error):
        return JSONResponse(status_code=503, content={"detail": "chain unavailable"})

    @app.exception_handler(PaymentRequired)
    async def payment_required(request, error):
        short = error.short
        return JSONResponse(status_code=402, content={
            "detail": error.detail, "needed": short.needed, "credits": short.credits,
            "shortfall": short.shortfall})

    @app.post("/attest", status_code=202)
    async def post_attest(eml: UploadFile = File(...), extract: str | None = Form(None),
                          account: dict = Depends(require_key)):
        try:
            mode = extract_mode(extract)
        except HTTPException:
            await eml.close()
            raise
        raw = await eml.read(settings.max_eml_bytes + 1)
        await eml.close()
        if len(raw) > settings.max_eml_bytes:
            raise HTTPException(status_code=413, detail="the message is too large")
        return create_job(raw, mode, account["id"])

    def create_job(raw, mode, account_id, via="api", mailbox=None):
        """The job stub for one message: the one path an upload and a
        delivery to a mailbox both take, so the worker cannot tell them apart.

        The hold is taken after parsing, because only then is it known
        whether the extraction can run, and before anything is staged, so a
        job the balance cannot cover leaves nothing behind.
        """
        body = skipped = None
        try:
            chosen = headers.select(raw)
            if mode != "none":
                body, skipped = headers.extraction_body(raw, chosen)
        except headers.UnusableMail as error:
            raise HTTPException(status_code=422, detail=str(error))
        finally:
            # Every unsigned header goes no further than this, and the body
            # only to its staging file when an extraction will use it.
            raw = None
        extracts = mode != "none" and skipped is None
        sender = store.sender(chosen.domain, chosen.selector)
        try:
            job_id = store.create_job(
                sender["id"], chosen.headers_sha256, chosen.bh, chosen.body_hash_ok,
                extract_mode=mode, skipped=skipped, via=via, mailbox=mailbox,
                account_id=account_id, held_attest=settings.price_attest,
                held_extract=settings.price_extract if extracts else 0)
        except InsufficientCredits as short:
            body = None
            raise PaymentRequired(short) from None
        try:
            blobs.stage(job_id, chosen.blob)
            if body is not None:
                bodies.stage(job_id, body)
        except OSError:
            blobs.delete(job_id)
            bodies.delete(job_id)
            store.update_job(job_id, status="failed", stage="stopped",
                             error="the message could not be stored")
            raise HTTPException(status_code=500, detail="the message could not be stored")
        finally:
            body = None
        return {"job_id": job_id, "status": "pending", "job": "/jobs/%s" % (job_id,),
                "extract": mode}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, account: dict = Depends(require_key)):
        job = store.job(job_id)
        # Another account's job is answered exactly as a missing one, as
        # for mailboxes, so a key cannot learn which job ids exist. A job
        # from before accounts has none and no key sees it; its record stays
        # readable through /records, which is public chain state.
        if job is None or job["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="no such job")
        return job_view(job)

    def job_view(job):
        sender = store.sender_by_id(job["sender_id"])
        last_tx = job["tx_ids"][-1] if job["tx_ids"] else None
        body = {
            "id": job["id"],
            "status": job["status"],
            "stage": job["stage"],
            "sender": {"domain": sender["domain"], "selector": sender["selector"]},
            "created_at": iso(job["created_at"]),
            "updated_at": iso(job["updated_at"]),
            "submitted_at": iso(job["submitted_at"]),
            "decided_at": iso(job["decided_at"]),
            "finished_at": iso(job["finished_at"]),
            "attempts": job["attempts"],
            "consensus_tx": last_tx,
            "explorer": chain.explorer(last_tx) if last_tx else None,
            "consensus_txs": [{"tx": tx, "explorer": chain.explorer(tx)}
                              for tx in job["tx_ids"]],
            "tx_status": job["tx_status"],
            "headers_sha256": job["headers_sha256"],
            "body_hash_matches": (None if job["body_hash_ok"] is None
                                  else bool(job["body_hash_ok"])),
            "via": job["via"] or "api",
            "mailbox": job["mailbox"],
            "cost": cost_view(job),
        }
        if job["stage"] == "sender in verification":
            body["sender_confirm_after"] = iso(job["confirm_after"])
        if job["record_id"] is not None:
            # A record id means nothing without its Verifier (rule 4).
            body["record_id"] = job["record_id"]
            body["verifier"] = job["verifier"]
            body["record"] = "/records/%s?verifier=%s" % (job["record_id"], job["verifier"])
            # Rule 2: finalized means the check ran; this is how it came out.
            body["valid_and_aligned"] = bool(job["valid_aligned"])
        if job["status"] == "refused":
            body["refusal_reason"] = job["refusal_reason"]
        if job["status"] == "failed":
            body["error"] = job["error"]
        body["extraction"] = extraction_view(job, chain)
        return body

    # ---- mailboxes -------------------------------------------------------------

    def owned_mailbox(mailbox_id, account):
        # Another account's mailbox is answered exactly as a missing one, so
        # a key cannot learn which ids exist.
        row = store.mailbox(mailbox_id) if MAILBOX_ID.match(mailbox_id) else None
        if row is None or row["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="no such mailbox")
        return row

    @app.post("/mailboxes", status_code=201)
    def post_mailbox(extract: str | None = Form(None), account: dict = Depends(require_key)):
        row = store.create_mailbox(account["id"], extract_mode(extract))
        log.info("mailbox %s created", row["id"])
        return mailbox_view(row, settings.mail_domain)

    @app.get("/mailboxes")
    def list_mailboxes(account: dict = Depends(require_key)):
        return {"mailboxes": [mailbox_view(r, settings.mail_domain)
                              for r in store.mailboxes(account["id"])]}

    @app.get("/mailboxes/{mailbox_id}")
    def get_mailbox(mailbox_id: str, account: dict = Depends(require_key)):
        return mailbox_view(owned_mailbox(mailbox_id, account), settings.mail_domain)

    @app.delete("/mailboxes/{mailbox_id}")
    def delete_mailbox(mailbox_id: str, account: dict = Depends(require_key)):
        # Disabled, not deleted: its jobs keep pointing at it and the
        # counters go on counting what is dropped there.
        owned_mailbox(mailbox_id, account)
        store.disable_mailbox(mailbox_id)
        log.info("mailbox %s disabled", mailbox_id)
        return mailbox_view(store.mailbox(mailbox_id), settings.mail_domain)

    @app.get("/mailboxes/{mailbox_id}/jobs")
    def mailbox_jobs(mailbox_id: str, account: dict = Depends(require_key),
                     limit: int = Query(20, ge=1, le=PAGE_MAX), offset: int = Query(0, ge=0)):
        owned_mailbox(mailbox_id, account)
        # One more than asked tells whether there is a next page without a
        # second query.
        jobs = store.mailbox_jobs(mailbox_id, limit + 1, offset)
        more = len(jobs) > limit
        return {
            "jobs": [job_view(j) for j in jobs[:limit]],
            "limit": limit,
            "offset": offset,
            "next": ("/mailboxes/%s/jobs?limit=%d&offset=%d" % (mailbox_id, limit, offset + limit)
                     if more else None),
        }

    # ---- inbound mail ----------------------------------------------------------

    async def read_capped(request):
        """The request body, or 413 once it passes LACRE_MAX_EML_BYTES,
        without reading the rest."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_eml_bytes:
            raise HTTPException(status_code=413, detail="the message is too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_eml_bytes:
                raise HTTPException(status_code=413, detail="the message is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/inbound", status_code=202)
    async def post_inbound(request: Request):
        if not settings.inbound_secret:
            raise HTTPException(status_code=503, detail="inbound mail is not configured")
        raw = await read_capped(request)
        given = request.headers.get(SIGNATURE_HEADER, "").strip().lower()
        recipient = request.headers.get(RECIPIENT_HEADER, "").strip()
        stamp = request.headers.get(TIMESTAMP_HEADER, "").strip()
        if not SIGNATURE.match(given) or not recipient or not stamp.isdigit():
            raise HTTPException(status_code=401, detail="missing or malformed signature headers")
        expected = inbound_mac(settings.inbound_secret, stamp, recipient, raw)
        if not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="bad signature")
        # Checked after the MAC, so the timestamp compared is one the Worker
        # signed and not one an attacker chose.
        if abs(clock() - int(stamp)) > INBOUND_SKEW_S:
            raise HTTPException(status_code=401, detail="timestamp outside the allowed window")
        # A timestamp is accepted up to INBOUND_SKEW_S ahead of the clock and
        # stays fresh INBOUND_SKEW_S after it, so a signature is kept twice
        # that long.
        if not store.remember_signature(given, 2 * INBOUND_SKEW_S):
            raise HTTPException(status_code=409, detail="this delivery was already taken")

        local, _, domain = recipient.lower().partition("@")
        mailbox_id = local[len("lacre-"):] if local.startswith("lacre-") else ""
        if domain != settings.mail_domain or not MAILBOX_ID.match(mailbox_id):
            store.count_unknown_inbound()
            log.info("inbound: not a mailbox address, dropped")
            raise HTTPException(status_code=404, detail="no such mailbox")
        row = store.mailbox(mailbox_id)
        if row is None:
            store.count_unknown_inbound()
            log.info("inbound: unknown mailbox, dropped")
            raise HTTPException(status_code=404, detail="no such mailbox")
        holder = store.account(row["account_id"]) if row["account_id"] else None
        # A disabled account's mailboxes take nothing, as a disabled mailbox.
        if not row["enabled"] or holder is None or not holder["enabled"]:
            store.count_inbound(mailbox_id, received=False)
            log.info("inbound: mailbox %s disabled, dropped", mailbox_id)
            raise HTTPException(status_code=404, detail="no such mailbox")
        try:
            # A 402 lands in the except below too: the delivery is dropped
            # and counted, the Worker bounces it, and nothing of it is kept.
            stub = create_job(raw, row["extract_mode"], holder["id"], via="inbound",
                              mailbox=mailbox_id)
        except HTTPException:
            store.count_inbound(mailbox_id, received=False)
            log.info("inbound: mailbox %s, message not accepted, dropped", mailbox_id)
            raise
        finally:
            raw = None
        store.count_inbound(mailbox_id, received=True)
        log.info("inbound: mailbox %s, job %s", mailbox_id, stub["job_id"])
        return stub

    # ---- the caller's account ----------------------------------------------------

    @app.get("/account")
    def get_account(account: dict = Depends(require_key)):
        return account_view(account, store.usage(account["id"]), settings)

    @app.post("/account/rotate-key")
    def rotate_key(account: dict = Depends(require_key)):
        # The account keeps its id, balance, mailboxes and jobs; only the
        # digest it is found by changes, so the old key stops at once.
        key = accounts.new_key()
        store.update_account(account["id"], key_sha256=accounts.digest(key))
        log.info("account %s: key rotated", account["id"])
        fresh = store.account(account["id"])
        return {"api_key": key,
                "account": account_view(fresh, store.usage(fresh["id"]), settings)}

    # ---- admin -------------------------------------------------------------------

    def admin_account(account_id):
        row = store.account(account_id) if ACCOUNT_ID.match(account_id) else None
        if row is None:
            raise HTTPException(status_code=404, detail="no such account")
        return row

    def admin_view(row):
        return dict(account_view(row, store.usage(row["id"]), settings),
                    bootstrap=row["bootstrap_sha256"] is not None)

    @app.post("/admin/accounts", status_code=201, dependencies=[Depends(require_admin)])
    def admin_create_account(new: NewAccount):
        name = new.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be blank")
        key = accounts.new_key()
        row = store.create_account(name, accounts.digest(key), credits=new.credits)
        log.info("account %s created", row["id"])
        # The only time the key is shown; the gateway keeps its digest.
        return {"api_key": key, "account": admin_view(row)}

    @app.get("/admin/accounts", dependencies=[Depends(require_admin)])
    def admin_list_accounts():
        return {"accounts": [admin_view(r) for r in store.accounts()]}

    @app.get("/admin/accounts/{account_id}", dependencies=[Depends(require_admin)])
    def admin_get_account(account_id: str, limit: int = Query(50, ge=1, le=LEDGER_MAX)):
        row = admin_account(account_id)
        return dict(admin_view(row),
                    ledger=[ledger_view(e) for e in store.ledger(row["id"], limit)])

    @app.post("/admin/accounts/{account_id}/topups", dependencies=[Depends(require_admin)])
    def admin_topup(account_id: str, new: NewTopUp):
        row = admin_account(account_id)
        try:
            topup, created = manual.credit(row["id"], new.credits, new.external_ref.strip(),
                                           new.note)
        except TopUpConflict as error:
            raise HTTPException(status_code=409, detail=str(error))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error))
        if created:
            log.info("account %s: topup %s", row["id"], topup["id"])
        # 200 for an event already credited, so a retried request is told
        # apart from a new top-up without a second credit.
        return JSONResponse(status_code=201 if created else 200, content={
            "topup": topup_view(topup), "created": created,
            "account": admin_view(store.account(row["id"]))})

    @app.post("/admin/accounts/{account_id}/disable", dependencies=[Depends(require_admin)])
    def admin_disable(account_id: str):
        row = admin_account(account_id)
        store.set_enabled(row["id"], False)
        log.info("account %s disabled", row["id"])
        return admin_view(store.account(row["id"]))

    @app.post("/admin/accounts/{account_id}/enable", dependencies=[Depends(require_admin)])
    def admin_enable(account_id: str):
        row = admin_account(account_id)
        store.set_enabled(row["id"], True)
        log.info("account %s enabled", row["id"])
        return admin_view(store.account(row["id"]))

    @app.get("/records/{record_id}", dependencies=[Depends(require_key)])
    def get_record(record_id: str, verifier: str | None = None):
        if not record_id.isdigit():
            raise HTTPException(status_code=400, detail="a record id is decimal digits")
        current = contracts.verifier()
        address = current
        if verifier and verifier.lower() != current.lower():
            # Only an address the Router has named "verifier" is read, never
            # one taken from the request alone.
            known = {a.lower(): a for a in contracts.verifier_history()}
            if verifier.lower() not in known:
                raise HTTPException(status_code=400,
                                    detail="not a Verifier the Router has named")
            address = known[verifier.lower()]
        record = contracts.record(address, record_id)
        if not record:
            raise HTTPException(status_code=404, detail="no such record at LATEST_FINAL")
        return {
            "verifier": address,
            "current_verifier": address.lower() == current.lower(),
            "record_id": record_id,
            "read_at": "LATEST_FINAL",
            # Rule 2: a record says the check ran; this says how it came out.
            "valid_and_aligned": bool(record.get("valid")) and bool(record.get("aligned")),
            "record": record,
            "extractions": extractions_of(address, record_id),
        }

    def extractions_of(verifier, record_id):
        """Extraction records of the gateway's wallet on both lanes that
        read this Verifier record, at LATEST_FINAL.

        Ids are local to a Verifier, so a record naming the same id on
        another Verifier is not one of these.
        """
        requester = chain.requester
        if not requester:
            return []
        found = []
        for lane in LANES:
            extractor = contracts.extractor(lane)
            if not extractor:
                continue
            for ext_id in contracts.records_of(extractor, requester):
                held = contracts.extraction(extractor, ext_id)
                if (str(held.get("record_id", "")) == record_id
                        and str(held.get("verifier", "")).lower() == verifier.lower()):
                    found.append({"lane": lane, "extractor": extractor, "id": ext_id,
                                  "record": held})
        return found

    @app.get("/extractions/{lane}/{ext_id}", dependencies=[Depends(require_key)])
    def get_extraction(lane: str, ext_id: str):
        if lane not in LANES:
            raise HTTPException(status_code=400, detail="a lane is patterns or llm")
        if not ext_id.isdigit():
            raise HTTPException(status_code=400, detail="a record id is decimal digits")
        extractor = contracts.extractor(lane)
        if not extractor:
            raise HTTPException(status_code=404, detail="the Router resolves no %s"
                                % (LANES[lane],))
        record = contracts.extraction(extractor, ext_id)
        if not record:
            raise HTTPException(status_code=404, detail="no such record at LATEST_FINAL")
        return {"lane": lane, "extractor": extractor, "id": ext_id, "read_at": "LATEST_FINAL",
                "record": record}

    @app.get("/senders/{domain}/{selector}", dependencies=[Depends(require_key)])
    def get_sender(domain: str, selector: str):
        name = attest.normalize(domain, attest.MAX_DOMAIN)
        label = attest.normalize(selector, attest.MAX_LABEL)
        if not name or not label:
            raise HTTPException(status_code=400, detail="bad domain or selector")
        status = contracts.key_status(name, label)
        state = sender_state(status)
        body = {"domain": name, "selector": label, "state": state}
        if status:
            for field in ("key_bits", "key_sha256", "first_seen", "activated_at",
                          "refreshed_at"):
                body[field] = status.get(field) or None
        if state == "pending":
            when = confirm_after(status, settings.key_quarantine_s)
            body["confirm_after"] = iso(when)
            body["can_confirm_now"] = clock() >= when
        return body

    @app.get("/health", dependencies=[Depends(require_key)])
    def health():
        checks = {"chain": False, "router": False, "worker": False}
        checks["chain"] = chain.ping()
        try:
            contracts.verifier()
            contracts.keycache()
            checks["router"] = True
        except ChainUnavailable:
            pass
        beat = store.heartbeat()
        checks["worker"] = bool(worker and worker.alive() and beat is not None
                                and clock() - beat < settings.worker_stale_s)
        body = dict(checks, signer="configured" if chain.can_sign else "absent",
                    network=settings.network, last_worker_pass=iso(beat),
                    inbound="configured" if settings.inbound_secret else "off",
                    inbound_unknown_dropped=store.unknown_inbound())
        return JSONResponse(status_code=200 if all(checks.values()) else 503, content=body)

    @app.get("/h/{token}")
    def served_headers(token: str):
        blob = blobs.read(token)
        if blob is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(content=blob, media_type="text/plain",
                        headers={"Cache-Control": "no-store"})

    @app.get("/b/{name}.bin")
    def served_body(name: str):
        # The same path as /h/: only a name the store issued and still
        # serves is read, byte for byte as it was uploaded.
        body = bodies.read(name)
        if body is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(content=body, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    return app
