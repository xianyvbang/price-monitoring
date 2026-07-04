import json
import shutil
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, config
from app.models import Database
from app.security import decrypt_value
from app.services.extension_pack import build_account_grabber_extension_zip, build_extension_zip


def setup_test_db(tmp_path, monkeypatch) -> Database:
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)
    return test_db


def login(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": "password123"})


def test_account_grabber_zip_patches_app_origin():
    body, filename = build_account_grabber_extension_zip("https://price.example.com/")

    assert filename == "account-grabber.zip"
    with zipfile.ZipFile(BytesIO(body)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        options_js = zf.read("options.js").decode("utf-8")
        names = set(zf.namelist())

    assert manifest["name"] == "NewAPI/Sub2API Account Grabber"
    assert "https://price.example.com/*" in manifest["host_permissions"]
    assert any("grabber_core.js" in script for cs in manifest["content_scripts"] for script in cs["js"])
    assert "__APP_ORIGIN_MATCH__" not in json.dumps(manifest)
    assert 'data.appBase || "https://price.example.com"' in options_js
    assert "background.js" in names
    assert "content.js" in names


def test_opencode_grabber_zip_still_uses_original_template():
    body, filename = build_extension_zip("https://price.example.com/")

    assert filename == "opencode-go-grabber.zip"
    with zipfile.ZipFile(BytesIO(body)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert manifest["name"] == "OpenCode Go Grabber"
    assert any(cs.get("matches") == ["https://opencode.ai/*"] for cs in manifest["content_scripts"])


def test_account_grabber_download_endpoint_requires_login_and_returns_zip(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)
    with TestClient(app) as client:
        anonymous = client.get("/api/account-grabber/extension.zip")
        login(client)
        response = client.get("/api/account-grabber/extension.zip")

    assert anonymous.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "account-grabber.zip" in response.headers["content-disposition"]


def test_api_create_accounts_from_grabber_defaults_encrypts_secrets(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client)
        new_resp = client.post(
            "/api/accounts",
            json={
                "platform": "newApi",
                "name": "new-grabbed",
                "base_url": "https://new.example",
                "note": "1:1",
                "recharge_paid_amount": 1,
                "recharge_received_amount": 1,
                "access_token": "new-at",
                "user_id": "42",
                "threshold": 5,
                "is_visible": True,
                "is_enabled": True,
            },
        )
        sub_resp = client.post(
            "/api/accounts",
            json={
                "platform": "sub2Api",
                "name": "sub-grabbed",
                "base_url": "https://sub.example",
                "note": "1:1",
                "recharge_paid_amount": 1,
                "recharge_received_amount": 1,
                "api_key": "sk-sub",
                "access_token": "sub-at",
                "refresh_token": "sub-rt",
                "threshold": 5,
                "is_visible": True,
                "is_enabled": True,
            },
        )

    assert new_resp.status_code == 200
    assert sub_resp.status_code == 200
    new_account = test_db.get_account(new_resp.json()["id"])
    sub_account = test_db.get_account(sub_resp.json()["id"])
    assert new_account["threshold"] == 5
    assert new_account["note"] == "1:1"
    assert bool(new_account["is_visible"]) is True
    assert bool(new_account["is_enabled"]) is True
    assert decrypt_value(new_account["access_token_enc"], config.app_secret_key) == "new-at"
    assert decrypt_value(new_account["user_id_enc"], config.app_secret_key) == "42"
    assert decrypt_value(sub_account["api_key_enc"], config.app_secret_key) == "sk-sub"
    assert decrypt_value(sub_account["access_token_enc"], config.app_secret_key) == "sub-at"
    assert decrypt_value(sub_account["refresh_token_enc"], config.app_secret_key) == "sub-rt"


def test_api_import_account_by_base_url_updates_same_base_url(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    existing_id = test_db.upsert_account(
        {
            "platform": "newApi",
            "name": "old-name",
            "base_url": "https://new.example",
            "access_token": "old-at",
            "user_id": "1",
            "threshold": 1,
        }
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/accounts/import-by-base-url",
            json={
                "platform": "newApi",
                "name": "new-name",
                "base_url": "https://new.example/",
                "access_token": "new-at",
                "user_id": "42",
                "threshold": 5,
                "note": "1:1",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == existing_id
    assert payload["action"] == "更新"
    account = test_db.get_account(existing_id)
    assert account["name"] == "new-name"
    assert account["base_url"] == "https://new.example"
    assert account["threshold"] == 5
    assert decrypt_value(account["access_token_enc"], config.app_secret_key) == "new-at"
    assert decrypt_value(account["user_id_enc"], config.app_secret_key) == "42"
    matching = [item for item in test_db.list_accounts(platform="newApi") if item["base_url"] == "https://new.example"]
    assert len(matching) == 1


def test_api_import_account_by_base_url_adds_different_base_url_even_when_name_matches(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    existing_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "180txt",
            "base_url": "https://a.180txt.cn",
            "api_key": "sk-old",
            "access_token": "old-at",
        }
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/accounts/import-by-base-url",
            json={
                "platform": "sub2Api",
                "name": "180txt",
                "base_url": "https://ccb.180txt.cn",
                "api_key": "sk-new",
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "threshold": 5,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] != existing_id
    assert payload["action"] == "新增"
    matching = [item for item in test_db.list_accounts(platform="sub2Api") if item["base_url"] in {"https://a.180txt.cn", "https://ccb.180txt.cn"}]
    assert len(matching) == 2
    created = test_db.get_account(payload["id"])
    assert created["name"] == "180txt (2)"
    assert created["base_url"] == "https://ccb.180txt.cn"
    assert decrypt_value(created["api_key_enc"], config.app_secret_key) == "sk-new"
    assert decrypt_value(created["access_token_enc"], config.app_secret_key) == "new-at"
    assert decrypt_value(created["refresh_token_enc"], config.app_secret_key) == "new-rt"
    original = test_db.get_account(existing_id)
    assert original["name"] == "180txt"
    assert original["base_url"] == "https://a.180txt.cn"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_account_grabber_core_extractors():
    repo = Path(__file__).resolve().parents[1]
    script = r"""
const core = require("./extension/account-grabber/grabber_core.js");
const assert = require("assert");

assert.strictEqual(core.inferName("www.newapi.price.example.com"), "example");
assert.strictEqual(core.inferName("console.sub2api.2chat.cc"), "2chat");
assert.strictEqual(core.inferName("ccb.180txt.cn"), "180txt");
assert.strictEqual(core.keysPageUrl("https://sub.example/admin"), "https://sub.example/keys");
assert.strictEqual(core.keysPageUrl("https://sub.example/"), "https://sub.example/keys");
assert.strictEqual(core.newApiTokenApiUrl("https://new.example/dashboard"), "https://new.example/api/user/token");
assert.strictEqual(core.newApiTokenApiUrl("https://new.example/"), "https://new.example/api/user/token");

const newPayload = { success: true, data: { id: 42, accessToken: "new-access" } };
assert.strictEqual(core.detectHttp("https://new.example/api/user/self", { Authorization: "Bearer header-token", "New-Api-User": "42" }, JSON.stringify(newPayload)), "newApi");
assert.strictEqual(core.extractUserId(newPayload), "42");
assert.strictEqual(core.bearerFromHeaders({ Authorization: "Bearer header-token" }), "header-token");
const generatedTokenPayload = { success: true, data: "newapi-generated-access-token-123456" };
assert.strictEqual(core.extractGeneratedAccessToken(generatedTokenPayload), "newapi-generated-access-token-123456");
assert.strictEqual(core.detectHttp("https://new.example/api/user/security/access-token", {}, JSON.stringify(generatedTokenPayload)), "newApi");
assert.strictEqual(core.detectHttp("https://new.example/api/user/token", { "New-Api-User": "42" }, JSON.stringify(generatedTokenPayload)), "newApi");

const subPayload = { data: { items: [{ key: "sk-first" }, { key: "sk-second" }] } };
assert.strictEqual(core.detectHttp("https://sub.example/api/v1/keys?page=1", { Authorization: "Bearer sub-at" }, JSON.stringify(subPayload)), "sub2Api");
assert.strictEqual(core.extractApiKey(subPayload), "sk-first");
assert.strictEqual(core.extractApiKey({ data: { items: [{ token: "sk-token-field" }] } }), "sk-token-field");
assert.strictEqual(core.extractApiKey({ data: { token: "login-access-token" } }), "");

const tokens = { data: { access_token: "sub-at", refresh_token: "sub-rt" } };
assert.strictEqual(core.extractAccessToken(tokens), "sub-at");
assert.strictEqual(core.extractRefreshToken(tokens), "sub-rt");

assert.strictEqual(core.detectHttp("https://github.com/session", {}, JSON.stringify({ data: { refresh_token: "github-refresh-token" } })), "");
assert.strictEqual(core.detectHttp("https://github.com/api/key", {}, JSON.stringify({ data: { key: "github-key" } })), "");
assert.strictEqual(core.detectHttp("https://github.com/user/repo/blob/main/sub2api-key.md", {}, JSON.stringify({ data: { key: "github-key" } }), "https://github.com", "github.com"), "");
assert.strictEqual(core.detectHttp("https://api.github.com/api/v1/keys", {}, JSON.stringify({ data: { items: [{ key: "github-key" }] } }), "https://github.com", "github.com"), "");
assert.strictEqual(core.detectHttp("https://sub.example/api/v1/auth/refresh", {}, JSON.stringify(tokens)), "sub2Api");
assert.strictEqual(core.detectHttp("https://sub.example/api/v1/auth/refresh", {}, JSON.stringify(tokens), "https://sub.example", "sub.example"), "sub2Api");

assert.strictEqual(core.detectProbeResponse("/api/v1/keys", 401, "application/json", JSON.stringify({ detail: "Unauthorized" })), "sub2Api");
assert.strictEqual(core.detectProbeResponse("/api/v1/keys", 200, "text/html", "<!doctype html><html></html>"), "");
assert.strictEqual(core.detectProbeResponse("/api/v1/keys", 404, "application/json", JSON.stringify({ message: "Not Found" })), "");
assert.strictEqual(core.detectProbeResponse("/api/user/self", 401, "application/json", JSON.stringify({ message: "Unauthorized" })), "newApi");
"""
    result = subprocess.run(["node", "-e", script], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
