"""Shared Turvo API client: auth, retries, pagination, and response shaping.

Factored out of server.py so it can be unit tested without spinning up the
MCP server, and reused by every tool instead of each tool hand-rolling its
own HTTP + pagination logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("turvo-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TURVO_ENV = os.environ.get("TURVO_ENV", "sandbox")  # "sandbox" or "production"
BASE_URLS = {
    "sandbox": "https://my-sandbox-publicapi.turvo.com/v1",
    "production": "https://publicapi.turvo.com/v1",
}


def _base_url() -> str:
    return BASE_URLS[TURVO_ENV]


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Set it as a host secret -- never hardcode Turvo credentials in source."
        )
    return value


# Retry policy for transient failures. 429 (rate limited) and 5xx are worth
# retrying; anything else (400/401/403/404/etc.) is a real request problem
# and retrying it would just repeat the same failure.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

# Hard ceiling on how many records any single tool call will pull across
# pages, regardless of what the caller asks for. Prevents an unbounded
# "give me everything" query from running forever or blowing up context.
DEFAULT_MAX_RECORDS = 500
HARD_MAX_RECORDS = 2000


class TurvoAPIError(RuntimeError):
    """Raised when a Turvo API call fails after retries are exhausted."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


async def get_access_token(client: Optional[httpx.AsyncClient] = None) -> str:
    """Fetch (and cache) a Turvo OAuth2 access token.

    Turvo access tokens last ~12 hours; we refresh a little early.
    """
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    client_id = _require_env("TURVO_CLIENT_ID")
    client_secret = _require_env("TURVO_CLIENT_SECRET")
    api_key = _require_env("TURVO_API_KEY")
    username = _require_env("TURVO_USERNAME")
    password = _require_env("TURVO_PASSWORD")

    url = f"{_base_url()}/oauth/token"
    params = {"client_id": client_id, "client_secret": client_secret}
    headers = {"x-api-key": api_key}
    body = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
        "scope": "read+trust+write",
        "type": "business",
    }

    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        resp = await client.post(url, params=params, headers=headers, json=body, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + float(data.get("expires_in", 3600))
    logger.info("Refreshed Turvo access token, expires in %s seconds", data.get("expires_in"))
    return _token_cache["access_token"]


def reset_token_cache() -> None:
    """Test helper: clear the cached token so the next call re-authenticates."""
    _token_cache["access_token"] = None
    _token_cache["expires_at"] = 0.0


# ---------------------------------------------------------------------------
# Core request with retry/backoff
# ---------------------------------------------------------------------------


async def turvo_request(
    method: str,
    path: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    **kwargs: Any,
) -> Any:
    """Make an authenticated, retried request against the Turvo public API.

    Retries on 429/5xx with exponential backoff (honoring Retry-After when
    Turvo sends one). Any other non-2xx status raises immediately -- retrying
    a 400/401/403/404 just repeats the same failure.
    """
    api_key = _require_env("TURVO_API_KEY")
    owns_client = client is None
    client = client or httpx.AsyncClient()

    last_error: Optional[Exception] = None
    try:
        for attempt in range(1, MAX_RETRIES + 1):
            token = await get_access_token(client=client)
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token}"
            headers["x-api-key"] = api_key

            try:
                resp = await client.request(
                    method, f"{_base_url()}{path}", headers=headers, timeout=30.0, **kwargs
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == MAX_RETRIES:
                    raise TurvoAPIError(f"Network error calling Turvo ({method} {path}): {exc}") from exc
                await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue

            if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Turvo %s %s returned %s, retrying in %.1fs (attempt %d/%d)",
                    method, path, resp.status_code, delay, attempt, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                raise TurvoAPIError(
                    f"Turvo API error {resp.status_code} on {method} {path}: {resp.text[:500]}",
                    status_code=resp.status_code,
                )

            return resp.json()
    finally:
        if owns_client:
            await client.aclose()

    # Should be unreachable, but keeps type checkers happy.
    raise TurvoAPIError(f"Turvo request failed after {MAX_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def paginate_all(
    fetch_page: Callable[[int, int], Awaitable[dict]],
    *,
    extract_records: Callable[[dict], list],
    page_size: int = 100,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> tuple[list, bool]:
    """Page through a Turvo list endpoint via start/pageSize.

    `fetch_page(start, page_size)` should return the raw decoded Turvo
    response for that page. `extract_records` pulls the list of records out
    of that response shape (Turvo nests them differently per resource, e.g.
    details.shipments vs details.accounts).

    Returns (records, truncated) -- `truncated` is True if more records were
    available than `max_records` allowed us to collect, so callers (and
    Claude) always know whether they're looking at a complete result set.
    """
    max_records = min(max_records, HARD_MAX_RECORDS)
    records: list = []
    start = 0
    while len(records) < max_records:
        remaining = max_records - len(records)
        fetch_size = min(page_size, remaining)
        page = await fetch_page(start, fetch_size)
        page_records = extract_records(page)
        if not page_records:
            return records, False
        records.extend(page_records)
        pagination = (page.get("details") or {}).get("pagination") or {}
        more_available = pagination.get("moreAvailable", len(page_records) == fetch_size)
        start += len(page_records)
        if not more_available:
            return records, False
    # We hit max_records -- find out if there was in fact more.
    probe = await fetch_page(start, 1)
    probe_records = extract_records(probe)
    return records, bool(probe_records)


# ---------------------------------------------------------------------------
# Response shaping -- compact, LLM-friendly views instead of the raw payload
# ---------------------------------------------------------------------------


def _kv(obj: Optional[dict]) -> Optional[str]:
    """Turvo represents most enums as {key, value}; we usually just want value."""
    if not obj:
        return None
    return obj.get("value")


def redact_carrier(carrier: dict) -> dict:
    """Strip bank account/routing numbers from a carrier record before it
    ever reaches the model. These fields are null in Curv's current data but
    the schema allows them, and there's no reporting use case that needs raw
    banking numbers."""
    carrier = dict(carrier)
    for key in ("paymentMethod", "carrier_pay_to"):
        val = carrier.get(key)
        if isinstance(val, list):
            carrier[key] = [_redact_payment_method(v) for v in val]
        elif isinstance(val, dict):
            carrier[key] = _redact_payment_method(val)
    return carrier


def _redact_payment_method(pm: dict) -> dict:
    pm = dict(pm)
    for field in ("accountNumber", "routingNumber"):
        if pm.get(field):
            pm[field] = "[redacted]"
    return pm


def shape_shipment_summary(raw: dict) -> dict:
    """Compact operational view of a shipment -- for list/search results."""
    customer_order = (raw.get("customerOrder") or [{}])[0]
    carrier_orders = raw.get("carrierOrder") or []
    customer = customer_order.get("customer") or {}
    return {
        "id": raw.get("id"),
        "customId": raw.get("customId"),
        "status": _kv(raw.get("status", {}).get("code")),
        "mode": _kv((raw.get("transportation") or {}).get("mode")),
        "created": raw.get("createdDate") or raw.get("created"),
        "pickupScheduled": (raw.get("startDate") or {}).get("date"),
        "deliveryScheduled": (raw.get("endDate") or {}).get("date"),
        "origin": (raw.get("lane") or {}).get("start"),
        "destination": (raw.get("lane") or {}).get("end"),
        "customer": {"id": customer.get("id"), "name": customer.get("name")},
        "carriers": [
            {"id": (co.get("carrier") or {}).get("id"), "name": (co.get("carrier") or {}).get("name")}
            for co in carrier_orders
            if not co.get("deleted")
        ],
        "equipment": [_kv(e.get("type")) for e in raw.get("equipment") or []],
    }


def shape_shipment_financials(raw: dict) -> dict:
    """Financial-only view: revenue, cost, margin, accessorials, invoice status."""
    customer_order = (raw.get("customerOrder") or [{}])[0]
    carrier_orders = raw.get("carrierOrder") or []
    margin = raw.get("margin") or {}
    customer_costs = customer_order.get("costs") or {}
    customer_line_items = customer_costs.get("lineItem") or []
    accessorials = [
        {"code": _kv(li.get("code")), "amount": li.get("amount")}
        for li in customer_line_items
        if _kv(li.get("code")) not in (None, "Freight - flat") and not li.get("deleted")
    ]
    customer_invoice = (customer_order.get("invoice") or [{}])[0] if customer_order.get("invoice") else None
    carrier_financials = []
    for co in carrier_orders:
        if co.get("deleted"):
            continue
        costs = co.get("costs") or {}
        invoice = (co.get("invoice") or [{}])[0] if co.get("invoice") else None
        carrier_financials.append({
            "carrierId": (co.get("carrier") or {}).get("id"),
            "carrierName": (co.get("carrier") or {}).get("name"),
            "cost": costs.get("totalAmount"),
            "settlementStatus": _kv(invoice.get("status")) if invoice else None,
            "settlementAmount": invoice.get("amount") if invoice else None,
            "settlementPaid": invoice.get("isFullyPaid") if invoice else None,
        })
    return {
        "id": raw.get("id"),
        "customId": raw.get("customId"),
        "revenue": customer_costs.get("totalAmount") or margin.get("totalReceivableAmount"),
        "carrierCostTotal": margin.get("totalPayableAmount"),
        "grossProfit": margin.get("value"),
        "marginPercent": margin.get("amount"),
        "accessorials": accessorials,
        "customerInvoice": {
            "status": _kv(customer_invoice.get("status")) if customer_invoice else None,
            "amount": customer_invoice.get("amount") if customer_invoice else None,
            "dueDate": customer_invoice.get("dueDate") if customer_invoice else None,
            "isFullyPaid": customer_invoice.get("isFullyPaid") if customer_invoice else None,
            "invoiceId": customer_invoice.get("invoiceId") if customer_invoice else None,
        } if customer_invoice else None,
        "carrierSettlements": carrier_financials,
    }


def shape_shipment_activity(raw: dict) -> dict:
    """Status timeline + actual vs. scheduled pickup/delivery, with on-time flags."""
    history = [
        {
            "status": _kv(h.get("code")),
            "timestamp": h.get("lastUpdatedOn"),
            "updatedBy": (h.get("lastUpdatedBy") or {}).get("id"),
        }
        for h in raw.get("statusHistory") or []
    ]

    stops = []
    for stop in raw.get("route") or []:
        appt = (stop.get("appointment") or {}).get("start")
        attrs = stop.get("attributes") or {}
        arrival = attrs.get("arrival", {}).get("date")
        departed = attrs.get("departed", {}).get("date")
        stops.append({
            "type": _kv(stop.get("stopType")),
            "location": (stop.get("location") or {}).get("name"),
            "scheduled": appt,
            "actualArrival": arrival,
            "actualDeparture": departed,
            "late": (appt is not None and arrival is not None and arrival > appt) if appt and arrival else None,
        })

    return {
        "id": raw.get("id"),
        "customId": raw.get("customId"),
        "currentStatus": _kv(raw.get("status", {}).get("code")),
        "statusHistory": history,
        "stops": stops,
    }


def shape_customer(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "status": _kv(raw.get("status", {}).get("code")),
        "owner": raw.get("owner"),
        "parentAccount": raw.get("parentAccount"),
        "specialInstructions": raw.get("specialInstructions"),
        "address": raw.get("address"),
        "email": raw.get("email"),
        "phone": raw.get("phone"),
    }
