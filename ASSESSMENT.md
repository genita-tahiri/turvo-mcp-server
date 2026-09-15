# Turvo MCP Connector — Technical Assessment & Implementation

Prepared for Curv Logistics. Covers the investigation requested before any code changes, followed by the implementation that was built on top of the findings.

## 1. Executive summary

- The connector is a genuine **starter skeleton** (the repo's own README says so), not a deliberately restricted integration. All 5 tools are a straight 1:1 wrapper around 5 of the ~150 operations Turvo's public API exposes. There is no unexposed/hidden functionality already sitting in the code — every Turvo call that exists in `server.py` is already registered as an MCP tool.
- Auth already requests the **widest available OAuth scope** (`read+trust+write`), so the missing breadth is not an auth/scope problem. It's an implementation-coverage problem.
- Turvo's public API has **no native aggregation/reporting/analytics endpoint** anywhere (0 of 97 documented paths). Every "revenue last month," "margin by customer," etc. question has to be computed client-side from shipment records — there is no shortcut on Turvo's side.
- The single biggest scaling concern: the list endpoint (`/shipments/list`) does **not** reliably return revenue/cost/margin (its `netRevenue`/`netCustomerCosts`/`netCarrierCosts` fields read `0.0` in every live sample pulled during this investigation, regardless of shipment phase). The real numbers live in the **per-shipment detail** response (`costs`, `margin`, `invoice` blocks). This means any financial report needs one detail call per matching shipment, not just one list call — a real cost/latency tradeoff addressed below.
- `/shipments/list` is documented as **"Filter active shipments created within last 180 days"** — a hard window on how far back this endpoint can report, independent of anything in our code.
- Turvo has no shipment "notes/activity" endpoint separate from what's already embedded in the shipment detail (`statusHistory`, `servicesNote`). If Curv needs freeform internal notes/comments, that data was not found anywhere in the public API and would need to be confirmed with Turvo support as possibly app-only.

## 2. Current architecture (before this change)

- **Location:** `genita-tahiri/turvo-mcp-server` on GitHub, deployed on Railway (`turvo-mcp-server-production.up.railway.app`), reached by Claude as a custom remote MCP connector over Streamable HTTP at `/mcp`.
- **Structure:** a single 176-line `server.py` using the official `mcp` Python SDK's `FastMCP`. No modules, no models/types, no tests, no CI.
- **Deps:** `mcp[cli]` and `httpx` only (`requirements.txt`).
- **Repo hygiene note (not a security issue):** `venv/` is committed despite being in `.gitignore` (leftover from before the ignore rule was added). No `.env` or real credentials were ever committed — confirmed via `git log -p` across the full history. `.env.example` is referenced in the README's setup instructions but doesn't actually exist in the repo.
- **No auth in front of the server itself** — the README already flags this as a TODO. Out of scope for this reporting-focused change, but worth Curv's attention before this holds any write-capable tool.

## 3. Current Turvo API integration

- **Base URL:** Turvo's **Public API v1** (`publicapi.turvo.com/v1` prod / `my-sandbox-publicapi.turvo.com/v1` sandbox) — this is Turvo's full self-service public API, not a cut-down partner tier. Confirmed by pulling Turvo's own live OpenAPI spec (see §5) — everything in it is reachable from the same credentials this server already has.
- **Auth flow:** OAuth2 Resource Owner Password Credentials grant (`grant_type: password`) against `/oauth/token`, using `TURVO_CLIENT_ID`, `TURVO_CLIENT_SECRET`, `TURVO_API_KEY` (sent as `x-api-key`), and a Turvo username/password, requesting `scope: read+trust+write`. Token is cached in memory and refreshed ~60s before the ~12h expiry.
- This pattern is sound and was kept as-is (see §7) — token caching, refresh-before-expiry, and `x-api-key` + bearer token on every request are all correct.

## 4. Current 5 MCP tools

| Tool | Turvo call | Notes |
|---|---|---|
| `get_shipment` | `GET /shipments/{id}` (resolves `customId` via `/shipments/list?customId[eq]=` first if not numeric) | Returns the **entire** raw shipment object as a Python `str(dict)` — not valid JSON, and often 5–10KB+ for one shipment. |
| `search_shipments` | `GET /shipments/list` | Only forwards `status`, `pageSize`, `start`. Turvo's list endpoint supports 21 filter params (§5.1); 18 of them were unreachable from this tool. |
| `update_shipment_status` | `PUT /shipments/{id}/status` | Write tool, unchanged in this update. |
| `get_carrier` | `GET /carriers/{id}` | Same `str(dict)` issue; also returns raw banking fields (`accountNumber`/`routingNumber`, currently null in Curv's data but present in the schema) with no redaction. |
| `search_carriers` | `GET /carriers/list` | Only forwards `name`, `pageSize`, `start`; Turvo also supports `mcNumber`, `dotNumber`, `scac`, `taxId`, `status`, `created`/`updated`. |

None of these have retry/backoff, rate-limit handling, or a pagination helper — `resp.raise_for_status()` raises immediately on any non-2xx, with no distinction between a transient 429/5xx (worth retrying) and a real client error.

## 5. Turvo Public API — what's actually there

Pulled directly from Turvo's own live OpenAPI spec (`app.turvo.com/lobby/documentation/main.json`, fetched from an authenticated Curv Turvo session): **97 paths, 153 operations**, covering Users, Groups, Locations, Customers, Factors, Carriers, Contacts, Assets, Items/Inventory, Shipments, LTL Quotes, Offers, Orders, Documents, Settlements 2.0 (Invoices), Tags, Tasks, VIN, Exceptions, Routing Guide, Reservations, Appointments, Parent/Other Accounts, Tracking, and Webhooks.

**There is no `/reports`, `/analytics`, `/dashboard`, or any aggregation endpoint anywhere in the spec.** Every rollup Curv wants has to be computed from raw records.

### 5.1 Shipments — the endpoint that matters most for reporting

`GET /shipments/list` — official description: **"Filter active shipments created within last 180 days."** Supported filters (operators shown where documented):

`created[gte/lte/in]`, `updated[gte/lte/in]`, `customId[eq/in]`, `status[eq/in]`, `locationId[eq/in]`, `pickupDate[gte/lte]`, `deliveryDate[gte/lte]`, `customerId[eq/in]`, `poNumber[eq]`, `bolNumber[eq]`, `containerNumber[eq]`, `proNumber[eq]`, `routeNumber[eq]`, `other[eq]`, `truckNumber[eq]`, `parentAccount[eq]`, `trackingProvider[in]`, `serviceAreaKey[eq]`, `sortBy`, plus `start`/`pageSize` (offset pagination — **no cursor param exists**, despite a `lastObjectKey` field appearing in the response envelope; that field isn't a usable request parameter per the spec).

**Gaps confirmed against the live spec:** no `carrierId` filter, no owner/rep filter, no mode/equipment filter. Those fields exist on the shipment object itself, so they can only be applied by filtering client-side after fetching, not by asking Turvo to do it server-side.

**Financial data gap confirmed empirically:** in every live shipment pulled during this investigation (`En route`, `Ready for billing`, various customers), `search_shipments`' `netCustomerCosts` / `netCarrierCosts` / `netRevenue` fields were `0.0`. The real numbers (`costs.subTotal`, `margin.value`, `margin.amount`, per-carrier `costs.subTotal`) only appear in the **shipment detail** response. This needs to be re-verified by Curv's team against a large enough sample (this assessment only spot-checked a handful of live records) — but as observed, **list-level revenue/cost reporting cannot be trusted**, and financial reporting requires a detail call per shipment.

`GET /shipments/{id}` (detail) already returns, in one call, nearly everything on Curv's requested field list — see §6.

### 5.2 Customers / Accounts

- `GET /customers/list` — filters: `created`, `updated`, `name[eq]`, `status[eq]`, `parentAccount[eq]`. No revenue/volume fields — these are account records, not reporting rollups.
- `GET /customers/{id}` — returns `id`, `name`, `taxId`, `status`, `owner` (sales rep), `parentAccount`, `groups`, `billings`, `contact`, `address`/`email`/`phone`, `accountDistribution` (external accounting-system IDs), `settings.markupProfiles`, `commission`.
- `GET /v2/accounts/{accountType}/list` ("Parent Or Other Accounts") also exists for a higher-level account hierarchy above individual customers. **`accountType`'s allowed values aren't documented in the spec** (blank description) — it wasn't wired into a tool in this pass; treat as experimental and verify with Turvo support or by trial call before relying on it.

### 5.3 Financial / Settlements 2.0 (Invoices)

- `GET /invoice/{invoiceId}` returns `accountId`/`accountName`/`accountType`, `invoiceDate`/`invoiceDueDate`, `invoiceStatus`, `paymentStatus`, `totalBalanceDueAmount`, `totalPaymentAmount`, `costs` (line items), `allocations`, `issuances`.
- **There is no `GET /invoice/list`** — invoices can only be fetched one at a time, by ID, in this API. That would be a dead end **except** that shipment detail already embeds an invoice summary directly: `customerOrder[].invoice[]` (the AR/customer invoice — status, amount, due date, `isFullyPaid`) and `carrierOrder[].invoice[]` (the AP/carrier settlement — same shape). For most reporting questions this embedded summary is sufficient; `GET /invoice/{id}` is only needed for full line-item/allocation detail, using the `invoiceId` found on the shipment.
- No separate "payments"/"billing" endpoint beyond this exists in the spec.

### 5.4 Exceptions

`GET /exceptions/list` (filters: `context`, `contextId[eq]`, `created`, `updated`) is a dedicated Turvo endpoint for shipment exceptions/issues that the current connector doesn't touch at all — directly answers "which shipments currently have issues."

### 5.5 Lookups

There is no live `/lookups` endpoint. Status/charge/equipment code tables (e.g. status key `2105` = "En route") are static reference tables in Turvo's documentation, not a queryable API — but this doesn't matter in practice, because every response already embeds both the code and the human-readable value together (e.g. `status.code: {key: "2105", value: "En route"}`), so a separate lookup call is never actually needed at runtime.

### 5.6 Rate limits

**Not documented anywhere in Turvo's OpenAPI spec.** No `429`/throttling language appears in the spec text. The new client (§7) treats this defensively — retries `429`/5xx with backoff regardless — but Curv should ask their Turvo account team for the actual documented limits rather than relying on trial and error in production.

## 6. Business field → Turvo source mapping

All of these were confirmed against **live shipment/carrier records** pulled during this investigation, not assumed from documentation.

| Requested field | Turvo source | Confirmed |
|---|---|---|
| Shipment / Load ID | `id`, `customId` | ✅ live |
| Shipment status | `status.code.value` (+ `.key`) | ✅ live |
| Created date | `createdDate` / `created` | ✅ live |
| Pickup date (scheduled) | `startDate.date`, or per-stop `globalRoute[].appointment` | ✅ live |
| Actual pickup date/time | `route[].attributes.arrival` / `.departed` on the pickup stop, or `statusHistory` entries `2104 At pickup` / `2115 Picked up` | ✅ live |
| Delivery date (scheduled) | `endDate.date`, or last stop's `appointment` | ✅ live |
| Actual delivery date/time | Last delivery stop's `route[].attributes.arrival`/`.departed`, or `statusHistory` `2106 At delivery` / `2116 Route complete` | ✅ live |
| Origin / Destination | `lane.start` / `lane.end` (summary), full detail in `route[]`/`globalRoute[]` | ✅ live |
| Customer / Account | `customerOrder[].customer.{id,name}` | ✅ live |
| Carrier | `carrierOrder[].carrier.{id,name}` | ✅ live |
| Shipment owner / rep | `contributors[]` (`Broker`, `Operator` roles with user id/name); also `customer.owner` / `carrier.owner` | ✅ live |
| Mode | `transportation.mode.value` | ✅ live |
| Equipment type | `equipment[].type.value` (+ size, temp) | ✅ live |
| Revenue / customer rate | `customerOrder[].costs.subTotal`/`.totalAmount`, or `margin.totalReceivableAmount` | ✅ live |
| Carrier cost | `carrierOrder[].costs.subTotal`/`.totalAmount`, or `margin.totalPayableAmount` | ✅ live |
| Gross profit | `margin.value` | ✅ live |
| Gross margin % | `margin.amount` (confusingly named — it's the percent, `value` is the dollar amount) | ✅ live |
| Accessorials / additional charges | `costs.lineItem[]` entries where `code.value` ≠ `"Freight - flat"` | ✅ live (charge-code table exists in Turvo's docs) |
| Shipment notes | `servicesNote` (special-instructions field) | ✅ live — but this is instructions, not a freeform notes thread |
| Freeform notes / comment thread | — | ❌ not found in the public API; likely app-only if it exists |
| Status history / timeline | `statusHistory[]` (full audit trail: code, timestamp, user) | ✅ live |
| Invoice / payment / billing status | `customerOrder[].invoice[]` (AR), `carrierOrder[].invoice[]` (AP); deeper detail via `GET /invoice/{id}` | ✅ live |
| Team / office | `groups[]` | ✅ live |

### Customer/account fields

| Requested field | Turvo source | Confirmed |
|---|---|---|
| Customer/account ID, name | `GET /customers/{id}`: `id`, `name` | ✅ live |
| Account status | `status.code.value` | ✅ live |
| Account owner / sales rep | `owner.{id,name}` | ✅ live |
| Customer-specific shipment activity, revenue, cost, GP, margin, volume | — | ❌ not returned by any customer endpoint; must be computed by pulling that customer's shipments (`customerId[eq]` filter, §5.1) and summing detail-level financials |

## 7. What was implemented

Per your last message, you'll apply these changes to the repo yourself — everything below is delivered as ready-to-drop-in files plus this document, not pushed directly (this session never had write access to the repo; it was only made public long enough to clone).

### New module: `turvo_client.py`

Factored the shared plumbing out of `server.py` so it's unit-testable and reusable across tools:

- **Same auth/token-caching logic as before** (unchanged behavior).
- **Retry with backoff** on `429` and `5xx` (3 attempts, exponential backoff honoring `Retry-After` when Turvo sends one), no retry on `4xx` other than `429` (those are real request errors).
- **`paginate_all()`** — pages through any Turvo list endpoint via `start`/`pageSize`, with a `max_records` safety cap (default 500, override-able per call) so a broad query can never silently vacuum an unbounded number of records or run forever. Returns `(records, truncated: bool)` so callers — and Claude — always know whether they're looking at the complete set.
- **JSON-safe output everywhere** — replaced every `return str(data)` with real `json.dumps(...)`. This alone makes every existing tool's output properly parseable instead of a Python-repr string.
- **Redaction** — `paymentMethod.accountNumber`/`routingNumber` are stripped from carrier responses by default (they're currently null in Curv's data but the schema allows them; no reason to ever hand full banking numbers to the model).
- **Shaping helpers** — `shape_shipment_summary()`, `shape_shipment_financials()`, `shape_shipment_activity()` condense the ~5-10KB raw shipment object into compact, clearly-labeled JSON (operational vs. financial vs. activity), so reporting-scale tools don't blow up Claude's context per record.

### Existing 5 tools — kept working, extended

- `get_shipment`, `get_carrier`: same signature, same behavior, now return real JSON and (for carrier) redact banking numbers.
- `search_shipments`: same required behavior preserved; added **optional** `customer_id`, `pickup_date_from/to`, `delivery_date_from/to`, `created_from/to`, `carrier_id` (client-side filtered, since Turvo has no server-side carrier filter on this endpoint — documented in the tool description so Claude knows this may need to scan more pages), and `fields="summary"|"full"` to control payload size.
- `search_carriers`: added optional `mc_number`, `dot_number`, `scac`, `status`.
- `update_shipment_status`: unchanged.

### New tools

| Tool | Purpose |
|---|---|
| `get_shipment_financials` | Compact financial-only view of one shipment: revenue, carrier cost, gross profit, margin %, accessorial breakdown, AR/AP invoice status. |
| `get_shipment_activity` | Status-history timeline + actual vs. scheduled pickup/delivery times, with on-time/late flags computed from the two. |
| `search_customers` | Wraps `/customers/list` (name, status, parent account, created/updated range). |
| `get_customer` | Wraps `/customers/{id}`. |
| `search_exceptions` | Wraps `/exceptions/list` — "which shipments currently have issues." |
| `get_invoice` | Wraps `/invoice/{id}` for full line-item/allocation detail beyond the embedded summary. |
| `query_shipment_report` | The aggregation tool: given a date range (+ optional customer/status filter), pages through matching shipments via `search_shipments`, then fetches detail for each (concurrency-limited, retried, capped by `max_records`) to compute total shipment count, revenue, carrier cost, gross profit, average margin, and an optional breakdown by customer/carrier/status. Always reports whether the result set was truncated by the cap, rather than silently under-counting. |

`query_shipment_report` is the one place where the cost/latency tradeoff from §5.1 is unavoidable — computing real revenue across N shipments means N detail calls. The tool defaults to a conservative cap and always tells Claude when it hit that cap, rather than quietly returning a partial number that looks complete.

## 8. Limitations Curv should know about (not fixable in code)

- **180-day window** on `/shipments/list` — nothing beyond that is reachable via this endpoint; multi-year trend questions aren't possible via this API without a different Turvo product/export.
- **No server-side aggregation** anywhere in Turvo's public API — every rollup is computed client-side, which means it costs API calls and time proportional to record count.
- **No server-side carrier/owner/mode filter on shipments** — those filters run client-side after fetch.
- **List-level financials read as `0.0`** in this investigation's samples — needs re-verification at volume, but plan on detail-level calls for real numbers.
- **No freeform shipment notes/activity feed** in the public API, only status history + the single `servicesNote` field.
- **Rate limits undocumented** — ask Turvo directly rather than assuming a number.
- **`v2/accounts/{accountType}` values undocumented** — left out of the new tools until Curv confirms valid `accountType` values with Turvo.
- **No auth in front of the MCP server itself** — pre-existing, flagged in the original README, unrelated to this change but worth fixing before any more write-capable tools are added.
