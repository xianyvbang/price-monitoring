from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
        groups = await self.list_groups(SUB2API_OPENAI_PLATFORM)
        return [group for group in groups if _is_openai_group(group)]

    async def list_groups(self, platform: str | None = None) -> list[dict[str, Any]]:
        params = {"platform": platform} if platform else None
        payload = await self._request("GET", "/api/v1/admin/groups/all", params=params)
        groups = _unwrap_sub2api_data(payload)
        if not isinstance(groups, list):
            raise Sub2ApiAdminError("Sub2API 分组响应格式不正确", status_code=502)
        return [group for group in groups if isinstance(group, dict)]

    async def list_openai_accounts(self, group_id: int | None = None) -> list[dict[str, Any]]:
        return await self.list_accounts(platform=SUB2API_OPENAI_PLATFORM, group_id=group_id)

    async def list_accounts(
        self,
        platform: str | None = None,
        account_type: str | None = None,
        status: str | None = None,
        search: str | None = None,
        group_id: int | None = None,
    ) -> list[dict[str, Any]]:
        page = 1
        result: list[dict[str, Any]] = []
        while True:
            page_data = await self.list_accounts_page(
                page=page,
                page_size=100,
                platform=platform,
                account_type=account_type,
                status=status,
                search=search,
                group_id=group_id,
            )
            records = page_data["accounts"]
            result.extend(records)
            pages = page_data.get("pages")
            total = page_data.get("total")
            if not records:
                break
            if pages is not None:
                if page >= pages:
                    break
            elif total is not None:
                if len(result) >= total:
                    break
            elif len(records) < 100:
                break
            page += 1
        return result

    async def list_accounts_page(
        self,
        page: int,
        page_size: int = 100,
        platform: str | None = None,
        account_type: str | None = None,
        status: str | None = None,
        search: str | None = None,
        group_id: int | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        params: dict[str, Any] = {"sort_by": "name", "sort_order": "asc"}
        if platform:
            params["platform"] = platform
        if account_type:
            params["type"] = account_type
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        if group_id is not None:
            params["group"] = int(group_id)
            params["group_id"] = int(group_id)
        params.update({"page": page, "page_size": page_size})
        payload = await self._request("GET", "/api/v1/admin/accounts", params=params)
        records = _extract_sub2api_list(payload, ("accounts", "items", "records", "rows", "list", "data"))
        if records is None:
            raise Sub2ApiAdminError("Sub2API 账号响应格式不正确", status_code=502)
        page_data = _unwrap_sub2api_data(payload)
        total = _non_negative_int_or_none(page_data.get("total")) if isinstance(page_data, dict) else None
        pages = _non_negative_int_or_none(page_data.get("pages")) if isinstance(page_data, dict) else None
        response_page = _positive_int(page_data.get("page")) if isinstance(page_data, dict) else None
        response_page_size = _positive_int(page_data.get("page_size")) if isinstance(page_data, dict) else None
        return {
            "accounts": [record for record in records if isinstance(record, dict)],
            "total": total,
            "pages": pages,
            "page": response_page or page,
            "page_size": response_page_size or page_size,
        }

    async def list_recent_usage(self, account_id: int, limit: int = 6) -> list[dict[str, Any]]:
        page = await self.list_usage_page(account_id, page=1, page_size=limit)
        return page["records"]

    async def list_usage_page(
        self,
        account_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        start_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "account_id": int(account_id),
            "page": max(1, int(page)),
            "page_size": max(1, min(100, int(page_size))),
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        if start_date:
            params["start_date"] = str(start_date)
        payload = await self._request(
            "GET",
            "/api/v1/admin/usage",
            params=params,
        )
        records = _extract_sub2api_list(payload, ("items", "records", "rows", "list", "data"))
        if records is None:
            raise Sub2ApiAdminError("Sub2API 使用记录响应格式不正确", status_code=502)
        data = _unwrap_sub2api_data(payload)
        return {
            "records": [record for record in records if isinstance(record, dict)],
            "page": _positive_int(data.get("page")) if isinstance(data, dict) else params["page"],
            "pages": _positive_int(data.get("pages")) if isinstance(data, dict) else None,
            "total": _non_negative_int_or_none(data.get("total")) if isinstance(data, dict) else None,
        }

    async def list_recent_errors(self, account_id: int | None = None, limit: int = 6) -> list[dict[str, Any]]:
        page = await self.list_errors_page(account_id, page=1, page_size=limit)
        return page["records"]

    async def list_errors_page(
        self,
        account_id: int | None = None,
        *,
        page: int = 1,
        page_size: int = 100,
        time_range: str = "30d",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": max(1, int(page)),
            "page_size": max(1, min(500, int(page_size))),
            "time_range": str(time_range or "30d"),
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        if account_id is not None:
            params["account_id"] = int(account_id)
        payload = await self._request("GET", "/api/v1/admin/ops/errors", params=params)
        records = _extract_sub2api_list(payload, ("items", "errors", "records", "rows", "list", "data"))
        if records is None:
            raise Sub2ApiAdminError("Sub2API 错误记录响应格式不正确", status_code=502)
        data = _unwrap_sub2api_data(payload)
        return {
            "records": [record for record in records if isinstance(record, dict)],
            "page": _positive_int(data.get("page")) if isinstance(data, dict) else params["page"],
            "pages": _positive_int(data.get("pages")) if isinstance(data, dict) else None,
            "total": _non_negative_int_or_none(data.get("total")) if isinstance(data, dict) else None,
        }

    async def get_concurrency_stats(self, platform: str | None = None) -> dict[str, Any]:
        params = {"platform": platform} if platform else None
        payload = await self._request("GET", "/api/v1/admin/ops/concurrency", params=params)
        data = _unwrap_sub2api_data(payload)
        if not isinstance(data, dict):
            raise Sub2ApiAdminError("Sub2API 实时并发响应格式不正确", status_code=502)
        return data

    async def get_account_availability(self, platform: str | None = None) -> dict[str, Any]:
        params = {"platform": platform} if platform else None
        payload = await self._request("GET", "/api/v1/admin/ops/account-availability", params=params)
        data = _unwrap_sub2api_data(payload)
        if not isinstance(data, dict):
            raise Sub2ApiAdminError("Sub2API 账号可用性响应格式不正确", status_code=502)
        return data

    async def platform_dispatch(
        self,
        recent_limit: int = 6,
        platform: str = "",
        account_type: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        recent_limit = max(1, min(20, int(recent_limit)))
        platform = str(platform or "").strip()
        account_type = str(account_type or "").strip()
        status = str(status or "").strip().lower()
        accounts, groups = await asyncio.gather(
            self.list_accounts(platform=platform or None, account_type=account_type or None, status=status or None),
            self.list_groups(platform=platform or None),
        )
        accounts = [
            account
            for account in accounts
            if matches_dispatch_filter(account, platform=platform, account_type=account_type, status=status)
        ]
        warnings: list[str] = []

        errors_available = True
        try:
            await self.list_recent_errors(limit=1)
        except Sub2ApiAdminError as exc:
            errors_available = False
            warnings.append(f"Sub2API 错误记录暂不可用: {exc}")

        semaphore = asyncio.Semaphore(8)

        async def load_activity(account: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
            account_id = _positive_int(account.get("id"))
            if account_id is None:
                return 0, []
            async with semaphore:
                tasks = [self.list_recent_usage(account_id, recent_limit)]
                if errors_available:
                    tasks.append(self.list_recent_errors(account_id, recent_limit))
                results = await asyncio.gather(*tasks, return_exceptions=True)

            usage_records: list[dict[str, Any]] = []
            error_records: list[dict[str, Any]] = []
            usage_result = results[0]
            if isinstance(usage_result, Exception):
                warnings.append(f"账号 {account.get('name') or account_id} 的使用记录读取失败: {usage_result}")
            else:
                usage_records = usage_result
            if errors_available:
                error_result = results[1]
                if isinstance(error_result, Exception):
                    warnings.append(f"账号 {account.get('name') or account_id} 的错误记录读取失败: {error_result}")
                else:
                    error_records = error_result
            return account_id, merge_recent_activity(usage_records, error_records, recent_limit)

        activity_pairs = await asyncio.gather(*(load_activity(account) for account in accounts))
        activity_by_account = {account_id: activity for account_id, activity in activity_pairs if account_id > 0}
        public_accounts = [
            public_dispatch_account(account, activity_by_account.get(_positive_int(account.get("id")) or 0, []))
            for account in accounts
        ]
        return {
            "accounts": public_accounts,
            "groups": [public_dispatch_group(group) for group in groups],
            "warnings": _unique_strings(warnings),
            "recent_limit": recent_limit,
            "recentLimit": recent_limit,
        }

    async def update_account_status(self, account_id: int, enabled: bool) -> dict[str, Any]:
        return await self.update_account_fields(account_id, {"status": "active" if enabled else "inactive"})

    async def update_account_fields(self, account_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = {"status", "concurrency", "load_factor"}
        payload_fields = {key: value for key, value in fields.items() if key in allowed}
        if not payload_fields:
            raise Sub2ApiAdminError("没有可更新的 Sub2API 账号字段")
        payload = await self._request(
            "PUT",
            f"/api/v1/admin/accounts/{int(account_id)}",
            json=payload_fields,
        )
        account = _unwrap_sub2api_data(payload)
        if not isinstance(account, dict):
            raise Sub2ApiAdminError("Sub2API 账号更新响应格式不正确", status_code=502)
        return public_dispatch_account(account, [])

    async def probe_account(self, account_id: int) -> dict[str, Any]:
        headers = {
            "x-api-key": self.admin_key,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.request(
                    "POST",
                    f"{self.site_url}/api/v1/admin/accounts/{int(account_id)}/test",
                    headers=headers,
                    json={},
                )
        except httpx.TimeoutException:
            return {"success": False, "is_timeout": True, "message": "账号探活超时"}
        except httpx.HTTPError as exc:
            return {"success": False, "is_timeout": False, "message": f"账号探活请求失败: {exc}"}

        body = str(getattr(response, "text", "") or "")
        if response.status_code < 200 or response.status_code >= 300:
            return {
                "success": False,
                "is_timeout": response.status_code in {408, 504},
                "status_code": response.status_code,
                "message": body.strip() or f"账号探活 HTTP {response.status_code}",
            }
        error = ""
        content_seen = False
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                error = str(event.get("error") or event.get("message") or "账号探活失败")
            elif event.get("type") in {"content", "done", "end"}:
                content_seen = True
        if error:
            lower = error.lower()
            return {
                "success": False,
                "is_timeout": "timeout" in lower or "timed out" in lower or "超时" in error,
                "message": error,
            }
        return {"success": True, "is_timeout": False, "message": "探活成功", "content_seen": content_seen}

    async def _list_all_pages(
        self,
        path: str,
        params: dict[str, Any],
        keys: tuple[str, ...],
        format_error: str,
    ) -> list[dict[str, Any]]:
        page = 1
        page_size = 1000
        result: list[dict[str, Any]] = []
        while True:
            page_params = {**params, "page": page, "page_size": page_size}
            payload = await self._request("GET", path, params=page_params)
            records = _extract_sub2api_list(payload, keys)
            if records is None:
                raise Sub2ApiAdminError(format_error, status_code=502)
            result.extend(record for record in records if isinstance(record, dict))
            page_data = _unwrap_sub2api_data(payload)
            total = _positive_int(page_data.get("total")) if isinstance(page_data, dict) else None
            pages = _positive_int(page_data.get("pages")) if isinstance(page_data, dict) else None
            if not records:
                break
            if pages is not None:
                if page >= pages:
                    break
            elif total is not None:
                if len(result) >= total:
                    break
            elif len(records) < page_size:
                break
            page += 1
        return result

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


def matches_dispatch_filter(account: dict[str, Any], platform: str, account_type: str, status: str) -> bool:
    if platform and str(account.get("platform") or "").strip().casefold() != platform.casefold():
        return False
    if account_type and str(account.get("type") or "").strip().casefold() != account_type.casefold():
        return False
    if status and _dispatch_filter_status(account) != status.casefold():
        return False
    return True


def _dispatch_filter_status(account: dict[str, Any]) -> str:
    status = str(account.get("status") or "inactive").strip().lower()
    if status != "active":
        return status
    if _is_future_timestamp(account.get("temp_unschedulable_until")):
        return "temp_unschedulable"
    if _is_future_timestamp(account.get("rate_limit_reset_at")):
        return "rate_limited"
    if account.get("schedulable") is False:
        return "unschedulable"
    return "active"


def _is_future_timestamp(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)
    except (OSError, TypeError, ValueError):
        return False


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


def public_dispatch_account(account: dict[str, Any], recent_activity: list[dict[str, Any]]) -> dict[str, Any]:
    account_id = _positive_int(account.get("id")) or 0
    status = str(account.get("status") or "inactive").strip().lower()
    if status not in {"active", "inactive", "error"}:
        status = "inactive"
    group_ids = sorted(_account_group_ids(account))
    filter_status = _dispatch_filter_status(account)
    groups = account.get("groups")
    public_groups = [public_dispatch_group(group) for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []
    result = {
        "id": account_id,
        "name": str(account.get("name") or f"账号 {account_id}"),
        "notes": str(account.get("notes") or ""),
        "platform": str(account.get("platform") or ""),
        "type": str(account.get("type") or ""),
        "status": status,
        "filter_status": filter_status,
        "filterStatus": filter_status,
        "is_enabled": status == "active",
        "isEnabled": status == "active",
        "error_message": str(account.get("error_message") or ""),
        "errorMessage": str(account.get("error_message") or ""),
        "group_ids": group_ids,
        "groupIds": group_ids,
        "groups": public_groups,
        "last_used_at": account.get("last_used_at"),
        "lastUsedAt": account.get("last_used_at"),
        "created_at": account.get("created_at"),
        "createdAt": account.get("created_at"),
        "updated_at": account.get("updated_at"),
        "updatedAt": account.get("updated_at"),
        "rate_limit_reset_at": account.get("rate_limit_reset_at"),
        "rateLimitResetAt": account.get("rate_limit_reset_at"),
        "overload_until": account.get("overload_until"),
        "overloadUntil": account.get("overload_until"),
        "temp_unschedulable_until": account.get("temp_unschedulable_until"),
        "tempUnschedulableUntil": account.get("temp_unschedulable_until"),
        "recent_activity": recent_activity,
        "recentActivity": recent_activity,
    }
    if "concurrency" in account:
        result["concurrency"] = _non_negative_int(account.get("concurrency"))
    if "load_factor" in account:
        result["load_factor"] = _positive_int(account.get("load_factor"))
        result["loadFactor"] = result["load_factor"]
    if "rate_multiplier" in account:
        result["rate_multiplier"] = _optional_number(account.get("rate_multiplier"))
        result["rateMultiplier"] = result["rate_multiplier"]
    if "schedulable" in account:
        result["schedulable"] = account.get("schedulable") is not False
    return result


def public_dispatch_group(group: dict[str, Any]) -> dict[str, Any]:
    group_id = _positive_int(group.get("id")) or 0
    return {
        "id": group_id,
        "name": str(group.get("name") or f"分组 {group_id}"),
        "description": str(group.get("description") or ""),
        "platform": str(group.get("platform") or ""),
        "status": str(group.get("status") or ""),
        "rate_multiplier": _optional_number(group.get("rate_multiplier")),
        "rateMultiplier": _optional_number(group.get("rate_multiplier")),
    }


def normalize_sub2api_usage_record(record: dict[str, Any]) -> dict[str, Any]:
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    input_tokens = _non_negative_int(record.get("input_tokens"))
    output_tokens = _non_negative_int(record.get("output_tokens"))
    cache_creation_tokens = _non_negative_int(record.get("cache_creation_tokens"))
    cache_read_tokens = _non_negative_int(record.get("cache_read_tokens"))
    total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    actual_cost = _optional_number(record.get("actual_cost"))
    total_cost = _optional_number(record.get("total_cost"))
    return {
        "id": f"usage-{record.get('id')}",
        "source_id": record.get("id"),
        "sourceId": record.get("id"),
        "kind": "success",
        "is_error": False,
        "isError": False,
        "user_id": _positive_int(record.get("user_id")) or _positive_int(user.get("id")),
        "userId": _positive_int(record.get("user_id")) or _positive_int(user.get("id")),
        "user_email": str(user.get("email") or record.get("user_email") or ""),
        "userEmail": str(user.get("email") or record.get("user_email") or ""),
        "model": str(record.get("upstream_model") or record.get("model") or ""),
        "requested_model": str(record.get("model") or ""),
        "requestedModel": str(record.get("model") or ""),
        "input_tokens": input_tokens,
        "inputTokens": input_tokens,
        "output_tokens": output_tokens,
        "outputTokens": output_tokens,
        "cache_tokens": cache_creation_tokens + cache_read_tokens,
        "cacheTokens": cache_creation_tokens + cache_read_tokens,
        "total_tokens": total_tokens,
        "totalTokens": total_tokens,
        "cost": actual_cost if actual_cost is not None else total_cost,
        "actual_cost": actual_cost,
        "actualCost": actual_cost,
        "total_cost": total_cost,
        "totalCost": total_cost,
        "first_token_ms": _optional_number(record.get("first_token_ms")),
        "firstTokenMs": _optional_number(record.get("first_token_ms")),
        "duration_ms": _optional_number(record.get("duration_ms")),
        "durationMs": _optional_number(record.get("duration_ms")),
        "status_code": 200,
        "statusCode": 200,
        "message": "",
        "created_at": record.get("created_at"),
        "createdAt": record.get("created_at"),
    }


def normalize_sub2api_error_record(record: dict[str, Any]) -> dict[str, Any]:
    model = str(record.get("requested_model") or record.get("model") or record.get("upstream_model") or "")
    first_token_ms = _optional_number(record.get("time_to_first_token_ms", record.get("first_token_ms")))
    duration_ms = _optional_number(
        record.get("response_latency_ms", record.get("duration_ms", record.get("upstream_latency_ms")))
    )
    return {
        "id": f"error-{record.get('id')}",
        "source_id": record.get("id"),
        "sourceId": record.get("id"),
        "kind": "error",
        "is_error": True,
        "isError": True,
        "user_id": _positive_int(record.get("user_id")),
        "userId": _positive_int(record.get("user_id")),
        "user_email": str(record.get("user_email") or ""),
        "userEmail": str(record.get("user_email") or ""),
        "model": model,
        "requested_model": str(record.get("requested_model") or record.get("model") or ""),
        "requestedModel": str(record.get("requested_model") or record.get("model") or ""),
        "input_tokens": None,
        "inputTokens": None,
        "output_tokens": None,
        "outputTokens": None,
        "cache_tokens": None,
        "cacheTokens": None,
        "total_tokens": None,
        "totalTokens": None,
        "cost": None,
        "actual_cost": None,
        "actualCost": None,
        "total_cost": None,
        "totalCost": None,
        "first_token_ms": first_token_ms,
        "firstTokenMs": first_token_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "status_code": _non_negative_int(record.get("status_code")),
        "statusCode": _non_negative_int(record.get("status_code")),
        "message": str(record.get("message") or record.get("type") or "请求失败"),
        "phase": str(record.get("phase") or ""),
        "created_at": record.get("created_at"),
        "createdAt": record.get("created_at"),
    }


def merge_recent_activity(
    usage_records: list[dict[str, Any]], error_records: list[dict[str, Any]], limit: int = 6
) -> list[dict[str, Any]]:
    merged = [normalize_sub2api_usage_record(record) for record in usage_records]
    merged.extend(normalize_sub2api_error_record(record) for record in error_records)
    merged.sort(key=lambda record: _timestamp_value(record.get("created_at")), reverse=True)
    return merged[: max(1, int(limit))]


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _optional_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_value(value: Any) -> float:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
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
