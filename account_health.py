#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号健康检测 / Token 刷新 / 轻量额度探测。

检测项:
- JWT 是否过期 / 剩余有效期
- 用 access_token 请求 Grok CLI 代理 (models)
- 401 时尝试 refresh_token 刷新并写回本地
- 解析 402 / spending-limit / rate-limit 等额度相关信号
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests as cffi_requests

from sso_to_auth_json import (
    CPA_GROK_BASE_URL,
    CPA_GROK_HEADERS,
    CPA_TOKEN_ENDPOINT,
    CLIENT_ID,
    decode_jwt_payload,
    token_to_cpa_record,
    upload_cpa_auth_remote,
    write_cpa_auth,
)

LogFn = Callable[[str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> float | None:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def token_expiry_info(record: dict) -> dict:
    """从 access_token JWT 或 record.expired 计算过期信息。"""
    access = str(record.get("access_token") or record.get("key") or "").strip()
    payload = decode_jwt_payload(access) if access else {}
    exp = payload.get("exp")
    if exp is None:
        exp_ts = _parse_iso(str(record.get("expired") or ""))
    else:
        try:
            exp_ts = float(exp)
        except Exception:
            exp_ts = None

    now = time.time()
    if exp_ts is None:
        return {
            "exp": None,
            "expired_at": str(record.get("expired") or ""),
            "seconds_left": None,
            "is_expired": False,
            "is_expiring_soon": False,
        }
    left = int(exp_ts - now)
    return {
        "exp": int(exp_ts),
        "expired_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds_left": left,
        "is_expired": left <= 0,
        "is_expiring_soon": 0 < left <= 3600,
    }


def refresh_access_token(refresh_token: str, proxy: str = "", timeout: int = 20) -> dict:
    """用 refresh_token 换新的 access/refresh。

    返回 token dict: access_token / refresh_token / expires_in / token_type ...
    失败抛异常。
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise ValueError("refresh_token 为空")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
    }
    resp = cffi_requests.post(
        CPA_TOKEN_ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        impersonate="chrome",
        timeout=timeout,
    )
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise RuntimeError(f"refresh HTTP {resp.status_code}: {body}")
    token = resp.json()
    if not token.get("access_token"):
        raise RuntimeError(f"refresh 响应无 access_token: {str(token)[:200]}")
    # 部分实现不回传新 refresh，保留旧的
    if not token.get("refresh_token"):
        token["refresh_token"] = rt
    return token


def apply_refreshed_token(record: dict, token: dict) -> dict:
    """把 refresh 得到的 token 合并进 CPA record。"""
    email = record.get("email") or ""
    # 优先用官方 builder 保持字段齐全
    try:
        new_rec = token_to_cpa_record(token, email=email)
        # 保留用户自定义字段
        for k in ("disabled", "headers", "base_url", "redirect_uri"):
            if k in record and record.get(k) not in (None, ""):
                if k == "headers" and isinstance(record.get("headers"), dict):
                    merged = dict(new_rec.get("headers") or {})
                    merged.update(record["headers"])
                    new_rec["headers"] = merged
                elif k != "headers":
                    new_rec[k] = record[k]
        return new_rec
    except Exception:
        out = dict(record)
        out["access_token"] = token.get("access_token") or out.get("access_token")
        if token.get("refresh_token"):
            out["refresh_token"] = token["refresh_token"]
        if token.get("id_token"):
            out["id_token"] = token["id_token"]
        out["expires_in"] = token.get("expires_in", out.get("expires_in"))
        out["last_refresh"] = _now_iso()
        payload = decode_jwt_payload(out.get("access_token") or "")
        if "exp" in payload:
            out["expired"] = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        return out


def _build_api_headers(record: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {record.get('access_token') or record.get('key') or ''}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    extra = record.get("headers") if isinstance(record.get("headers"), dict) else {}
    # 默认带上 Grok CLI 头
    for k, v in CPA_GROK_HEADERS.items():
        headers.setdefault(k, v)
    for k, v in extra.items():
        if v is not None and str(v) != "":
            headers[str(k)] = str(v)
    return headers


def _base_url(record: dict) -> str:
    base = str(record.get("base_url") or CPA_GROK_BASE_URL).strip().rstrip("/")
    return base or CPA_GROK_BASE_URL


def probe_models(record: dict, proxy: str = "", timeout: int = 20) -> dict:
    """GET {base}/models — 验证 token 是否可用。"""
    url = f"{_base_url(record)}/models"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = _build_api_headers(record)
    t0 = time.time()
    try:
        resp = cffi_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            impersonate="chrome",
            timeout=timeout,
        )
        latency_ms = int((time.time() - t0) * 1000)
        text = (resp.text or "")[:500]
        models = []
        body = {}
        try:
            body = resp.json()
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, list):
                models = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        except Exception:
            pass

        # 额度 / 限流相关头
        rate = {}
        for hk, hv in (resp.headers or {}).items():
            lk = str(hk).lower()
            if any(x in lk for x in ("rate", "limit", "remaining", "quota", "retry")):
                rate[str(hk)] = str(hv)

        return {
            "ok": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "models": models[:30],
            "model_count": len(models),
            "body_preview": text,
            "rate_headers": rate,
            "error": None if 200 <= resp.status_code < 300 else _classify_http_error(resp.status_code, text),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": int((time.time() - t0) * 1000),
            "models": [],
            "model_count": 0,
            "body_preview": "",
            "rate_headers": {},
            "error": f"request_error: {exc}",
        }


def probe_mini_chat(record: dict, proxy: str = "", timeout: int = 25) -> dict:
    """可选：发一条极短 chat，用于更深探测（默认不强制）。"""
    url = f"{_base_url(record)}/chat/completions"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = _build_api_headers(record)
    payload = {
        "model": "grok-3-mini",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    t0 = time.time()
    try:
        resp = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=proxies,
            impersonate="chrome",
            timeout=timeout,
        )
        text = (resp.text or "")[:400]
        return {
            "ok": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "latency_ms": int((time.time() - t0) * 1000),
            "body_preview": text,
            "error": None if 200 <= resp.status_code < 300 else _classify_http_error(resp.status_code, text),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": int((time.time() - t0) * 1000),
            "body_preview": "",
            "error": f"request_error: {exc}",
        }


def _classify_http_error(status: int, body: str) -> str:
    b = (body or "").lower()
    if status == 401:
        return "unauthorized_401"
    if status == 402 or "spending-limit" in b or "payment" in b or "quota" in b:
        return "quota_or_billing"
    if status == 403:
        return "forbidden_403"
    if status == 429 or "rate" in b:
        return "rate_limited"
    if status >= 500:
        return f"server_error_{status}"
    return f"http_{status}"


def health_score(status: str, probe: dict, exp: dict) -> int:
    """0-100 健康分。"""
    if status == "healthy":
        base = 90
    elif status == "expiring_soon":
        base = 75
    elif status == "refreshed":
        base = 85
    elif status == "quota_blocked":
        base = 40
    elif status == "rate_limited":
        base = 55
    elif status == "unauthorized":
        base = 15
    elif status == "expired":
        base = 10
    elif status == "disabled":
        base = 5
    else:
        base = 30

    # 延迟微调
    lat = int(probe.get("latency_ms") or 0)
    if lat and lat < 800:
        base = min(100, base + 5)
    elif lat > 5000:
        base = max(0, base - 10)
    if exp.get("is_expired"):
        base = min(base, 20)
    return max(0, min(100, base))


def check_one_account(
    path: Path,
    *,
    proxy: str = "",
    auto_refresh: bool = True,
    deep: bool = False,
    sync_remote: bool = False,
    remote_url: str = "",
    remote_key: str = "",
    log: LogFn | None = None,
) -> dict:
    """检测单个本地 auth 文件。

    返回结构化结果；必要时写回本地并同步远程。
    """
    def _log(msg: str):
        if log:
            log(msg)

    name = path.name
    result: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "email": "",
        "status": "unknown",
        "score": 0,
        "message": "",
        "checked_at": _now_iso(),
        "expiry": {},
        "probe": {},
        "refresh": None,
        "synced_remote": False,
        "record_updated": False,
    }

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["status"] = "invalid_file"
        result["message"] = f"读取失败: {exc}"
        result["score"] = 0
        return result

    result["email"] = record.get("email") or ""
    if record.get("disabled"):
        result["status"] = "disabled"
        result["message"] = "账号已禁用"
        result["expiry"] = token_expiry_info(record)
        result["score"] = health_score("disabled", {}, result["expiry"])
        return result

    exp = token_expiry_info(record)
    result["expiry"] = exp

    # 本地已过期：先尝试 refresh
    refreshed = False
    if (exp.get("is_expired") or not (record.get("access_token") or record.get("key"))) and auto_refresh:
        rt = str(record.get("refresh_token") or "").strip()
        if rt:
            _log(f"[health] {name}: token 过期/缺失，尝试 refresh...")
            try:
                token = refresh_access_token(rt, proxy=proxy)
                record = apply_refreshed_token(record, token)
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result["record_updated"] = True
                refreshed = True
                result["refresh"] = {"ok": True, "message": "refresh 成功"}
                exp = token_expiry_info(record)
                result["expiry"] = exp
                _log(f"[health] {name}: refresh 成功")
            except Exception as exc:
                result["refresh"] = {"ok": False, "message": str(exc)}
                _log(f"[health] {name}: refresh 失败: {exc}")

    probe = probe_models(record, proxy=proxy)
    result["probe"] = {
        "models": {
            "ok": probe.get("ok"),
            "status_code": probe.get("status_code"),
            "latency_ms": probe.get("latency_ms"),
            "model_count": probe.get("model_count"),
            "models": probe.get("models") or [],
            "error": probe.get("error"),
            "rate_headers": probe.get("rate_headers") or {},
        }
    }

    # 401 → 再试 refresh 一次
    if probe.get("status_code") == 401 and auto_refresh and not refreshed:
        rt = str(record.get("refresh_token") or "").strip()
        if rt:
            _log(f"[health] {name}: API 401，尝试 refresh 后重试...")
            try:
                token = refresh_access_token(rt, proxy=proxy)
                record = apply_refreshed_token(record, token)
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result["record_updated"] = True
                refreshed = True
                result["refresh"] = {"ok": True, "message": "401 后 refresh 成功"}
                exp = token_expiry_info(record)
                result["expiry"] = exp
                probe = probe_models(record, proxy=proxy)
                result["probe"]["models"] = {
                    "ok": probe.get("ok"),
                    "status_code": probe.get("status_code"),
                    "latency_ms": probe.get("latency_ms"),
                    "model_count": probe.get("model_count"),
                    "models": probe.get("models") or [],
                    "error": probe.get("error"),
                    "rate_headers": probe.get("rate_headers") or {},
                }
            except Exception as exc:
                result["refresh"] = {"ok": False, "message": str(exc)}
                _log(f"[health] {name}: 401 refresh 失败: {exc}")

    if deep:
        chat = probe_mini_chat(record, proxy=proxy)
        result["probe"]["chat"] = chat

    # 汇总状态
    sc = int(probe.get("status_code") or 0)
    err = str(probe.get("error") or "")
    if probe.get("ok"):
        if exp.get("is_expiring_soon"):
            result["status"] = "expiring_soon"
            result["message"] = f"可用，但 token 将在 {exp.get('seconds_left')}s 内过期"
        elif refreshed:
            result["status"] = "refreshed"
            result["message"] = "已自动刷新 token，API 正常"
        else:
            result["status"] = "healthy"
            result["message"] = f"正常 · {probe.get('model_count') or 0} 个模型 · {probe.get('latency_ms')}ms"
    elif sc == 401 or err == "unauthorized_401":
        result["status"] = "unauthorized"
        result["message"] = "401 未授权（refresh 失败或无 refresh_token，需重新注册/换 SSO）"
    elif sc == 402 or err == "quota_or_billing" or "spending" in (probe.get("body_preview") or "").lower():
        result["status"] = "quota_blocked"
        result["message"] = "额度/账单受限 (402/spending-limit)"
    elif sc == 429 or err == "rate_limited":
        result["status"] = "rate_limited"
        result["message"] = "触发限流 (429)"
    elif exp.get("is_expired") and not probe.get("ok"):
        result["status"] = "expired"
        result["message"] = "Token 已过期且无法刷新"
    else:
        result["status"] = "error"
        result["message"] = err or f"探测失败 HTTP {sc}"

    result["score"] = health_score(result["status"], probe, exp)

    # 余额/额度粗信息
    result["quota"] = {
        "signal": (
            "blocked"
            if result["status"] == "quota_blocked"
            else "rate_limited"
            if result["status"] == "rate_limited"
            else "ok"
            if result["status"] in ("healthy", "expiring_soon", "refreshed")
            else "unknown"
        ),
        "rate_headers": probe.get("rate_headers") or {},
        "note": "xAI CLI 通道通常不直接返回余额数字；此处根据 HTTP/报错推断额度状态。",
    }

    # 同步远程
    if result["record_updated"] and sync_remote and remote_url and remote_key:
        try:
            upload_cpa_auth_remote(remote_url, remote_key, record)
            result["synced_remote"] = True
            _log(f"[health] {name}: 已同步远程 CPA")
        except Exception as exc:
            result["synced_remote"] = False
            result["sync_error"] = str(exc)
            _log(f"[health] {name}: 同步远程失败: {exc}")

    return result


def check_many(
    paths: list[Path],
    *,
    proxy: str = "",
    auto_refresh: bool = True,
    deep: bool = False,
    sync_remote: bool = False,
    remote_url: str = "",
    remote_key: str = "",
    log: LogFn | None = None,
    delay: float = 0.4,
) -> list[dict]:
    out = []
    for i, p in enumerate(paths):
        out.append(
            check_one_account(
                p,
                proxy=proxy,
                auto_refresh=auto_refresh,
                deep=deep,
                sync_remote=sync_remote,
                remote_url=remote_url,
                remote_key=remote_key,
                log=log,
            )
        )
        if delay > 0 and i < len(paths) - 1:
            time.sleep(delay)
    return out
