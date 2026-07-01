from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.security import decrypt_value

OPENCODE_GO_CPA_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_CPA_MODELS_URL = f"{OPENCODE_GO_CPA_BASE_URL}/models"
CPA_AUTHORIZATION_SETTING = "cpa_authorization_enc"
CPA_SITE_URL_SETTING = "cpa_site_url"


class CpaAdminError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class CpaAdminClient:
    def __init__(self, site_url: str, authorization: str, timeout: float = 60) -> None:
        self.management_url = normalize_cpa_management_url(site_url)
        self.authorization = normalize_cpa_authorization(authorization)
        self.timeout = timeout
        if not self.management_url:
            raise CpaAdminError("请先在通用配置中设置 CPA 站点地址")
        if not self.authorization:
            raise CpaAdminError("请先在通用配置中设置 CPA Authorization")

    async def import_opencode_go_account(self, email: str, api_key: str) -> dict[str, Any]:
        email = str(email or "").strip()
        api_key = str(api_key or "").strip()
        if not email:
            raise CpaAdminError("OpenCode Go 账号缺少邮箱")
        if not api_key:
            raise CpaAdminError("OpenCode Go 账号尚未获取 API key，请先刷新账号")

        models = await self.fetch_opencode_models(api_key)
        config_payload = await self._request("GET", "/config")
        providers = _openai_compatibility_providers(config_payload)
        provider = _build_opencode_openai_provider(email, api_key, models)
        saved_providers, updated = _upsert_provider(providers, provider)
        await self._request("PUT", "/openai-compatibility", json=saved_providers)
        return {
            "name": email,
            "base_url": OPENCODE_GO_CPA_BASE_URL,
            "baseUrl": OPENCODE_GO_CPA_BASE_URL,
            "models": models,
            "model_count": len(models),
            "modelCount": len(models),
            "updated": updated,
        }

    async def set_openai_provider_disabled(self, email: str, disabled: bool) -> dict[str, Any]:
        email = str(email or "").strip()
        if not email:
            raise CpaAdminError("OpenCode Go 账号缺少邮箱")
        config_payload = await self._request("GET", "/config")
        providers = _openai_compatibility_providers(config_payload)
        saved_providers, found, changed = _set_provider_disabled(providers, email, disabled)
        if not found:
            raise CpaAdminError("CPA 中未找到邮箱 provider，请先导入 CPA", status_code=404)
        if changed:
            await self._request("PUT", "/openai-compatibility", json=saved_providers)
        return {
            "name": email,
            "disabled": disabled,
            "changed": changed,
        }

    async def fetch_opencode_models(self, api_key: str) -> list[str]:
        payload = await self._request(
            "POST",
            "/api-call",
            json={
                "method": "GET",
                "url": OPENCODE_GO_CPA_MODELS_URL,
                "header": {"Authorization": f"Bearer {api_key}"},
            },
        )
        if not isinstance(payload, dict):
            raise CpaAdminError("CPA 模型拉取响应格式不正确", status_code=502)
        status_code = int(payload.get("status_code") or payload.get("statusCode") or 0)
        if status_code < 200 or status_code >= 300:
            raise CpaAdminError(_api_call_error_message(payload, status_code), status_code=502)
        body = payload.get("body")
        if body is None:
            body = payload.get("body_text", payload.get("bodyText"))
        return _normalize_model_ids(body)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.authorization}", "Accept": "application/json"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.request(method, f"{self.management_url}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise CpaAdminError("请求 CPA 超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise CpaAdminError(f"请求 CPA 失败: {exc}", status_code=502) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise CpaAdminError("CPA 返回内容不是 JSON", status_code=502) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise CpaAdminError(_cpa_error_message(payload, response.status_code), status_code=response.status_code)
        return payload


def normalize_cpa_authorization(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def normalize_cpa_management_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CpaAdminError("CPA 站点地址必须是 http 或 https URL")
    suffix = "/v0/management"
    if text.lower().endswith(suffix):
        return text
    return f"{text}{suffix}"


def cpa_admin_client_from_db(db: Any, timeout_default: float = 60) -> CpaAdminClient:
    authorization_enc = db.get_setting(CPA_AUTHORIZATION_SETTING, "")
    authorization = decrypt_value(authorization_enc, db.secret_key) if authorization_enc else ""
    site_url = db.get_setting(CPA_SITE_URL_SETTING, "")
    timeout = db.get_general_settings().get("request_timeout", timeout_default)
    return CpaAdminClient(site_url, authorization, timeout=float(timeout or timeout_default))


def _openai_compatibility_providers(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CpaAdminError("CPA 配置响应格式不正确", status_code=502)
    providers = payload.get("openai-compatibility")
    if providers is None and isinstance(payload.get("config"), dict):
        providers = payload["config"].get("openai-compatibility")
    if providers is None:
        providers = []
    if not isinstance(providers, list):
        raise CpaAdminError("CPA OpenAI 提供商配置格式不正确", status_code=502)
    return [dict(item) for item in providers if isinstance(item, dict)]


def _build_opencode_openai_provider(email: str, api_key: str, models: list[str]) -> dict[str, Any]:
    return {
        "name": email,
        "base-url": OPENCODE_GO_CPA_BASE_URL,
        "api-key-entries": [{"api-key": api_key}],
        "models": [{"name": model} for model in models],
    }


def _upsert_provider(providers: list[dict[str, Any]], provider: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    target_name = str(provider.get("name") or "").strip()
    saved = []
    updated = False
    for item in providers:
        if str(item.get("name") or "").strip() == target_name:
            next_item = dict(item)
            next_item.update(provider)
            saved.append(next_item)
            updated = True
        else:
            saved.append(item)
    if not updated:
        saved.append(provider)
    return saved, updated


def _set_provider_disabled(providers: list[dict[str, Any]], name: str, disabled: bool) -> tuple[list[dict[str, Any]], bool, bool]:
    target_name = str(name or "").strip()
    saved = []
    found = False
    changed = False
    for item in providers:
        if str(item.get("name") or "").strip() != target_name:
            saved.append(item)
            continue
        found = True
        current_disabled = bool(item.get("disabled", False))
        if current_disabled == disabled:
            saved.append(item)
            continue
        next_item = dict(item)
        next_item["disabled"] = disabled
        saved.append(next_item)
        changed = True
    return saved, found, changed


def _normalize_model_ids(payload: Any) -> list[str]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    if isinstance(payload, dict):
        raw_models = payload.get("data")
        if raw_models is None:
            raw_models = payload.get("models")
    else:
        raw_models = payload
    if raw_models is None:
        raw_models = []
    if not isinstance(raw_models, list):
        raise CpaAdminError("CPA 模型列表响应格式不正确", status_code=502)
    seen = set()
    result = []
    for item in raw_models:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
        else:
            value = item
        model = str(value or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def _api_call_error_message(payload: dict[str, Any], status_code: int) -> str:
    body = payload.get("body")
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif error:
            message = str(error)
        if not message:
            message = str(body.get("message") or "")
    elif isinstance(body, str):
        message = body
    return f"CPA 拉取模型失败: {status_code} {message}".strip()


def _cpa_error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        if isinstance(message, dict):
            message = message.get("message")
        if message:
            return f"CPA 请求失败: {message}"
    return f"CPA 请求失败，HTTP {status_code}"
