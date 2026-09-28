# PSX MCP server

A remote MCP connector that gives Claude live Pakistan Stock Exchange data:
prices, market summary, top movers, company fundamentals, price history with
indicators, dividends, announcements, index members and sectors.

Data comes from the public PSX Data Portal (dps.psx.com.pk). This is for
**personal use**. Redistributing PSX market data needs a PSX licence.

## Tools

| Tool | What it answers |
|---|---|
| `psx_market_summary` | "How is the market today?" Index levels, breadth, top 5 gainers, losers and most active |
| `psx_quote` | Price, change, volume and day range for one or many tickers |
| `psx_top_movers` | Gainers, losers, most active or top traded value, filterable by Shariah (KMI), sector, volume and price |
| `psx_company` | P/E, 52‑week range, 1Y/YTD change, profile, market cap, free float, annual/quarterly financials, ratios, recent announcements |
| `psx_price_history` | Daily, weekly or intraday bars plus returns, SMA 20/50/200, RSI 14, MACD, volatility |
| `psx_dividends` | Payout history with book closure dates and PKR per share |
| `psx_announcements` | Latest results, board meetings and notices, with PDF links |
| `psx_index_constituents` | KSE100, KMI30 and other index members with weights and points contributed |
| `psx_sectors` | Sector breadth, turnover and market cap |
| `psx_search` | Find a ticker from a company name |
| `psx_selftest` | Checks every PSX endpoint; run it first after deploying |

## How it talks to PSX

The portal's data endpoints only answer browser AJAX calls. They need
`X-Requested-With: XMLHttpRequest` plus an `X-Req-Id` token that the portal
embeds in its HTML (`window.__ps._k`). The client fetches that token from the
homepage, refreshes it on a 403, paces requests (0.35 s apart, 2 at a time),
retries 503s, and caches results (30 s for prices, 1 h for daily history,
24 h for the symbol list). If PSX changes its page layout, fixes go in
`psx_mcp/parsers.py`.

## Deploy on Render (free)

1. Create a **private** GitHub repo (for example `psx-mcp`) and upload
   everything in this folder.
2. On render.com, choose **New → Blueprint**, pick the repo, and Render reads
   `render.yaml` (Singapore region, free plan).
3. When it asks for `MCP_SECRET`, paste a long random string (32+ letters and
   digits, no `/`).
4. After the deploy goes green, open `https://<your-app>.onrender.com/health`.
   It should show `{"status":"ok"}`.
5. Your connector URL is:
   `https://<your-app>.onrender.com/<MCP_SECRET>/mcp`

## Connect to Claude

Go to claude.ai → **Settings → Connectors → Add custom connector**. Name it
`PSX`, paste the connector URL, and leave OAuth empty. In a chat, enable the
connector and ask Claude to run `psx_selftest`. All 11 checks should pass.

Treat the connector URL like a password, since anyone with it can use your server.

## Things to know

* **Cold starts:** Render's free plan sleeps after about 15 minutes idle. The
  first call after that can take 30 to 60 seconds, and Claude may time out
  once. Retry, or upgrade to the Starter plan to keep it awake.
* **IP blocking:** if `psx_selftest` shows the token or market watch failing
  with 403s, PSX is blocking the host's IP. Try another region or host.
  Running locally on a Pakistani connection always works.
* **Accuracy:** the numbers are PSX's own. The portal can lag the live feed
  by a little. Tool output is data, not investment advice.

## Run locally / test

```bash
pip install -r requirements-dev.txt
pytest -q tests                      # uses saved fixtures, no network
MCP_SECRET=local-dev-secret-123456 python -m psx_mcp.server
# connector URL: http://localhost:8000/local-dev-secret-123456/mcp
```
