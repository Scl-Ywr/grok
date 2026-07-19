#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grok 注册机 WebUI

功能覆盖 Tk/CLI 全部能力：
- 配置读写（邮箱/代理/NSFW/CPA）
- 启动 / 停止批量注册
- SSE 实时日志
- 账号文件 / 本地 CPA 文件浏览
- 上传到远程 CPA
- 查询远程 CPA auth-files

启动:
  python3 webui.py
  python3 webui.py --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import grok_register_ttk as reg
from account_health import check_many, check_one_account, probe_models, refresh_access_token, apply_refreshed_token, token_expiry_info
from sso_to_auth_json import sso_to_token, token_to_cpa_record, upload_cpa_auth_remote, write_cpa_auth

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "webui_static"
# Docker: ACCOUNTS_DIR=/data/accounts 让账号文件落到持久卷
_ACCOUNTS_DIR = Path(os.environ.get("ACCOUNTS_DIR") or os.environ.get("DATA_DIR") or ROOT)
if str(_ACCOUNTS_DIR) != str(ROOT):
    _ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
ACCOUNTS_DIR = _ACCOUNTS_DIR

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

# ---------------------------------------------------------------------------
# 全局任务状态
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_log_queue: queue.Queue = queue.Queue(maxsize=5000)
_subscribers: list[queue.Queue] = []

_task = {
    "running": False,
    "stop_requested": False,
    "success": 0,
    "fail": 0,
    "target": 0,
    "current": 0,
    "accounts_file": "",
    "started_at": "",
    "finished_at": "",
    "last_error": "",
    "results": [],
}


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _broadcast(message: str) -> None:
    line = f"[{_now()}] {message}"
    try:
        _log_queue.put_nowait(line)
    except queue.Full:
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _log_queue.put_nowait(line)
        except queue.Full:
            pass
    dead = []
    for q in list(_subscribers):
        try:
            q.put_nowait(line)
        except queue.Full:
            dead.append(q)
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _log(message: str) -> None:
    _broadcast(str(message))


def _should_stop() -> bool:
    with _state_lock:
        return bool(_task["stop_requested"])


def _set_task(**kwargs) -> None:
    with _state_lock:
        _task.update(kwargs)


def _get_task() -> dict:
    with _state_lock:
        return dict(_task)


def _public_config() -> dict:
    reg.load_config()
    cfg = dict(reg.config)
    # 不在前端明文回显超长 token 时可按需裁剪；当前按完整配置返回便于编辑
    return cfg


# ---------------------------------------------------------------------------
# 注册任务（复用 grok_register_ttk 核心流程）
# ---------------------------------------------------------------------------

