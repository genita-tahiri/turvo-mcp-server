import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import turvo_client as tc
from turvo_client import (
    TurvoAPIError,
    paginate_all,
    redact_carrier,
    shape_customer,
    shape_shipment_activity,
    shape_shipment_financials,
    shape_shipment_summary,
    turvo_request,
)

# ---------------------------------------------------------------------------
# Logging: for HTTP-based MCP servers it's fine to log to stdout/stderr.
# ---------------------------------------------------------------------------
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("turvo-mcp")

mcp = FastMCP(
    "turvo",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "turvo-mcp-server-production.up.railway.app",
            "turvo-mcp-server-production.up.railway.app:*",
        ],
        allowed_origins=[],
    ),
)


def _ok(data: Any) -> str:
    return json.dumps(data, default=str)


def _error(exc: Exception) -> str:
    if isinstance(exc, TurvoAPIError):
        logger.warning("Turvo API error: %s", exc)
        return json.dumps({"error": str(exc), "status_code": exc.status_code})
    logger.exception("Unexpected error calling Turvo")
    return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Shipments -- existing tools, kept backward compatible
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_shipment(shipment_id: str) -> str:
    """Retrieve full details of a Turvo shipment by its numeric ID or customId (e.g. 'CL-25690').

    Returns the complete raw shipment record (route, items, costs, margin,
    contributors, status history, embedded invoice summaries, etc). For
    reporting across many shipments, prefer search_shipments (fields="summary"),
    get_shipment_financials, or get_shipment_activity instead -- they return a
    much smaller, purpose-built payload per shipment.
    """
    try:
        resolved_id = shipment_id
        if not shipment_id.isdigit():
            lookup = await turvo_request(
                "GET", "/shipments/list",
                params={"customId[eq]": shipment_id, "pageSize": 1},
            )
            shipments = lookup.get("details", {}).get("shipments", [])
            if not shipments:
                return _ok({"error": f"No shipment found with customId '{shipment_id}'."})
            resolved_id = str(shipments[0]["id"])
        data = await turvo_request("GET", f"/shipments/{resolved_id}")
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def search_shipments(
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    carrier_id: Optional[str] = None,
    pickup_date_from: Optional[str] = None,
    pickup_date_to: Optional[str] = None,
    delivery_date_from: Optional[str] = None,
    delivery_date_to: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    page_size: int = 10,
    start: int = 0,
    fields: str = "summary",
) -> str:
    """List / filter shipments.

    Only covers ACTIVE shipments created within the last 180 days -- that's a
    limit on Turvo's own /shipments/list endpoint, not something this tool
    can work around.

    Server-side filters (fast, exact): status, customer_id, and the date
    ranges (pickup/delivery/created -- pass UTC datetimes like
    '2026-08-01T00:00:00Z').

    Client-side filter (slower): carrier_id. Turvo's list endpoint has no
    carrier filter, so when carrier_id is set this tool scans additional
    pages internally (up to an internal safety cap) looking for matches --
    it may return fewer than page_size results even when more exist further
    back, and will say so via "truncated" in that case. Prefer filtering by
    customer_id or a date range instead when possible.

    Args:
        status: Turvo shipment status key or value to filter by.
        customer_id: Turvo customer/account ID.
        carrier_id: Turvo carrier ID (client-side filtered, see above).
        pickup_date_from / pickup_date_to: UTC datetime bounds on scheduled pickup.
        delivery_date_from / delivery_date_to: UTC datetime bounds on scheduled delivery.
        created_from / created_to: UTC datetime bounds on shipment creation.
        page_size: Max number of results to return (default 10).
        start: Number of records to skip, for paging through results (default 0).
        fields: "summary" (compact, default) or "full" (entire raw shipment per record).
    """
    try:
        params: dict[str, Any] = {}
        if status:
            params["status[eq]"] = status
        if customer_id:
            params["customerId[eq]"] = customer_id
        if pickup_date_from:
            params["pickupDate[gte]"] = pickup_date_from
        if pickup_date_to:
            params["pickupDate[lte]"] = pickup_date_to
        if delivery_date_from:
            params["deliveryDate[gte]"] = delivery_date_from
        if delivery_date_to:
            params["deliveryDate[lte]"] = delivery_date_to
        if created_from:
            params["created[gte]"] = created_from
        if created_to:
            params["created[lte]"] = created_to

        def extract(page: dict) -> list:
            return (page.get("details") or {}).get("shipments") or []

        if carrier_id:
            async def fetch_page(page_start: int, page_size_: int) -> dict:
                p = dict(params)
                p["start"] = page_start
                p["pageSize"] = page_size_
                return await turvo_request("GET", "/shipments/list", params=p)

            scan_cap = max(page_size * 20, 100)
            all_records, truncated = await paginate_all(
                fetch_page, extract_records=extract, page_size=100, max_records=scan_cap,
            )
            matches = [r for r in all_records if str((r.get("carrierOrder") or [{}])[0].get("carrier", {}).get("id")) == str(carrier_id)
                       or any(str((co.get("carrier") or {}).get("id")) == str(carrier_id) for co in r.get("carrierOrder") or [])]
            page = matches[start:start + page_size]
            result = {
                "shipments": page if fields == "full" else [shape_shipment_summary(s) for s in page],
                "matchedCount": len(matches),
                "scannedCount": len(all_records),
                "truncated": truncated and len(matches) <= page_size,
                "note": "carrier_id is filtered client-side; increase scan scope by narrowing with customer_id or a date range if results seem incomplete.",
            }
            return _ok(result)

        params["start"] = start
        params["pageSize"] = page_size
        data = await turvo_request("GET", "/shipments/list", params=params)
        shipments = extract(data)
        if fields != "full":
            shipments = [shape_shipment_summary(s) for s in shipments]
        pagination = (data.get("details") or {}).get("pagination") or {}
        return _ok({"shipments": shipments, "pagination": pagination})
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def update_shipment_status(shipment_id: str, status_key: str, status_value: str) -> str:
    """Update the status of a shipment.

    Args:
        shipment_id: The Turvo shipment ID.
        status_key: The Turvo lookup key for the new status (see Turvo API docs -> Lookups -> Shipment).
        status_value: The human-readable value for the new status.
    """
    try:
        body = {"status": {"code": {"key": status_key, "value": status_value}}}
        data = await turvo_request("PUT", f"/shipments/{shipment_id}/status", json=body)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---------------------------------------------------------------------------
