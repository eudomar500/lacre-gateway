# Lacre gateway

The gateway takes an email as an .eml upload, attests its DKIM signature on
GenLayer Testnet Bradbury through the public Lacre contracts, and then, by
default, extracts what the body says through one of the two Extractors. One
POST gives an agent the verified sender and the extracted fields. An agent
can also hold a mailbox, an address of its own: mail that arrives there
takes the same path without the agent uploading anything (see
[Mailboxes](#mailboxes)). It is the private product layer over the public repository
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
- **A mailbox receives the full message.** Mail to a mailbox reaches the
  gateway whole, every header and the body, as an upload does, and is then
  handled exactly as an upload is: the same selection, the same staging,
  the same deletion. The same retention rules apply; nothing about a
  delivery is kept that an uploaded job would not keep, apart from the id
  of the mailbox it came through and the mailbox's counters.
- **Nothing personal in the database.** No email address, subject, header
  value or body. The only values taken from a message are the signing domain
  and selector (`d=`, `s=`), which are public arguments of every KeyCache and
  Verifier call, and two hashes (`bh`, the SHA-256 of the served headers).
  A mailbox address is the gateway's own random name, not a person's; the
  sender of mail to it is never stored. API keys are stored only as
  SHA-256 digests, to find the account a request belongs to. The one
  exception is the access form of the web app: a request stores the name,
  email address and free text the visitor typed, and the time, so the
  operator can answer it (see [Web](#web)).
- **Nothing personal in the logs.** The gateway logs job ids, tx ids and
  statuses. The public tools print to stdout for a person at a terminal;
  the service sends their stdout nowhere. The HTTP access log is off,
  because paths carry sender names and blob tokens.
- **No key material in any response or log.** The signing key is read from
  a file only, never from the environment, and is never printed. `/senders`
  returns the key's digest and size, not the key.

## Endpoints

Every endpoint takes the API key in `X-API-Key`, except `POST /inbound`
(see [Mailboxes](#mailboxes)) and `/admin/*` (see [Accounts](#accounts)).
An unknown key is 401, the key of a disabled account 403. Times are UTC ISO
8601.

| method and path | answer |
|-----------------|--------|
| `POST /attest` | multipart form, field `eml`, optional field `extract` (`none`, `patterns`, `llm`, `auto`; default `LACRE_EXTRACT_DEFAULT`, `auto`). `202 {"job_id", "status": "pending", "job", "extract"}`. 422 for a message that cannot be attested or an unknown `extract`, 413 over `LACRE_MAX_EML_BYTES`, 402 when the balance cannot cover the hold (see [Pricing](#pricing)). |
| `GET /jobs/{id}` | only the account that created the job (by upload, or through one of its mailboxes) sees it; any other key, bootstrap keys included, gets 404, as for a job that does not exist. `status` (`pending`, `attesting`, `extracting`, `finalized`, `refused`, `failed`), `stage`, timestamps, `consensus_tx` and `explorer` once sent (every attempt in `consensus_txs`), `tx_status`, `sender_confirm_after` while the sender is in verification, `record_id`, `verifier` and `valid_and_aligned` when written (a record means the check ran, not that it passed), `refusal_reason` when refused, `error` when failed, `body_hash_matches`, `via` (`api` for an upload, `inbound` for mail to a mailbox), `mailbox` (its id, or `null`), `cost` (`{"held", "charged", "released"}`, see [Pricing](#pricing)), and `extraction` (below; `null` for `extract=none`). |
| `POST /mailboxes` | optional form field `extract` (as for `/attest`; default `LACRE_EXTRACT_DEFAULT`). `201` with the mailbox: `id` (12 base32 characters), `address` (`lacre-<id>@<LACRE_MAIL_DOMAIN>`), `extract`, `enabled`, `received`, `dropped`, `last_received_at`, `created_at`, `disabled_at`, `jobs`. |
| `GET /mailboxes` | `{"mailboxes": [...]}`, the caller key's mailboxes, oldest first. |
| `GET /mailboxes/{id}` | one mailbox. 404 for an id that does not exist or belongs to another key; the two are not told apart. |
| `DELETE /mailboxes/{id}` | disables the mailbox and returns it. It is kept, with its jobs and counters; mail to it is dropped from then on. |
| `GET /mailboxes/{id}/jobs` | `{"jobs": [...], "limit", "offset", "next"}`: the mailbox's jobs, newest first, each as `GET /jobs/{id}` shows it. `limit` 1 to 100 (default 20), `offset` from 0; `next` is the path of the next page, or `null`. |
| `GET /records/{id}` | the record, read from the Verifier at `LATEST_FINAL`. Any key reads any record, as does `/extractions`: records are public chain state. The Verifier is resolved through the Router on every request. `?verifier=` reads an earlier Verifier, only one the Router's history names. The answer carries `verifier` (a record id means nothing without it), `valid_and_aligned`, and `extractions`: the records on both Extractors, found through `records_of` for the gateway's wallet, whose `record_id` is this id and whose `verifier` is this Verifier, each as `{"lane", "extractor", "id", "record"}`. |
| `GET /extractions/{lane}/{id}` | one extraction record, `lane` `patterns` or `llm`, read at `LATEST_FINAL` from the Extractor the Router names for that lane now: `{"lane", "extractor", "id", "read_at", "record"}`. |
| `GET /senders/{domain}/{selector}` | `state`: `active`, `pending` (with `confirm_after` and `can_confirm_now`), `unknown`, `rotated` or `retired`, from the KeyCache at `LATEST_FINAL`. |
| `GET /account` | the caller's account: `id`, `name`, `enabled`, `unlimited`, `credits` (available now), `held` (reserved by open jobs, already out of `credits`), `charged` (spent by finished jobs), `prices` (`{"attest", "extract"}`), `counts` (`jobs`, `open`, `finalized`, `refused`, `failed`, `mailboxes`), `created_at`, `disabled_at`. |
| `POST /account/rotate-key` | `{"api_key", "account"}`: a new key, shown this once. The old key stops working at once; the account keeps its id, balance, mailboxes and jobs. |
| `GET /health` | `chain` (the RPC answers with the right chain id), `router` (it resolves `verifier` and `keycache`), `worker` (alive and passed recently), and `signer` (configured or absent). 200 when all hold, 503 otherwise. `layers` says for each of `router`, `keycache`, `verifier`, `extractor_patterns` and `extractor_llm` whether the Router resolves it now, and `addresses` gives each one's address (`""` where the Router names none); both are reported only and do not change the status, since a Router may name no Extractor. |

`GET /h/{token}` and `GET /b/{name}.bin` are not part of the API and take
no key: they serve the headers of an attest call and the body of an extract
call in flight to the validators, from the URL in the call.

`/mcp` is the MCP transport for these same endpoints, with the same key
(see [MCP](#mcp)).

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

## Mailboxes

A mailbox gives an agent an address, `lacre-<id>@in-sidr.xyz`. Mail sent
there is attested, and extracted in the mailbox's `extract` mode, as if
the account that owns the mailbox had uploaded it, and paid for as such
(see [Pricing](#pricing)); the agent reads the results with
`GET /mailboxes/{id}/jobs` or `GET /jobs/{id}`. The id is 12 lowercase
base32 characters (60 bits) from a CSPRNG. A mailbox belongs to the
account whose key created it, keeps belonging to it when the key is
rotated, and another account cannot see, list, disable or read the jobs of
it.

Mail arrives through Cloudflare Email Routing: a catch-all rule on the zone
hands every message to an Email Worker (`deploy/worker`), and the Worker
posts the ones for mailbox addresses to the gateway.

### The inbound contract

`POST /inbound`, called by the Worker only.

- Body: the message, RFC 5322, byte for byte as received. Over
  `LACRE_MAX_EML_BYTES`: 413, before anything else is checked.
- `X-Lacre-Recipient`: the envelope recipient, `lacre-<id>@<domain>`,
  compared in lower case.
- `X-Lacre-Timestamp`: Unix time in seconds when the Worker signed.
- `X-Lacre-Signature`: lowercase hex HMAC-SHA256, keyed with
  `LACRE_INBOUND_SECRET`, over
  `<timestamp> LF <recipient> LF <raw message>`, the header values exactly
  as sent.

Answers: `202` with the same stub as `POST /attest`
(`{"job_id", "status": "pending", "job", "extract"}`); `401` for a missing,
malformed or wrong signature, or a timestamp more than 300 seconds from the
gateway clock either way; `409` for a signature already taken inside that
window; `404` for an address that is not a mailbox, an unknown mailbox or a
disabled one, or one whose account is disabled; `402` when the owning
account's balance cannot cover the hold; `422` for a message that cannot be
attested, as for `/attest`; `503` when `LACRE_INBOUND_SECRET` is not set.
Only a 202 creates a job.

Mail to a disabled mailbox counts in its `dropped`; so does mail that was
signed correctly but could not be attested or paid for. Mail to an unknown mailbox has
no row to count on and counts in `inbound_unknown_dropped` of `/health`.
`received` and `last_received_at` count the deliveries that became jobs.
Nothing of a dropped message is stored or staged.

To try the contract by hand (`$SECRET` is `LACRE_INBOUND_SECRET`):

```
RCPT=lacre-<id>@in-sidr.xyz
TS=$(date +%s)
SIG=$( { printf '%s\n%s\n' "$TS" "$RCPT"; cat message.eml; } \
    | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1 )
curl -s -X POST https://lacre.in-sidr.xyz/inbound \
    -H "Content-Type: message/rfc822" \
    -H "X-Lacre-Recipient: $RCPT" -H "X-Lacre-Timestamp: $TS" \
    -H "X-Lacre-Signature: $SIG" --data-binary @message.eml
```

### Security model

- **Why an HMAC and not an API key.** `/inbound` is reachable from the
  internet, and whoever can post to it creates paid jobs for the mailbox's
  owner. The Worker holds a secret no agent has, and the MAC covers the
  exact bytes posted, so a request not made by the Worker, or changed on
  the way, is refused.
- **Why the timestamp and recipient are inside the MAC.** A MAC over the
  body alone would let anyone who saw one request send it again later with
  a fresh timestamp, or to another mailbox. Signed together, a captured
  request is good for 300 seconds at most, and inside that window the
  gateway remembers every signature it took and refuses it a second time.
  Clock skew between Cloudflare and the VPS has to stay under that window.
- **Why the Worker checks the recipient shape.** The catch-all rule gives
  the Worker mail for every address on the zone. Checking
  `^lacre-[a-z2-7]{12}@<domain>$` in the Worker means mail for any other
  address is never read, signed or sent to the gateway, and the gateway is
  not a place where arbitrary mail for the zone can land. The gateway checks
  the shape again and looks the id up; it does not trust the Worker's check.
- **What the Worker sees.** The whole message, as Cloudflare does for any
  mail routed through it, and the secret. It keeps nothing: it logs the
  mailbox id, the size and the gateway's status, never a header, the body,
  the sender or the full address.
- **What the Worker cannot do.** It holds no API key and cannot read jobs,
  mailboxes, records or anything else from the gateway; `/inbound` answers
  only with a job stub. A leaked Worker secret lets its holder create jobs
  in existing mailboxes, not read them; rotate it on both sides.
- **Bounces instead of silence.** When the gateway answers anything but
  2xx, or cannot be reached, the Worker rejects the message permanently and
  the sender gets a bounce. Mail is never accepted and then lost.

### Privacy

A mailbox means the gateway receives the full message: every header,
including the addresses of the sender and of the other recipients, and the
body. It is then handled exactly as an upload, and the same retention rules
apply (see [Privacy guarantees](#privacy-guarantees)): only the signed
headers are staged and served, the body is staged only for a mailbox whose
`extract` is not `none`, everything is deleted when the job ends, and
nothing of the message but what an uploaded job keeps is written to the
database. Create a mailbox with `extract=none` for mail whose body must
not go to the validators.

## Accounts

Every API key belongs to an account: a random id (12 lowercase base32
characters from a CSPRNG), a name, the SHA-256 of its key (never the key),
a credit balance, and an enabled flag. Mailboxes and jobs belong to the
account, not to the key, so rotating a key (`POST /account/rotate-key`)
changes nothing else. A disabled account's key is answered 403 everywhere
and mail to its mailboxes bounces; its open jobs run to the end and settle.

How a customer gets access today: an operator creates the account with
`POST /admin/accounts`, hands over the key it returns (shown once), and adds
credits with a top-up whenever the customer pays, outside the gateway. The
customer reads its balance with `GET /account` or `lacre_account`.

### Bootstrap keys

`LACRE_API_KEYS` keeps working. On every start, each entry that has no
account gets one, named `bootstrap-N`, with `LACRE_BOOTSTRAP_CREDITS`
credits. While that is 0, the default, bootstrap accounts are unlimited: a
job is priced and settled as for any account and shows its `cost`, but
nothing is taken from a balance, so a gateway deployed before accounts
behaves as it did. An entry taken out of the list revokes its key, as it
did before. A bootstrap account whose key was rotated is matched to its
entry by the entry's digest, so it does not get a second account; taken out
of the list it is not revoked (its key is no longer that entry) and only
stops being unlimited.

On the first start with accounts, mailboxes and jobs owned by a key digest
move to the account of that key, and the digest columns are dropped. A row
whose digest no configured key has keeps no account: its key was already
gone, and no key could reach it before either.

**Before mainnet:** unlimited bootstrap credits are a testnet convenience.
Set `LACRE_BOOTSTRAP_CREDITS` to a number (it is granted once, when the
account is made; existing bootstrap accounts become metered with the
balance they have) or move every customer to an admin-made account and
empty `LACRE_API_KEYS` of anything but an operator key.

### The admin API

`/admin/*` takes `LACRE_ADMIN_TOKEN` in `X-Admin-Token`, never an API key.
Without the variable every admin path answers 503; with it, a missing or
wrong token is 401. Bodies are JSON.

| method and path | answer |
|-----------------|--------|
| `POST /admin/accounts` | `{"name", "credits"}` (`credits` 0 or more, default 0, recorded as an `adjust`). `201 {"api_key", "account"}`; the key is not shown again. |
| `GET /admin/accounts` | `{"accounts": [...]}`, each as `GET /account` shows it, plus `bootstrap`. |
| `GET /admin/accounts/{id}` | the account with `ledger`: its newest entries first, `?limit=` 1 to 500 (default 50). |
| `POST /admin/accounts/{id}/topups` | `{"credits", "external_ref", "note"}` through the manual source. `201 {"topup", "created": true, "account"}`; the same `external_ref` again is `200` with `created` false and the first top-up, and nothing is credited twice; with another amount it is 409. |
| `POST /admin/accounts/{id}/disable`, `/enable` | the account after the change. |
| `GET /admin/access-requests` | `{"access_requests": [...], "limit", "offset", "next"}`: the requests from the web app's access form, newest first, each `{"id", "name", "email", "what", "created_at"}`. `limit` 1 to 500 (default 50). |

The admin paths are not in the tunnel's routes by default:
`deploy/cloudflared.yml.example` has them as a commented-out rule. Call
them on the VPS against `127.0.0.1:8080`, or publish them only with
Cloudflare Access in front.

```
curl -s -X POST http://127.0.0.1:8080/admin/accounts \
    -H "X-Admin-Token: $ADMIN" -H "Content-Type: application/json" \
    -d '{"name": "acme", "credits": 0}'
curl -s -X POST http://127.0.0.1:8080/admin/accounts/<id>/topups \
    -H "X-Admin-Token: $ADMIN" -H "Content-Type: application/json" \
    -d '{"credits": 100, "external_ref": "invoice-2026-0042"}'
```

## Pricing

Prices are in credits and come from the configuration:
`LACRE_PRICE_ATTEST` (default 1) and `LACRE_PRICE_EXTRACT` (default 1).
`GET /account` shows them.

- **Hold.** Creating a job, by upload or by mail to a mailbox, holds the
  attest price, plus the extract price when an extraction is asked for and
  can run (one known at upload to be skipped, such as a body over the cap,
  holds nothing for it). The hold comes out of the balance at once. When
  the balance is short the answer is 402 with
  `{"detail", "needed", "credits", "shortfall"}`, and nothing is created or
  staged; mail is dropped, counted in the mailbox's `dropped`, and bounced.
- **Settle.** When the job ends (`finalized`, `refused` or `failed`), a
  step is charged only if it produced a record: a Verifier record charges
  attest, valid or not; an extraction record charges extract, `match`
  false included, because the Extractor kept its fee for it.
- **Release.** The rest of the hold goes back: a refusal (refunded on
  chain), a failure, a skipped or refused extraction.

The job's `cost` shows `held`, and once it ended `charged` and `released`
(`held` = `charged` + `released`). The hold is taken from the prices at
creation; a price change applies to jobs created after it.

### The ledger

Every change to a balance is a ledger row: `account_id`, `delta`, `kind`,
`job_id` or `topup_id`, `note`, `created_at`, written in the same
transaction as the change.

| kind | delta | when |
|------|-------|------|
| `hold` | minus the hold | a job is created |
| `settle` | 0 | the job ended; `note` says what was charged, attest and extract. The charge is the part of the hold not released, so this row moves nothing. |
| `release` | plus what was not charged | the job ended with part of its hold unused (no row when all of it was charged) |
| `topup` | plus the credits | a top-up was credited |
| `adjust` | plus or minus | an operator change: the credits an account is made with |

The balance is the sum of the ledger, cached on the account. On every start
the gateway compares the two; a cached balance that differs is logged and
set to the sum. A ledger that sums below zero stops the start, since it
means the data was changed by hand. An unlimited account's jobs write no
ledger rows.

## Top-ups

A top-up credits an account from one external event, named by
`(source, external_ref)`; the gateway credits each pair once, however often
it is reported. `lacre_gateway/topups.py` defines the interface
(`TopUpSource.credit`) and the registry of sources (`SOURCES`). There is one
source today, `manual`: the operator, through the admin API, after a
payment made outside the gateway, with the invoice or receipt as
`external_ref`.

When a payment source exists (a payment provider's webhook, or a watcher of
GEN deposits to a gateway address), it will be a `TopUpSource` registered
in `SOURCES`, with its own entry point that verifies the event (the
provider's signature, or the deposit read at a final block) and calls
`credit` with the provider's payment id or the deposit's transaction as
`external_ref`. Customers will then top up themselves; accounts, prices,
holds and the ledger stay as they are.

## MCP

`lacre_mcp` gives an agent the API as MCP tools, so it does not have to
read this document to use it. It is a thin client of the REST API above:
every tool is one request (`lacre_wait_job` is a loop of them), with the
same key, the same answers and the same errors. A 4xx or 5xx from the
gateway, or a gateway that cannot be reached, is a tool error carrying the
gateway's `detail`; the server goes on. The message and the key are never
logged.

| tool | request |
|------|---------|
| `lacre_attest(eml, extract="auto")` | `POST /attest`. `eml` is the raw RFC 5322 message as text, or its base64 when it is not text that starts with a header field. Text with no CR is sent with each LF made CRLF; base64 is sent byte for byte. |
| `lacre_job(job_id)` | `GET /jobs/{id}` |
| `lacre_wait_job(job_id, timeout_s=3600)` | `GET /jobs/{id}` every 30 s until `finalized`, `refused` or `failed`, or the timeout; returns the last read. 5xx and network errors are retried until the timeout, a 4xx ends it. |
| `lacre_record(record_id, verifier=None)` | `GET /records/{id}` |
| `lacre_extraction(lane, record_id)` | `GET /extractions/{lane}/{id}` |
| `lacre_sender(domain, selector)` | `GET /senders/{domain}/{selector}` |
| `lacre_health()` | `GET /health`, with `ok`: a 503 here is the answer "not every check holds", not an error |
| `lacre_account()` | `GET /account` |
| `lacre_mailbox_create(extract="auto")` | `POST /mailboxes` |
| `lacre_mailboxes()` | `GET /mailboxes` |
| `lacre_mailbox(mailbox_id)` | `GET /mailboxes/{id}` |
| `lacre_mailbox_jobs(mailbox_id, limit=20, offset=0)` | `GET /mailboxes/{id}/jobs` |
| `lacre_mailbox_disable(mailbox_id)` | `DELETE /mailboxes/{id}` |

The tools send `extract` explicitly, `auto` unless told otherwise, so
`LACRE_EXTRACT_DEFAULT` does not apply to them. Each argument that goes into
a path is quoted as one segment, so no argument reaches another endpoint.

The same rules as for the API hold, and the tool descriptions state them:
a job stub is not proof; a job is provisional until its `status` is
`finalized`, `refused` or `failed`; a record is final only when its job is
`finalized`, means the check ran and not that it passed
(`valid_and_aligned`), and means nothing without its `verifier`. A consumer
decides on the record with `check_for` on the Verifier, reads the key
status at decision time (`lacre_sender`), gates an extraction on `match`
true and `reason` `extracted`, reads `eta_date` and `order_id_found` only
when `method` is `patterns`, and pins the digest it reviewed.

An on-chain step takes about 35 minutes, and a sender the gateway has not
seen before waits 24 hours in quarantine before its first attestation is
sent. `lacre_wait_job` can therefore run for as long as `timeout_s`; the
host's own tool-call timeout has to be longer, or the agent should wait in
shorter calls.

### Local, over stdio

```
pip install -e .                  # in a checkout; installs lacre-mcp
lacre-mcp --list-tools            # prints the tool names and exits
```

`lacre-mcp` (or `python -m lacre_mcp`) serves the tools over stdio. It reads
`LACRE_GATEWAY_URL` (default `https://lacre.in-sidr.xyz`) and `LACRE_API_KEY`
from its environment. It needs only `mcp` and `httpx`, not the gateway's
dependencies or the submodule.

Any MCP host that launches a stdio server takes it as a command, its
arguments and its environment:

```
{
  "mcpServers": {
    "lacre": {
      "command": "lacre-mcp",
      "args": [],
      "env": {
        "LACRE_GATEWAY_URL": "https://lacre.in-sidr.xyz",
        "LACRE_API_KEY": "<your key>"
      }
    }
  }
}
```

Without the console script on the host's PATH, use the interpreter that has
the package: `"command": "/path/to/.venv/bin/python", "args": ["-m",
"lacre_mcp"]`.

### Remote, over streamable HTTP

The gateway serves the same tools at `https://lacre.in-sidr.xyz/mcp`
(streamable HTTP, stateless). A request without a known key in `X-API-Key`
is answered 401, as the API answers it. The tools act with the key of the
request they answer, so a remote agent configures nothing but the URL and
its key, and a key sees over MCP exactly what it sees over REST: its own
mailboxes and nothing of another key's. Any MCP host that speaks streamable
HTTP with a custom header takes it as:

```
{
  "mcpServers": {
    "lacre": {
      "type": "http",
      "url": "https://lacre.in-sidr.xyz/mcp",
      "headers": {"X-API-Key": "<your key>"}
    }
  }
}
```

Inside the gateway the tools call the API in process, not over the network,
so `/mcp` works whatever the tunnel publishes and adds no configuration.
DNS rebinding protection is off at `/mcp`: it guards servers that answer
without credentials, and the Host behind the tunnel is the public name. A
long `lacre_wait_job` keeps its request open; the SDK sends an SSE ping
every 15 seconds, which keeps the tunnel from closing it as idle.

## Web

The gateway serves a web app from `web/`: plain HTML, one stylesheet
(`app.css`), one script (`app.js`), no framework, no build step and no
runtime dependency; `state.js` holds what survives a reload. Fonts (Jost, JetBrains Mono) are self-hosted in
`web/fonts` with their SIL Open Font License files; icons are inline SVG.
The pages load nothing from another origin, and a Content-Security-Policy
header holds them to that.

| path | what it is |
|------|------------|
| `/` | one scrolling page: the main screen (readouts, the disk, attest and the plugin rail), then the sections `#how`, `#primitives`, `#why`, `#mcp` and `#access` |
| `/docs.html` | the endpoints and MCP tools, and the rule that only a finalized record is proof |
| `/how.html`, `/why.html`, `/mcp.html`, `/access.html` | 301 to their section of `/` |
| `/favicon.svg` | the disk, as the page icon |
| `/static/*` | the stylesheet, the scripts and the fonts |

None of these takes an API key. What is live on the main screen, against
the API of the same origin with the visitor's key:

- the account (`GET /account`: balance, prices, job counts), read as
  soon as the key is accepted and again on every change of the job
  followed, and the contract layers (`GET /health`, `layers`); the
  Primitives section shows each contract's address from `addresses`, so
  it follows the Router;
- attest: a .eml by drop or file picker, or pasted source, with the
  extraction mode, as `POST /attest`; the job is then read from
  `GET /jobs/{id}` every 20 seconds and drawn on the disk, stage by
  stage, with the time each on-chain step takes (about 35 minutes; up to
  24 hours when the sender is in verification). A refused or failed job
  shows its reason. The output card shows the sender domain, the valid and
  aligned checks (read from `GET /records/{id}` once final), the record
  id, the attest and the extract transactions with their explorer links,
  and the extracted fields; it stays provisional until the job is
  `finalized` and its transaction `FINALIZED`. A job id given under
  "Resume a job" is followed the same way. While a job is in flight the
  input card says which one it follows; starting another asks first, and
  the job keeps running on chain;
- the plugin rail: the curl for `POST /attest` and the MCP config, both
  with the key masked on screen and whole in what is copied, and the
  mailboxes of the account (`GET /mailboxes`, `POST /mailboxes`).

**What survives a reload.** The job followed is in the URL (`?job=`)
and in sessionStorage, and is picked up again on load with no click. The
key is kept in sessionStorage while "Remember for this session" is on (the
default), so it lasts until the tab closes; with the switch off it is kept
in memory only and a reload asks for it again. It is never put in
localStorage, a cookie or the URL, and is sent in `X-API-Key` to this
origin and nowhere else. localStorage holds only durable choices: the
switch itself, and the address of a wallet once connected, which is
checked again on load with `eth_accounts` (no prompt) and forgotten if the
wallet no longer grants it.

**Access requests.** `POST /access-request` takes JSON
`{"name", "email", "what"}` with no key: name 1 to 100 characters, a
syntactically valid email up to 254, `what` optional up to 2000, no
control characters. It answers 201, 422 for a field that fails, and 429
past 5 requests an hour from one address (`CF-Connecting-IP` behind the
tunnel) or 100 an hour from all together. The limit is kept in memory; the
address is never stored. Requests are listed with
`GET /admin/access-requests` (see [The admin API](#the-admin-api)).

**Coming soon**, shown disabled on the access page: self-service sign-up
and top-ups. Until then an operator creates the account and its key.

**The design source** is a self-contained design bundle kept outside this
repository, with the product design files; nothing of it (its runtime,
support scripts or the file itself) is copied here. `web/` reproduces it
by hand.

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

The web app is at `http://127.0.0.1:8080/`: paste the printed key and drop
the same file; the fixture chain finalizes each step in a few seconds.

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
`/h/{token}`, `/b/{name}.bin`, `/inbound`, the API paths, `/mcp`, and the web
app (`/`, the five pages, `/static/*`, `/access-request`) only, not
`/admin/*`. Check with
`curl -H "X-API-Key: ..." https://lacre.in-sidr.xyz/health`.

Mailboxes need `LACRE_INBOUND_SECRET` in `gateway.env` and the Email
Worker in `deploy/worker`; its README has the steps.

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
lane, each taking at least one finalization (about 35 minutes on
Bradbury).

To update the public tools, move the submodule to a new commit of
`lacre` main, run the tests, and restart the service. Nothing under
`vendor/lacre` is ever edited here.

## Configuration

Environment only (see `deploy/gateway.env.example`):
`LACRE_NETWORK`, `LACRE_ROUTER`, `LACRE_API_KEYS`, `LACRE_BLOB_BASE_URL`,
`LACRE_BODY_BASE_URL` (default: `LACRE_BLOB_BASE_URL` with its last `/h`
made `/b`), `LACRE_EXTRACT_DEFAULT` (`auto`), `LACRE_MAIL_DOMAIN`
(`in-sidr.xyz`), `LACRE_INBOUND_SECRET` (at least 32 characters; without
it `/inbound` answers 503), `LACRE_BOOTSTRAP_CREDITS` (0: unlimited
bootstrap accounts), `LACRE_PRICE_ATTEST` and `LACRE_PRICE_EXTRACT` (1
each), `LACRE_ADMIN_TOKEN` (at least 32 characters; without it `/admin/*`
answers 503), `LACRE_DATA_DIR`, `LACRE_SIGNING_KEY_FILE` (a path; without it the gateway
reads but sends nothing), and the delays `LACRE_POLL_S`,
`LACRE_FINAL_BOUND_S`, `LACRE_MAX_ATTEMPTS`, `LACRE_MAX_SEND_FAILURES`,
`LACRE_KEY_QUARANTINE_S`, `LACRE_CONFIRM_MARGIN_S`, `LACRE_CONFIRM_RETRY_S`,
`LACRE_WORKER_STALE_S`, `LACRE_MAX_EML_BYTES`. `LACRE_MAX_ATTEMPTS`,
`LACRE_MAX_SEND_FAILURES` and `LACRE_FINAL_BOUND_S` apply to extract calls as
they do to attest calls. No Extractor address is configured: `extractor` and
`extractor_llm` are resolved on the Router each time one is needed.

## Out of scope

- Automated top-ups: no payment source exists yet (see [Top-ups](#top-ups)).
- Self-service sign-up and top-ups from the web app (shown as coming soon).
