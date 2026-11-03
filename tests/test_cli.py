"""Exercise launch boundaries without starting a listener or opening user data."""

import sys
import json

import pytest

from wardrobe import __main__ as cli


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["rewear", *arguments])
    cli.main()


def test_default_is_local_and_does_not_import_app(monkeypatch):
    calls = []
    monkeypatch.delenv("REWEAR_PUBLIC_ORIGIN", raising=False)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    invoke(monkeypatch)
    assert calls == [(("wardrobe.app:app",), {"host": "127.0.0.1", "port": 8765, "reload": False})]


def test_lan_opt_in_trusts_only_the_private_lan_address(monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("REWEAR_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("REWEAR_LAN_HOSTS", raising=False)
    monkeypatch.setattr(cli, "lan_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    invoke(monkeypatch, "--lan")
    assert calls == [(("wardrobe.app:app",), {"host": "0.0.0.0", "port": 8765, "reload": False})]
    assert cli.os.environ["REWEAR_LAN_HOSTS"] == "192.168.1.20"
    assert "no login" in capsys.readouterr().err

    from wardrobe.app import lan_hosts

    assert lan_hosts() == ["192.168.1.20"]
    monkeypatch.delenv("REWEAR_LAN_HOSTS")
    assert lan_hosts() == []


@pytest.mark.parametrize(
    "arguments", [("--lan",), ("--lan", "--hosted", "--public-origin", "https://wardrobe.example")]
)
def test_lan_requires_local_mode_and_a_private_address(monkeypatch, arguments):
    monkeypatch.delenv("REWEAR_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("REWEAR_LAN_HOSTS", raising=False)
    monkeypatch.setattr(cli, "lan_addresses", lambda: [] if arguments == ("--lan",) else ["192.168.1.20"])
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: pytest.fail("Invalid launch reached the server"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)
    assert error.value.code == 2
    assert "REWEAR_LAN_HOSTS" not in cli.os.environ


@pytest.mark.parametrize(
    "arguments",
    [
        ("--host", "0.0.0.0"),
        ("--host", "192.0.2.1"),
        ("--hosted-data-dir", "/unused"),
        ("--allow-insecure-local",),
        ("--port", "0"),
        ("--port", "65536"),
        ("--max-accounts", "0"),
        ("--max-active-tenants", "-1"),
        ("--max-jobs-per-tenant", "0"),
        ("--max-media-mib-per-tenant", "-20"),
        ("--hosted",),
        ("--hosted", "--public-origin", "https://wardrobe.example", "--reload"),
        (
            "--hosted",
            "--public-origin",
            "http://127.0.0.1:8765",
            "--allow-insecure-local",
            "--host",
            "0.0.0.0",
        ),
    ],
)
def test_unsafe_or_incomplete_launch_never_starts_server(monkeypatch, arguments):
    monkeypatch.delenv("REWEAR_PUBLIC_ORIGIN", raising=False)

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid launch reached the server")

    monkeypatch.setattr(cli.uvicorn, "run", unexpected)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)
    assert error.value.code == 2


def test_hosted_launch_passes_explicit_origin_and_separate_data(monkeypatch, tmp_path):
    import wardrobe.hosted as hosted

    sentinel = object()
    factories, servers = [], []

    def factory(settings, **kwargs):
        factories.append((settings, kwargs))
        return sentinel

    monkeypatch.setattr(hosted, "create_hosted_app", factory)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: servers.append((a, kw)))
    monkeypatch.setenv("REWEAR_PUBLIC_ORIGIN", "https://wardrobe.example")
    destination = tmp_path / "hosted"
    invoke(
        monkeypatch,
        "--hosted",
        "--host",
        "0.0.0.0",
        "--port",
        "8791",
        "--hosted-data-dir",
        str(destination),
        "--max-accounts",
        "50",
        "--max-active-tenants",
        "4",
        "--max-jobs-per-tenant",
        "30",
        "--max-media-mib-per-tenant",
        "512",
    )
    assert factories[0][1] == {
        "origin": "https://wardrobe.example",
        "hosted_dir": destination,
        "allow_insecure_local": False,
        "max_accounts": 50,
        "max_active_tenants": 4,
        "max_jobs_per_tenant": 30,
        "max_media_bytes_per_tenant": 512 * 1024**2,
    }
    assert not destination.exists()
    assert servers == [
        (
            (sentinel,),
            {
                "host": "0.0.0.0",
                "port": 8791,
                "forwarded_allow_ips": "127.0.0.1,::1",
            },
        )
    ]


def test_hosted_configuration_error_has_no_listener(monkeypatch, capsys):
    import wardrobe.hosted as hosted

    def invalid(*args, **kwargs):
        raise ValueError("網站需要 HTTPS 來源。")

    monkeypatch.setattr(hosted, "create_hosted_app", invalid)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: pytest.fail("Invalid origin served"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--hosted", "--public-origin", "http://example.invalid")
    assert error.value.code == 2
    assert "HTTPS" in capsys.readouterr().err


def test_offline_backup_restore_cli_roundtrip_never_starts_server(monkeypatch, tmp_path, capsys):
    from wardrobe.store import Store

    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: pytest.fail("Offline command served"))
    source = tmp_path / "source"
    store = Store(source)
    job = store.create_job(filename="isolated.jpg", status="completed")
    archive = tmp_path / "snapshot.zip"
    restored = tmp_path / "restored"
    invoke(monkeypatch, "backup", "--data-dir", str(source), "--output", str(archive))
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed" and report["mode"] == "local"
    assert report["files"] == 1 and report["bytes"] > 0
    invoke(monkeypatch, "restore", "--archive", str(archive), "--destination", str(restored))
    assert json.loads(capsys.readouterr().out)["operation"] == "restore"
    assert Store(restored).job(job["id"])["filename"] == "isolated.jpg"


def test_restore_cli_refuses_existing_catalogue_with_safe_message(monkeypatch, tmp_path, capsys):
    from wardrobe.backup import backup_data
    from wardrobe.store import Store

    source, target = tmp_path / "source", tmp_path / "target"
    Store(source)
    existing = Store(target)
    job = existing.create_job(filename="keep-me", status="completed")
    archive = tmp_path / "snapshot.zip"
    backup_data(source, archive, mode="local")
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "restore", "--archive", str(archive), "--destination", str(target))
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "Existing data was not overwritten" in message
    assert "Traceback" not in message
    assert existing.job(job["id"])["filename"] == "keep-me"


def test_account_create_prints_a_one_time_password_that_signs_in(monkeypatch, tmp_path, capsys):
    from wardrobe.accounts import AccountStore

    monkeypatch.delenv("REWEAR_ACCOUNT_PASSWORD", raising=False)
    invoke(monkeypatch, "account", "create", "--hosted-data-dir", str(tmp_path / "site"), "--username", "judge", "--display-name", "Judges")
    created = json.loads(capsys.readouterr().out)
    assert created["username"] == "judge" and created["display_name"] == "Judges"
    assert len(created["password"]) >= 20
    store = AccountStore(tmp_path / "site" / "control")
    assert store.authenticate("judge", created["password"])["username"] == "judge"
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "account", "create", "--hosted-data-dir", str(tmp_path / "site"), "--username", "judge")


