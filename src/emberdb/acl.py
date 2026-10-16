"""AUTH and per-user command/key-pattern permissions."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Optional

from .protocol import RespError


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + digest.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = _hash_password(password, salt)
    return hmac.compare_digest(expected, stored)


@dataclass
class User:
    username: str
    password_hash: Optional[str] = None  # None means "no password required"
    enabled: bool = True
    command_patterns: set = field(default_factory=lambda: {"*"})
    key_pattern: str = "*"

    def allows_command(self, command_name: str) -> bool:
        cmd = command_name.upper()
        return any(fnmatch.fnmatchcase(cmd, pat.upper()) for pat in self.command_patterns)

    def allows_key(self, key: str) -> bool:
        return fnmatch.fnmatchcase(key, self.key_pattern)


class ACLManager:
    def __init__(self, require_auth: bool = False):
        self.users: dict[str, User] = {
            "default": User(username="default", password_hash=None),
        }
        self.require_auth = require_auth  # if True, "default" also needs AUTH first

    def set_password(self, username: str, password: str) -> None:
        salt = os.urandom(16)
        user = self.users.setdefault(username, User(username=username))
        user.password_hash = _hash_password(password, salt)

    def clear_password(self, username: str) -> None:
        user = self.users.get(username)
        if user is not None:
            user.password_hash = None

    def create_or_update_user(self, username: str, *, password: Optional[str] = None,
                               enabled: Optional[bool] = None,
                               command_patterns: Optional[set] = None,
                               key_pattern: Optional[str] = None) -> User:
        user = self.users.setdefault(username, User(username=username, password_hash="unset"))
        if password is not None:
            self.set_password(username, password)
        if enabled is not None:
            user.enabled = enabled
        if command_patterns is not None:
            user.command_patterns = command_patterns
        if key_pattern is not None:
            user.key_pattern = key_pattern
        return user

    def delete_user(self, username: str) -> bool:
        if username == "default":
            raise RespError("the 'default' user cannot be removed")
        return self.users.pop(username, None) is not None

    def authenticate(self, username: str, password: str) -> bool:
        user = self.users.get(username)
        if user is None or not user.enabled:
            return False
        if user.password_hash is None:
            return True  # no password configured for this user
        return _verify_password(password, user.password_hash)

    def needs_auth_by_default(self) -> bool:
        default = self.users["default"]
        return self.require_auth or default.password_hash is not None or not default.enabled

    def check(self, username: str, command_name: str, keys: list[str]) -> None:
        user = self.users.get(username)
        if user is None or not user.enabled:
            raise RespError("authentication required", code="NOAUTH")
        if not user.allows_command(command_name):
            raise RespError(
                f"this user has no permissions to run the '{command_name.lower()}' command",
                code="NOPERM",
            )
        for key in keys:
            if not user.allows_key(key):
                raise RespError(
                    f"this user has no permissions to access the '{key}' key",
                    code="NOPERM",
                )

    def describe_user(self, username: str) -> dict:
        u = self.users[username]
        return {
            "username": u.username,
            "enabled": u.enabled,
            "has_password": u.password_hash is not None,
            "commands": sorted(u.command_patterns),
            "keys": u.key_pattern,
        }

    def list_users(self) -> list[dict]:
        return [self.describe_user(name) for name in sorted(self.users)]
