"""Lacre as MCP tools: a thin client of the gateway's REST API.

python -m lacre_mcp serves the tools over stdio with the gateway URL and key
from the environment. The gateway itself serves the same tools at /mcp,
where each request's own X-API-Key is the key the tools use.
"""