def _run_registration(count: int) -> None:
    success = 0
    fail = 0
    retry_count_for_slot = 0
    max_slot_retry = 3
    accounts_file = str(
        ACCOUNTS_DIR / f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    results = []

    _set_task(
        running=True,
        stop_requested=False,
        success=0,
        fail=0,
        target=count,
        current=0,
        accounts_file=accounts_file,
        started_at=datetime.datetime.now().isoformat(timespec="seconds"),
        finished_at="",
        last_error="",
        results=[],
    )
    _log(f"[*] WebUI 任务启动，目标数量: {count}")
    _log(f"[*] 成功账号将实时保存到: {accounts_file}")

    try:
        reg.start_browser(log_callback=_log)
        _log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if _should_stop():
                _log("[!] 用户停止注册")
                break
            _set_task(current=i + 1)
            _log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
            try:
                email = ""
                dev_token = ""
                code = ""
                mail_ok = False
                max_mail_retry = 3
                for mail_try in range(1, max_mail_retry + 1):
                    if _should_stop():
                        raise reg.RegistrationCancelled("用户停止注册")
                    _log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                    reg.open_signup_page(log_callback=_log, cancel_callback=_should_stop)
                    _log("[*] 2. 创建邮箱并提交")
                    email, dev_token = reg.fill_email_and_submit(
                        log_callback=_log, cancel_callback=_should_stop
                    )
                    _log(f"[*] 邮箱: {email}")
                    try:
                        with open(ACCOUNTS_DIR / "mail_credentials.txt", "a", encoding="utf-8") as f:
                            f.write(f"{email}\t{dev_token}\n")
                    except Exception:
                        pass
                    _log("[*] 3. 拉取验证码")
                    try:
                        code = reg.fill_code_and_submit(
                            email,
                            dev_token,
                            log_callback=_log,
                            cancel_callback=_should_stop,
                        )
                        mail_ok = True
                        break
                    except Exception as mail_exc:
                        msg = str(mail_exc)
                        if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                            _log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                            reg.restart_browser(log_callback=_log)
                            reg.sleep_with_cancel(1, _should_stop)
                            continue
                        raise

                if not mail_ok:
                    raise Exception("验证码阶段失败，已达到最大重试次数")
                _log(f"[*] 验证码: {code}")
                _log("[*] 4. 填写资料")
                profile = reg.fill_profile_and_submit(
                    log_callback=_log, cancel_callback=_should_stop
                )
                _log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                _log("[*] 5. 等待 sso cookie")
                sso = reg.wait_for_sso_cookie(
                    log_callback=_log, cancel_callback=_should_stop
                )
                if reg.config.get("enable_nsfw", True):
                    _log("[*] 6. 开启 NSFW")
                    reg.ensure_browser_on_host(
                        "grok.com",
                        path="/",
                        log_callback=_log,
                        timeout=45,
                        require_clearance=True,
                    )
                    clearance_map, browser_ua = reg.extract_cf_clearance_map(_log)
                    nsfw_ok, nsfw_msg = reg.enable_nsfw_for_token(
                        sso,
                        user_agent=browser_ua,
                        clearance_map=clearance_map,
                        log_callback=_log,
                    )
                    if nsfw_ok:
                        _log(f"[+] NSFW 开启成功: {nsfw_msg}")
                    else:
                        _log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")

                item = {
                    "email": email,
                    "password": profile.get("password", ""),
                    "sso": sso,
                    "profile": {
                        "given_name": profile.get("given_name", ""),
                        "family_name": profile.get("family_name", ""),
                    },
                }
                results.append(item)
                try:
                    with open(accounts_file, "a", encoding="utf-8") as f:
                        f.write(f"{email}----{profile.get('password','')}----{sso}\n")
                except Exception as file_exc:
                    _log(f"[Debug] 保存账号文件失败: {file_exc}")

                reg.add_sso_to_cpa(sso, email=email, log_callback=_log)
                success += 1
                retry_count_for_slot = 0
                i += 1
                _set_task(success=success, fail=fail, results=list(results))
                _log(f"[+] 注册成功: {email}")
                _log(f"[*] 当前统计: 成功 {success} | 失败 {fail}")
                if i < count:
                    reg.sleep_with_cancel(5, _should_stop)
                if success > 0 and success % reg.MEMORY_CLEANUP_INTERVAL == 0 and i < count:
                    reg.cleanup_runtime_memory(
                        log_callback=_log,
                        reason=f"已成功 {success} 个账号，执行定期清理",
                    )
            except reg.RegistrationCancelled:
                _log("[!] 注册被用户停止")
                break
            except reg.AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    _log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    fail += 1
                    retry_count_for_slot = 0
                    i += 1
                    _set_task(success=success, fail=fail)
                    _log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
            except Exception as exc:
                fail += 1
                retry_count_for_slot = 0
                i += 1
                _set_task(success=success, fail=fail, last_error=str(exc))
                _log(f"[-] 注册失败: {exc}")
            finally:
                if _should_stop():
                    break
                try:
                    if reg.browser is None:
                        reg.start_browser(log_callback=_log)
                    else:
                        reg.restart_browser(log_callback=_log)
                    time.sleep(1)
                except reg.RegistrationCancelled:
                    break
                except Exception as restart_exc:
                    if _should_stop():
                        break
                    _log(f"[Debug] 轮次清理/重启浏览器失败: {restart_exc}")
    except reg.RegistrationCancelled:
        _log("[!] 注册被用户停止")
    except Exception as exc:
        _set_task(last_error=str(exc))
        _log(f"[!] 任务异常: {exc}")
        _log(traceback.format_exc())
    finally:
        try:
            reg.stop_browser()
        except BaseException:
            pass
        _set_task(
            running=False,
            finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
            success=success,
            fail=fail,
            results=list(results),
        )
        _log(f"[*] 任务结束。成功 {success} | 失败 {fail}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "time": datetime.datetime.now().isoformat()})


@app.get("/api/config")
def api_get_config():
    return jsonify({"ok": True, "config": _public_config()})


@app.post("/api/config")
def api_save_config():
    data = request.get_json(force=True, silent=True) or {}
    cfg = data.get("config") if isinstance(data.get("config"), dict) else data
    if not isinstance(cfg, dict):
        return jsonify({"ok": False, "error": "无效配置"}), 400

    reg.load_config()
    # 只合并已知字段 + 现有键，避免污染
    allowed = set(reg.DEFAULT_CONFIG.keys()) | set(reg.config.keys()) | {
        "yyds_api_key",
        "yyds_jwt",
        "defaultDomains",
        "email_provider",
        "enable_nsfw",
        "register_count",
        "proxy",
        "user_agent",
        "cpa_auto_add",
        "cpa_auth_dir",
        "cpa_remote_url",
        "cpa_management_key",
        "duckmail_api_key",
        "cloudflare_api_base",
        "cloudflare_api_key",
        "cloudflare_auth_mode",
        "cloudflare_custom_auth",
        "cloudflare_path_domains",
        "cloudflare_path_accounts",
        "cloudflare_path_token",
        "cloudflare_path_messages",
    }
    for key, value in cfg.items():
        if key in allowed:
            reg.config[key] = value

    # 类型规整
    try:
        reg.config["register_count"] = int(reg.config.get("register_count") or 1)
    except Exception:
        reg.config["register_count"] = 1
    reg.config["enable_nsfw"] = bool(reg.config.get("enable_nsfw", True))
    reg.config["cpa_auto_add"] = bool(reg.config.get("cpa_auto_add", False))

    remote = str(reg.config.get("cpa_remote_url") or "").strip()
    if remote and "://" not in remote:
        reg.config["cpa_remote_url"] = f"https://{remote}"

    reg.save_config()
    _log("[*] 配置已保存")
    return jsonify({"ok": True, "config": _public_config()})


@app.get("/api/status")
def api_status():
    task = _get_task()
    return jsonify(
        {
            "ok": True,
            "task": task,
            "config_summary": {
                "email_provider": reg.config.get("email_provider"),
                "register_count": reg.config.get("register_count"),
                "enable_nsfw": reg.config.get("enable_nsfw"),
                "cpa_auto_add": reg.config.get("cpa_auto_add"),
                "cpa_remote_url": reg.config.get("cpa_remote_url"),
                "proxy": reg.config.get("proxy"),
            },
        }
    )


@app.post("/api/register/start")
def api_register_start():
    if _get_task()["running"]:
        return jsonify({"ok": False, "error": "已有任务在运行"}), 409

    body = request.get_json(force=True, silent=True) or {}
    # 允许启动前顺带更新部分配置
    if isinstance(body.get("config"), dict):
        reg.load_config()
        for k, v in body["config"].items():
            reg.config[k] = v
        reg.save_config()

    reg.load_config()
    try:
        count = int(body.get("count") or reg.config.get("register_count") or 1)
    except Exception:
        return jsonify({"ok": False, "error": "注册数量无效"}), 400
    if count < 1:
        return jsonify({"ok": False, "error": "注册数量至少为 1"}), 400

    if reg.config.get("email_provider") == "cloudflare" and not reg.config.get(
        "cloudflare_api_base"
    ):
        return jsonify({"ok": False, "error": "Cloudflare 模式需要填写 cloudflare_api_base"}), 400

    reg.config["register_count"] = count
    reg.save_config()

    t = threading.Thread(target=_run_registration, args=(count,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"已启动，目标 {count} 个"})


@app.post("/api/register/stop")
def api_register_stop():
    if not _get_task()["running"]:
        return jsonify({"ok": False, "error": "当前没有运行中的任务"}), 400
    _set_task(stop_requested=True)
    _log("[!] 收到停止请求")
    return jsonify({"ok": True, "message": "正在停止..."})


@app.get("/api/logs/stream")
def api_logs_stream():
    q: queue.Queue = queue.Queue(maxsize=1000)
    _subscribers.append(q)

    # 回放最近日志
    recent = list(_log_queue.queue)

    def gen():
        try:
            for line in recent:
                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'line': f'[{_now()}] [*] 日志流已连接'}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    line = q.get(timeout=15)
                    yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'ping': True})}\n\n"
        finally:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str, prefix: str = "") -> str:
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("非法文件名")
    if prefix and not name.startswith(prefix):
        raise ValueError(f"文件名必须以 {prefix} 开头")
    return name


