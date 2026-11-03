import argparse
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path

import uvicorn


def lan_addresses() -> list[str]:
    """Private IPv4 address of the default route; a UDP connect sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = ipaddress.ip_address(probe.getsockname()[0])
    except OSError:
        return []
    return [str(address)] if address.is_private and not address.is_loopback else []


def maintenance(operation, arguments):
    """Offline operations have explicit paths and never start an ASGI app."""
    from .backup import BackupError, backup_data, restore_data

    parser = argparse.ArgumentParser(prog=f"rewear {operation}")
    if operation == "backup":
        parser.add_argument("--data-dir", type=Path, required=True, help="已停止服務的資料目錄")
        parser.add_argument("--output", type=Path, required=True, help="尚不存在的備份 ZIP 路徑")
        parser.add_argument("--mode", choices=("local", "hosted"), default="local")
    else:
        parser.add_argument("--archive", type=Path, required=True, help="衣序離線備份 ZIP")
        parser.add_argument("--destination", type=Path, required=True, help="不存在或空白的還原目錄")
    args = parser.parse_args(arguments)
    try:
        if operation == "backup":
            result = backup_data(args.data_dir, args.output, mode=args.mode)
        else:
            result = restore_data(args.archive, args.destination)
    except BackupError as error:
        parser.exit(1, f"{error.message}\n")
    print(
        json.dumps(
            {
                "operation": operation,
                "status": "completed",
                "mode": result["mode"],
                "files": len(result["files"]),
                "bytes": result["total_size"],
            },
            ensure_ascii=False,
        )
    )


def account_command(arguments):
    """Create an operator-managed website account (for example a judge/demo login) and print it once."""
    import secrets

    from .accounts import AccountError, AccountStore
    from .hosted import _private_directory

    parser = argparse.ArgumentParser(prog="rewear account")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="Create a website account; the password is printed once.")
    create.add_argument("--hosted-data-dir", type=Path, required=True, help="Website data directory (same as --hosted-data-dir)")
    create.add_argument("--username", required=True, help="3–80 letters, digits or . _ @ + -")
    create.add_argument("--display-name", default="", help="Shown in the wardrobe (defaults to the username)")
    args = parser.parse_args(arguments)
    password = os.environ.get("REWEAR_ACCOUNT_PASSWORD") or secrets.token_urlsafe(15)
    try:
        directory = _private_directory(args.hosted_data_dir)
        control = _private_directory(directory / "control")
        user = AccountStore(control).register(args.username, password, args.display_name)["user"]
    except AccountError as error:
        parser.exit(1, f"{error.message}\n")
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps({"username": user["username"], "display_name": user["display_name"], "password": password}, ensure_ascii=False))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"backup", "restore"}:
        return maintenance(sys.argv[1], sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "account":
        return account_command(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="衣序 Rewear 衣物整理工作室",
        epilog="離線備份／還原：rewear backup --help 或 rewear restore --help。",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="開發時自動重新載入")
    parser.add_argument("--hosted", action="store_true", help="啟動帳號隔離的網站模式")
    parser.add_argument("--host", default="127.0.0.1", help="監聽位址；非本機位址僅限網站模式")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="Local mode only: also serve this machine's private LAN address so a phone on the same Wi-Fi can open it. No login — trusted networks only.",
    )
    parser.add_argument(
        "--public-origin",
        default=os.environ.get("REWEAR_PUBLIC_ORIGIN"),
        help="網站完整來源，例如 https://wardrobe.example.com",
    )
    parser.add_argument("--hosted-data-dir", type=Path, help="網站帳號與衣櫃資料目錄")
    parser.add_argument("--max-accounts", type=int, default=1000, help="網站最多註冊帳號數（預設 1000）")
    parser.add_argument("--max-active-tenants", type=int, default=16, help="同時載入的衣櫃數（預設 16）")
    parser.add_argument(
        "--max-jobs-per-tenant", type=int, default=200, help="每個帳號保留工作上限（預設 200）"
    )
    parser.add_argument(
        "--max-media-mib-per-tenant", type=int, default=2048, help="每個帳號媒體准入上限 MiB（預設 2048）"
    )
    parser.add_argument(
        "--accounts",
        choices=("open", "managed"),
        default=os.environ.get("REWEAR_ACCOUNTS") or "open",
        help="Website accounts: open self-registration, or managed (no sign-up, password change or self-recovery; "
        "create logins with `rewear account create`)",
    )
    parser.add_argument(
        "--daily-spend-limit-usd",
        type=float,
        default=os.environ.get("REWEAR_DAILY_SPEND_LIMIT_USD") or None,  # argparse applies type=float to it
        help="Website only: per-account cap on estimated model spend over a rolling 24 hours "
        "(or REWEAR_DAILY_SPEND_LIMIT_USD)",
    )
    parser.add_argument(
        "--allow-insecure-local",
        action="store_true",
        help="僅供 loopback HTTP 開發／驗證；正式網站使用 HTTPS",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("連接埠必須介於 1 與 65535。")
    if (
        min(
            args.max_accounts,
            args.max_active_tenants,
            args.max_jobs_per_tenant,
            args.max_media_mib_per_tenant,
        )
        < 1
    ):
        parser.error("帳號、同時載入、保留工作及媒體上限必須是正整數。")
    if not args.hosted:
        if args.host not in {"127.0.0.1", "::1", "localhost"}:
            parser.error("本機模式只可監聽本機；對外服務請明確使用 --hosted。")
        if args.hosted_data_dir or args.allow_insecure_local or args.accounts != "open" or args.daily_spend_limit_usd is not None:
            parser.error("網站設定必須搭配 --hosted。")
        host = args.host
        if args.lan:
            addresses = lan_addresses()
            if not addresses:
                parser.error(
                    "No private LAN address found; connect this machine to Wi-Fi or a local network first."
                )
            os.environ["REWEAR_LAN_HOSTS"] = ",".join(addresses)
            host = "0.0.0.0"
            print(
                f"LAN mode: open http://{addresses[0]}:{args.port} on a phone on the same network. "
                "Local mode has no login, so anyone on this network can view and change this wardrobe.",
                file=sys.stderr,
            )
        uvicorn.run("wardrobe.app:app", host=host, port=args.port, reload=args.reload)
        return
    if args.lan:
        parser.error("--lan is for local mode; website mode uses --public-origin behind HTTPS.")
    if not args.public_origin:
        parser.error("網站模式需要 --public-origin 或 REWEAR_PUBLIC_ORIGIN。")
    if args.reload:
        parser.error("網站模式請以單一程序啟動；修改後重新啟動服務。")
    if args.allow_insecure_local and args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("HTTP 開發模式只可監聽本機。")
    from .config import Settings
    from .hosted import create_hosted_app

    try:
        app = create_hosted_app(
            Settings(),
            origin=args.public_origin,
            hosted_dir=args.hosted_data_dir,
            allow_insecure_local=args.allow_insecure_local,
            max_accounts=args.max_accounts,
            max_active_tenants=args.max_active_tenants,
            max_jobs_per_tenant=args.max_jobs_per_tenant,
            max_media_bytes_per_tenant=args.max_media_mib_per_tenant * 1024**2,
            **({"accounts_mode": args.accounts} if args.accounts != "open" else {}),
            **({"daily_spend_limit_usd": args.daily_spend_limit_usd} if args.daily_spend_limit_usd is not None else {}),
        )
    except ValueError as error:
        parser.error(str(error))
    uvicorn.run(app, host=args.host, port=args.port, forwarded_allow_ips="127.0.0.1,::1")


if __name__ == "__main__":
    main()
