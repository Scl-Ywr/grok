#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量上传本地 CPA auth 文件到远程 CLIProxyAPI。

默认读取:
  - 本地目录: ~/.cli-proxy-api/xai-*.json
  - 远程配置: 同目录 config.json 的 cpa_remote_url / cpa_management_key

用法:
  python3 upload_to_cpa.py
  python3 upload_to_cpa.py --auth-dir ~/.cli-proxy-api
  python3 upload_to_cpa.py --remote https://cpa.example.com --key YOUR_KEY
  python3 upload_to_cpa.py --sso accounts_xxx.txt   # 从 SSO 文本重新换 token 再上传
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sso_to_auth_json import (
    load_sso_list,
    sso_to_token,
    token_to_cpa_record,
    upload_cpa_auth_remote,
    write_cpa_auth,
)


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
DEFAULT_AUTH_DIR = Path.home() / ".cli-proxy-api"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[!] 读取 config.json 失败: {exc}")
        return {}


def normalize_remote(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = f"https://{base}"
    return base


def upload_local_auth_files(auth_dir: Path, remote: str, key: str) -> int:
    files = sorted(auth_dir.glob("xai-*.json"))
    if not files:
        print(f"[!] 目录里没有 xai-*.json: {auth_dir}")
        return 1

    print(f"[*] 准备上传 {len(files)} 个文件")
    print(f"[*] 本地目录: {auth_dir}")
    print(f"[*] 远程地址: {remote}")
    ok = fail = 0

    for idx, path in enumerate(files, 1):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            name = upload_cpa_auth_remote(remote, key, record)
            email = record.get("email") or path.name
            print(f"  [{idx}/{len(files)}] OK  {email} -> {name}")
            ok += 1
        except Exception as exc:
            print(f"  [{idx}/{len(files)}] FAIL {path.name}: {exc}")
            fail += 1

    print(f"\n完成: 成功 {ok}, 失败 {fail}")
    return 0 if fail == 0 else 1


def upload_from_sso_file(
    sso_file: Path,
    remote: str,
    key: str,
    proxy: str = "",
    auth_dir: Path | None = None,
    delay: float = 0,
) -> int:
    cookies = load_sso_list(str(sso_file), None)
    if not cookies:
        print(f"[!] SSO 列表为空: {sso_file}")
        return 1

    print(f"[*] 从 SSO 文件上传 {len(cookies)} 个账号")
    print(f"[*] SSO 文件: {sso_file}")
    print(f"[*] 远程地址: {remote}")
    if proxy:
        print(f"[*] 代理: {proxy}")
    ok = fail = 0

    import time

    for idx, sso in enumerate(cookies, 1):
        print(f"\n[{idx}/{len(cookies)}] 换 token ...")
        try:
            # load_sso_list 可能返回纯 sso，或我们从文本解析
            token = sso_to_token(sso, proxy=proxy)
            if not token:
                print("  FAIL device-flow 换 token 失败")
                fail += 1
                continue
            # 尝试从原始行提取 email（若是 email----pwd----sso 格式，load 后只剩 sso）
            record = token_to_cpa_record(token, email="")
            if auth_dir is not None:
                path = write_cpa_auth(auth_dir, record)
                print(f"  本地写入: {path}")
            name = upload_cpa_auth_remote(remote, key, record)
            print(f"  OK 远程 -> {name}  email={record.get('email') or '-'}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL {exc}")
            fail += 1
        if delay > 0 and idx < len(cookies):
            time.sleep(delay)

    print(f"\n完成: 成功 {ok}, 失败 {fail}")
    return 0 if fail == 0 else 1


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="上传本地/SSO 账号到远程 CPA")
    ap.add_argument(
        "--auth-dir",
        default=str(cfg.get("cpa_auth_dir") or DEFAULT_AUTH_DIR),
        help="本地 CPA auth 目录（默认 ~/.cli-proxy-api 或 config.json）",
    )
    ap.add_argument(
        "--remote",
        default=str(cfg.get("cpa_remote_url") or ""),
        help="远程 CPA 地址，如 https://cpa.example.com",
    )
    ap.add_argument(
        "--key",
        default=str(cfg.get("cpa_management_key") or ""),
        help="远程 CPA 管理密钥",
    )
    ap.add_argument(
        "--sso",
        default="",
        help="可选：从 accounts 文本（email----pwd----sso）重新换 token 再上传",
    )
    ap.add_argument(
        "--proxy",
        default=str(cfg.get("proxy") or ""),
        help="device-flow 代理（仅 --sso 模式需要）",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0,
        help="--sso 模式下每个账号间隔秒数",
    )
    ap.add_argument(
        "--also-write-local",
        action="store_true",
        help="--sso 模式下同时写回本地 auth-dir",
    )
    args = ap.parse_args()

    remote = normalize_remote(args.remote)
    key = str(args.key or "").strip()
    if not remote:
        print("[!] 未配置远程地址。请在 config.json 填 cpa_remote_url，或传 --remote")
        return 2
    if not key:
        print("[!] 未配置管理密钥。请在 config.json 填 cpa_management_key，或传 --key")
        return 2

    auth_dir = Path(args.auth_dir).expanduser()

    if args.sso:
        return upload_from_sso_file(
            sso_file=Path(args.sso).expanduser(),
            remote=remote,
            key=key,
            proxy=str(args.proxy or "").strip(),
            auth_dir=auth_dir if args.also_write_local else None,
            delay=args.delay,
        )

    return upload_local_auth_files(auth_dir=auth_dir, remote=remote, key=key)


if __name__ == "__main__":
    sys.exit(main())
