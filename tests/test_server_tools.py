import json

import httpx
import respx

import server
import turvo_client as tc
from tests.conftest import sample_shipment


def _token_route(base_url):
    return respx.post(f"{base_url}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
    )


@respx.mock
async def test_get_shipment_returns_valid_json_not_python_repr(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/121862582").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": sample_shipment()})
    )
    result = await server.get_shipment("121862582")
    parsed = json.loads(result)  # would raise if this were still str(dict)
    assert parsed["details"]["id"] == 121862582


@respx.mock
async def test_get_shipment_resolves_custom_id(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200, json={"Status": "SUCCESS", "details": {"shipments": [{"id": 121862582}]}}
        )
    )
    respx.get(f"{base_url}/shipments/121862582").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": sample_shipment()})
    )
    result = await server.get_shipment("CL-26498")
    parsed = json.loads(result)
    assert parsed["details"]["id"] == 121862582


@respx.mock
async def test_get_shipment_unknown_custom_id_returns_error_json_not_exception(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": {"shipments": []}})
    )
    result = await server.get_shipment("NOPE-1")
    parsed = json.loads(result)
    assert "error" in parsed


@respx.mock
async def test_get_shipment_404_returns_error_json_not_exception(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/999").mock(return_value=httpx.Response(404, text="not found"))
    result = await server.get_shipment("999")
    parsed = json.loads(result)
    assert parsed["error"]
    assert parsed["status_code"] == 404


@respx.mock
async def test_get_shipment_financials_shapes_output(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/121862582").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": sample_shipment()})
    )
    result = await server.get_shipment_financials("121862582")
    parsed = json.loads(result)
    assert parsed["revenue"] == 12900.0
    assert parsed["grossProfit"] == 1000.0
    assert parsed["marginPercent"] == 8.0


@respx.mock
async def test_search_shipments_default_returns_summaries(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {"shipments": [sample_shipment()], "pagination": {"moreAvailable": False}},
            },
        )
    )
    result = await server.search_shipments(page_size=10)
    parsed = json.loads(result)
    assert parsed["shipments"][0]["customId"] == "CL-26498"
    # summary shape, not the full raw payload
    assert "statusHistory" not in parsed["shipments"][0]


@respx.mock
async def test_search_shipments_full_fields_returns_raw(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {"shipments": [sample_shipment()], "pagination": {"moreAvailable": False}},
            },
        )
    )
    result = await server.search_shipments(page_size=10, fields="full")
    parsed = json.loads(result)
    assert "statusHistory" in parsed["shipments"][0]


@respx.mock
async def test_search_shipments_carrier_id_filters_client_side(base_url):
    _token_route(base_url)
    other = sample_shipment()
    other["id"] = 999
    other["carrierOrder"] = [{"deleted": False, "carrier": {"id": 42, "name": "OTHER CARRIER"}, "costs": {}}]
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {"shipments": [sample_shipment(), other], "pagination": {"moreAvailable": False}},
            },
        )
    )
    result = await server.search_shipments(carrier_id="8604590", page_size=10)
    parsed = json.loads(result)
    assert parsed["matchedCount"] == 1
    assert parsed["shipments"][0]["customId"] == "CL-26498"


@respx.mock
async def test_search_carriers_redacts_payment_info(base_url):
    _token_route(base_url)
    respx.get(f"{base_url}/carriers/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {
                    "accounts": [
                        {
                            "id": 1,
                            "name": "Test Carrier",
                            "paymentMethod": [{"accountNumber": "111", "routingNumber": "222"}],
                        }
                    ]
                },
            },
        )
    )
    result = await server.search_carriers(name="Test Carrier")
    parsed = json.loads(result)
    pm = parsed["details"]["accounts"][0]["paymentMethod"][0]
    assert pm["accountNumber"] == "[redacted]"
    assert pm["routingNumber"] == "[redacted]"


@respx.mock
async def test_query_shipment_report_aggregates_across_shipments(base_url):
    _token_route(base_url)
    s1 = sample_shipment()
    s2 = sample_shipment()
    s2["id"] = 555
    s2["customId"] = "CL-9999"
    s2["margin"] = {"totalReceivableAmount": 1000.0, "totalPayableAmount": 800.0, "amount": 20.0, "value": 200.0}
    s2["customerOrder"][0]["costs"]["totalAmount"] = 1000.0
    s2["carrierOrder"][0]["costs"]["totalAmount"] = 800.0

    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {"shipments": [{"id": 121862582}, {"id": 555}], "pagination": {"moreAvailable": False}},
            },
        )
    )
    respx.get(f"{base_url}/shipments/121862582").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": s1})
    )
    respx.get(f"{base_url}/shipments/555").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": s2})
    )

    result = await server.query_shipment_report(
        date_from="2026-09-01T00:00:00Z", date_to="2026-09-30T00:00:00Z", group_by="customer",
    )
    parsed = json.loads(result)
    assert parsed["shipmentCount"] == 2
    assert parsed["totalRevenue"] == 13900.0
    assert parsed["totalGrossProfit"] == 1200.0
    assert parsed["truncated"] is False
    assert "DYNASTY FARMS INC" in parsed["groups"]


@respx.mock
async def test_query_shipment_report_continues_after_one_detail_failure(base_url, monkeypatch):
    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(tc.asyncio, "sleep", _fake_sleep)
    _token_route(base_url)
    respx.get(f"{base_url}/shipments/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "Status": "SUCCESS",
                "details": {"shipments": [{"id": 1}, {"id": 2}], "pagination": {"moreAvailable": False}},
            },
        )
    )
    respx.get(f"{base_url}/shipments/1").mock(return_value=httpx.Response(500, text="down"))
    respx.get(f"{base_url}/shipments/2").mock(
        return_value=httpx.Response(200, json={"Status": "SUCCESS", "details": sample_shipment()})
    )
    result = await server.query_shipment_report(date_from="2026-09-01T00:00:00Z", date_to="2026-09-30T00:00:00Z")
    parsed = json.loads(result)
    assert parsed["shipmentCount"] == 1
    assert len(parsed["detailFetchFailures"]) == 1
    assert parsed["detailFetchFailures"][0]["shipmentId"] == 1


@respx.mock
async def test_query_shipment_report_rejects_bad_date_field(base_url):
    result = await server.query_shipment_report(
        date_from="2026-09-01T00:00:00Z", date_to="2026-09-30T00:00:00Z", date_field="bogus",
    )
    parsed = json.loads(result)
    assert "error" in parsed
