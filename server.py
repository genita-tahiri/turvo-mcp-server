import os
import sys
import time
import logging
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging: for HTTP-based MCP servers it's fine to log to stdout/stderr.
# ---------------------------------------------------------------------------
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("turvo-mcp")

# ---------------------------------------------------------------------------
# Configuration (set these as environment variables / host secrets, never
# hardcode them in source).
# ---------------------------------------------------------------------------
TURVO_ENV = os.environ.get("TURVO_ENV", "sandbox")  # "sandbox" or "production"
BASE_URLS = {
    "sandbox": "https://my-sandbox-publicapi.turvo.com/v1",
    "production": "https://publicapi.turvo.com/v1",
}
BASE_URL = BASE_URLS[TURVO_ENV]

TURVO_CLIENT_ID = os.environ["TURVO_CLIENT_ID"]
TURVO_CLIENT_SECRET = os.environ["TURVO_CLIENT_SECRET"]
TURVO_API_KEY = os.environ["TURVO_API_KEY"]
TURVO_USERNAME = os.environ["TURVO_USERNAME"]
TURVO_PASSWORD = os.environ["TURVO_PASSWORD"]

mcp = FastMCP("turvo")

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


async def get_access_token() -> str:
    """Fetch (and cache) a Turvo OAuth2 access token.

    Turvo access tokens last ~12 hours; we refresh a little early.
    """
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    url = f"{BASE_URL}/oauth/token"
    params = {"client_id": TURVO_CLIENT_ID, "client_secret": TURVO_CLIENT_SECRET}
    headers = {"x-api-key": TURVO_API_KEY}
    body = {
        "grant_type": "password",
        "client_id": TURVO_CLIENT_ID,
        "client_secret": TURVO_CLIENT_SECRET,
        "username": TURVO_USERNAME,
        "password": TURVO_PASSWORD,
        "scope": "read+trust+write",
        "type": "business",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, params=params, headers=headers, json=body, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + float(data.get("expires_in", 3600))
    logger.info("Refreshed Turvo access token, expires in %s seconds", data.get("expires_in"))
    return _token_cache["access_token"]


async def turvo_request(method: str, path: str, **kwargs) -> dict:
    """Make an authenticated request against the Turvo public API."""
    token = await get_access_token()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as client:
        resp = await client.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30.0, **kwargs)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}


# ---------------------------------------------------------------------------
# MCP tools -- each of these becomes something Claude can call.
# Keep tools narrow and well-documented; the docstring is shown to the model.
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_shipment(shipment_id: str) -> str:
    """Retrieve full details of a Turvo shipment by its ID.

    Args:
        shipment_id: The Turvo shipment ID.
    """
    data = await turvo_request("GET", f"/shipments/{shipment_id}")
    return str(data)


@mcp.tool()
async def search_shipments(status: Optional[str] = None, page_size: int = 10) -> str:
    """List / filter shipments, optionally by status.

    Args:
        status: Optional Turvo shipment status key to filter by.
        page_size: Max number of results to return (default 10).
    """
    params: dict[str, Any] = {"pageSize": page_size}
    if status:
        params["status[eq]"] = status
    data = await turvo_request("GET", "/shipments/list", params=params)
    return str(data)


@mcp.tool()
async def update_shipment_status(shipment_id: str, status_key: str, status_value: str) -> str:
    """Update the status of a shipment.

    Args:
        shipment_id: The Turvo shipment ID.
        status_key: The Turvo lookup key for the new status (see Turvo API docs -> Lookups -> Shipment).
        status_value: The human-readable value for the new status.
    """
    body = {"status": {"code": {"key": status_key, "value": status_value}}}
    data = await turvo_request("PUT", f"/shipments/{shipment_id}/status", json=body)
    return str(data)


@mcp.tool()
async def get_carrier(carrier_id: str) -> str:
    """Retrieve details of a carrier account by ID.

    Args:
        carrier_id: The Turvo carrier account ID.
    """
    data = await turvo_request("GET", f"/carriers/{carrier_id}")
    return str(data)


@mcp.tool()
async def search_carriers(name: Optional[str] = None, page_size: int = 10) -> str:
    """Search carrier accounts, optionally filtering by name.

    Args:
        name: Optional carrier name to filter by.
        page_size: Max number of results to return (default 10).
    """
    params: dict[str, Any] = {"pageSize": page_size}
    if name:
        params["name[eq]"] = name
    data = await turvo_request("GET", "/carriers/list", params=params)
    return str(data)


if __name__ == "__main__":
    # Streamable HTTP transport so this can run as a normal web service and be
    # used as a "Remote MCP server URL". Check your installed mcp SDK version
    # for the exact run()/FastMCP() kwargs, as these have changed between
    # releases -- see https://modelcontextprotocol.io for the current API.
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
