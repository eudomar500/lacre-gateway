# Lacre gateway

The gateway takes an email as an .eml upload, attests its DKIM signature on
GenLayer Testnet Bradbury through the public Lacre contracts, and then, by
default, extracts what the body says through one of the two Extractors. One
POST gives an agent the verified sender and the extracted fields. It is the private product layer over the public repository
[lacre](https://github.com/eudomar500/lacre), which is pinned here as the
submodule `vendor/lacre` and is where every chain rule comes from
(`vendor/lacre/docs/interfaces.md`, section 5). The gateway imports that
repository's tools rather than copying them:

| from vendor/lacre | used for |
|-------------------|----------|
| `tools/chain.py` | network table, reads (`chain.read`), calldata, gas and broadcast |
| `tools/attest.py` | the send with nonce pinning (`Session.send`), the verdict (`judge`), record matching (`ours`), the outcome of an extract call (`snapshot`, `outcome`), the Verifier limits |
| `tools/extract.py` | the Extractor lanes and their Router names (`LANES`), `resolve`, the pre-send checks (`refusal`), extraction record matching (`ours`) |
| `tools/txstate.py` | the stored consensus status (`stored`), `executed`, `DECIDED`, `APPEAL` |
| `lacre/dkimcore.py` | parsing header fields exactly as the Verifier does |
| `lacre/dkimbody.py` | the body hash, to check `bh=` |

One process runs the HTTP API (FastAPI) and a background worker, over one
SQLite database. It runs as a systemd service on the VPS, behind cloudflared,
at `https://lacre.in-sidr.xyz`.

## What happens to an email

1. **Parse and select.** The gateway picks the DKIM-Signature to attest: the
   one whose `d=` equals the From domain, else one aligned with it, else the
   first. It keeps only the header fields that signature's `h=` lists, and
   for a name listed k times only the last k instances (the ones RFC 6376
   5.4.2 signs), plus the DKIM-Signature itself, first. Everything else is
   dropped: Received, Return-Path, ARC, Authentication-Results, X- headers,
   other signatures. The body is checked against `bh=`. Unless the job asked
   for an extraction it is then discarded; for an extraction the raw octets
   after the first CRLF CRLF are staged, byte-exact, in a private directory
   (see 6). A message with no DKIM-Signature, with `l=` (Verifier v1.2
   refuses it), or whose signed headers are over 16384 bytes is refused
   with 422 and nothing is stored.
2. **Sender key.** The key is read from the KeyCache (resolved through the
   Router) at `LATEST_FINAL`. If it is unknown the worker sends
   `register_key`, and the job shows the stage `sender in verification`.
   A registered key is `pending` for 24 hours; the job then shows
   `sender_confirm_after`. Once that time has passed the worker sends
   `confirm_key`, and when the key reads `active` the job resumes. A
   `rotated` or `retired` key refuses the job without sending anything.
3. **Serve the headers.** Just before the attest call, the kept headers move
   from a private staging directory to `/h/{token}`, a random 256 bit path
   under `LACRE_BLOB_BASE_URL`. The URL is public in calldata from the moment
   the call is submitted, which is why only signed headers are ever served.
   The file is deleted when the job is finalized, refused or failed.
4. **Attest and confirm.** `attest(url, domain, selector)` is sent with
   exactly `fee()`, read just before the send. The worker follows the stored
   status until FINALIZED, reads `records_of(requester)` and the new records
   at `LATEST_FINAL`, and decides with `attest.judge`: recorded, refused
   (the reason from `last_refusal`), send again (only after a finalization
   without execution), or stop. Nothing is sent again while the call is
   undecided, appealed or CANCELED. One call per contract is in flight at a
   time, across all jobs and sender keys.
5. **Persist.** Per job: ids, statuses, tx ids, timestamps, the refusal
   reason, the digest of the served headers and `bh`, and of an extraction
   the fields the Extractor stored on chain.
6. **Extract.** Only when the Verifier record is FINALIZED, valid and
   aligned, and the job asked for it (`extract`, below). The lane is
   chosen: `auto` takes `patterns` when the pattern Extractor (Router name
   `extractor`) holds patterns for the sender domain, else `llm` (Router
   name `extractor_llm`). The Extractor is resolved through the Router and
   must name the same Router and the same Verifier the attest record is on.
   The body moves from staging to `/b/{name}.bin`, 32 hex characters from a
   CSPRNG, the Extractor's own refusal checks are run read-only (a call it
   would refuse is not sent), and `extract(record_id, url)` is sent with
   exactly its `fee()`. The call is confirmed as `tools/extract.py` confirms
   it: `records_of` and `last_refusal` for the gateway's wallet before the
   first attempt and after each one, a record only when one is read back at
   `LATEST_FINAL` whose `record_id` and `fee_paid` are the call's, a new
   attempt only after a finalization without execution (a validator
   TIMEOUT leaves no record), up to three attempts. The body is deleted at
   the end, whatever the end is. Stages after `recorded`: `serving body`,
   `extracting`, `waiting for extraction FINALIZED`, `extracted`; or one of
   `extraction refused`, `extraction did not match`, `extraction stopped`.
   The job's `status` is `finalized` in every one of these, because the
   attest record stands whatever the extraction came to; `extraction.status`
   says what that was.

