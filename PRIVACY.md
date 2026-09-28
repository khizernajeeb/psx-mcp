# Privacy policy

This connector proxies public market data from the PSX Data Portal
(dps.psx.com.pk) to Claude. It does not have user accounts and does not
authenticate individual people, so it has very little to say about personal
data — but here is what happens in practice.

## What data is processed

* **Tool arguments** (tickers, dates, portfolio holdings you type into
  Claude) are used only to build the request to the PSX Data Portal and to
  shape the response. They are not logged, written to disk, or sent anywhere
  other than the PSX portal itself.
* **`psx_portfolio` holdings** (symbol/quantity/cost) are used to compute the
  answer for that one request and are never persisted.
* **No accounts, cookies, or tracking.** There is no login, no analytics, and
  no third-party trackers in this server.

## Caching

Responses from PSX are cached in memory, keyed by ticker/date/report type
(never by user or session), for between 30 seconds and 24 hours depending on
the endpoint (see `psx_mcp/client.py`). The cache is process-local: it is not
shared across deployments and is cleared whenever the process restarts.

## Logs

Standard web server / hosting-platform logs (request path, status code,
timestamp) may be retained by whichever host runs this server (Render,
Vercel, or your own machine), per that host's own log-retention policy. This
project does not add its own request logging beyond Python's standard
library logging at `INFO` level (errors and startup messages, no tool
arguments).

## Third parties

The only outbound calls this server makes are to `dps.psx.com.pk` (the
public PSX Data Portal) to fetch market data. No data is shared with any
other third party.

## Access control

Access to a deployed instance is gated by a secret path segment
(`MCP_SECRET`) chosen by whoever deploys it. Treat that URL like a password:
anyone who has it can query the server.

## Changes

This policy may be updated as the project evolves; the current version is
always the one in this file in the project's GitHub repository.

## Contact

Questions or concerns: open an issue at
https://github.com/khizernajeeb/psx-mcp/issues.
