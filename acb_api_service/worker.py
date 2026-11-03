"""Independent polling worker for the standalone ACB API service."""
from __future__ import annotations

import hashlib
import json
import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from acb_api_service.acb_adapter import ACBAdapter
from acb_api_service.core import Crypto, Database, Settings, utcnow


LOGGER = logging.getLogger("acb_api.worker")
UTC = timezone.utc


def normalize_transaction(item: dict[str, Any]) -> tuple[str, int, str, str, str | None, str]:
    external_id = str(item.get("transactionNumber") or item.get("transactionID") or "").strip()
    timestamp = item.get("activeDatetime") or item.get("postingDate") or item.get("effectiveDate") or item.get("timestamp")
    amount_raw = item.get("amount") or 0
    try:
        amount = int(float(amount_raw))
    except (TypeError, ValueError):
        amount = 0
    description = str(item.get("description") or "")[:2000]
    tx_type = str(item.get("type") or "UNKNOWN").upper()[:32]
    transacted_at = None
    if timestamp is not None:
        try:
            numeric = float(timestamp)
            if numeric > 10_000_000_000:
                numeric /= 1000
            transacted_at = datetime.fromtimestamp(numeric, UTC).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            transacted_at = str(timestamp)[:64]
    if not external_id:
        stable = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        external_id = "derived_" + hashlib.sha256(stable.encode()).hexdigest()
    raw_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return external_id, amount, description, tx_type, transacted_at, raw_json


class Poller:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        crypto: Crypto,
        adapter_factory: Callable[..., ACBAdapter] = ACBAdapter,
    ):
        self.settings = settings
        self.database = database
        self.crypto = crypto
        self.adapter_factory = adapter_factory
        self.stop_event = threading.Event()
        self._customer_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, customer_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._customer_locks.setdefault(customer_id, threading.Lock())

    def sync_customer(self, customer_id: int, *, force: bool = False) -> int:
        lock = self._lock_for(customer_id)
        if not lock.acquire(blocking=False):
            return 0
        try:
            customer = self.database.one("SELECT * FROM customers WHERE id=?", (customer_id,))
            if not customer or customer["locked"]:
                return 0
            now = utcnow()
            if customer["expires_at"] and customer["expires_at"] <= now:
                return 0
            credentials = self.crypto.decrypt_json(customer["credential_ciphertext"])
            adapter = self.adapter_factory(
                credentials.get("username", ""), credentials.get("password", ""),
                customer["account_number"], timeout=self.settings.acb_timeout_seconds,
            )
            try:
                rows = adapter.fetch_transactions(rows=300, days_back=7 if customer["last_sync_ok_at"] is None else 3)
            finally:
                adapter.close()
            inserted = 0
            with self.database.transaction() as connection:
                for item in rows:
                    values = normalize_transaction(item)
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO transactions(customer_id,external_id,amount,description,transaction_type,transacted_at,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (customer_id, *values, now),
                    )
                    inserted += cursor.rowcount
                connection.execute(
                    "UPDATE customers SET last_sync_at=?,last_sync_ok_at=?,last_sync_error=NULL,failure_count=0,next_sync_at=?,updated_at=? WHERE id=?",
                    (now, now, (datetime.now(UTC) + timedelta(seconds=self.settings.poll_interval_seconds)).isoformat(timespec="seconds"), now, customer_id),
                )
            LOGGER.info("sync ok customer_id=%s fetched=%s inserted=%s", customer_id, len(rows), inserted)
            return inserted
        except Exception as exc:
            customer = self.database.one("SELECT failure_count FROM customers WHERE id=?", (customer_id,))
            failures = int(customer["failure_count"] if customer else 0) + 1
            delay = min(3600, self.settings.poll_interval_seconds * (2 ** min(failures, 5)))
            next_sync = datetime.now(UTC) + timedelta(seconds=delay + random.randint(0, 20))
            self.database.execute(
                "UPDATE customers SET last_sync_at=?,last_sync_error=?,failure_count=?,next_sync_at=?,updated_at=? WHERE id=?",
                (utcnow(), type(exc).__name__, failures, next_sync.isoformat(timespec="seconds"), utcnow(), customer_id),
            )
            LOGGER.warning("sync failed customer_id=%s error=%s", customer_id, type(exc).__name__)
            if force:
                raise
            return 0
        finally:
            lock.release()

    def run_once(self) -> None:
        now = utcnow()
        customers = self.database.all(
            "SELECT id FROM customers WHERE locked=0 AND (expires_at IS NULL OR expires_at>?) AND (next_sync_at IS NULL OR next_sync_at<=?)",
            (now, now),
        )
        with ThreadPoolExecutor(max_workers=self.settings.poll_concurrency) as executor:
            futures = [executor.submit(self.sync_customer, int(customer["id"])) for customer in customers]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    LOGGER.exception("Unexpected polling failure")

    def run_forever(self) -> None:
        # Check due customers frequently without calling ACB before next_sync_at.
        # A short scheduler tick prevents a 30-second customer interval from being
        # skipped and delayed by another full polling cycle.
        scheduler_tick_seconds = min(5, self.settings.poll_interval_seconds)
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(scheduler_tick_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    poller = Poller(settings, Database(settings.database_path), Crypto(settings.encryption_key, settings.session_secret))
    signal.signal(signal.SIGTERM, lambda *_: poller.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: poller.stop_event.set())
    poller.run_forever()


if __name__ == "__main__":
    main()
