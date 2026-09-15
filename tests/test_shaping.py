from tests.conftest import sample_carrier, sample_shipment

from turvo_client import (
    redact_carrier,
    shape_shipment_activity,
    shape_shipment_financials,
    shape_shipment_summary,
)


def test_shape_shipment_summary_has_core_operational_fields():
    summary = shape_shipment_summary(sample_shipment())
    assert summary["id"] == 121862582
    assert summary["customId"] == "CL-26498"
    assert summary["status"] == "En route"
    assert summary["mode"] == "TL"
    assert summary["origin"] == "SANTA MARIA, CA, US"
    assert summary["destination"] == "Haines City, FL, US"
    assert summary["customer"] == {"id": 5885703, "name": "DYNASTY FARMS INC"}
    assert summary["carriers"] == [{"id": 8604590, "name": "PATHFINDER TRANS INC"}]
    assert summary["equipment"] == ["Reefer"]


def test_shape_shipment_financials_computes_revenue_cost_margin():
    fin = shape_shipment_financials(sample_shipment())
    assert fin["revenue"] == 12900.0
    assert fin["carrierCostTotal"] == 11900.0
    assert fin["grossProfit"] == 1000.0
    assert fin["marginPercent"] == 8.0


def test_shape_shipment_financials_extracts_accessorials_excluding_freight():
    fin = shape_shipment_financials(sample_shipment())
    codes = [a["code"] for a in fin["accessorials"]]
    assert "Detention" in codes
    assert "Freight - flat" not in codes


def test_shape_shipment_financials_includes_invoice_status():
    fin = shape_shipment_financials(sample_shipment())
    assert fin["customerInvoice"]["status"] == "Approved"
    assert fin["customerInvoice"]["isFullyPaid"] is False
    assert fin["customerInvoice"]["invoiceId"] == "abc-123"
    assert fin["carrierSettlements"][0]["settlementStatus"] == "Draft"


def test_shape_shipment_activity_builds_timeline_and_stops():
    activity = shape_shipment_activity(sample_shipment())
    assert activity["currentStatus"] == "En route"
    assert len(activity["statusHistory"]) == 2
    assert activity["statusHistory"][1]["status"] == "En route"
    pickup_stop = activity["stops"][0]
    assert pickup_stop["type"] == "Pickup"
    assert pickup_stop["actualArrival"] == "2026-09-12T00:21:31Z"
    # Scheduled appointment was 2026-09-11T15:00:00Z; actual arrival was
    # 2026-09-12T00:21:31Z, i.e. after it -- so this stop was late.
    assert pickup_stop["late"] is True


def test_shape_shipment_activity_delivery_stop_has_no_actuals_yet():
    activity = shape_shipment_activity(sample_shipment())
    delivery_stop = activity["stops"][1]
    assert delivery_stop["actualArrival"] is None
    assert delivery_stop["late"] is None


def test_redact_carrier_strips_bank_numbers_but_keeps_other_fields():
    redacted = redact_carrier(sample_carrier())
    assert redacted["name"] == "KHARB BROS TRUCKING INC"
    assert redacted["mcNumber"] == "1681813"
    assert redacted["paymentMethod"][0]["accountNumber"] == "[redacted]"
    assert redacted["paymentMethod"][0]["routingNumber"] == "[redacted]"
    assert redacted["paymentMethod"][0]["bankName"] == "Some Bank"  # not sensitive on its own
    assert redacted["carrier_pay_to"]["accountNumber"] == "[redacted]"


def test_redact_carrier_does_not_mutate_input():
    original = sample_carrier()
    redact_carrier(original)
    assert original["paymentMethod"][0]["accountNumber"] == "987654321"
