"""打包 OpenCode Go Grabber 浏览器扩展为 zip，下载时把当前部署域名烘焙进 manifest/options。

模板目录位于仓库的 ``extension/opencode-go-grabber``。manifest.json 用 ``__APP_ORIGIN_MATCH__``
占位标记 app 所在源（content_scripts.matches / externally_connectable.matches / host_permissions），
options.js 用 ``__APP_BASE_URL__`` 作为 app base URL 默认值。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# 模板目录：镜像内 /app/extension/opencode-go-grabber
TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "extension" / "opencode-go-grabber"

ORIGIN_MATCH_PLACEHOLDER = "__APP_ORIGIN_MATCH__"
BASE_URL_PLACEHOLDER = "__APP_BASE_URL__"


def _origin_match_from_base_url(base_url: str) -> str:
    """将 https://price.example.com/  → https://price.example.com/*  （标准端口省略端口）"""
    parsed = urlparse(str(base_url).rstrip("/"))
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"无法解析 base_url: {base_url!r}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    port = parsed.port
    # 标准/常见端口省略
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    return f"{origin}/*"


def build_extension_zip(base_url: str) -> tuple[bytes, str]:
    """生成定制后的扩展 zip。返回 (zip_bytes, suggested_filename)。

    base_url 形如 ``https://price.example.com/``（一般取 FastAPI request.base_url）。
    """
    if not TEMPLATE_DIR.exists():
        raise FileNotFoundError(f"扩展模板目录不存在: {TEMPLATE_DIR}")

    base_url = str(base_url)
    origin_match = _origin_match_from_base_url(base_url)
    base_url_clean = base_url.rstrip("/") or "/"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(TEMPLATE_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(TEMPLATE_DIR).as_posix()
            data = path.read_bytes()
            if rel == "manifest.json":
                data = _patch_manifest(data, origin_match)
            elif rel == "options.js":
                text = data.decode("utf-8")
                text = text.replace(BASE_URL_PLACEHOLDER, base_url_clean)
                data = text.encode("utf-8")
            zf.writestr(f"opencode-go-grabber/{rel}", data)
    return buf.getvalue(), "opencode-go-grabber.zip"


def _patch_manifest(data: bytes, origin_match: str) -> bytes:
    manifest: dict[str, Any] = json.loads(data.decode("utf-8"))
    # host_permissions
    manifest["host_permissions"] = [m.replace(ORIGIN_MATCH_PLACEHOLDER, origin_match) for m in manifest.get("host_permissions", [])]
    # content_scripts
    for cs in manifest.get("content_scripts", []):
        cs["matches"] = [m.replace(ORIGIN_MATCH_PLACEHOLDER, origin_match) for m in cs.get("matches", [])]
    # externally_connectable
    ec = manifest.get("externally_connectable")
    if isinstance(ec, dict) and "matches" in ec:
        ec["matches"] = [m.replace(ORIGIN_MATCH_PLACEHOLDER, origin_match) for m in ec.get("matches", [])]
    return json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")