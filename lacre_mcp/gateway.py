"""The gateway's REST API over httpx, and nothing more.

Every failure, whether the gateway answered 4xx or 5xx or could not be
reached, becomes a GatewayError carrying what the gateway said, so a tool
turns it into a tool error and the server keeps running.
"""

import json
from urllib.parse import quote

import httpx

DEFAULT_URL = "https://lacre.in-sidr.xyz"
API_KEY_HEADER = "X-API-Key"
# Every endpoint answers from the database or from reads at LATEST_FINAL;
# a minute covers a slow chain read without hanging a tool call for good.
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# How much of a non-JSON error body is quoted back, so a proxy's HTML error
# page does not flood the agent's context.
MAX_DETAIL = 500


class GatewayError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def segment(value):
    """One path segment. Nothing an agent passes can add a segment or a
    query, so a tool reaches only the endpoint it names."""
    return quote(str(value), safe="")


class Gateway:
    def __init__(self, base_url, api_key, transport=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    async def call(self, method, path, *, params=None, data=None, files=None, ok=(200,)):
        """The decoded JSON answer, or GatewayError.

        ok lists the statuses whose body is the answer rather than an error.
        """
        if not self.api_key:
            raise GatewayError("no API key: set LACRE_API_KEY, or send X-API-Key to /mcp")
        try:
            async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport,
                                         headers={API_KEY_HEADER: self.api_key},
                                         timeout=TIMEOUT) as client:
                response = await client.request(method, path, params=params, data=data,
                                                files=files)
        except httpx.HTTPError as error:
            # The message names the failure; the request, which carries the
            # key in a header and possibly a message, is left out.
            raise GatewayError("the gateway could not be reached: %s%s"
                               % (type(error).__name__, (": %s" % (error,)) if str(error) else ""))
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code in ok and body is not None:
            return response.status_code, body
        raise GatewayError("the gateway answered %d: %s"
                           % (response.status_code, detail(response, body)),
                           status=response.status_code)


def detail(response, body):
    if isinstance(body, dict) and "detail" in body:
        found = body["detail"]
        return found if isinstance(found, str) else json.dumps(found)
    if body is not None:
        return json.dumps(body)
    text = response.text.strip()
    return text[:MAX_DETAIL] if text else response.reason_phrase or "no body"
