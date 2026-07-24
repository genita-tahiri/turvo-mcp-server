# turvo-mcp-server

A starter **remote MCP (Model Context Protocol) server** that wraps the Turvo
public API so Claude (or any MCP-compatible client) can look up shipments,
carriers, and update shipment status on your behalf.

This is a starting skeleton, not a finished production integration. Review
and extend it before relying on it with real data.

## What's included

- `server.py` -- an MCP server (using the official `mcp` Python SDK) exposing
  a handful of tools: `get_shipment`, `search_shipments`,
  `update_shipment_status`, `get_carrier`, `search_carriers`.
- `requirements.txt` -- Python dependencies.
- `.env.example` -- the environment variables the server expects (copy to
  `.env` locally, or set as secrets on your host -- never commit real values).

## How it authenticates to Turvo

Turvo uses OAuth2 (password grant) with a bearer token. You'll need, from
your Turvo Admin console under **API & Webhooks**:

- `TURVO_CLIENT_ID` / `TURVO_CLIENT_SECRET` (a Public API Profile)
- `TURVO_API_KEY` (the `x-api-key` header value)
- A Turvo username/password for the account the integration acts as

See `.env.example` for the full list. `server.py` caches the access token
in memory and refreshes it automatically.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
export $(cat .env | xargs)  # or use a tool like direnv / python-dotenv
python server.py
```

By default this starts an HTTP server on `0.0.0.0:8000` speaking the MCP
Streamable HTTP transport at the `/mcp` path (confirm the exact path/kwargs
against your installed `mcp` SDK version -- the API has changed across
releases, see https://modelcontextprotocol.io).

You can test it locally with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector@latest
```

Point the inspector at `http://localhost:8000/mcp`.

## Deploying to get a public "Remote MCP server URL"

Claude's custom connectors are reached from Anthropic's cloud, not your local
machine, so this needs to run somewhere with a public HTTPS address. Options
include:

- **Render / Fly.io / a small VPS**: deploy this repo as a normal long-running
  Python web service, put it behind HTTPS, and use
  `https://your-domain.com/mcp` as the connector URL.
- **Cloudflare Workers**: Cloudflare has a template specifically for remote
  MCP servers (JavaScript/TypeScript, not Python) -- see
  https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/
  if you'd rather port the tools to that stack for a one-command deploy.
- **AWS / GCP / Azure**: any container or serverless HTTP service works, as
  long as it exposes the MCP endpoint over HTTPS.

Once deployed, take the resulting URL (e.g. `https://your-app.example.com/mcp`)
and paste it into Claude Desktop's **Settings > Connectors > Add custom
connector > Remote MCP server URL** field.

## Security notes

- Never commit real Turvo credentials -- use `.env` locally (already
  git-ignored) and your host's secrets manager in production.
- Consider creating a dedicated Turvo API profile for this integration rather
  than reusing one shared with other tools.
- Add authentication in front of this server before exposing it publicly if
  it will hold write access to Turvo (e.g. Cloudflare Access, an OAuth layer,
  or at minimum an IP allowlist / shared secret header), since currently
  `server.py` has no auth of its own.
- Review and limit which tools are enabled from Claude's "Search and tools"
  menu, especially any that can modify data (like `update_shipment_status`).
# turvo-mcp-server
Remote MCP server exposing Turvo TMS API as tools fr Claude
