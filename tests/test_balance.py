import pytest
import httpx

from app.services.balance import _SUB2API_TOKEN_CACHE
from app.services.balance import normalize_result, query_newapi, query_sub2api, query_sub2api_group
from app.models import Database
from app.security import encrypt_value


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://sub.example")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)
        return None

    def json(self):
        return self.payload


class DummyClient:
    payload = {}
    post_payload = {}
    get_payloads = None
    last_request = None
    requests = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, headers):
        request = {"method": "GET", "url": url, "headers": headers, "timeout": self.timeout}
        DummyClient.last_request = request
        DummyClient.requests.append(request)
        if DummyClient.get_payloads is not None:
            payload = DummyClient.get_payloads.pop(0)
            return payload if isinstance(payload, DummyResponse) else DummyResponse(payload)
        return DummyResponse(DummyClient.payload)

    async def post(self, url, json, headers):
        request = {"method": "POST", "url": url, "json": json, "headers": headers, "timeout": self.timeout}
        DummyClient.last_request = request
        DummyClient.requests.append(request)
        return DummyResponse(DummyClient.post_payload)


@pytest.fixture(autouse=True)
def clear_sub2api_token_cache():
    _SUB2API_TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_sub2api_parses_remaining_from_usage_response(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.payload = {"remaining": 12.5, "unit": "USD", "is_active": True, "planName": "pro"}
    DummyClient.get_payloads = None
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
    }
    logs = []

    result = await query_sub2api(account, "test-key", 3, lambda *args: logs.append(args))

    assert result["is_valid"] is True
    assert result["remaining"] == 12.5
    assert result["unit"] == "USD"
    assert result["plan_name"] == "pro"
    assert DummyClient.last_request["url"] == "https://sub.example/v1/usage"
    assert DummyClient.last_request["headers"]["Authorization"] == "Bearer secret"
    assert "Bearer secret" not in logs[0][2]
    assert "response=" in logs[1][2]


