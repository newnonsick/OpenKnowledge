from __future__ import annotations

import pytest

from src.gateway.application.parsers.code_parser import CodeParser
from src.gateway.application.parsers.json_parser import JSONParser
from src.gateway.application.parsers.text_parser import TextParser
from src.gateway.application.services.git_connector_service import _is_connector_secret_path
from src.gateway.domain.exceptions import ValidationException


def test_connector_secret_paths_are_skipped() -> None:
    skipped = [
        ".env",
        "config/.env",
        "a/.env.local",
        "deploy.pem",
        "server.key",
        "bundle.p12",
        "store.pfx",
        "vault.jks",
        "secrets.kdbx",
        "id_rsa",
        "keys/id_ed25519",
        "id_dsa",
        "id_ecdsa",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        "token.json",
        "credentials.json",
        "credentials.yaml",
        "id_rsa.txt",
        "key.pem.txt",
        "prod.env",
        "production.env",
        "my.env",
        "service-account.json",
        "client_secret.json",
        "google-credentials.json",
        "gcp-key.json",
        "firebase-adminsdk-abc12.json",
        "prod.env.txt",
        "secrets.yaml.txt",
        "server_cert.pem",
    ]
    for path in skipped:
        assert _is_connector_secret_path(path) is True, path


def test_connector_benign_paths_are_kept() -> None:
    kept = [
        "notes.txt",
        "environment.md",
        "myenv.json",
        ".envx",
        "README.md",
        "src/app.py",
        "key.txt",
        "config.json",
        "tokenizer.py",
        "credentialism.md",
        "id_rsa.pub",
        ".env.sample",
        ".env.example",
        ".env.template",
    ]
    for path in kept:
        assert _is_connector_secret_path(path) is False, path


def test_text_and_code_parsers_reject_null_bytes() -> None:
    with pytest.raises(ValidationException):
        TextParser().parse(b"hello\x00world", filename="notes.txt")
    with pytest.raises(ValidationException):
        CodeParser().parse(b"x = 1\x00\n", filename="app.py")
    assert TextParser().parse(b"clean text", filename="notes.txt") == "clean text"


def test_json_parser_rejects_null_bytes() -> None:
    with pytest.raises(ValidationException):
        JSONParser().parse(b'{"key": "a\x00b"}', filename="data.json")
