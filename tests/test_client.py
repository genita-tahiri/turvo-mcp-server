import httpx
import pytest
import respx

import turvo_client as tc


def _token_route(respx_mock, base_url):
    return respx_mock.post(f"{base_url}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
    )


@respx.mock
async def test_get_access_token_caches_between_calls(base_url):
    token_route = _token_route(respx, base_url)
    token1 = await tc.get_access_token()
    token2 = await tc.get_access_token()
    assert token1 == "fake-token"
    assert token2 == "fake-token"
    assert token_route.call_count == 1  # second call served from cache


@respx.mock
async def test_get_access_token_raises_without_required_env(monkeypatch, base_url):
    monkeypatch.delenv("TURVO_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TURVO_CLIENT_ID"):
        await tc.get_access_token()


@respx.mock
async def test_turvo_request_retries_on_429_then_succeeds(base_url):
    _token_route(respx, base_url)
    route = respx.get(f"{base_url}/shipments/123").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"}),
            httpx.Response(200, json={"Status": "SUCCESS", "details": {"id": 123}}),
        ]
    )
    result = await tc.turvo_request("GET", "/shipments/123")
    assert result["details"]["id"] == 123
    assert route.call_count == 2


@respx.mock
async def test_turvo_request_retries_on_500_with_backoff(base_url, monkeypatch):
    # Avoid real sleeping in tests.
    monkeypatch.setattr(tc.asyncio, "sleep", _fake_sleep)
    _token_route(respx, base_url)
    route = respx.get(f"{base_url}/shipments/123").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"Status": "SUCCESS", "details": {"id": 123}}),
        ]
    )
    result = await tc.turvo_request("GET", "/shipments/123")
    assert result["details"]["id"] == 123
    assert route.call_count == 2


@respx.mock
async def test_turvo_request_does_not_retry_on_404(base_url):
    _token_route(respx, base_url)
    route = respx.get(f"{base_url}/shipments/999").mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(tc.TurvoAPIError) as exc_info:
        await tc.turvo_request("GET", "/shipments/999")
    assert exc_info.value.status_code == 404
    assert route.call_count == 1


@respx.mock
async def test_turvo_request_gives_up_after_max_retries(base_url, monkeypatch):
    monkeypatch.setattr(tc.asyncio, "sleep", _fake_sleep)
    _token_route(respx, base_url)
    route = respx.get(f"{base_url}/shipments/123").mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(tc.TurvoAPIError):
        await tc.turvo_request("GET", "/shipments/123")
    assert route.call_count == tc.MAX_RETRIES


async def _fake_sleep(_seconds):
    return None


def _page(records, more_available):
    return {"details": {"shipments": records, "pagination": {"moreAvailable": more_available}}}


def _extract(page):
    return page["details"]["shipments"]


async def test_paginate_all_stops_when_more_available_is_false():
    pages = [_page([{"id": 1}, {"id": 2}], True), _page([{"id": 3}], False)]

    async def fetch_page(start, page_size):
        return pages.pop(0)

    records, truncated = await tc.paginate_all(fetch_page, extract_records=_extract, page_size=2, max_records=100)
    assert [r["id"] for r in records] == [1, 2, 3]
    assert truncated is False


async def test_paginate_all_respects_max_records_and_reports_truncated():
    call_count = 0

    async def fetch_page(start, page_size):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _page([{"id": start + i} for i in range(page_size)], True)
        # probe call after hitting the cap
        return _page([{"id": 9999}], True)

    records, truncated = await tc.paginate_all(fetch_page, extract_records=_extract, page_size=5, max_records=10)
    assert len(records) == 10
    assert truncated is True


async def test_paginate_all_empty_result():
    async def fetch_page(start, page_size):
        return _page([], False)

    records, truncated = await tc.paginate_all(fetch_page, extract_records=_extract, page_size=10, max_records=50)
    assert records == []
    assert truncated is False
