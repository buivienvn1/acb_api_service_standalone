"""Isolated, synchronous adapter for ACB's unofficial mobile API."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


LOGGER = logging.getLogger("acb_api.adapter")
VN_TZ = timezone(timedelta(hours=7))


class ACBError(RuntimeError):
    pass


class ACBAdapter:
    """One adapter owns one HTTP session and serializes calls for one connection."""

    BASE_URL = "https://apiapp.acb.com.vn"

    def __init__(self, username: str, password: str, account_number: str, timeout: int = 20):
        if not username or not password or not account_number:
            raise ValueError("ACB username, password và account number là bắt buộc")
        self.username = username
        self.password = password
        self.account_number = account_number
        self.timeout = timeout
        self.device_id = str(uuid.uuid4()).upper()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "apikey": "CQk6S5usauGmMgMYLGqCuDtgtqIM8FI1",
            "User-Agent": "ACB-MBA/8 CFNetwork/1335.0.3 Darwin/21.6.0",
            "x-app-version": "3.26.0",
        })

    def _headers(self, authenticated: bool = False) -> dict[str, str]:
        headers = {"x-request-id": str(uuid.uuid4())}
        if authenticated and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _login(self) -> None:
        try:
            response = self.session.post(
                f"{self.BASE_URL}/mb/v2/auth/tokens",
                json={
                    "username": self.username,
                    "password": self.password,
                    "deviceId": self.device_id,
                    "clientId": "iuSuHYVufIUuNIREV0FB9EoLn9kHsDbm",
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            self.access_token = payload.get("accessToken")
            self.refresh_token = payload.get("refreshToken")
            if not self.access_token:
                raise ACBError("ACB login không trả access token")
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            LOGGER.warning("ACB login failed status=%s", status)
            raise ACBError("Không thể đăng nhập ACB") from exc

    def fetch_transactions(self, rows: int = 200, days_back: int = 3) -> list[dict[str, Any]]:
        with self.lock:
            self._login()
            now = datetime.now(VN_TZ)
            start = (now - timedelta(days=max(1, days_back))).replace(hour=0, minute=0, second=0, microsecond=0)
            end = (now + timedelta(days=5)).replace(hour=23, minute=59, second=59, microsecond=999000)
            url = f"{self.BASE_URL}/mb/legacy/ss/cs/bankservice/saving/{self.account_number}/tx-history"
            params = {
                "account": self.account_number,
                "transactionType": "ALL",
                "from": int(start.timestamp() * 1000),
                "to": int(end.timestamp() * 1000),
                "min": 0,
                "max": 9007199254740991,
                "page": 1,
                "size": min(max(rows, 1), 500),
            }
            try:
                response = self.session.get(
                    url, params=params, headers=self._headers(authenticated=True), timeout=self.timeout
                )
                response.raise_for_status()
                payload = response.json().get("data", [])
            except (requests.RequestException, ValueError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                LOGGER.warning("ACB history failed account_suffix=%s status=%s", self.account_number[-4:], status)
                raise ACBError("Không thể đọc lịch sử ACB") from exc
            if isinstance(payload, dict):
                payload = payload.get("transactions", [])
            if not isinstance(payload, list):
                raise ACBError("ACB trả dữ liệu lịch sử không hợp lệ")
            return [item for item in payload if isinstance(item, dict)]

    def close(self) -> None:
        self.session.close()