def _auth_dir() -> Path:
    return Path(str(reg.config.get("cpa_auth_dir") or (Path.home() / ".cli-proxy-api"))).expanduser()


def _remote_cfg(body=None):
    body = body or {}
    reg.load_config()
    remote = str(body.get("remote") or reg.config.get("cpa_remote_url") or "").strip()
    key = str(body.get("key") or reg.config.get("cpa_management_key") or "").strip()
    if remote and "://" not in remote:
        remote = f"https://{remote}"
    return remote.rstrip("/"), key


def _parse_account_line(ln: str) -> dict:
    ln = str(ln or "").strip()
    parts = ln.split("----")
    return {
        "email": parts[0] if len(parts) > 0 else "",
        "password": parts[1] if len(parts) > 1 else "",
        "sso": parts[2] if len(parts) > 2 else "",
        "raw": ln,
    }


def _write_account_rows(path: Path, rows: list) -> None:
    lines = []
    for r in rows:
        if isinstance(r, dict):
            email = str(r.get("email") or "").strip()
            password = str(r.get("password") or "").strip()
            sso = str(r.get("sso") or "").strip()
            if not email:
                continue
            lines.append(f"{email}----{password}----{sso}")
        else:
            s = str(r).strip()
            if s:
                lines.append(s)
    path.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")


