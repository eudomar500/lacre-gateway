# Lacre gateway

The gateway takes an email as an .eml upload, attests its DKIM signature on
GenLayer Testnet Bradbury through the public Lacre contracts, and returns
the record. It is the private product layer over the public repository
[lacre](https://github.com/eudomar500/lacre), which is pinned here as the
submodule `vendor/lacre` and is where every chain rule comes from
(`vendor/lacre/docs/interfaces.md`, section 5). The gateway imports that
repository's tools rather than copying them:

| from vendor/lacre | used for |
|-------------------|----------|
| `tools/chain.py` | network table, reads (`chain.read`), calldata, gas and broadcast |
| `tools/attest.py` | the send with nonce pinning (`Session.send`), the verdict (`judge`), record matching (`ours`), the Verifier limits |
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
   other signatures. The body is used once, to check it against `bh=`, and
   is then discarded. A message with no DKIM-Signature, with `l=` (Verifier
   v1.2 refuses it), or whose signed headers are over 16384 bytes is
   refused with 422 and nothing is stored.
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
   reason, the digest of the served headers and `bh`.

## Privacy guarantees

- **Only signed headers leave the process.** What is served, and therefore
  what anyone reading the chain can fetch while a call is in flight, is the
  DKIM-Signature and the header fields it covers, nothing else.
- **The body is never stored or served.** It is hashed to check `bh=` in
  memory and dropped with the upload.
- **Headers are served only while needed.** They are unreachable until the
  attest call is sent and are deleted as soon as the job ends, whatever the
  outcome.
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
| `POST /attest` | multipart form, field `eml`. `202 {"job_id", "status": "pending", "job"}`. 422 for a message that cannot be attested, 413 over `LACRE_MAX_EML_BYTES`. |
| `GET /jobs/{id}` | `status` (`pending`, `attesting`, `finalized`, `refused`, `failed`), `stage`, timestamps, `consensus_tx` and `explorer` once sent (every attempt in `consensus_txs`), `tx_status`, `sender_confirm_after` while the sender is in verification, `record_id`, `verifier` and `valid_and_aligned` when written (a record means the check ran, not that it passed), `refusal_reason` when refused, `error` when failed, `body_hash_matches`. |
| `GET /records/{id}` | the record, read from the Verifier at `LATEST_FINAL`. The Verifier is resolved through the Router on every request. `?verifier=` reads an earlier Verifier, only one the Router's history names. The answer carries `verifier` (a record id means nothing without it) and `valid_and_aligned`. |
| `GET /senders/{domain}/{selector}` | `state`: `active`, `pending` (with `confirm_after` and `can_confirm_now`), `unknown`, `rotated` or `retired`, from the KeyCache at `LATEST_FINAL`. |
| `GET /health` | `chain` (the RPC answers with the right chain id), `router` (it resolves `verifier` and `keycache`), `worker` (alive and passed recently), and `signer` (configured or absent). 200 when all hold, 503 otherwise. |

`GET /h/{token}` is not part of the API and takes no key: it serves the
headers of a call in flight to the validators, from the URL in the call.

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
`/h/{token}` and the API paths only. Check with
`curl -H "X-API-Key: ..." https://lacre.in-sidr.xyz/health`.

The account behind the key pays each attest's `fee()` and L2 gas, and must
be able to receive GEN (refunds of refused and non-executed calls come back
to it).

To update the public tools, move the submodule to a new commit of
`lacre` main, run the tests, and restart the service. Nothing under
`vendor/lacre` is ever edited here.

## Configuration

Environment only (see `deploy/gateway.env.example`):
`LACRE_NETWORK`, `LACRE_ROUTER`, `LACRE_API_KEYS`, `LACRE_BLOB_BASE_URL`,
`LACRE_DATA_DIR`, `LACRE_SIGNING_KEY_FILE` (a path; without it the gateway
reads but sends nothing), and the delays `LACRE_POLL_S`,
`LACRE_FINAL_BOUND_S`, `LACRE_MAX_ATTEMPTS`, `LACRE_MAX_SEND_FAILURES`,
`LACRE_KEY_QUARANTINE_S`, `LACRE_CONFIRM_MARGIN_S`, `LACRE_CONFIRM_RETRY_S`,
`LACRE_WORKER_STALE_S`, `LACRE_MAX_EML_BYTES`.

## Out of scope

- Content extraction: reading what a body says. No Extractor is deployed;
  the gateway attests who sent a message, not what it says.
- Billing, accounts and quotas beyond static API keys.
- An MCP server.
- A web front end.
