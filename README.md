# PSX MCP server

A remote MCP connector that gives Claude live Pakistan Stock Exchange data:
prices, market summary, top movers, company fundamentals, price history with
indicators, dividends, announcements, index members and sectors.

Data comes from the public PSX Data Portal (dps.psx.com.pk). This is for
**personal use**. Redistributing PSX market data needs a PSX licence.

See [PRIVACY.md](PRIVACY.md) for what data this server processes, and
[LICENSE](LICENSE) for the code's licence (MIT; separate from PSX's own
licensing terms on the underlying data).

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
| `psx_screener` | Filter every listed stock by P/E, dividend yield, market cap, 1Y return, liquidity, sector, index, Shariah |
| `psx_compare` | Side‑by‑side valuation and technicals for 2–10 stocks |
| `psx_portfolio` | Value your holdings at live prices: P&L, day change, weights, sector and Shariah allocation |
| `psx_recent_payouts` | Market‑wide latest dividend/bonus/right announcements and book closures |
| `psx_corporate_calendar` | Upcoming AGMs, EOGMs and annual review meetings |
| `psx_financial_reports` | Annual and quarterly report PDF links |
| `psx_selftest` | Checks every PSX endpoint; run it first after deploying |

## How it talks to PSX

The portal's data endpoints only answer browser AJAX calls. They need
`X-Requested-With: XMLHttpRequest` plus an `X-Req-Id` token that the portal
embeds in its HTML (`window.__ps._k`). The client fetches that token from the
homepage, refreshes it on a 403, paces requests (0.35 s apart, 2 at a time),
retries 503s, and caches results (30 s for prices, 1 h for daily history,
24 h for the symbol list). If PSX changes its page layout, fixes go in
`psx_mcp/parsers.py`.

## Deploy on Vercel (free, no card)

1. Push this folder to a GitHub repo (public or private both work; nothing
   secret is committed — `MCP_SECRET` is set as an env var, never in code).
2. vercel.com → **Add New… → Project** → import the repo. Leave build settings as detected.
3. Add environment variable `MCP_SECRET` (32+ letters/digits), then **Deploy**.
4. Check `https://<project>.vercel.app/health`.
5. Connector URL: `https://<project>.vercel.app/<MCP_SECRET>/mcp`

`vercel.json` pins the function to Singapore (`sin1`). PSX's firewall (DOSarrest) returns "462 Forbidden Region" to Indian IPs, so avoid `bom1`.
`app.py` is the Vercel entrypoint; it serves MCP statelessly, one request at a time.

## Deploy on Render (free)

1. Create a GitHub repo (for example `psx-mcp`, public or private) and
   upload everything in this folder.
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
connector and ask Claude to run `psx_selftest`. All 16 checks should pass.

Treat the connector URL like a password, since anyone with it can use your server.

## Things to know

* **Single-tenant by design:** this is built for one person's own deployment
  with one shared secret, not a multi-tenant public service. All requests
  from a deployment leave from the same IP and share one in-memory cache; if
  many people used the same instance, PSX could rate-limit or block that IP
  for everyone. Don't share a deployed connector URL widely.
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
