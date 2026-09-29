"""The Lacre tools.

build_server takes gateway_for, which gives the Gateway a tool call goes
through: over stdio one Gateway from the environment, inside the gateway
one that carries the key of the /mcp request being answered. The tools do
not know which transport they are behind.
"""

import base64
import binascii
import logging
import re
import time
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .gateway import GatewayError, segment

# httpx logs every request URL at INFO. Paths carry sender domains and
# selectors, which the gateway keeps out of its logs, so neither does this.
logging.getLogger("httpx").setLevel(logging.WARNING)

TERMINAL = ("finalized", "refused", "failed")
POLL_S = 30
# A header field name (RFC 5322 3.6.8) then a colon. Base64 has no colon, so
# a message and its base64 cannot be taken for each other.
FIELD_LINE = re.compile(rb"^[\x21-\x39\x3b-\x7e]+[ \t]*:")

INSTRUCTIONS = """\
Lacre attests an email's DKIM signature on GenLayer and, optionally, extracts
what its body says. Submit a message with lacre_attest, or create a mailbox
with lacre_mailbox_create and have mail sent to its address. Either way you
get jobs. A job is not proof: follow it with lacre_job or lacre_wait_job
until its status is finalized, refused or failed. Only a record is evidence,
only once the job is finalized, and only with its Verifier address. Before
acting on a record, check it with check_for on the Verifier and read the
sender's key status at that time (lacre_sender). On-chain steps take about 35
minutes each, and a sender the gateway has not seen before waits 24 hours in
quarantine first. Every job costs credits; lacre_account shows the balance
and the prices."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                        open_world_hint=True)

Extract = Literal["none", "patterns", "llm", "auto"]
JobId = Annotated[str, Field(description="The job_id from lacre_attest or a mailbox job.")]
MailboxId = Annotated[str, Field(description="The mailbox id, 12 lowercase base32 characters.")]
RecordId = Annotated[str, Field(description="A record id, decimal digits.")]


def message_bytes(eml):
    """The octets to upload for the eml argument.

    Text that starts with a header field is the message itself. It goes out
    as UTF-8, and with every LF made CRLF when it has no CR at all: that is
    how a .eml saved on Unix differs from the message on the wire, and the
    gateway looks for CRLF CRLF between headers and body. Anything else is
    taken as base64 of the exact octets, which are sent unchanged.
    """
    try:
        raw = eml.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("eml is not valid text; send the message base64-encoded")
    if FIELD_LINE.match(raw):
        return raw.replace(b"\n", b"\r\n") if b"\r" not in raw else raw
    try:
        decoded = base64.b64decode("".join(eml.split()), validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if not FIELD_LINE.match(decoded):
        raise ToolError("eml is neither an RFC 5322 message (it must start with a header "
                        "field) nor base64 of one")
    return decoded


def build_server(gateway_for, *, sleep=anyio.sleep, clock=time.monotonic):
    server = MCPServer("lacre", instructions=INSTRUCTIONS)

    async def call(ctx, method, path, **options):
        try:
            return (await gateway_for(ctx).call(method, path, **options))[1]
        except GatewayError as error:
            raise ToolError(str(error)) from None

    @server.tool(annotations=WRITE, description=(
        "Submit one email for DKIM attestation on GenLayer and, unless extract is none, "
        "for extraction of what its body says. eml is the complete raw message, headers "
        "and body, as RFC 5322 text; text with no CR characters is sent with each LF made "
        "CRLF. When the exact bytes are not UTF-8 text, pass the message base64-encoded "
        "instead; base64 is sent byte for byte. extract: none attests only and the body "
        "never leaves the gateway; patterns or llm choose the Extractor; auto takes "
        "patterns when the sender domain has patterns, else llm. With any mode but none "
        "the body is served publicly while the extraction runs, and on the llm lane it "
        "also goes to the validators' model providers. The result is a job stub: job_id, "
        "status pending, job, extract. The job is not proof of anything and nothing has "
        "been sent on chain yet. Call lacre_job or lacre_wait_job until status is "
        "finalized, refused or failed. Only a record (record_id with its verifier, read "
        "with lacre_record) is evidence, and only once the job is finalized. Each "
        "attestation and each extraction is a paid transaction."))
    async def lacre_attest(
            eml: Annotated[str, Field(description="The raw RFC 5322 message, or its base64.")],
            ctx: Context,
            extract: Annotated[Extract, Field(description="none, patterns, llm or auto.")] = "auto",
    ) -> dict:
        raw = message_bytes(eml)
        return await call(ctx, "POST", "/attest", data={"extract": extract},
                          files={"eml": ("message.eml", raw, "message/rfc822")}, ok=(202,))

    async def get_job(ctx, job_id):
        return await call(ctx, "GET", "/jobs/%s" % (segment(job_id),))

    @server.tool(annotations=READ, description=(
        "Read one job of this API key's account, created by lacre_attest or through one "
        "of its mailboxes; a job of another account gives the same not-found error as one "
        "that does not exist. status is pending, attesting, extracting, finalized, refused or "
        "failed. Only finalized, refused and failed are final; any other status is "
        "provisional and will change. stage says what the gateway is doing now, and "
        "sender_confirm_after appears while the sender's key is in its 24 hour "
        "quarantine. When the attestation is written, record_id and verifier name the "
        "Verifier record and valid_and_aligned says whether the DKIM check passed: a "
        "record means the check ran, not that it passed. refusal_reason (refused) and "
        "error (failed) say why a job ended without a record. extraction is null for "
        "extract none; otherwise extraction.status says how far it got, and its fields "
        "(match, shipped, eta_day, eta_date, order_id_found, method and the rest) are the "
        "Extractor's stored record only when extraction.status is extracted. Consumers "
        "gate on match true and reason extracted. Decide on the record itself, read with "
        "lacre_record, not on this summary."))
    async def lacre_job(job_id: JobId, ctx: Context) -> dict:
        return await get_job(ctx, job_id)

    @server.tool(annotations=READ, description=(
        "Wait for a job: read it every 30 seconds until its status is finalized, refused "
        "or failed, or until timeout_s seconds have passed, and return the last read, as "
        "lacre_job returns it. If the returned status is not one of those three, the "
        "timeout came first and the job is still running; call again. An on-chain step "
        "takes about 35 minutes: an attestation needs at least one, an extraction at "
        "least one more, and validator timeouts add retries. A sender key the gateway has "
        "not seen before is registered first and stays in quarantine for 24 hours before "
        "the attestation is sent, so a job from a new sender can take more than a day; "
        "stage 'sender in verification' and sender_confirm_after show this. A slow job "
        "has not failed. Network errors and 5xx answers during the wait are retried until "
        "the timeout; a 4xx answer ends the wait as an error."))
    async def lacre_wait_job(
            job_id: JobId,
            ctx: Context,
            timeout_s: Annotated[int, Field(ge=0, description="Seconds to wait at most.")] = 3600,
    ) -> dict:
        deadline = clock() + timeout_s
        job = failure = None
        while True:
            try:
                job = (await gateway_for(ctx).call("GET", "/jobs/%s" % (segment(job_id),)))[1]
                failure = None
            except GatewayError as error:
                if error.status is not None and error.status < 500:
                    raise ToolError(str(error)) from None
                failure = error
            if failure is None and job["status"] in TERMINAL:
                return job
            remaining = deadline - clock()
            if remaining <= 0:
                if job is None:
                    raise ToolError(str(failure))
                return job
            await sleep(min(POLL_S, remaining))

    @server.tool(annotations=READ, description=(
        "Read a Verifier record at LATEST_FINAL. This is the evidence an attestation "
        "produces. A record id means nothing without its Verifier: pass verifier as the "
        "job gave it; without it the Router's current Verifier is read, and only a "
        "Verifier the Router has named can be read at all. valid_and_aligned is true only "
        "when the DKIM signature verified and its domain is aligned with From; a record "
        "with false is still a record, of a check that failed. extractions lists the "
        "extraction records of the gateway's wallet that read this record. Before acting "
        "on a record, check it with check_for on the Verifier and read the sender's key "
        "status at that time with lacre_sender."))
    async def lacre_record(
            record_id: RecordId,
            ctx: Context,
            verifier: Annotated[str | None, Field(
                description="The Verifier address the job gave, or omit for the current one.")] = None,
    ) -> dict:
        params = {"verifier": verifier} if verifier else None
        return await call(ctx, "GET", "/records/%s" % (segment(record_id),), params=params)

    @server.tool(annotations=READ, description=(
        "Read one extraction record at LATEST_FINAL, from the Extractor the Router names "
        "for the lane now. lane is patterns or llm; record_id is the Extractor's own id, "
        "extraction.record_id of the job (ids are local to one Extractor). Use the fields "
        "only when match is true and reason is extracted. eta_date and order_id_found "
        "mean something only when method is patterns. Pin patterns_sha256 or "
        "prompt_sha256 to the version you reviewed. reason is the record's own field; why "
        "the gateway stopped an extraction is in the job, not here."))
    async def lacre_extraction(
            lane: Annotated[Literal["patterns", "llm"], Field(description="patterns or llm.")],
            record_id: RecordId,
            ctx: Context,
    ) -> dict:
        return await call(ctx, "GET", "/extractions/%s/%s" % (segment(lane), segment(record_id)))

    @server.tool(annotations=READ, description=(
        "Read a sender's DKIM key state from the KeyCache at LATEST_FINAL. domain and "
        "selector are the d= and s= of the signature. state is active; pending, meaning "
        "registered and in its 24 hour quarantine, with confirm_after and can_confirm_now; "
        "unknown, never registered, which the first job from it will do; rotated or "
        "retired, and jobs from it are refused. A key's state can change, so read it at "
        "the time you decide on a record."))
    async def lacre_sender(
            domain: Annotated[str, Field(description="The signing domain, d=.")],
            selector: Annotated[str, Field(description="The selector, s=.")],
            ctx: Context,
    ) -> dict:
        return await call(ctx, "GET", "/senders/%s/%s" % (segment(domain), segment(selector)))

    @server.tool(annotations=READ, description=(
        "Check the gateway. chain: the RPC answers on the right chain. router: it "
        "resolves the Verifier and the KeyCache. worker: the background worker ran "
        "recently. signer: configured, or absent when the gateway cannot send. ok is "
        "true when chain, router and worker all hold. When ok is false jobs may stall, "
        "and they resume when the gateway recovers; it does not mean a job failed."))
    async def lacre_health(ctx: Context) -> dict:
        try:
            status, body = await gateway_for(ctx).call("GET", "/health", ok=(200, 503))
        except GatewayError as error:
            raise ToolError(str(error)) from None
        # 503 is the answer "not every check holds", not a failed call; its
        # body says which.
        return dict(body, ok=status == 200)

    @server.tool(annotations=READ, description=(
        "Read this API key's account: id, name, enabled, unlimited, credits (available "
        "now), held (credits reserved by open jobs, already taken out of credits), charged "
        "(credits spent by finished jobs), prices (credits per attestation and per "
        "extraction), and counts of jobs by status and of mailboxes. Creating a job holds "
        "the attestation price, plus the extraction price when an extraction is asked for "
        "and can run; when it ends, a step that wrote a record is charged (an extraction "
        "record with match false included) and the rest is released. Each job shows this "
        "as cost: held, charged, released. When credits cannot cover a job, lacre_attest "
        "fails with the shortfall and mail to a mailbox bounces; an operator adds credits. "
        "unlimited true means no credits are taken, though jobs still show their cost."))
    async def lacre_account(ctx: Context) -> dict:
        return await call(ctx, "GET", "/account")

    @server.tool(annotations=WRITE, description=(
        "Create a mailbox: an email address of its own, lacre-<id>@in-sidr.xyz, owned by "
        "this API key. Each message sent to it becomes a job, attested and extracted in "
        "the mailbox's extract mode (as for lacre_attest), as if this key had uploaded it. "
        "Give the address to whoever will send the mail, then poll lacre_mailbox_jobs for "
        "the jobs it creates; each job follows the same rules as lacre_job. The gateway "
        "receives the whole message; with any mode but none the body is served publicly "
        "during extraction and on the llm lane goes to the validators' model providers. "
        "Returns the mailbox: id, address, extract, enabled, received, dropped, "
        "last_received_at, created_at, disabled_at."))
    async def lacre_mailbox_create(
            ctx: Context,
            extract: Annotated[Extract, Field(description="none, patterns, llm or auto.")] = "auto",
    ) -> dict:
        return await call(ctx, "POST", "/mailboxes", data={"extract": extract}, ok=(201,))

    @server.tool(annotations=READ, description=(
        "List this API key's mailboxes, oldest first, each as lacre_mailbox returns it."))
    async def lacre_mailboxes(ctx: Context) -> dict:
        return await call(ctx, "GET", "/mailboxes")

    @server.tool(annotations=READ, description=(
        "Read one mailbox of this API key. received counts deliveries that became jobs; "
        "dropped counts mail that was not accepted, because the mailbox was disabled or "
        "the message could not be attested. A mailbox that does not exist and one owned "
        "by another key give the same not-found error."))
    async def lacre_mailbox(mailbox_id: MailboxId, ctx: Context) -> dict:
        return await call(ctx, "GET", "/mailboxes/%s" % (segment(mailbox_id),))

    @server.tool(annotations=READ, description=(
        "List a mailbox's jobs, newest first, each as lacre_job returns it. limit is 1 to "
        "100, offset counts from 0. next is non-null when there are more: call again "
        "with offset plus limit. A job here is provisional until its status is "
        "finalized, refused or failed, exactly as with lacre_job."))
    async def lacre_mailbox_jobs(
            mailbox_id: MailboxId,
            ctx: Context,
            limit: Annotated[int, Field(ge=1, le=100, description="Jobs per page.")] = 20,
            offset: Annotated[int, Field(ge=0, description="Jobs to skip.")] = 0,
    ) -> dict:
        return await call(ctx, "GET", "/mailboxes/%s/jobs" % (segment(mailbox_id),),
                          params={"limit": limit, "offset": offset})

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                             idempotent_hint=True, open_world_hint=True),
                 description=(
        "Disable a mailbox. It is kept, with its jobs and counters, and can still be "
        "read, but mail sent to it from now on is refused and bounces back to its "
        "sender. There is no way to enable it again. Returns the mailbox, enabled "
        "false."))
    async def lacre_mailbox_disable(mailbox_id: MailboxId, ctx: Context) -> dict:
        return await call(ctx, "DELETE", "/mailboxes/%s" % (segment(mailbox_id),))

    return server