@pytest.mark.asyncio
async def test_sub2api_group_query_uses_key_id(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "team"},
        {"data": [{"id": 3, "name": "team", "platform": "openai", "rate_multiplier": 1.2, "status": "active"}]},
        {"data": {"3": 0.8}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "key_id_enc": encrypt_value("3", "test-key"),
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "team 倍率 0.8"
    assert '"effective_rate_multiplier": 0.8' in result["extra"]
    assert DummyClient.requests[0]["url"] == "https://sub.example/v1/usage"
    assert DummyClient.requests[0]["headers"]["Authorization"] == "Bearer secret"
    assert DummyClient.requests[1]["url"] == "https://sub.example/api/v1/auth/login"
    assert DummyClient.requests[1]["json"]["email"] == "user@example.com"
    assert DummyClient.requests[2]["url"] == "https://sub.example/api/v1/groups/available"
    assert DummyClient.requests[3]["url"] == "https://sub.example/api/v1/groups/rates"
    assert DummyClient.requests[2]["headers"]["Authorization"] == "Bearer jwt"


@pytest.mark.asyncio
async def test_sub2api_group_query_allows_empty_key_id(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "team"},
        {"data": [{"id": 3, "name": "team", "rate_multiplier": 1.2}]},
        {"data": {"3": 0.8}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "team 倍率 0.8"
    assert '"user_rate_multiplier": 0.8' in result["extra"]
    assert '"plan_name": "team"' in result["extra"]


@pytest.mark.asyncio
async def test_sub2api_group_query_keeps_only_active_api_key_group(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "Basic Plan"},
        {
            "data": [
                {"id": "basic", "plan_name": "Basic Plan", "rate_multiplier": 1.2},
                {"id": "pro", "planName": "Pro Plan", "rate_multiplier": 2.0},
            ]
        },
        {"data": {"basic": 0.8}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "Basic Plan 倍率 0.8"
    assert '"plan_name": "Basic Plan"' in result["extra"]
    assert '"effective_rate_multiplier": 0.8' in result["extra"]
    assert '"plan_name": "Pro Plan"' not in result["extra"]


@pytest.mark.asyncio
async def test_sub2api_group_query_reuses_unexpired_login_token(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt", "expires_in": 3600}}
    DummyClient.get_payloads = [
        {"planName": "team"},
        {"data": [{"id": 3, "name": "team", "rate_multiplier": 1.2}]},
        {"data": {"3": 0.8}},
        {"planName": "team"},
        {"data": [{"id": 3, "name": "team", "rate_multiplier": 1.2}]},
        {"data": {"3": 0.8}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    first = await query_sub2api_group(account, "test-key", 3)
    second = await query_sub2api_group(account, "test-key", 3)

    assert first["is_valid"] is True
    assert second["is_valid"] is True
    login_requests = [request for request in DummyClient.requests if request["url"].endswith("/api/v1/auth/login")]
    assert len(login_requests) == 1


@pytest.mark.asyncio
async def test_sub2api_group_query_relogs_when_cached_token_fails(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "fresh-jwt", "expires_in": 3600}}
    DummyClient.get_payloads = [
        {"planName": "team"},
        DummyResponse({"message": "expired"}, status_code=401),
        {"data": [{"id": 3, "name": "team", "rate_multiplier": 1.2}]},
        {"data": {"3": 0.8}},
    ]
    DummyClient.requests = []
    _SUB2API_TOKEN_CACHE["https://sub.example|user@example.com"] = {"token": "stale-jwt", "expires_at": 9999999999}
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    login_requests = [request for request in DummyClient.requests if request["url"].endswith("/api/v1/auth/login")]
    assert len(login_requests) == 1
    group_requests = [request for request in DummyClient.requests if request["url"].endswith("/api/v1/groups/available")]
    assert group_requests[0]["headers"]["Authorization"] == "Bearer stale-jwt"
    assert group_requests[1]["headers"]["Authorization"] == "Bearer fresh-jwt"


@pytest.mark.asyncio
async def test_sub2api_group_query_accepts_sqlite_row_without_key_id(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "team"},
        {"data": [{"id": 3, "name": "team", "rate_multiplier": 1.2}]},
        {"data": {}},
    ]
    DummyClient.requests = []
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "email": "user@example.com",
            "password": "password",
        }
    )

    result = await query_sub2api_group(db.get_account(account_id), "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "team 倍率 1.2"
    assert DummyClient.requests[0]["url"] == "https://sub.example/v1/usage"
    assert DummyClient.requests[1]["url"] == "https://sub.example/api/v1/auth/login"


@pytest.mark.asyncio
async def test_sub2api_group_query_uses_single_group_when_usage_plan_is_generic(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "钱包余额"},
        {"data": [{"id": "default", "name": "默认分组", "rate_multiplier": 1.2}]},
        {"data": {"default": 0.7}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "默认分组 倍率 0.7"
    assert '"plan_name": "默认分组"' in result["extra"]
    assert '"active_plan_name": "钱包余额"' in result["extra"]


@pytest.mark.asyncio
async def test_sub2api_group_query_does_not_create_empty_group_when_no_match(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "钱包余额"},
        {
            "data": [
                {"id": "basic", "plan_name": "Basic Plan", "rate_multiplier": 1.2},
                {"id": "pro", "plan_name": "Pro Plan", "rate_multiplier": 2.0},
            ]
        },
        {"data": {"basic": 0.8, "pro": 1.5}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "未识别当前 apiKey 分组"
    assert '"groups": []' in result["extra"]
    assert '"plan_name": "钱包余额"' not in result["extra"]
    assert "钱包余额 倍率 -" not in result["extra"]


@pytest.mark.asyncio
async def test_sub2api_group_query_matches_group_from_current_api_key(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.post_payload = {"data": {"access_token": "jwt"}}
    DummyClient.get_payloads = [
        {"planName": "钱包余额"},
        {
            "data": [
                {"id": "basic", "plan_name": "Basic Plan", "rate_multiplier": 1.2},
                {"id": "pro", "plan_name": "Pro Plan", "rate_multiplier": 2.0},
            ]
        },
        {"data": {"basic": 0.8, "pro": 1.5}},
        {"data": {"items": [{"key": "secret", "group_id": "pro"}]}},
    ]
    DummyClient.requests = []
    account = {
        "platform": "sub2Api",
        "name": "sub",
        "base_url": "https://sub.example",
        "api_key_enc": encrypt_value("secret", "test-key"),
        "email_enc": encrypt_value("user@example.com", "test-key"),
        "password_enc": encrypt_value("password", "test-key"),
    }

    result = await query_sub2api_group(account, "test-key", 3)

    assert result["is_valid"] is True
    assert result["plan_name"] == "Pro Plan 倍率 1.5"
    assert '"active_key_group_id": "pro"' in result["extra"]
    assert DummyClient.requests[-1]["url"] == "https://sub.example/api/v1/keys?page=1&page_size=100"


@pytest.mark.asyncio
async def test_newapi_converts_quota(monkeypatch):
    monkeypatch.setattr("app.services.balance.httpx.AsyncClient", DummyClient)
    DummyClient.payload = {
        "success": True,
        "data": {"group": "pro", "quota": 1000000, "used_quota": 500000},
    }
    DummyClient.get_payloads = None
    DummyClient.requests = []
    account = {
        "platform": "newApi",
        "name": "new",
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
