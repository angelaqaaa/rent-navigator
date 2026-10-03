"""Private test isolation for original SDK construction and in-memory transports."""

import builtins
import io
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest


@dataclass
class SDKBoundary:
    home: Path
    pointer: str
    attempts: list[tuple[str, str]] = field(default_factory=list)
    transports: list[httpx2.AsyncClient] = field(default_factory=list)


def lexical_path(value: object) -> str:
    if type(value) is str or isinstance(value, Path):
        return os.fspath(value)
    raise AssertionError("SDK filesystem access requires a synthetic path")


def synthetic_missing(boundary: SDKBoundary, api: str, value: object) -> Any:
    path = lexical_path(value)
    boundary.attempts.append((api, path))
    if path == boundary.pointer:
        raise FileNotFoundError("Synthetic active profile is absent")
    raise AssertionError("SDK constructor filesystem access is denied")


FILESYSTEM_APIS = (
    (Path, "open"),
    (builtins, "open"),
    (io, "open"),
    (os, "open"),
    (Path, "stat"),
    (Path, "lstat"),
    (os, "stat"),
    (os, "lstat"),
)


@contextmanager
def constructor_filesystem_boundary(
    monkeypatch: pytest.MonkeyPatch,
    boundary: SDKBoundary,
) -> Iterator[None]:
    with monkeypatch.context() as scope:
        for owner, name in FILESYSTEM_APIS:
            api = ("Path" if owner is Path else owner.__name__) + "." + name

            def deny(*args: Any, _api: str = api, **kwargs: Any) -> Any:
                value = args[0] if args else kwargs.get("file", kwargs.get("path"))
                return synthetic_missing(boundary, _api, value)

            scope.setattr(owner, name, deny)
        yield


def install_sdk_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SDKBoundary:
    # Replace the mapping without reading or retaining inherited environment values.
    monkeypatch.setattr(os, "environ", {})
    home = tmp_path / "sdk-home"
    boundary = SDKBoundary(home, os.fspath(home / ".config" / "anthropic" / "active_config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    original_open, original_path_open = builtins.open, Path.open

    def reject_credential_file(api: str, path: object) -> None:
        if not isinstance(path, str | Path):
            return
        name = lexical_path(path)
        if name == boundary.pointer:
            synthetic_missing(boundary, api, path)
        parts = Path(name).parts
        if any(part.startswith(".env") for part in parts) or (
            "anthropic" in parts
            and any(part in parts for part in (".config", "configs", "credentials"))
        ):
            raise AssertionError("SDK controls must not read credential or profile files")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        reject_credential_file("builtins.open", file)
        return original_open(file, *args, **kwargs)

    def guarded_path_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        reject_credential_file("Path.open", path)
        return original_path_open(path, *args, **kwargs)

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SDK controls use only the in-memory transport")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)
    return boundary
