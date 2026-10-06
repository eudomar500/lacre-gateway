"""POST /rpc: the Bradbury RPC, for wallets whose request ids it refuses.

The Bradbury RPC answers a request whose id is a string with -32700
(Request.id of type int), and MetaMask numbers its requests with strings, so
it can neither read the chain nor send there. This route takes a request or
a batch, gives every id an integer of its own, forwards it to the RPC and
puts the original ids back in the answer. A request whose id is null and a
notification, which has none, go as they came.

Nothing else is changed and no method is refused: the RPC is public and
this only renames ids. What is refused is anything that is not JSON-RPC
2.0, a body over RPC_MAX_BYTES and an address over its rate limit. The
answers are open to every origin, since a wallet asks from its own.
"""

import json

import httpx
from fastapi.responses import Response

from .web import BRADBURY_RPC, RateLimit, client_address

# A signed attest_inline carries a headers blob of up to 16 KiB as hex
# inside the calldata, well under this.
RPC_MAX_BYTES = 256 * 1024
RPC_BATCH_MAX = 100
# What an answer may weigh before the gateway stops reading it.
RPC_ANSWER_MAX = 4 * 1024 * 1024
RPC_TIMEOUT_S = 10.0
# A wallet polls the chain every few seconds; a batch counts once.
RPC_WINDOW_S = 60
RPC_PER_IP = 300
RPC_TOTAL = 6000
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}


class NotJsonRpc(ValueError):
    """The body is not a JSON-RPC 2.0 request or batch."""


def renumber(payload):
    """(what to forward, {integer id: original id}).

    Ids are numbered from 1 in the order of the batch, so no two requests of
    one forward share an id whatever they came with.
    """
    batch = isinstance(payload, list)
    calls = payload if batch else [payload]
    if batch and not calls:
        raise NotJsonRpc("an empty batch")
    if len(calls) > RPC_BATCH_MAX:
        raise NotJsonRpc("a batch of more than %d requests" % (RPC_BATCH_MAX,))
    ids, out = {}, []
    for call in calls:
        if (not isinstance(call, dict) or call.get("jsonrpc") != "2.0"
                or not isinstance(call.get("method"), str)):
            raise NotJsonRpc("not a JSON-RPC 2.0 request")
        if "id" not in call or call["id"] is None:
            out.append(call)
            continue
        given = call["id"]
        if isinstance(given, bool) or not isinstance(given, (str, int, float)):
            raise NotJsonRpc("an id is a string, a number or null")
        number = len(ids) + 1
        ids[number] = given
        out.append(dict(call, id=number))
    return (out if batch else out[0]), ids


def restore(answer, ids):
    """The answer with each renumbered id back as it came; null ids, and
    anything that is not a response object, are left as they are."""
    def one(item):
        if isinstance(item, dict):
            key = item.get("id")
            if isinstance(key, int) and not isinstance(key, bool) and key in ids:
                return dict(item, id=ids[key])
        return item
    return [one(item) for item in answer] if isinstance(answer, list) else one(answer)


def rpc_error(status, code, message, headers=None):
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    return Response(content=json.dumps(body), status_code=status, media_type="application/json",
                    headers=dict(CORS, **(headers or {})))


class RpcProxy:
    def __init__(self, clock, transport=None, upstream=BRADBURY_RPC):
        self.upstream = upstream
        self.limit = RateLimit(clock, window_s=RPC_WINDOW_S, per_key=RPC_PER_IP,
                               total=RPC_TOTAL)
        self.client = httpx.AsyncClient(transport=transport, timeout=RPC_TIMEOUT_S,
                                        follow_redirects=False)

    async def aclose(self):
        await self.client.aclose()

    def preflight(self):
        return Response(status_code=204, headers=CORS)

    async def handle(self, request):
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            return rpc_error(415, -32600, "the body must be application/json")
        raw = await self.read_capped(request)
        if raw is None:
            return rpc_error(413, -32600, "the request is too large")
        # Counted after the size check, before anything is forwarded.
        if not self.limit.allow(client_address(request)):
            return rpc_error(429, -32005, "too many requests, try again later",
                             {"Retry-After": str(RPC_WINDOW_S)})
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return rpc_error(400, -32700, "parse error")
        try:
            outgoing, ids = renumber(payload)
        except NotJsonRpc as error:
            return rpc_error(400, -32600, "invalid request: %s" % (error,))
        try:
            status, body = await self.forward(outgoing)
        except httpx.TimeoutException:
            return rpc_error(504, -32603, "the Bradbury RPC did not answer in time")
        except (httpx.HTTPError, OverflowError):
            return rpc_error(502, -32603, "the Bradbury RPC could not be reached")
        if not body.strip():
            # Notifications only: there is nothing to answer.
            return Response(status_code=status if status != 200 else 204, headers=CORS)
        try:
            answer = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return rpc_error(502, -32603, "the Bradbury RPC answered with something not JSON")
        return Response(content=json.dumps(restore(answer, ids)), status_code=status,
                        media_type="application/json", headers=CORS)

    async def read_capped(self, request):
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > RPC_MAX_BYTES:
            return None
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > RPC_MAX_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def forward(self, outgoing):
        """(status, body) of the RPC's answer. Only the body is sent on:
        none of the caller's headers, its address included, reach the RPC."""
        data = json.dumps(outgoing).encode()
        async with self.client.stream("POST", self.upstream, content=data, headers={
                "Content-Type": "application/json", "Accept": "application/json"}) as answer:
            chunks, size = [], 0
            async for chunk in answer.aiter_bytes():
                size += len(chunk)
                if size > RPC_ANSWER_MAX:
                    raise OverflowError("the answer is too large")
                chunks.append(chunk)
            return answer.status_code, b"".join(chunks)
