"""python -m lacre_mcp, or lacre-mcp: the Lacre tools over stdio.

LACRE_GATEWAY_URL (default https://lacre.in-sidr.xyz) and LACRE_API_KEY
come from the environment. stdout is the protocol channel, so nothing else
may print there; logging goes to stderr.
"""

import argparse
import logging
import os
import sys

from .gateway import DEFAULT_URL, Gateway
from .server import build_server


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lacre-mcp",
        description="Serve the Lacre tools over MCP stdio. Reads LACRE_GATEWAY_URL "
                    "(default %s) and LACRE_API_KEY from the environment." % (DEFAULT_URL,))
    parser.add_argument("--list-tools", action="store_true",
                        help="print the tool names and exit")
    options = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    gateway = Gateway(os.environ.get("LACRE_GATEWAY_URL") or DEFAULT_URL,
                      os.environ.get("LACRE_API_KEY", ""))
    server = build_server(lambda ctx: gateway)
    if options.list_tools:
        import anyio

        for tool in anyio.run(server.list_tools):
            print(tool.name)
        return 0
    if not gateway.api_key:
        # Started anyway: every tool answers with this same message, which
        # an agent can show, instead of a server that fails to start.
        logging.getLogger("lacre_mcp").warning("LACRE_API_KEY is not set")
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