An extraction is skipped, with `extraction.skipped_reason`, and nothing is
sent for it, when the body is over 262144 bytes (the Extractors' cap), has
no CRLF CRLF before it, does not hash to `bh=` or uses a body
canonicalization other than simple or relaxed (each of these would be a
refusal or a charged record with `match` false), when the Verifier record
is not valid and aligned, or when the Router names another Verifier by the
time the extraction starts.

## Privacy guarantees

- **Only signed headers, and the body when asked for, leave the process.**
  What is served, and therefore what anyone reading the chain can fetch
  while a call is in flight, is the DKIM-Signature and the header fields it
  covers and, for a job with an extraction, the body. No unsigned header is
  ever served. A job with `extract=none` keeps the old guarantee: its body
  is hashed in memory and dropped with the upload.
- **The body of an extraction is public while it is served, and goes to
  the validators.** The URL is in calldata; on the `llm` lane each
  validator also hands the text to its model provider
  (`vendor/lacre/docs/llmextractor.md`). A body carries the recipient's name
  and address and whatever the sender wrote. Send `extract=none` for mail
  that must not leave the gateway beyond its signed headers.
- **Files exist only while needed.** Headers and body are staged mode 600 in
  directories only the service can read, are unreachable until their call is
  sent, and are deleted when that call is FINALIZED, refused or failed, or
  when the job ends for any other reason. The body is written nowhere else
  and never logged.
- **The order number is never kept.** Neither Extractor stores it; the
  pattern lane stores only whether one was found, and the gateway keeps
  only the stored fields.
- **Nothing personal in the database.** No email address, subject, header
  value or body. The only values taken from a message are the signing domain
  and selector (`d=`, `s=`), which are public arguments of every KeyCache and
  Verifier call, and two hashes (`bh`, the SHA-256 of the served headers).
- **Nothing personal in the logs.** The gateway logs job ids, tx ids and
  statuses. The public tools print to stdout for a person at a terminal;
  the service sends their stdout nowhere. The HTTP access log is off,
  because paths carry sender names and blob tokens.
- **No key material in any response or log.** The signing key is read from
  a file only, never from the environment, and is never printed. `/senders`
  returns the key's digest and size, not the key.

## Endpoints

Every endpoint takes the API key in `X-API-Key`. Times are UTC ISO 8601.

| method and path | answer |
|-----------------|--------|
| `POST /attest` | multipart form, field `eml`, optional field `extract` (`none`, `patterns`, `llm`, `auto`; default `LACRE_EXTRACT_DEFAULT`, `auto`). `202 {"job_id", "status": "pending", "job", "extract"}`. 422 for a message that cannot be attested or an unknown `extract`, 413 over `LACRE_MAX_EML_BYTES`. |
| `GET /jobs/{id}` | `status` (`pending`, `attesting`, `extracting`, `finalized`, `refused`, `failed`), `stage`, timestamps, `consensus_tx` and `explorer` once sent (every attempt in `consensus_txs`), `tx_status`, `sender_confirm_after` while the sender is in verification, `record_id`, `verifier` and `valid_and_aligned` when written (a record means the check ran, not that it passed), `refusal_reason` when refused, `error` when failed, `body_hash_matches`, and `extraction` (below; `null` for `extract=none`). |
| `GET /records/{id}` | the record, read from the Verifier at `LATEST_FINAL`. The Verifier is resolved through the Router on every request. `?verifier=` reads an earlier Verifier, only one the Router's history names. The answer carries `verifier` (a record id means nothing without it), `valid_and_aligned`, and `extractions`: the records on both Extractors, found through `records_of` for the gateway's wallet, whose `record_id` is this id and whose `verifier` is this Verifier, each as `{"lane", "extractor", "id", "record"}`. |
| `GET /extractions/{lane}/{id}` | one extraction record, `lane` `patterns` or `llm`, read at `LATEST_FINAL` from the Extractor the Router names for that lane now: `{"lane", "extractor", "id", "read_at", "record"}`. |
| `GET /senders/{domain}/{selector}` | `state`: `active`, `pending` (with `confirm_after` and `can_confirm_now`), `unknown`, `rotated` or `retired`, from the KeyCache at `LATEST_FINAL`. |
| `GET /health` | `chain` (the RPC answers with the right chain id), `router` (it resolves `verifier` and `keycache`), `worker` (alive and passed recently), and `signer` (configured or absent). 200 when all hold, 503 otherwise. |

`GET /h/{token}` and `GET /b/{name}.bin` are not part of the API and take
no key: they serve the headers of an attest call and the body of an extract
call in flight to the validators, from the URL in the call.

The `extraction` object of a job: `requested` (the mode), `status`
(`waiting for the attestation`, `in progress`, `extracted`, `no match`,
`refused`, `failed`, `skipped`, or `not run` when the attestation itself
was refused or failed), `lane`, `extractor` (the address the call went
to), `attempts`, `consensus_tx`, `consensus_txs`, `tx_status`, `record_id`
(the Extractor's record id; ids are local to one Extractor). Once recorded
it adds `record` (the `/extractions` path) and the stored fields `match`,
`shipped`, `eta_day`, `eta_date`, `order_id_found`, `flagged` (llm lane),
`method`, `patterns_sha256` or `prompt_sha256`, `reason` and `signed_at`.
Why the gateway stopped is in `refusal_reason`, `error` or
`skipped_reason`, never in `reason`, which is the record's own field. A
consumer gates on `match` true and `reason` `extracted`, reads
`eta_date` and `order_id_found` only when `method` is `patterns`, and pins
the digest it reviewed, as the Extractor docs require.

A record is final when the job is `finalized`: the gateway never reports one
before its call is FINALIZED. A consumer still decides on the record, with
`check_for` on the Verifier, and reads the key status at decision time, as
`vendor/lacre/docs/interfaces.md` section 5 requires.

## Run locally against the fixtures

```
git submodule update --init
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests use no network (a fixture fails any test that opens a
connection). To try the API by hand, the offline runner serves the real
application over a chain that answers from the recorded fixtures:

```
.venv/bin/python scripts/run_offline.py          # prints an API key
curl -s -H "X-API-Key: $KEY" -F eml=@tests/fixtures/amazon_layout.eml \
    http://127.0.0.1:8080/attest
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8080/jobs/<job_id>
```

`tests/fixtures/amazon_layout.eml` is synthetic: it has the layout of an
Amazon order confirmation, with example.org addresses and placeholder
signatures that the tests replace with signatures from a key generated at
test time. `tests/make_amazon_layout.py` writes it. The consensus fixtures
are copied unchanged from `vendor/lacre/tests/fixtures`.

## Deploy

On the VPS, as root:

```
useradd --system --home /opt/lacre-gateway --shell /usr/sbin/nologin lacre
git clone --recurse-submodules <this repository> /opt/lacre-gateway
cd /opt/lacre-gateway
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

install -d -m 0750 -o root -g lacre /etc/lacre-gateway
install -m 0640 -o root -g lacre deploy/gateway.env.example /etc/lacre-gateway/gateway.env
install -m 0600 -o root -g root /dev/null /etc/lacre-gateway/signing.key
```

Edit `/etc/lacre-gateway/gateway.env` (API keys, Router, blob base URL).
Put the signing key, `0x` and 64 hex characters, into
`/etc/lacre-gateway/signing.key` with an editor running as root, so that it
never passes through a shell variable, a command line or shell history. The
file stays root-owned and mode 600; the unit hands it to the service through
`LoadCredential`, into a directory only the service can read. Then:

```
cp deploy/lacre-gateway.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now lacre-gateway
```

The service listens on 127.0.0.1:8080. `deploy/cloudflared.yml.example` is
the tunnel config that publishes it at `https://lacre.in-sidr.xyz`, with
`/h/{token}`, `/b/{name}.bin` and the API paths only. Check with
`curl -H "X-API-Key: ..." https://lacre.in-sidr.xyz/health`.

The gateway serves bodies itself, at `/b/{name}.bin` on the same port as
everything else, from its own data directory. The Caddy site on :8091 that
served `/b/` from `/var/www/lacre` is no longer used and can be removed on the VPS,
together with that directory, once the tunnel config above is in place.

The account behind the key pays each attest's `fee()` and L2 gas, and must
be able to receive GEN (refunds of refused and non-executed calls come back
to it). An extraction costs the same wallet more: one `extract` transaction
per attempt, carrying the Extractor's `fee()` plus its L2 gas, up to three
attempts when validators time out. A record with `match` false keeps the fee,
as the Extractors document; a refusal and a finalization without execution
are refunded, the L2 gas is not. Send `extract=none` or set
`LACRE_EXTRACT_DEFAULT=none` to attest only.

ConsensusMain runs one transaction per contract at a time
(`vendor/lacre/docs/interfaces.md`, section 5, rule 13). The worker keeps
one slot per contract, for the Verifier, the KeyCache and each Extractor
separately, and a job never sends to a contract while another call of the
gateway's is in flight there. An Extractor's slot is held for the whole
extraction, from serving the body until the job ends, because
`last_refusal` is per wallet and the confirmation must not read another
job's refusal as its own. Extractions therefore go out one at a time per
lane, each taking at least one finalization (about 30 minutes on
Bradbury).

To update the public tools, move the submodule to a new commit of
`lacre` main, run the tests, and restart the service. Nothing under
`vendor/lacre` is ever edited here.

## Configuration

Environment only (see `deploy/gateway.env.example`):
`LACRE_NETWORK`, `LACRE_ROUTER`, `LACRE_API_KEYS`, `LACRE_BLOB_BASE_URL`,
`LACRE_BODY_BASE_URL` (default: `LACRE_BLOB_BASE_URL` with its last `/h`
made `/b`), `LACRE_EXTRACT_DEFAULT` (`auto`), `LACRE_DATA_DIR`, `LACRE_SIGNING_KEY_FILE` (a path; without it the gateway
reads but sends nothing), and the delays `LACRE_POLL_S`,
`LACRE_FINAL_BOUND_S`, `LACRE_MAX_ATTEMPTS`, `LACRE_MAX_SEND_FAILURES`,
`LACRE_KEY_QUARANTINE_S`, `LACRE_CONFIRM_MARGIN_S`, `LACRE_CONFIRM_RETRY_S`,
`LACRE_WORKER_STALE_S`, `LACRE_MAX_EML_BYTES`. `LACRE_MAX_ATTEMPTS`,
`LACRE_MAX_SEND_FAILURES` and `LACRE_FINAL_BOUND_S` apply to extract calls as
they do to attest calls. No Extractor address is configured: `extractor` and
`extractor_llm` are resolved on the Router each time one is needed.

## Out of scope

- Billing, accounts and quotas beyond static API keys.
- An MCP server.
- A web front end.
