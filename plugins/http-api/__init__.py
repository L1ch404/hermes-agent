"""
HTTP API Plugin

Adds a ``call_http_api`` tool that sends HTTP requests.
Uses Python stdlib ``urllib.request`` — zero external dependencies.
"""

from __future__ import annotations

import json
import logging
import ssl
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)

# ============================================================================
# 1. JSON Schema
# ============================================================================

CALL_HTTP_API_SCHEMA = {
    "name": "call_http_api",
    "description": (
        "Send an HTTP request to a given URL and return the response. "
        "Supports GET, POST, PUT, DELETE, PATCH. "
        "Use this to call REST APIs, webhooks, or fetch data from external services."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to call (e.g. 'https://api.example.com/v1/users').",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                "description": "HTTP method. Default: GET.",
                "default": "GET",
            },
            "body": {
                "type": "string",
                "description": (
                    "Request body as a JSON string. "
                    "Only used for POST/PUT/PATCH. "
                    "Example: '{\"name\": \"Alice\"}'."
                ),
            },
            "headers": {
                "type": "object",
                "description": (
                    "Additional HTTP headers as key-value pairs. "
                    "Content-Type defaults to 'application/json' when body is set. "
                    "Example: {\"Authorization\": \"Bearer token123\"}."
                ),
                "additionalProperties": {"type": "string"},
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds. Default: 30.",
                "minimum": 1,
                "maximum": 120,
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

# ============================================================================
# 2. Handler
# ============================================================================


def _handle_call_http_api(args: dict[str, Any], **kw: Any) -> str:
    """Tool handler — send an HTTP request and return the response."""

    url: str = args.get("url", "")
    method: str = (args.get("method") or "GET").upper()
    body: str | None = args.get("body")
    headers: dict | None = args.get("headers")
    timeout: int = args.get("timeout", 30)

    # ── 打印入参（方便观察 LLM 传了什么） ──
    print(f"\n{'=' * 60}")
    print(f"[call_http_api] 被 LLM 调用")
    print(f"[call_http_api] url     = {url}")
    print(f"[call_http_api] method  = {method}")
    print(f"[call_http_api] body    = {(body or '')[:200]}")
    print(f"[call_http_api] headers = {json.dumps(headers or {}, ensure_ascii=False)}")
    print(f"[call_http_api] timeout = {timeout}")

    if not url:
        return json.dumps({"error": "url is required"}, ensure_ascii=False)

    # ── 构建请求 ──
    req_headers: dict[str, str] = {}
    if headers:
        req_headers.update({str(k): str(v) for k, v in headers.items()})

    # 有 body 时默认 Content-Type = application/json
    data_bytes: bytes | None = None
    if body is not None and method in ("POST", "PUT", "PATCH"):
        data_bytes = body.encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    # 证书：生产环境验证，开发环境允许自签名
    ctx = ssl.create_default_context()

    try:
        req = Request(url, data=data_bytes, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            resp_body = resp.read().decode("utf-8", errors="replace")

        # 尝试把响应体解析成 JSON 用于结构化返回
        try:
            response_json = json.loads(resp_body)
        except (json.JSONDecodeError, ValueError):
            response_json = resp_body

        result = json.dumps({
            "status": status,
            "headers": resp_headers,
            "body": response_json,
        }, ensure_ascii=False)

    except HTTPError as e:
        # HTTP 4xx/5xx — 也返回完整信息给 LLM
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            error_body = json.loads(error_body)
        except Exception:
            pass
        result = json.dumps({
            "error": f"HTTP {e.code} {e.reason}",
            "status": e.code,
            "body": error_body,
        }, ensure_ascii=False)

    except URLError as e:
        result = json.dumps({
            "error": f"Connection failed: {e.reason}",
            "status": 0,
            "body": None,
        }, ensure_ascii=False)

    except Exception as e:
        result = json.dumps({
            "error": f"Unexpected error: {type(e).__name__}: {e}",
            "status": 0,
            "body": None,
        }, ensure_ascii=False)

    print(f"[call_http_api] 返回值: {(result)[:500]}")
    print(f"{'=' * 60}\n")
    return result


# ============================================================================
# 3. register()
# ============================================================================


def register(ctx) -> None:
    """Register the ``call_http_api`` tool via the plugin context."""
    ctx.register_tool(
        name="call_http_api",
        toolset="http",
        schema=CALL_HTTP_API_SCHEMA,
        handler=_handle_call_http_api,
        emoji="🌐",
        description="Send HTTP requests to any URL with custom method, headers, and body.",
    )
    logger.info("http-api plugin: registered call_http_api tool")
