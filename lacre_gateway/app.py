"""The HTTP API.

Every endpoint takes an API key in the X-API-Key header. The two paths that
do not, /h/{token} and /b/{name}.bin, are not part of the API: they are
where the validators fetch the signed headers of an attest call and the body
of an extract call in flight, from the URL in that call's public calldata,
and they serve nothing once the call is over.
"""

import datetime
import hmac

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import headers
from .blobs import BODY, BlobStore
from .chainio import ChainUnavailable
from .config import EXTRACT_MODES
from .contracts import LANES, confirm_after, sender_state
from .vendor import attest

API_KEY_HEADER = "X-API-Key"


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


def create_app(settings, store, blobs, contracts, worker=None, clock=None, bodies=None):
    import time

    clock = clock or time.time
    bodies = bodies or BlobStore(settings.body_dir, settings.body_url, BODY)
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
    async def post_attest(eml: UploadFile = File(...), extract: str | None = Form(None)):
        mode = (extract or settings.extract_default).strip().lower()
        if mode not in EXTRACT_MODES:
            await eml.close()
            raise HTTPException(status_code=422, detail="extract must be one of %s"
                                % (", ".join(EXTRACT_MODES),))
        raw = await eml.read(settings.max_eml_bytes + 1)
        await eml.close()
        if len(raw) > settings.max_eml_bytes:
            raise HTTPException(status_code=413, detail="the message is too large")
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
        sender = store.sender(chosen.domain, chosen.selector)
        job_id = store.create_job(sender["id"], chosen.headers_sha256, chosen.bh,
                                  chosen.body_hash_ok, extract_mode=mode, skipped=skipped)
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
        body["extraction"] = extraction_view(job, chain)
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
                    network=settings.network, last_worker_pass=iso(beat))
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
