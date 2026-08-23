from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.services.bootstrap_service import (
    BootstrapAlreadyCompleted,
    BootstrapValidationError,
)


class FakeFactory:
    @asynccontextmanager
    async def begin(self):
        yield object()


class FakeBootstrapService:
    calls = []

    def __init__(self, session, password_service):
        self.session = session

    async def create_first_super_admin(self, **values):
        self.calls.append(("bootstrap", values))
        return SimpleNamespace(
            member_id=UUID("00000000-0000-0000-0000-000000000001"),
            temporary_password=SecretValue("one-time-secret"),
            expires_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )

    async def recover_super_admin(self, **values):
        self.calls.append(("recover", values))
        return await self.create_first_super_admin(**values)


def test_cli_never_accepts_password_arguments() -> None:
    from src.gateway import cli

    with pytest.raises(SystemExit):
        cli.main(["bootstrap-super-admin", "--username", "admin", "--password", "unsafe"])


def test_cli_refuses_secret_reveal_to_noninteractive_output(monkeypatch, capsys) -> None:
    from src.gateway import cli

    FakeBootstrapService.calls.clear()
    monkeypatch.setattr(cli, "BootstrapService", FakeBootstrapService)
    monkeypatch.setattr(cli, "get_migration_session_factory", lambda: FakeFactory())
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    result = cli.main(
        ["bootstrap-super-admin", "--username", "admin", "--display-name", "Admin"]
    )
    output = capsys.readouterr().out
    assert result == 2
    assert "one-time-secret" not in output
    assert FakeBootstrapService.calls == []


def test_cli_explicit_secret_output_reveals_once(monkeypatch, capsys) -> None:
    from src.gateway import cli

    FakeBootstrapService.calls.clear()
    monkeypatch.setattr(cli, "BootstrapService", FakeBootstrapService)
    monkeypatch.setattr(cli, "get_migration_session_factory", lambda: FakeFactory())
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    result = cli.main(
        [
            "bootstrap-super-admin",
            "--username",
            "admin",
            "--display-name",
            "Admin",
            "--allow-secret-output",
        ]
    )
    output = capsys.readouterr().out
    assert result == 0
    assert output.count("one-time-secret") == 1
    assert "postgresql" not in output.casefold()
    assert FakeBootstrapService.calls[0][0] == "bootstrap"


@pytest.mark.parametrize(
    ("command", "exception"),
    [
        (
            "bootstrap-super-admin",
            BootstrapAlreadyCompleted("A Super Admin already exists"),
        ),
        ("recover-super-admin", BootstrapValidationError("Eligible Super Admin not found")),
    ],
)
def test_cli_reports_safe_domain_failures(monkeypatch, capsys, command, exception) -> None:
    from src.gateway import cli

    class FailingBootstrapService:
        def __init__(self, session, password_service):
            self.session = session

        async def fail(self, **values):
            raise exception

    setattr(
        FailingBootstrapService,
        "create_first_super_admin",
        FailingBootstrapService.fail,
    )
    setattr(FailingBootstrapService, "recover_super_admin", FailingBootstrapService.fail)
    monkeypatch.setattr(cli, "BootstrapService", FailingBootstrapService)
    monkeypatch.setattr(cli, "get_migration_session_factory", lambda: FakeFactory())
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)

    arguments = [command, "--username", "admin"]
    if command == "bootstrap-super-admin":
        arguments.extend(["--display-name", "Admin"])
    result = cli.main([*arguments, "--allow-secret-output"])
    output = capsys.readouterr().out

    assert result == 1
    assert str(exception) in output
    assert f"Database command failed ({type(exception).__name__})." not in output


@pytest.mark.parametrize("exception", [ValueError("internal database endpoint db-host-01"), RuntimeError("internal database endpoint db-host-01")])
def test_cli_failure_does_not_expose_unexpected_details(monkeypatch, capsys, exception) -> None:
    from src.gateway import cli

    class UnexpectedBootstrapService:
        secret = "one-time-secret"

        def __init__(self, session, password_service):
            self.session = session

        async def fail(self, **values):
            raise exception

    setattr(
        UnexpectedBootstrapService,
        "create_first_super_admin",
        UnexpectedBootstrapService.fail,
    )
    setattr(UnexpectedBootstrapService, "recover_super_admin", UnexpectedBootstrapService.fail)
    monkeypatch.setattr(cli, "BootstrapService", UnexpectedBootstrapService)
    monkeypatch.setattr(cli, "get_migration_session_factory", lambda: FakeFactory())
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)

    result = cli.main(
        [
            "bootstrap-super-admin",
            "--username",
            "admin",
            "--display-name",
            "Admin",
            "--allow-secret-output",
        ]
    )
    output = capsys.readouterr().out

    assert result == 1
    assert output == f"Database command failed ({type(exception).__name__}).\n"
