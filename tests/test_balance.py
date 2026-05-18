import pytest

from app.services.balance import normalize_result, query_newapi, query_sub2api
from app.security import encrypt_value


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DummyClient:
    payload = {}
    last_request = None

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, headers):
        DummyClient.last_request = {"url": url, "headers": headers, "timeout": self.timeout}
        return DummyResponse(DummyClient.payload)


@pytest.mark.asyncio
async def test_sub2api_parses_remaining_from_quota(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.payload = {"quota": {"remaining": 12.5, "unit": "USD"}, "is_active": True}
    account = {
        "platform": "sub2Api",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
    }

    result = await query_sub2api(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["remaining"] == 12.5
    assert result["unit"] == "USD"
    assert DummyClient.last_request["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_newapi_converts_quota(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.payload = {
        "success": True,
        "data": {"group": "pro", "quota": 1000000, "used_quota": 500000},
    }
    account = {
        "platform": "newApi",
        "base_url": "https://new.example",
        "access_token_enc": encrypt_value("token", "test-key"),
        "user_id_enc": encrypt_value("42", "test-key"),
    }

    result = await query_newapi(account, "test-key", 3)

    assert result["plan_name"] == "pro"
    assert result["remaining"] == 2
    assert result["used"] == 1
    assert result["total"] == 3
    assert DummyClient.last_request["headers"]["New-Api-User"] == "42"


def test_normalize_invalid_message():
    result = normalize_result({"isValid": False, "invalidMessage": "bad"})

    assert result["is_valid"] is False
    assert result["invalid_message"] == "bad"
