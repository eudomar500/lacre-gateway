"""The HTTP API.

Every endpoint takes an API key in the X-API-Key header. The one path that
does not is /h/{token}, which is not part of the API: it is where the
validators fetch the signed headers of a call in flight, from the URL in
that call's public calldata, and it serves nothing once the job is over.
"""

import datetime
import hmac

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import headers
from .chainio import ChainUnavailable
from .contracts import confirm_after, sender_state
from .vendor import attest

API_KEY_HEADER = "X-API-Key"


def iso(seconds):
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app(settings, store, blobs, contracts, worker=None, clock=None):
    import time

    clock = clock or time.time
    app = FastAPI(title="Lacre gateway", docs_url=None, redoc_url=None, openapi_url=None)
    chain = contracts.chain

    def require_key(request: Request):
        given = request.headers.get(API_KEY_HEADER, "")
        ok = False
        for key in settings.api_keys:
            # Every key is compared, so timing does not tell which one is near.
            ok |= hmac.compare_digest(given.encode(), key.encode())
        if not given or not ok:
            raise HTTPException(status_code=401, detail="missing or unknown API key")

    @app.exception_handler(ChainUnavailable)
    async def chain_down(request, error):
        return JSONResponse(status_code=503, content={"detail": "chain unavailable"})

    @app.post("/attest", status_code=202, dependencies=[Depends(require_key)])
    async def post_attest(eml: UploadFile = File(...)):
        raw = await eml.read(settings.max_eml_bytes + 1)
        await eml.close()
        if len(raw) > settings.max_eml_bytes:
            raise HTTPException(status_code=413, detail="the message is too large")
        try:
            chosen = headers.select(raw)
        except headers.UnusableMail as error:
            raise HTTPException(status_code=422, detail=str(error))
        finally:
            # The body and every unsigned header go no further than this.
            raw = None
        sender = store.sender(chosen.domain, chosen.selector)
        job_id = store.create_job(sender["id"], chosen.headers_sha256, chosen.bh,
                                  chosen.body_hash_ok)
        try:
            blobs.stage(job_id, chosen.blob)
        except OSError:
            store.update_job(job_id, status="failed", stage="stopped",
                             error="the headers could not be stored")
            raise HTTPException(status_code=500, detail="the headers could not be stored")
        return {"job_id": job_id, "status": "pending", "job": "/jobs/%s" % (job_id,)}

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
    def get_job(job_id: str):
        job = store.job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
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
        return body

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
        }

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
                    network=settings.network, last_worker_pass=iso(beat))
        return JSONResponse(status_code=200 if all(checks.values()) else 503, content=body)

    @app.get("/h/{token}")
    def served_headers(token: str):
        blob = blobs.read(token)
        if blob is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(content=blob, media_type="text/plain",
                        headers={"Cache-Control": "no-store"})

    return app
