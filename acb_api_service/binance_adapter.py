"""Synchronous, read-only Binance REST adapter for the unified gateway."""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests


class BinanceAdapter:
    """One isolated read-only Binance API session for one customer."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        timeout: int = 20,
        base_url: str = "https://api.binance.com",
        proxy_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = max(3, timeout)
        self.base_url = base_url.rstrip("/")
        self.proxy_url = proxy_url.strip()
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        if self.proxy_url:
            self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})
        self.lock = threading.Lock()
        self.time_drift_ms = 0
        self.last_success_at = 0.0

    def close(self) -> None:
        self.session.close()

    def _sign(self, params: dict[str, Any]) -> str:
        query = urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, path: str, params: dict[str, Any] | None = None, *, signed: bool = True) -> Any:
        request_params = dict(params or {})
        if signed:
            request_params["timestamp"] = int(time.time() * 1000) + self.time_drift_ms
            request_params["recvWindow"] = 60_000
            request_params["signature"] = self._sign(request_params)
        response = self.session.get(
            f"{self.base_url}{path}",
            params=request_params,
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Binance trả response không hợp lệ HTTP {response.status_code}") from exc
        if response.status_code >= 400 or (isinstance(payload, dict) and int(payload.get("code", 0)) < 0):
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("msg") if isinstance(payload, dict) else str(payload)
            raise RuntimeError(f"Binance HTTP {response.status_code} code={code}: {message}")
        self.last_success_at = time.time()
        return payload

    def sync_time(self) -> None:
        samples: list[int] = []
        for _ in range(3):
            before = int(time.time() * 1000)
            payload = self._request("/api/v3/time", {}, signed=False)
            after = int(time.time() * 1000)
            server_time = int(payload.get("serverTime", 0))
            if server_time:
                samples.append(server_time - ((before + after) // 2))
        if samples:
            samples.sort()
            self.time_drift_ms = samples[len(samples) // 2] - 1000

    def test_connection(self) -> dict[str, Any]:
        """Run a small public-time and signed-account probe with safe diagnostics."""
        started = time.perf_counter()
        self.sync_time()
        public_ms = round((time.perf_counter() - started) * 1000)
        signed_started = time.perf_counter()
        # This read-only endpoint validates key, secret, timestamp and API permissions.
        self._request("/sapi/v1/capital/deposit/hisrec", {"coin": "USDT", "limit": 1})
        return {
            "ok": True,
            "proxy": bool(self.proxy_url),
            "latency_ms": public_ms,
            "signed_latency_ms": round((time.perf_counter() - signed_started) * 1000),
            "time_drift_ms": self.time_drift_ms,
            "message": "Kết nối Binance và API key hợp lệ",
        }

    def fetch_transactions(self, kind: str, *, history_limit: int = 100, history_days: int = 7) -> list[dict[str, Any]]:
        """Fetch and normalize Binance Pay, BSC USDT, or TRX deposit rows."""
        with self.lock:
            if not self.time_drift_ms:
                self.sync_time()
            now = int(time.time() * 1000) + self.time_drift_ms
            start = now - max(1, history_days) * 24 * 60 * 60 * 1000
            if kind == "pay":
                payload = self._request(
                    "/sapi/v1/pay/transactions",
                    {"startTime": start, "endTime": now, "limit": min(1000, max(1, history_limit))},
                )
                rows = payload.get("data", []) if isinstance(payload, dict) else []
                return [self._normalize_pay(row) for row in rows if isinstance(row, dict)]
            if kind in {"bsc", "trx"}:
                coin = "USDT" if kind == "bsc" else "TRX"
                payload = self._request(
                    "/sapi/v1/capital/deposit/hisrec",
                    {"coin": coin, "limit": min(1000, max(1, history_limit))},
                )
                rows = payload if isinstance(payload, list) else []
                return [self._normalize_deposit(row, kind) for row in rows if isinstance(row, dict)]
            raise ValueError(f"Unsupported Binance feed kind: {kind}")

    @staticmethod
    def _normalize_pay(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "transactionNumber": str(row.get("transactionId") or row.get("id") or ""),
            "amount": row.get("amount", 0),
            "description": row.get("note") or row.get("remark") or "",
            "type": "BINANCE_PAY",
            "timestamp": row.get("transactionTime") or row.get("createTime") or 0,
            "currency": row.get("currency") or "USDT",
            "orderId": row.get("orderId") or "",
            "status": row.get("status") or "SUCCESS",
            "raw": row,
        }

    @staticmethod
    def _normalize_deposit(row: dict[str, Any], kind: str) -> dict[str, Any]:
        return {
            "transactionNumber": str(row.get("txId") or row.get("id") or row.get("insertTime") or ""),
            "amount": row.get("amount", 0),
            "description": row.get("addressTag") or "",
            "type": "BINANCE_BSC_DEPOSIT" if kind == "bsc" else "BINANCE_TRX_DEPOSIT",
            "timestamp": row.get("insertTime") or 0,
            "currency": "USDT" if kind == "bsc" else "TRX",
            "network": row.get("network") or ("BSC" if kind == "bsc" else "TRX"),
            "address": row.get("address") or "",
            "status": "SUCCESS" if str(row.get("status")) == "1" else str(row.get("status") or "UNKNOWN"),
            "raw": row,
        }
