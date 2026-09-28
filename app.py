"""Vercel entrypoint: Vercel loads the top-level `app` and routes every request to it."""
from psx_mcp.server import build_serverless_app

app = build_serverless_app()