def test_account_create_can_take_the_password_from_the_environment(monkeypatch, tmp_path, capsys):
    from wardrobe.accounts import AccountStore

    monkeypatch.setenv("REWEAR_ACCOUNT_PASSWORD", "operator-chosen-password-42")
    invoke(monkeypatch, "account", "create", "--hosted-data-dir", str(tmp_path / "site"), "--username", "judge-2")
    assert json.loads(capsys.readouterr().out)["password"] == "operator-chosen-password-42"
    assert AccountStore(tmp_path / "site" / "control").authenticate("judge-2", "operator-chosen-password-42")


def test_hosted_launch_passes_managed_accounts_and_spend_limit(monkeypatch, tmp_path):
    import wardrobe.hosted as hosted

    factories = []
    monkeypatch.setattr(hosted, "create_hosted_app", lambda settings, **kwargs: factories.append(kwargs) or object())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: None)
    monkeypatch.setenv("REWEAR_PUBLIC_ORIGIN", "https://wardrobe.example")
    invoke(monkeypatch, "--hosted", "--hosted-data-dir", str(tmp_path / "site"), "--accounts", "managed", "--daily-spend-limit-usd", "3")
    assert factories[0]["accounts_mode"] == "managed" and factories[0]["daily_spend_limit_usd"] == 3.0


@pytest.mark.parametrize("arguments", [("--accounts", "managed"), ("--daily-spend-limit-usd", "2")])
def test_website_account_options_require_hosted_mode(monkeypatch, arguments):
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: pytest.fail("server started"))
    with pytest.raises(SystemExit):
        invoke(monkeypatch, *arguments)


def test_deployment_environment_sets_managed_accounts_and_spend_limit(monkeypatch, tmp_path):
    import wardrobe.hosted as hosted

    factories = []
    monkeypatch.setattr(hosted, "create_hosted_app", lambda settings, **kwargs: factories.append(kwargs) or object())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: None)
    monkeypatch.setenv("REWEAR_PUBLIC_ORIGIN", "https://wardrobe.example")
    monkeypatch.setenv("REWEAR_ACCOUNTS", "managed")
    monkeypatch.setenv("REWEAR_DAILY_SPEND_LIMIT_USD", "4.5")
    invoke(monkeypatch, "--hosted", "--hosted-data-dir", str(tmp_path / "site"))
    assert factories[0]["accounts_mode"] == "managed" and factories[0]["daily_spend_limit_usd"] == 4.5


@pytest.mark.parametrize("variable, value", [("REWEAR_ACCOUNTS", "invite"), ("REWEAR_DAILY_SPEND_LIMIT_USD", "lots")])
def test_invalid_deployment_environment_is_reported_without_a_listener(monkeypatch, tmp_path, variable, value):
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: pytest.fail("server started"))
    monkeypatch.setenv("REWEAR_PUBLIC_ORIGIN", "https://wardrobe.example")
    monkeypatch.setenv(variable, value)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--hosted", "--hosted-data-dir", str(tmp_path / "site"))
