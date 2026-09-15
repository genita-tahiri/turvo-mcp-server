# turvo-mcp-server

A **remote MCP (Model Context Protocol) server** that wraps the Turvo public
API so Claude (or any MCP-compatible client) can look up and report on
shipments, carriers, customers, exceptions, and financials.

See [`ASSESSMENT.md`](./ASSESSMENT.md) for the full investigation this build
is based on: what Turvo's public API actually exposes, where the original
5-tool skeleton fell short of that, field-by-field mapping of Curv's
requested reporting data to real Turvo fields, and the limitations that
aren't fixable in code (the 180-day shipment window, no server-side
aggregation, etc).

## What's included

- `turvo_client.py` -- shared HTTP client: OAuth2 token caching/refresh,
  retry with backoff on 429/5xx, a generic pagination helper with a safety
  cap, and response-shaping functions that turn Turvo's large raw payloads
  into compact, clearly-labeled JSON for the model.
- `server.py` -- the MCP server (FastMCP) registering 12 tools (the original
  5, kept backward compatible, plus 7 new ones -- see below).
- `tests/` -- pytest suite (29 tests) covering the HTTP client, pagination,
  response shaping, redaction, and tool-level behavior against mocked Turvo
  responses.
- `requirements.txt` / `requirements-dev.txt` -- runtime vs. test dependencies.
- `.env.example` -- the environment variables the server expects.

## Tools

**Shipments**
- `get_shipment` -- full raw shipment record by ID or customId.
- `search_shipments` -- filter by status, customer, carrier (client-side),
  pickup/delivery/created date ranges; `fields="summary"` (default, compact)
  or `"full"`.
- `update_shipment_status` -- unchanged from the original.
- `get_shipment_financials` -- revenue, carrier cost, gross profit, margin %,
  accessorials, invoice/settlement status for one shipment.
- `get_shipment_activity` -- status-history timeline + actual vs. scheduled
  pickup/delivery per stop, with a computed late flag.
- `query_shipment_report` -- aggregate volume/revenue/cost/margin over a date
  range, optionally grouped by customer/carrier/status. Reads via
  `search_shipments` + a detail call per matching shipment (see
  ASSESSMENT.md §5.1/§7 for why), capped by `max_records`, always reporting
  whether the result was truncated.

**Carriers**
- `get_carrier`, `search_carriers` (now also filterable by MC number, DOT
  number, SCAC, status) -- both redact bank account/routing numbers before
  returning.

**Customers / accounts**
- `search_customers`, `get_customer` -- new.

**Exceptions**
- `search_exceptions` -- new; shipment issues/exceptions via Turvo's
  Exceptions API.

**Financials**
- `get_invoice` -- full invoice/settlement detail by ID (Turvo has no
  invoice search/list endpoint -- get the ID from `get_shipment_financials`
  or `get_shipment` first).

## How it authenticates to Turvo

Unchanged from the original: OAuth2 password grant with a bearer token. From
your Turvo Admin console under **API & Webhooks**, you need a
`TURVO_CLIENT_ID` / `TURVO_CLIENT_SECRET` (Public API Profile), a
`TURVO_API_KEY` (`x-api-key` header), and a Turvo username/password. See
`.env.example` for the full list.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes runtime deps + test tools
cp .env.example .env                  # then fill in real values
export $(cat .env | xargs)            # or use direnv / python-dotenv
python server.py
```

By default this starts an HTTP server on `0.0.0.0:8000` speaking the MCP
Streamable HTTP transport at the `/mcp` path.

Test it locally with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector@latest
```

Point the inspector at `http://localhost:8000/mcp`.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

No real Turvo credentials or network access are needed -- all HTTP calls are
mocked with `respx`.

## Deploying

Same as before -- this repo is currently deployed to Railway at
`turvo-mcp-server-production.up.railway.app`. Any host that can run a normal
long-lived Python web service over HTTPS works; see the transport security
`allowed_hosts` in `server.py` if you move it elsewhere.

## Security notes

- Never commit real Turvo credentials -- use `.env` locally (git-ignored)
  and your host's secrets manager in production.
- Carrier bank account/routing numbers are redacted from every tool response
  by default (`turvo_client.redact_carrier`).
- This server still has **no authentication of its own** in front of it --
  the original repo flagged this as a TODO and it remains one. Worth fixing
  (Cloudflare Access, an OAuth layer, or at minimum a shared-secret header)
  before adding more write-capable tools.
- Review and limit which tools are enabled from Claude's "Search and tools"
  menu, especially `update_shipment_status`.
- If you're cleaning up the repo: `venv/` was accidentally committed before
  `.gitignore` covered it. No secrets were ever committed (verified via
  `git log -p` across the full history), but it's worth `git rm -r --cached
  venv` in a follow-up commit.

## Known limitations (see ASSESSMENT.md for detail)

- `/shipments/list` only covers active shipments created in the last 180
  days -- a Turvo API limit, not something this server can work around.
- Turvo's public API has no server-side aggregation/reporting endpoint;
  `query_shipment_report` computes totals client-side, which costs one API
  call per matching shipment.
- No server-side carrier/owner/mode filter on shipments -- `search_shipments`
  filters carrier client-side; owner and mode aren't filterable at all yet.
- No freeform shipment notes/activity thread in the public API, only status
  history and the single `servicesNote` field.
- Turvo doesn't document rate limits in its OpenAPI spec -- the retry logic
  here is defensive, not tuned to a known number.
