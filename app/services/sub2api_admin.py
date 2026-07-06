from __future__ import annotations

from typing import Any

import httpx

OPENCODE_GO_SUB2API_BASE_URL = "https://opencode.ai/zen/go"
SUB2API_OPENAI_PLATFORM = "openai"
SUB2API_APIKEY_TYPE = "apikey"


class Sub2ApiAdminError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class Sub2ApiAdminClient:
    def __init__(self, site_url: str, admin_key: str, timeout: float = 60) -> None:
        self.site_url = str(site_url or "").strip().rstrip("/")
        self.admin_key = str(admin_key or "").strip()
        self.timeout = timeout
        if not self.site_url:
            raise Sub2ApiAdminError("请先在通用配置中设置 Sub2API 站点地址")
        if not self.admin_key:
            raise Sub2ApiAdminError("请先在通用配置中设置 Sub2API AdminKey")

    async def list_openai_groups(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/v1/admin/groups/all", params={"platform": SUB2API_OPENAI_PLATFORM})
        groups = _unwrap_sub2api_data(payload)
        if not isinstance(groups, list):
            raise Sub2ApiAdminError("Sub2API 分组响应格式不正确", status_code=502)
        return [group for group in groups if isinstance(group, dict) and _is_openai_group(group)]

    async def list_openai_accounts(self, group_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"platform": SUB2API_OPENAI_PLATFORM, "page": 1, "page_size": 1000}
        if group_id is not None:
            params["group_id"] = int(group_id)
        payload = await self._request("GET", "/api/v1/admin/accounts", params=params)
        accounts = _extract_sub2api_list(payload, ("accounts", "items", "records", "rows", "list", "data"))
        if accounts is None:
            raise Sub2ApiAdminError("Sub2API 账号响应格式不正确", status_code=502)
        return [account for account in accounts if isinstance(account, dict) and _is_openai_account(account)]

    async def existing_openai_account_names_in_groups(self, group_ids: list[int]) -> set[str]:
        names: set[str] = set()
        seen_group_ids: set[int] = set()
        for group_id in group_ids:
            group_id = int(group_id)
            if group_id in seen_group_ids:
                continue
            seen_group_ids.add(group_id)
            for account in await self.list_openai_accounts(group_id):
                if not _account_belongs_to_group_or_unknown(account, group_id):
                    continue
                name = _normalize_account_name(account.get("name"))
                if name:
                    names.add(name)
        return names

    async def import_opencode_go_account(self, email: str, api_key: str, group_ids: list[int]) -> dict[str, Any]:
        email = str(email or "").strip()
        api_key = str(api_key or "").strip()
        if not email:
            raise Sub2ApiAdminError("OpenCode Go 账号缺少邮箱")
        if not api_key:
            raise Sub2ApiAdminError("OpenCode Go 账号尚未获取 API key，请先刷新账号")

        models = await self.sync_openai_models_preview(api_key)
        credentials: dict[str, Any] = {
            "base_url": OPENCODE_GO_SUB2API_BASE_URL,
            "api_key": api_key,
            "pool_mode": True,
        }
        if models:
            credentials["model_mapping"] = {model: model for model in models}
        payload = {
            "name": f"opencode-{email}",
            "platform": SUB2API_OPENAI_PLATFORM,
            "type": SUB2API_APIKEY_TYPE,
            "concurrency": 10,
            "credentials": credentials,
            "extra": {"codex_image_generation_bridge": False},
            "group_ids": group_ids,
        }
        account_payload = await self._request("POST", "/api/v1/admin/accounts", json=payload)
        return {
            "account": _redact_account_payload(_unwrap_sub2api_data(account_payload)),
            "models": models,
            "model_count": len(models),
            "modelCount": len(models),
            "group_ids": group_ids,
            "groupIds": group_ids,
            "name": payload["name"],
        }

    async def sync_openai_models_preview(self, api_key: str) -> list[str]:
        payload = await self._request(
            "POST",
            "/api/v1/admin/accounts/models/sync-upstream-preview",
            json={
                "platform": SUB2API_OPENAI_PLATFORM,
                "type": SUB2API_APIKEY_TYPE,
                "base_url": OPENCODE_GO_SUB2API_BASE_URL,
                "api_key": api_key,
            },
        )
        data = _unwrap_sub2api_data(payload)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise Sub2ApiAdminError("Sub2API 模型同步响应格式不正确", status_code=502)
        return _normalize_model_ids(models)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"x-api-key": self.admin_key, "Accept": "application/json"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.request(method, f"{self.site_url}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise Sub2ApiAdminError("请求 Sub2API 超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise Sub2ApiAdminError(f"请求 Sub2API 失败: {exc}", status_code=502) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise Sub2ApiAdminError("Sub2API 返回内容不是 JSON", status_code=502) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise Sub2ApiAdminError(_sub2api_error_message(payload, response.status_code), status_code=response.status_code)
        if isinstance(payload, dict) and "code" in payload and payload.get("code") not in {0, "0", None}:
            raise Sub2ApiAdminError(_sub2api_error_message(payload, response.status_code), status_code=502)
        return payload


def _unwrap_sub2api_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    return payload


def _sub2api_error_message(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or payload.get("detail")
        if message:
            return f"Sub2API 请求失败: {message}"
    return f"Sub2API 请求失败，HTTP {status_code}"


def _is_openai_group(group: dict[str, Any]) -> bool:
    platform = str(group.get("platform") or "").strip().lower()
    return not platform or platform == SUB2API_OPENAI_PLATFORM


def _is_openai_account(account: dict[str, Any]) -> bool:
    platform = str(account.get("platform") or "").strip().lower()
    return not platform or platform == SUB2API_OPENAI_PLATFORM


def _extract_sub2api_list(payload: Any, keys: tuple[str, ...]) -> list[Any] | None:
    data = _unwrap_sub2api_data(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _extract_sub2api_list(value, keys)
                if nested is not None:
                    return nested
    return None


def _account_belongs_to_group_or_unknown(account: dict[str, Any], group_id: int) -> bool:
    group_ids = _account_group_ids(account)
    return not group_ids or int(group_id) in group_ids


def _account_group_ids(account: dict[str, Any]) -> set[int]:
    values: list[Any] = []
    for key in ("group_id", "groupId", "group_ids", "groupIds", "groups", "plans"):
        if key in account:
            values.append(account.get(key))
    group_ids: set[int] = set()
    for value in values:
        _collect_group_ids(group_ids, value)
    return group_ids


def _collect_group_ids(target: set[int], value: Any) -> None:
    if value is None or value == "":
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_group_ids(target, item)
        return
    if isinstance(value, dict):
        for key in ("id", "group_id", "groupId"):
            _collect_group_ids(target, value.get(key))
        return
    try:
        group_id = int(value)
    except (TypeError, ValueError):
        return
    if group_id > 0:
        target.add(group_id)


def _normalize_account_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_model_ids(models: list[Any]) -> list[str]:
    seen = set()
    result = []
    for model in models:
        text = str(model or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _redact_account_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    redacted = dict(payload)
    credentials = redacted.get("credentials")
    if isinstance(credentials, dict):
        credentials = dict(credentials)
        for key in _sensitive_credential_keys(credentials):
            credentials.pop(key, None)
        redacted["credentials"] = credentials
    return redacted


def _sensitive_credential_keys(credentials: dict[str, Any]) -> list[str]:
    sensitive_names = {
        "access_token",
        "api_key",
        "apikey",
        "id_token",
        "refresh_token",
        "secret",
        "token",
    }
    result = []
    for key in credentials:
        normalized = str(key).strip().lower().replace("-", "_")
        if normalized in sensitive_names or normalized.endswith("_token") or normalized.endswith("_key"):
            result.append(key)
    return result