# Shipments -- new reporting-focused tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_shipment_financials(shipment_id: str) -> str:
    """Get a compact financial-only view of one shipment: revenue, carrier
    cost, gross profit, margin percent, accessorial charges, and customer/
    carrier invoice (settlement) status. Much smaller than get_shipment --
    use this when you only need the numbers, not the full operational record.
    """
    try:
        resolved_id = shipment_id
        if not shipment_id.isdigit():
            lookup = await turvo_request(
                "GET", "/shipments/list", params={"customId[eq]": shipment_id, "pageSize": 1},
            )
            shipments = lookup.get("details", {}).get("shipments", [])
            if not shipments:
                return _ok({"error": f"No shipment found with customId '{shipment_id}'."})
            resolved_id = str(shipments[0]["id"])
        data = await turvo_request("GET", f"/shipments/{resolved_id}")
        details = data.get("details") or {}
        return _ok(shape_shipment_financials(details))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def get_shipment_activity(shipment_id: str) -> str:
    """Get a shipment's status-history timeline plus actual vs. scheduled
    pickup/delivery times per stop, with a computed "late" flag on each stop.
    Use this for on-time performance questions instead of get_shipment.
    """
    try:
        resolved_id = shipment_id
        if not shipment_id.isdigit():
            lookup = await turvo_request(
                "GET", "/shipments/list", params={"customId[eq]": shipment_id, "pageSize": 1},
            )
            shipments = lookup.get("details", {}).get("shipments", [])
            if not shipments:
                return _ok({"error": f"No shipment found with customId '{shipment_id}'."})
            resolved_id = str(shipments[0]["id"])
        data = await turvo_request("GET", f"/shipments/{resolved_id}")
        details = data.get("details") or {}
        return _ok(shape_shipment_activity(details))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def query_shipment_report(
    date_from: str,
    date_to: str,
    date_field: str = "pickupDate",
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    group_by: Optional[str] = None,
    max_records: int = 200,
) -> str:
    """Aggregate shipment volume and financials over a date range.

    Turvo's API has no server-side aggregation endpoint, so this tool pages
    through matching shipments and fetches per-shipment financial detail for
    each one to compute real totals (list-level revenue/cost fields are
    unreliable -- see the connector's ASSESSMENT.md). That means cost scales
    with the number of matching shipments: narrow with customer_id and/or a
    tighter date range for faster, cheaper reports. Only shipments created in
    the last 180 days are reachable at all (a Turvo API limit).

    Args:
        date_from / date_to: UTC datetime bounds, e.g. '2026-08-01T00:00:00Z'.
        date_field: Which date to filter on -- "pickupDate", "deliveryDate", or "created".
        customer_id: Optional Turvo customer/account ID to restrict to one customer.
        status: Optional shipment status to restrict to.
        group_by: Optional breakdown dimension -- "customer", "carrier", or "status".
        max_records: Safety cap on shipments included (default 200, hard max 2000).
            The response's "truncated" field tells you if more matches existed
            than this cap allowed.
    """
    try:
        if date_field not in ("pickupDate", "deliveryDate", "created"):
            return _ok({"error": "date_field must be one of: pickupDate, deliveryDate, created"})

        params: dict[str, Any] = {f"{date_field}[gte]": date_from, f"{date_field}[lte]": date_to}
        if customer_id:
            params["customerId[eq]"] = customer_id
        if status:
            params["status[eq]"] = status

        async def fetch_page(page_start: int, page_size_: int) -> dict:
            p = dict(params)
            p["start"] = page_start
            p["pageSize"] = page_size_
            return await turvo_request("GET", "/shipments/list", params=p)

        def extract(page: dict) -> list:
            return (page.get("details") or {}).get("shipments") or []

        summaries, truncated = await paginate_all(
            fetch_page, extract_records=extract, page_size=100, max_records=max_records,
        )

        semaphore = asyncio.Semaphore(5)
        failures: list[dict] = []

        async def fetch_detail(shipment_id: Any) -> Optional[dict]:
            async with semaphore:
                try:
                    resp = await turvo_request("GET", f"/shipments/{shipment_id}")
                    return (resp.get("details") or {})
                except Exception as exc:  # noqa: BLE001
                    failures.append({"shipmentId": shipment_id, "error": str(exc)})
                    return None

        details_list = await asyncio.gather(*(fetch_detail(s["id"]) for s in summaries))
        details_list = [d for d in details_list if d is not None]

        total_revenue = 0.0
        total_cost = 0.0
        total_profit = 0.0
        margin_values = []
        groups: dict[str, dict] = {}

        for d in details_list:
            fin = shape_shipment_financials(d)
            summ = shape_shipment_summary(d)
            revenue = fin.get("revenue") or 0.0
            cost = fin.get("carrierCostTotal") or 0.0
            profit = fin.get("grossProfit") or 0.0
            total_revenue += revenue
            total_cost += cost
            total_profit += profit
            if fin.get("marginPercent") is not None:
                margin_values.append(fin["marginPercent"])

            if group_by == "customer":
                key = summ["customer"]["name"] or "Unknown"
            elif group_by == "carrier":
                key = summ["carriers"][0]["name"] if summ["carriers"] else "Unassigned"
            elif group_by == "status":
                key = summ["status"] or "Unknown"
            else:
                key = None

            if key is not None:
                g = groups.setdefault(key, {"shipmentCount": 0, "revenue": 0.0, "carrierCost": 0.0, "grossProfit": 0.0})
                g["shipmentCount"] += 1
                g["revenue"] += revenue
                g["carrierCost"] += cost
                g["grossProfit"] += profit

        result = {
            "dateField": date_field,
            "dateFrom": date_from,
            "dateTo": date_to,
            "shipmentCount": len(details_list),
            "totalRevenue": round(total_revenue, 2),
            "totalCarrierCost": round(total_cost, 2),
            "totalGrossProfit": round(total_profit, 2),
            "blendedMarginPercent": round((total_profit / total_revenue * 100), 2) if total_revenue else None,
            "averageMarginPercentSimple": round(sum(margin_values) / len(margin_values), 2) if margin_values else None,
            "truncated": truncated,
            "detailFetchFailures": failures,
        }
        if group_by:
            result["groupBy"] = group_by
            result["groups"] = {
                k: {**v, "revenue": round(v["revenue"], 2), "carrierCost": round(v["carrierCost"], 2), "grossProfit": round(v["grossProfit"], 2)}
                for k, v in sorted(groups.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
            }
        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---------------------------------------------------------------------------
# Carriers -- existing tools, kept backward compatible
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_carrier(carrier_id: str) -> str:
    """Retrieve details of a carrier account by ID.

    Args:
        carrier_id: The Turvo carrier account ID.
    """
    try:
        data = await turvo_request("GET", f"/carriers/{carrier_id}")
        details = data.get("details")
        if details is not None:
            data = {**data, "details": redact_carrier(details)}
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def search_carriers(
    name: Optional[str] = None,
    mc_number: Optional[str] = None,
    dot_number: Optional[str] = None,
    scac: Optional[str] = None,
    status: Optional[str] = None,
    page_size: int = 10,
    start: int = 0,
) -> str:
    """Search carrier accounts.

    Args:
        name: Optional carrier name to filter by.
        mc_number: Optional MC number to filter by.
        dot_number: Optional DOT number to filter by.
        scac: Optional SCAC code to filter by.
        status: Optional carrier status (e.g. "Active", "Inactive") to filter by.
        page_size: Max number of results to return (default 10).
        start: Number of records to skip, for paging through results (default 0).
    """
    try:
        params: dict[str, Any] = {"pageSize": page_size, "start": start}
        if name:
            params["name[eq]"] = name
        if mc_number:
            params["mcNumber[eq]"] = mc_number
        if dot_number:
            params["dotNumber[eq]"] = dot_number
        if scac:
            params["scac[eq]"] = scac
        if status:
            params["status[eq]"] = status
        data = await turvo_request("GET", "/carriers/list", params=params)
        details = data.get("details") or {}
        accounts = details.get("accounts") or []
        details["accounts"] = [redact_carrier(a) for a in accounts]
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---------------------------------------------------------------------------
# Customers / accounts -- new
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_customers(
    name: Optional[str] = None,
    status: Optional[str] = None,
    parent_account: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    page_size: int = 10,
    start: int = 0,
) -> str:
    """Search customer/account records.

    Note: Turvo doesn't return revenue, cost, or shipment-volume rollups on
    customer records -- those aren't stored on the account, they have to be
    computed from that customer's shipments (see query_shipment_report with
    customer_id set).

    Args:
        name: Optional customer name to filter by.
        status: Optional account status to filter by.
        parent_account: Optional parent account ID to filter by.
        created_from / created_to: UTC datetime bounds on account creation.
        page_size: Max number of results to return (default 10).
        start: Number of records to skip, for paging through results (default 0).
    """
    try:
        params: dict[str, Any] = {"pageSize": page_size, "start": start}
        if name:
            params["name[eq]"] = name
        if status:
            params["status[eq]"] = status
        if parent_account:
            params["parentAccount[eq]"] = parent_account
        if created_from:
            params["created[gte]"] = created_from
        if created_to:
            params["created[lte]"] = created_to
        data = await turvo_request("GET", "/customers/list", params=params)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool()
async def get_customer(customer_id: str) -> str:
    """Retrieve details of a customer/account by ID, including account
    owner/sales rep, status, parent account, and billing contact info.

    Args:
        customer_id: The Turvo customer/account ID.
    """
    try:
        data = await turvo_request("GET", f"/customers/{customer_id}")
        details = data.get("details") or {}
        return _ok(shape_customer(details))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---------------------------------------------------------------------------
# Exceptions -- new
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_exceptions(
    context: Optional[str] = None,
    context_id: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    page_size: int = 10,
    start: int = 0,
) -> str:
    """Find shipment exceptions/issues via Turvo's Exceptions API. Use this
    for "which shipments currently have issues" style questions.

    Args:
        context: Optional Turvo exception context (e.g. the entity type the exception is on).
        context_id: Optional ID within that context (e.g. a specific shipment ID) to filter to.
        created_from / created_to: UTC datetime bounds on when the exception was created.
        page_size: Max number of results to return (default 10).
        start: Number of records to skip, for paging through results (default 0).
    """
    try:
        params: dict[str, Any] = {"pageSize": page_size, "start": start}
        if context:
            params["context"] = context
        if context_id:
            params["contextId[eq]"] = context_id
        if created_from:
            params["created[gte]"] = created_from
        if created_to:
            params["created[lte]"] = created_to
        data = await turvo_request("GET", "/exceptions/list", params=params)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ---------------------------------------------------------------------------
# Invoices (Settlements 2.0) -- new
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_invoice(invoice_id: str) -> str:
    """Retrieve full detail for a single invoice/settlement (line items,
    allocations, payment status) by its Turvo invoice ID. Invoice IDs for a
    shipment are found via get_shipment_financials (customerInvoice.invoiceId)
    or get_shipment (customerOrder[].invoice[] / carrierOrder[].invoice[]) --
    Turvo has no endpoint to list/search invoices directly.

    Args:
        invoice_id: The Turvo invoice ID.
    """
    try:
        data = await turvo_request("GET", f"/invoice/{invoice_id}")
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


if __name__ == "__main__":
    # Streamable HTTP transport so this can run as a normal web service and be
    # used as a "Remote MCP server URL". Check your installed mcp SDK version
    # for the exact run()/FastMCP() kwargs, as these have changed between
    # releases -- see https://modelcontextprotocol.io for the current API.
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
