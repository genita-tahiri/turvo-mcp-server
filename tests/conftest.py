import os

import pytest

import turvo_client


@pytest.fixture(autouse=True)
def _turvo_env(monkeypatch):
    """Every test gets fake-but-present Turvo credentials, and a clean token
    cache so tests don't leak state into each other."""
    monkeypatch.setenv("TURVO_ENV", "sandbox")
    monkeypatch.setenv("TURVO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TURVO_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("TURVO_API_KEY", "test-api-key")
    monkeypatch.setenv("TURVO_USERNAME", "test-user")
    monkeypatch.setenv("TURVO_PASSWORD", "test-pass")
    turvo_client.reset_token_cache()
    yield
    turvo_client.reset_token_cache()


@pytest.fixture
def base_url():
    return turvo_client.BASE_URLS["sandbox"]


def sample_shipment() -> dict:
    """Trimmed version of a real shipment payload shape (fields this repo's
    shaping functions actually read), used across multiple tests."""
    return {
        "id": 121862582,
        "customId": "CL-26498",
        "status": {"code": {"key": "2105", "value": "En route"}},
        "transportation": {"mode": {"key": "24105", "value": "TL"}},
        "createdDate": "2026-09-09T16:02:10Z",
        "startDate": {"date": "2026-09-11T15:00:00Z"},
        "endDate": {"date": "2026-09-16T08:00:00Z"},
        "lane": {"start": "SANTA MARIA, CA, US", "end": "Haines City, FL, US"},
        "margin": {
            "totalReceivableAmount": 12900.0,
            "totalPayableAmount": 11900.0,
            "amount": 8.0,
            "value": 1000.0,
        },
        "equipment": [{"type": {"key": "24487", "value": "Reefer"}}],
        "customerOrder": [
            {
                "deleted": False,
                "customer": {"id": 5885703, "name": "DYNASTY FARMS INC"},
                "costs": {
                    "totalAmount": 12900.0,
                    "lineItem": [
                        {"code": {"value": "Freight - flat"}, "amount": 12900.0, "deleted": False},
                        {"code": {"value": "Detention"}, "amount": 150.0, "deleted": False},
                    ],
                },
                "invoice": [
                    {
                        "status": {"value": "Approved"},
                        "amount": 12900.0,
                        "dueDate": "2026-10-15T00:00:00Z",
                        "isFullyPaid": False,
                        "invoiceId": "abc-123",
                    }
                ],
            }
        ],
        "carrierOrder": [
            {
                "deleted": False,
                "carrier": {"id": 8604590, "name": "PATHFINDER TRANS INC"},
                "costs": {"totalAmount": 11900.0},
                "invoice": [{"status": {"value": "Draft"}, "amount": 11900.0, "isFullyPaid": False}],
            }
        ],
        "statusHistory": [
            {"code": {"value": "Draft"}, "lastUpdatedOn": "2026-09-09T16:02:10Z", "lastUpdatedBy": {"id": 1}},
            {"code": {"value": "En route"}, "lastUpdatedOn": "2026-09-15T08:04:28Z", "lastUpdatedBy": {"id": 2}},
        ],
        "route": [
            {
                "stopType": {"value": "Pickup"},
                "location": {"name": "CENTRAL CITY SANTA MARIA"},
                "appointment": {"start": "2026-09-11T15:00:00Z"},
                "attributes": {
                    "arrival": {"date": "2026-09-12T00:21:31Z"},
                    "departed": {"date": "2026-09-12T02:12:00Z"},
                },
            },
            {
                "stopType": {"value": "Delivery"},
                "location": {"name": "RESTAURANT DEPOT #407"},
                "appointment": {"start": "2026-09-16T08:00:00Z"},
                "attributes": {},
            },
        ],
    }


def sample_carrier() -> dict:
    return {
        "id": 8596037,
        "name": "KHARB BROS TRUCKING INC",
        "mcNumber": "1681813",
        "paymentMethod": [
            {
                "type": {"value": "ACH"},
                "bankName": "Some Bank",
                "routingNumber": "123456789",
                "accountNumber": "987654321",
            }
        ],
        "carrier_pay_to": {"name": "KHARB BROS c/o RTS Financial", "accountNumber": "111222333"},
    }
