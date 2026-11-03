"""Core configuration, persistence, and security helpers for the standalone ACB API service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken


UTC = timezone.utc


def load_env_file(path: Path | None = None) -> None:
    """Load repository .env without importing the bot's mandatory configuration."""
    env_path = path or (Path(__file__).resolve().parent.parent / ".env")
    if not env_path.is_file():
        return
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    encryption_key: str
    admin_username: str
    admin_password_hash: str
    session_secret: str
    public_base_url: str
    cookie_secure: bool
    poll_interval_seconds: int
    poll_concurrency: int
    stale_after_seconds: int
    api_rate_limit_per_minute: int
    session_ttl_seconds: int
    acb_timeout_seconds: int
    binance_base_url: str = "https://api.binance.com"
    binance_timeout_seconds: int = 20
    binance_history_limit: int = 100
    binance_history_days: int = 7

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        required = {
            "ACB_API_ENCRYPTION_KEY": os.getenv("ACB_API_ENCRYPTION_KEY", ""),
            "ACB_API_ADMIN_PASSWORD_HASH": os.getenv("ACB_API_ADMIN_PASSWORD_HASH", ""),
            "ACB_API_SESSION_SECRET": os.getenv("ACB_API_SESSION_SECRET", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Thiếu biến môi trường bắt buộc: " + ", ".join(missing))
        return cls(
            database_path=Path(os.getenv("ACB_API_DATABASE_PATH", "data/acb_api/acb_api.sqlite3")),
            encryption_key=required["ACB_API_ENCRYPTION_KEY"],
            admin_username=os.getenv("ACB_API_ADMIN_USERNAME", "admin"),
            admin_password_hash=required["ACB_API_ADMIN_PASSWORD_HASH"],
            session_secret=required["ACB_API_SESSION_SECRET"],
            public_base_url=os.getenv("ACB_API_PUBLIC_BASE_URL", "").rstrip("/"),
            cookie_secure=os.getenv("ACB_API_COOKIE_SECURE", "true").lower() == "true",
            poll_interval_seconds=max(15, int(os.getenv("ACB_API_POLL_INTERVAL_SECONDS", "20"))),
            poll_concurrency=max(1, min(10, int(os.getenv("ACB_API_POLL_CONCURRENCY", "3")))),
            stale_after_seconds=max(60, int(os.getenv("ACB_API_STALE_AFTER_SECONDS", "600"))),
            api_rate_limit_per_minute=max(1, int(os.getenv("ACB_API_RATE_LIMIT_PER_MINUTE", "60"))),
            session_ttl_seconds=max(300, int(os.getenv("ACB_API_SESSION_TTL_SECONDS", "28800"))),
            acb_timeout_seconds=max(5, int(os.getenv("ACB_API_ACB_TIMEOUT_SECONDS", "20"))),
            binance_base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
            binance_timeout_seconds=max(5, int(os.getenv("BINANCE_REQUEST_TIMEOUT_SECONDS", "20"))),
            binance_history_limit=max(1, min(1000, int(os.getenv("BINANCE_HISTORY_LIMIT", "100")))),
            binance_history_days=max(1, min(30, int(os.getenv("BINANCE_HISTORY_DAYS", "7")))),
        )


class Crypto:
    def __init__(self, encryption_key: str, session_secret: str):
        try:
            self.fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("ACB_API_ENCRYPTION_KEY không phải Fernet key hợp lệ") from exc
        self.session_secret = session_secret.encode("utf-8")

    def encrypt_string(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_string(self, token: str) -> str:
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise RuntimeError("Không thể giải mã secret") from exc

    def encrypt_json(self, value: dict[str, str]) -> str:
        return self.encrypt_string(json.dumps(value, separators=(",", ":")))

    def decrypt_json(self, token: str) -> dict[str, str]:
        try:
            data = json.loads(self.decrypt_string(token))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Không thể giải mã credential") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Credential đã mã hóa không hợp lệ")
        return {str(k): str(v) for k, v in data.items()}

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def new_api_key() -> str:
        return "acb_live_" + secrets.token_urlsafe(32)

    @staticmethod
    def new_feed_token() -> str:
        return "acb_feed_" + secrets.token_urlsafe(48)

    def sign(self, value: str) -> str:
        return hmac.new(self.session_secret, value.encode(), hashlib.sha256).hexdigest()

    def session_cookie(self, session_id: str) -> str:
        return f"{session_id}.{self.sign(session_id)}"

    def verify_session_cookie(self, cookie: str) -> str | None:
        session_id, separator, signature = cookie.partition(".")
        if not separator or not session_id or not hmac.compare_digest(signature, self.sign(session_id)):
            return None
        return session_id


def password_hash(password: str, *, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        pad = lambda value: value + "=" * (-len(value) % 4)
        salt = base64.urlsafe_b64decode(pad(salt_raw))
        expected = base64.urlsafe_b64decode(pad(digest_raw))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations_raw))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, account_number TEXT NOT NULL,
 credential_ciphertext TEXT NOT NULL, expires_at TEXT, locked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_sync_at TEXT, last_sync_ok_at TEXT,
 last_sync_error TEXT, next_sync_at TEXT, failure_count INTEGER NOT NULL DEFAULT 0,
 provider TEXT NOT NULL DEFAULT 'acb', channel_type TEXT DEFAULT NULL,
 UNIQUE(account_number)
);
CREATE TABLE IF NOT EXISTS api_keys (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
 key_prefix TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 revoked_at TEXT, last_used_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_key_per_customer ON api_keys(customer_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS feed_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
 token_prefix TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, token_ciphertext TEXT NOT NULL,
 created_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_feed_per_customer ON feed_links(customer_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS transactions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
 external_id TEXT NOT NULL, amount REAL NOT NULL, description TEXT NOT NULL DEFAULT '',
 transaction_type TEXT NOT NULL DEFAULT 'UNKNOWN', transacted_at TEXT, raw_json TEXT NOT NULL,
 created_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'VND', extra_meta TEXT NOT NULL DEFAULT '{}',
 UNIQUE(customer_id, external_id)
);
CREATE INDEX IF NOT EXISTS transactions_customer_cursor ON transactions(customer_id, id DESC);
CREATE TABLE IF NOT EXISTS admin_sessions (
 id TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, action TEXT NOT NULL,
 customer_id INTEGER, remote_ip TEXT, details TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._init_lock:
            connection = self.connect()
            try:
                connection.executescript(SCHEMA)
                self._additive_migrations(connection)
            finally:
                connection.close()

    @staticmethod
    def _additive_migrations(connection: sqlite3.Connection) -> None:
        migrations = {
            "customers": {
                "provider": "TEXT NOT NULL DEFAULT 'acb'",
                "channel_type": "TEXT DEFAULT NULL",
            },
            "transactions": {
                "currency": "TEXT NOT NULL DEFAULT 'VND'",
                "extra_meta": "TEXT NOT NULL DEFAULT '{}'",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(query, params).fetchone()
        finally:
            connection.close()

    def all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(query, params).fetchall()
        finally:
            connection.close()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(query, params)
            return int(cursor.lastrowid or 0)

    def audit(self, action: str, customer_id: int | None, remote_ip: str, details: dict[str, Any] | None = None) -> None:
        safe = details or {}
        blocked_fragments = {"password", "credential", "api_key", "token", "secret", "link", "url"}
        safe = {
            key: value for key, value in safe.items()
            if not any(fragment in key.lower() for fragment in blocked_fragments)
        }
        self.execute(
            "INSERT INTO audit_logs(created_at,action,customer_id,remote_ip,details) VALUES(?,?,?,?,?)",
            (utcnow(), action, customer_id, remote_ip[:64], json.dumps(safe, ensure_ascii=False)),
        )


class SlidingWindowLimiter:
    """Process-local limiter; suitable for the single-process MVP."""
    def __init__(self):
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        with self._lock:
            events = [event for event in self._events.get(key, []) if event > now - window_seconds]
            if len(events) >= limit:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True