def _remote_request(method: str, remote: str, key: str, name: str = "", body=None):
    import requests as _req

    url = f"{remote}/v0/management/auth-files"
    params = {"name": name} if name else None
    headers = {"Authorization": f"Bearer {key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    else:
        data = None
    resp = _req.request(method, url, params=params, headers=headers, data=data, timeout=30)
    return resp


# ---------------------------------------------------------------------------
# Accounts file CRUD  (accounts_*.txt)
# ---------------------------------------------------------------------------

@app.get("/api/accounts")
def api_accounts():
    files = sorted(ACCOUNTS_DIR.glob("accounts_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for p in files[:100]:
        try:
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        except Exception:
            lines = []
        items.append(
            {
                "name": p.name,
                "path": str(p),
                "count": len(lines),
                "size": p.stat().st_size,
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                "preview": [{"email": (ln.split("----")[0] if "----" in ln else ln[:40])} for ln in lines[:8]],
            }
        )
    return jsonify({"ok": True, "files": items})


@app.post("/api/accounts")
def api_accounts_create():
    """创建账号文件。body: {name?, rows?}"""
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        name = f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    if not name.endswith(".txt"):
        name += ".txt"
    if not name.startswith("accounts_"):
        name = "accounts_" + name
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if path.exists() and not body.get("overwrite"):
        return jsonify({"ok": False, "error": f"文件已存在: {name}"}), 409
    rows = body.get("rows") or []
    _write_account_rows(path, rows)
    _log(f"[*] 创建账号文件: {name} ({len(rows)} 行)")
    return jsonify({"ok": True, "name": name})


@app.get("/api/accounts/<name>")
def api_account_file(name: str):
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows = []
    for i, ln in enumerate(text.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        item = _parse_account_line(ln)
        item["index"] = i
        rows.append(item)
    return jsonify({"ok": True, "name": name, "rows": rows, "count": len(rows)})


@app.put("/api/accounts/<name>")
def api_account_file_replace(name: str):
    """整文件替换 rows。"""
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "rows 必须是数组"}), 400
    _write_account_rows(path, rows)
    _log(f"[*] 更新账号文件: {name} -> {len(rows)} 行")
    return jsonify({"ok": True, "name": name, "count": len(rows)})


@app.post("/api/accounts/<name>/rows")
def api_account_add_row(name: str):
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "").strip()
    sso = str(body.get("sso") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email 必填"}), 400
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{email}----{password}----{sso}\n")
    _log(f"[*] 账号文件追加: {name} + {email}")
    return jsonify({"ok": True})


@app.put("/api/accounts/<name>/rows/<int:index>")
def api_account_update_row(name: str, index: int):
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if index < 0 or index >= len(lines):
        return jsonify({"ok": False, "error": "行索引越界"}), 400
    cur = _parse_account_line(lines[index])
    email = str(body.get("email", cur["email"])).strip()
    password = str(body.get("password", cur["password"])).strip()
    sso = str(body.get("sso", cur["sso"])).strip()
    lines[index] = f"{email}----{password}----{sso}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log(f"[*] 账号文件改行: {name}#{index} -> {email}")
    return jsonify({"ok": True})


@app.delete("/api/accounts/<name>/rows/<int:index>")
def api_account_delete_row(name: str, index: int):
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if index < 0 or index >= len(lines):
        return jsonify({"ok": False, "error": "行索引越界"}), 400
    removed = lines.pop(index)
    path.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")
    _log(f"[*] 账号文件删行: {name}#{index} ({removed.split('----')[0]})")
    return jsonify({"ok": True, "removed": removed})


@app.delete("/api/accounts/<name>")
def api_account_delete_file(name: str):
    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    path.unlink()
    _log(f"[*] 删除账号文件: {name}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Local CPA auth CRUD  (xai-*.json)
# ---------------------------------------------------------------------------

@app.get("/api/local-auth")
def api_local_auth():
    auth_dir = _auth_dir()
    files = sorted(auth_dir.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for p in files[:300]:
        email = ""
        provider = ""
        disabled = False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            email = data.get("email") or ""
            provider = data.get("type") or data.get("provider") or ""
            disabled = bool(data.get("disabled", False))
        except Exception:
            data = {}
        items.append(
            {
                "name": p.name,
                "email": email,
                "provider": provider,
                "disabled": disabled,
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                "size": p.stat().st_size,
            }
        )
    return jsonify({"ok": True, "dir": str(auth_dir), "files": items, "count": len(items)})


@app.get("/api/local-auth/<name>")
def api_local_auth_get(name: str):
    try:
        name = _safe_name(name, "xai-")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = _auth_dir() / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"JSON 解析失败: {e}"}), 500
    return jsonify({"ok": True, "name": name, "record": data})


@app.post("/api/local-auth")
def api_local_auth_create():
    """创建/覆盖本地 auth。body: {name?, record}"""
    body = request.get_json(force=True, silent=True) or {}
    record = body.get("record")
    if not isinstance(record, dict):
        return jsonify({"ok": False, "error": "record 必须是对象"}), 400
    name = str(body.get("name") or "").strip()
    if not name:
        email = str(record.get("email") or "unknown").strip()
        safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email) or "unknown"
        name = safe if safe.lower().startswith("xai") else f"xai-{safe}"
        if not name.endswith(".json"):
            name += ".json"
    try:
        name = _safe_name(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not name.endswith(".json"):
        name += ".json"
    auth_dir = _auth_dir()
    auth_dir.mkdir(parents=True, exist_ok=True)
    path = auth_dir / name
    if path.exists() and not body.get("overwrite"):
        return jsonify({"ok": False, "error": f"已存在: {name}，传 overwrite=true 可覆盖"}), 409
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _log(f"[*] 本地 auth 写入: {name}")
    return jsonify({"ok": True, "name": name})


@app.put("/api/local-auth/<name>")
def api_local_auth_update(name: str):
    try:
        name = _safe_name(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = _auth_dir() / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    record = body.get("record")
    if not isinstance(record, dict):
        # 允许直接 patch 字段
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        for k, v in body.items():
            if k != "record":
                current[k] = v
        record = current
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _log(f"[*] 本地 auth 更新: {name}")
    return jsonify({"ok": True, "name": name, "record": record})


@app.delete("/api/local-auth/<name>")
def api_local_auth_delete(name: str):
    try:
        name = _safe_name(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = _auth_dir() / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    path.unlink()
    _log(f"[*] 本地 auth 删除: {name}")
    return jsonify({"ok": True})


@app.post("/api/local-auth/upload")
def api_local_auth_upload_selected():
    """上传指定本地文件到远程。body: {names: []} 或全部。"""
    if _get_task()["running"]:
        return jsonify({"ok": False, "error": "注册任务运行中，请稍后再上传"}), 409
    body = request.get_json(force=True, silent=True) or {}
    remote, key = _remote_cfg(body)
    if not remote or not key:
        return jsonify({"ok": False, "error": "缺少 remote / key"}), 400
    auth_dir = _auth_dir()
    names = body.get("names")
    if names:
        files = []
        for n in names:
            try:
                n = _safe_name(str(n))
            except ValueError:
                continue
            p = auth_dir / n
            if p.exists():
                files.append(p)
    else:
        files = sorted(auth_dir.glob("xai-*.json"))
    if not files:
        return jsonify({"ok": False, "error": "没有可上传的文件"}), 404

    ok = fail = 0
    details = []
    _log(f"[*] 上传本地 auth 到远程: {remote} ({len(files)} 个)")
    for p in files:
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
            name = upload_cpa_auth_remote(remote, key, record)
            email = record.get("email") or p.name
            details.append({"email": email, "name": name, "ok": True})
            _log(f"  OK {email} -> {name}")
            ok += 1
        except Exception as exc:
            details.append({"email": p.name, "error": str(exc), "ok": False})
            _log(f"  FAIL {p.name}: {exc}")
            fail += 1
    _log(f"[*] 上传完成: 成功 {ok}, 失败 {fail}")
    return jsonify({"ok": True, "success": ok, "fail": fail, "details": details})


# 兼容旧接口
@app.post("/api/upload-cpa")
def api_upload_cpa():
    return api_local_auth_upload_selected()


# ---------------------------------------------------------------------------
# Remote CPA auth CRUD
# ---------------------------------------------------------------------------

@app.get("/api/remote-auth")
def api_remote_auth():
    remote, key = _remote_cfg()
    if not remote or not key:
        return jsonify({"ok": False, "error": "未配置 cpa_remote_url / cpa_management_key"}), 400
    try:
        resp = _remote_request("GET", remote, key)
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"}), 502
        data = resp.json()
        files = data.get("files") if isinstance(data, dict) else data
        return jsonify({"ok": True, "remote": remote, "files": files or []})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/accounts/convert-to-json")
def api_accounts_convert_to_json():
    """把账号文件的 SSO 转成 CPA JSON，可选写本地 / 上传远程。

    body: {
      name: string,           # accounts 文件名
      saveLocal: bool,        # 写入本地 auth 目录
      uploadRemote: bool,     # 上传远程 CPA
      rowIndexes?: number[],  # 可选：只处理指定行（0-based），不传则全量
    }
    """
    if _get_task()["running"]:
        return jsonify({"ok": False, "error": "注册任务运行中"}), 409

    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()
    save_local = bool(body.get("saveLocal", True))
    upload_remote = bool(body.get("uploadRemote", False))
    row_indexes = body.get("rowIndexes")

    if not name:
        return jsonify({"ok": False, "error": "缺少 name"}), 400

    try:
        name = _safe_name(name, "accounts_")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    path = ACCOUNTS_DIR / name
    if not path.exists():
        return jsonify({"ok": False, "error": f"文件不存在: {name}"}), 404

    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    if not lines:
        return jsonify({"ok": False, "error": "文件为空"}), 400

    if row_indexes is not None and isinstance(row_indexes, list):
        targets = [(i, lines[i]) for i in row_indexes if 0 <= i < len(lines)]
    else:
        targets = list(enumerate(lines))

    if not targets:
        return jsonify({"ok": False, "error": "无有效行"}), 400

    reg.load_config()
    proxy = str(reg.config.get("proxy") or "").strip()
    auth_dir = _auth_dir()
    remote, key = _remote_cfg(reg.config if upload_remote else {})

    _log(f"[*] 账号转 CPA JSON: {name} → {len(targets)} 行 (local={save_local}, remote={upload_remote})")

    ok = fail = 0
    details = []
    for idx, ln in targets:
        parts = ln.split("----", 2)
        if len(parts) < 3:
            fail += 1
            details.append({"index": idx, "email": parts[0] if parts else "", "ok": False, "message": "格式错误，需 email----password----sso"})
            _log(f"  [{idx}] 格式错误: {ln[:60]}")
            continue

        email = parts[0].strip()
        sso = parts[2].strip()
        if not sso:
            fail += 1
            details.append({"index": idx, "email": email, "ok": False, "message": "SSO 为空"})
            continue

        entry = {"index": idx, "email": email, "ok": False, "method": "", "message": ""}
        _log(f"  [{idx}] {email} → 换 token...")

        try:
            token = sso_to_token(sso, proxy=proxy, log=lambda m: None, max_retries=2)
            if not token:
                raise RuntimeError("device flow 未返回 token")

            record = token_to_cpa_record(token, email=email)
            entry["method"] = "sso_exchange"

            if save_local:
                p = write_cpa_auth(auth_dir, record)
                _log(f"  [{idx}] 本地写入: {p.name}")
                entry["local_name"] = p.name

            if upload_remote and remote and key:
                try:
                    rname = upload_cpa_auth_remote(remote, key, record)
                    _log(f"  [{idx}] 远程上传: {rname}")
                    entry["remote_name"] = rname
                except Exception as re:
                    entry["remote_error"] = str(re)
                    _log(f"  [{idx}] 远程上传失败: {re}")

            entry["ok"] = True
            entry["message"] = "成功"
            ok += 1
        except Exception as exc:
            entry["message"] = str(exc)
            fail += 1
            _log(f"  [{idx}] 失败: {exc}")

        details.append(entry)

    _log(f"[*] 转换完成: 成功 {ok}, 失败 {fail}")
    return jsonify({"ok": True, "success": ok, "fail": fail, "details": details})


@app.post("/api/remote-auth")
def api_remote_auth_create():
    """创建/覆盖远程 auth。body: {name?, record}"""
    body = request.get_json(force=True, silent=True) or {}
    remote, key = _remote_cfg(body)
    if not remote or not key:
        return jsonify({"ok": False, "error": "缺少 remote / key"}), 400
    record = body.get("record")
    if not isinstance(record, dict):
        return jsonify({"ok": False, "error": "record 必须是对象"}), 400
    name = str(body.get("name") or "").strip()
    if not name:
        email = str(record.get("email") or "unknown").strip()
        safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email) or "unknown"
        name = safe if safe.lower().startswith("xai") else f"xai-{safe}"
        if not name.endswith(".json"):
            name += ".json"
    try:
        resp = _remote_request("POST", remote, key, name=name, body=record)
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"}), 502
        _log(f"[*] 远程 auth 写入: {name}")
        return jsonify({"ok": True, "name": name, "remote": remote})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.delete("/api/remote-auth/<path:name>")
def api_remote_auth_delete(name: str):
    remote, key = _remote_cfg(request.get_json(force=True, silent=True) or {})
    if not remote or not key:
        # also allow query
        remote, key = _remote_cfg({"remote": request.args.get("remote"), "key": request.args.get("key")})
        if not remote or not key:
            remote, key = _remote_cfg()
    if not remote or not key:
        return jsonify({"ok": False, "error": "缺少 remote / key"}), 400
    name = str(name or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name 必填"}), 400
    try:
        resp = _remote_request("DELETE", remote, key, name=name)
        if resp.status_code >= 400:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"}), 502
        _log(f"[*] 远程 auth 删除: {name}")
        return jsonify({"ok": True, "name": name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/remote-auth/batch-delete")
def api_remote_auth_batch_delete():
    body = request.get_json(force=True, silent=True) or {}
    remote, key = _remote_cfg(body)
    names = body.get("names") or []
    if not remote or not key:
        return jsonify({"ok": False, "error": "缺少 remote / key"}), 400
    if not names:
        return jsonify({"ok": False, "error": "names 为空"}), 400
    ok = fail = 0
    details = []
    for name in names:
        try:
            resp = _remote_request("DELETE", remote, key, name=str(name))
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
            details.append({"name": name, "ok": True})
            ok += 1
        except Exception as exc:
            details.append({"name": name, "ok": False, "error": str(exc)})
            fail += 1
    _log(f"[*] 远程批量删除: 成功 {ok}, 失败 {fail}")
    return jsonify({"ok": True, "success": ok, "fail": fail, "details": details})


# ---------------------------------------------------------------------------
# 健康检测 API
# ---------------------------------------------------------------------------

@app.post("/api/health-check")
def api_health_check():
    """检测本地 auth 文件健康状态。

    body: {names?: string[], deep?: bool, autoRefresh?: bool, syncRemote?: bool}
    不传 names 则检测全部。
    """
    if _get_task()["running"]:
        return jsonify({"ok": False, "error": "注册任务运行中，请稍后再检测"}), 409

    body = request.get_json(force=True, silent=True) or {}
    reg.load_config()
    proxy = str(body.get("proxy") or reg.config.get("proxy") or "").strip()
    auto_refresh = bool(body.get("autoRefresh", True))
    deep = bool(body.get("deep", False))
    sync_remote = bool(body.get("syncRemote", False))
    remote, key = _remote_cfg(reg.config if sync_remote else {})

    auth_dir = _auth_dir()
    names = body.get("names")
    if names:
        files = []
        for n in names:
            try:
                n = _safe_name(str(n))
            except ValueError:
                continue
            p = auth_dir / n
            if p.exists():
                files.append(p)
    else:
        files = sorted(auth_dir.glob("xai-*.json"))

    if not files:
        return jsonify({"ok": False, "error": "没有可检测的文件"}), 404

    _log(f"[*] 开始健康检测: {len(files)} 个 (autoRefresh={auto_refresh}, deep={deep}, syncRemote={sync_remote})")
    results = check_many(
        files,
        proxy=proxy,
        auto_refresh=auto_refresh,
        deep=deep,
        sync_remote=sync_remote,
        remote_url=remote or "",
        remote_key=key or "",
        log=_log,
        delay=0.5 if deep else 0.3,
    )
    summary = {
        "total": len(results),
        "healthy": sum(1 for r in results if r.get("status") == "healthy"),
        "expiring_soon": sum(1 for r in results if r.get("status") == "expiring_soon"),
        "refreshed": sum(1 for r in results if r.get("status") == "refreshed"),
        "unauthorized": sum(1 for r in results if r.get("status") == "unauthorized"),
        "expired": sum(1 for r in results if r.get("status") == "expired"),
        "quota_blocked": sum(1 for r in results if r.get("status") == "quota_blocked"),
        "rate_limited": sum(1 for r in results if r.get("status") == "rate_limited"),
        "error": sum(1 for r in results if r.get("status") in ("error", "invalid_file")),
        "disabled": sum(1 for r in results if r.get("status") == "disabled"),
        "avg_score": round(sum(r.get("score", 0) for r in results) / max(len(results), 1)),
        "auto_refreshed": sum(1 for r in results if r.get("record_updated")),
        "synced_remote": sum(1 for r in results if r.get("synced_remote")),
    }
    _log(f"[*] 健康检测完成: 健康 {summary['healthy']}, 需关注 {summary['unauthorized']+summary['expired']+summary['quota_blocked']}, 平均分 {summary['avg_score']}")
    return jsonify({"ok": True, "summary": summary, "results": results})


@app.post("/api/health-check/<name>")
def api_health_check_one(name: str):
    """检测单个本地 auth 文件。"""
    try:
        name = _safe_name(name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    path = _auth_dir() / name
    if not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404

    body = request.get_json(force=True, silent=True) or {}
    reg.load_config()
    proxy = str(body.get("proxy") or reg.config.get("proxy") or "").strip()
    auto_refresh = bool(body.get("autoRefresh", True))
    deep = bool(body.get("deep", False))
    sync_remote = bool(body.get("syncRemote", False))
    remote, key = _remote_cfg(reg.config if sync_remote else {})

    result = check_one_account(
        path,
        proxy=proxy,
        auto_refresh=auto_refresh,
        deep=deep,
        sync_remote=sync_remote,
        remote_url=remote or "",
        remote_key=key or "",
        log=_log,
    )
    return jsonify({"ok": True, "result": result})


@app.post("/api/health-refresh")
def api_health_refresh():
    """强制用 refresh_token 刷新指定账号并同步远程。

    body: {names: string[], syncRemote?: bool}
    """
    body = request.get_json(force=True, silent=True) or {}
    reg.load_config()
    proxy = str(body.get("proxy") or reg.config.get("proxy") or "").strip()
    sync_remote = bool(body.get("syncRemote", True))
    remote, key = _remote_cfg(reg.config if sync_remote else {})
    auth_dir = _auth_dir()
    names = body.get("names") or []
    if not names:
        return jsonify({"ok": False, "error": "names 为空"}), 400

    ok = fail = 0
    details = []
    for n in names:
        try:
            n = _safe_name(str(n))
            path = auth_dir / n
            if not path.exists():
                raise FileNotFoundError(f"文件不存在: {n}")
            result = check_one_account(
                path,
                proxy=proxy,
                auto_refresh=True,
                deep=False,
                sync_remote=sync_remote,
                remote_url=remote or "",
                remote_key=key or "",
                log=_log,
            )
            if result.get("refresh", {}).get("ok"):
                ok += 1
            elif result.get("status") in ("healthy", "expiring_soon"):
                ok += 1
            else:
                fail += 1
            details.append({"name": n, "status": result.get("status"), "message": result.get("message", ""), "ok": result.get("refresh", {}).get("ok")})
        except Exception as exc:
            fail += 1
            details.append({"name": n, "ok": False, "error": str(exc)})
    _log(f"[*] 强制刷新: 成功 {ok}, 失败 {fail}")
    return jsonify({"ok": True, "success": ok, "fail": fail, "details": details})


@app.post("/api/health-delete")
def api_health_delete():
    """批量删除本地 auth 文件并可选删除远程。

    body: {names: string[], deleteRemote?: bool}
    """
    body = request.get_json(force=True, silent=True) or {}
    names = body.get("names") or []
    delete_remote = bool(body.get("deleteRemote", True))
    remote, key = _remote_cfg(reg.config if delete_remote else {})
    auth_dir = _auth_dir()

    if not names:
        return jsonify({"ok": False, "error": "names 为空"}), 400

    ok = fail = 0
    details = []
    _log(f"[*] 删除账号: {len(names)} 个 (remote={delete_remote})")
    for n in names:
        entry = {"name": n, "local_ok": False, "remote_ok": False}
        # 本地删除
        try:
            safe = _safe_name(str(n))
            path = auth_dir / safe
            if path.exists():
                path.unlink()
                entry["local_ok"] = True
                _log(f"  本地已删除: {safe}")
            else:
                entry["local_ok"] = True  # 不存在也算成功
                _log(f"  本地已不存在: {safe}")
        except Exception as exc:
            entry["local_error"] = str(exc)
            _log(f"  本地删除失败: {n} — {exc}")
        # 远程删除
        if delete_remote and remote and key:
            try:
                resp = _remote_request("DELETE", remote, key, name=str(n))
                if resp.status_code >= 400:
                    body_text = (resp.text or "")[:200]
                    if resp.status_code == 404 or "not found" in body_text.lower():
                        entry["remote_ok"] = True
                    else:
                        raise RuntimeError(f"HTTP {resp.status_code}: {body_text}")
                else:
                    entry["remote_ok"] = True
                _log(f"  远程已删除: {n}")
            except Exception as exc:
                entry["remote_error"] = str(exc)
                _log(f"  远程删除失败: {n} — {exc}")
        else:
            entry["remote_ok"] = True  # 不需要删远程时视为跳过
        if entry["local_ok"] and entry["remote_ok"]:
            ok += 1
        else:
            fail += 1
        details.append(entry)

    _log(f"[*] 删除完成: 成功 {ok}, 失败 {fail}")
    return jsonify({"ok": True, "success": ok, "fail": fail, "details": details})


# ---------------------------------------------------------------------------
# SSO 重新登录 / 导出
# ---------------------------------------------------------------------------

def _load_sso_map() -> dict[str, str]:
    """扫描所有 accounts_*.txt，构建 email -> sso 映射。"""
    sso_map = {}
    for p in ACCOUNTS_DIR.glob("accounts_*.txt"):
        try:
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                ln = ln.strip()
                if not ln or "----" not in ln:
                    continue
                parts = ln.split("----", 2)
                if len(parts) >= 3 and parts[2].strip():
                    sso_map[parts[0].strip().lower()] = parts[2].strip()
        except Exception:
            pass
    return sso_map


def _load_sso_map_from_anywhere() -> dict[str, str]:
    """从 accounts 文件和本地 auth 目录收集 email → sso/refresh 映射。"""
    sso_map = _load_sso_map()
    # 也把现有 auth 文件的 refresh_token 当作备选
    auth_dir = _auth_dir()
    for p in auth_dir.glob("xai-*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            email = str(d.get("email") or "").strip().lower()
            rt = str(d.get("refresh_token") or "").strip()
            if email and rt and email not in sso_map:
                sso_map[f"{email}__refresh"] = rt
        except Exception:
            pass
    return sso_map


@app.get("/api/sso-map")
def api_sso_map():
    """查看当前 email → SSO 映射（供前端显示哪些有原始 SSO）。"""
    sso_map = _load_sso_map()
    return jsonify({
        "ok": True,
        "count": len(sso_map),
        "emails": sorted(sso_map.keys()),
    })


@app.post("/api/sso-relogin")
def api_sso_relogin():
    """批量用原始 SSO 重新 device flow 换 token，更新本地 auth + 远程 CPA。

    body: {names?: string[], syncRemote?: bool, proxy?: string}
    不传 names 则对所有401/过期的 auth 文件操作。
    """
    if _get_task()["running"]:
        return jsonify({"ok": False, "error": "注册任务运行中"}), 409

    body = request.get_json(force=True, silent=True) or {}
    reg.load_config()
    proxy = str(body.get("proxy") or reg.config.get("proxy") or "").strip()
    sync_remote = bool(body.get("syncRemote", True))
    remote, key = _remote_cfg(reg.config if sync_remote else {})
    auth_dir = _auth_dir()

    # 加载原始 SSO 映射
    sso_map = _load_sso_map()
    _log(f"[*] SSO 重新登录: 已加载 {len(sso_map)} 条原始 SSO")

    # 确定目标
    names = body.get("names")
    if names:
        targets = [n for n in names if str(n).strip()]
    else:
        targets = [p.name for p in sorted(auth_dir.glob("xai-*.json"))]

    if not targets:
        return jsonify({"ok": False, "error": "没有可处理的账号"}), 404

    ok = fail = skip = 0
    details = []
    _log(f"[*] SSO 重新登录: 目标 {len(targets)} 个")

    for n in targets:
        entry = {"name": n, "email": "", "method": "", "ok": False, "message": ""}
        try:
            safe = _safe_name(str(n))
            path = auth_dir / safe
            if not path.exists():
                raise FileNotFoundError(f"文件不存在: {safe}")
            record = json.loads(path.read_text(encoding="utf-8"))
            email = str(record.get("email") or "").strip()
            entry["email"] = email

            # 优先尝试 refresh_token
            rt = str(record.get("refresh_token") or "").strip()
            if rt:
                _log(f"  [{n}] 尝试 refresh_token...")
                try:
                    token = refresh_access_token(rt, proxy=proxy)
                    record = apply_refreshed_token(record, token)
                    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    entry["method"] = "refresh"
                    entry["ok"] = True
                    entry["message"] = "refresh 成功"
                    _log(f"  [{n}] refresh 成功")
                    ok += 1
                    if sync_remote and remote and key:
                        try:
                            upload_cpa_auth_remote(remote, key, record)
                            _log(f"  [{n}] 已同步远程")
                        except Exception as re:
                            _log(f"  [{n}] 同步远程失败: {re}")
                    details.append(entry)
                    continue
                except Exception as re:
                    _log(f"  [{n}] refresh 失败: {re}")

            # fallback: 用原始 SSO 重新 device flow
            sso_email = email.lower()
            sso = sso_map.get(sso_email)
            if not sso:
                # 尝试用户名部分匹配
                for map_email, map_sso in sso_map.items():
                    if map_email.startswith(sso_email.split("@")[0].lower()):
                        sso = map_sso
                        break

            if not sso:
                entry["method"] = "none"
                entry["message"] = "无原始 SSO，也无 refresh_token，跳过"
                skip += 1
                _log(f"  [{n}] 无 SSO，跳过")
                details.append(entry)
                continue

            _log(f"  [{n}] 用原始 SSO 重新 device flow...")
            token = sso_to_token(sso, proxy=proxy, log=lambda m: _log(f"  [{n}] {m}"), max_retries=2)
            if not token:
                raise RuntimeError("device flow 未返回 token")

            # 构建新 CPA record
            new_record = token_to_cpa_record(token, email=email)
            # 保留原记录的 headers/base_url 等
            for k in ("disabled", "headers", "base_url", "redirect_uri"):
                if k in record and record.get(k) not in (None, ""):
                    if k == "headers" and isinstance(record.get("headers"), dict):
                        merged = dict(new_record.get("headers") or {})
                        merged.update(record["headers"])
                        new_record["headers"] = merged
                    else:
                        new_record[k] = record[k]

            path.write_text(json.dumps(new_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            entry["method"] = "sso_relogin"
            entry["ok"] = True
            entry["message"] = "SSO 重新登录成功"
            _log(f"  [{n}] SSO 重新登录成功")
            ok += 1

            if sync_remote and remote and key:
                try:
                    upload_cpa_auth_remote(remote, key, new_record)
                    _log(f"  [{n}] 已同步远程")
                except Exception as re:
                    _log(f"  [{n}] 同步远程失败: {re}")

        except Exception as exc:
            entry["message"] = str(exc)
            fail += 1
            _log(f"  [{n}] 失败: {exc}")
        details.append(entry)

    _log(f"[*] SSO 重新登录完成: 成功 {ok}, 失败 {fail}, 跳过 {skip}")
    return jsonify({
        "ok": True,
        "success": ok,
        "fail": fail,
        "skip": skip,
        "details": details,
    })


@app.get("/api/sso-export")
def api_sso_export():
    """导出可用账号的 SSO 列表，格式：email----password----sso。

    query: filter=all|healthy|non-failing|failing
    """
    filter_type = request.args.get("filter", "all").strip().lower()
    sso_map = _load_sso_map()  # email → sso
    auth_dir = _auth_dir()

    out_lines = []
    for p in sorted(auth_dir.glob("xai-*.json")):
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        email = str(record.get("email") or "").strip()
        if not email:
            continue

        # 简易状态判断（不做网络请求）
        exp = token_expiry_info(record)
        is_expired = exp.get("is_expired", False)
        rt = str(record.get("refresh_token") or "").strip()
        at = str(record.get("access_token") or record.get("key") or "").strip()

        if filter_type == "healthy" and (is_expired or not at):
            continue
        elif filter_type == "failing" and not is_expired and at:
            continue

        sso = sso_map.get(email.lower(), "")
        out_lines.append(f"{email}----{sso or 'N/A'}")

    return jsonify({
        "ok": True,
        "count": len(out_lines),
        "lines": out_lines,
        "filter": filter_type,
    })


@app.post("/api/sso-export-download")
def api_sso_export_download():
    """下载导出的 SSO 文本文件。"""
    import io
    body = request.get_json(force=True, silent=True) or {}
    lines = body.get("lines") or []
    if not lines:
        sso_data = api_sso_export()
        lines = sso_data.get("lines") or []
    content = "\n".join(lines) + ("\n" if lines else "")
    return Response(
        content,
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=sso_export.txt"},
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Grok 注册机 WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    reg.load_config()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[*] Grok 注册机 WebUI: http://{args.host}:{args.port}")
    print(f"[*] 配置文件: {reg.CONFIG_FILE}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
