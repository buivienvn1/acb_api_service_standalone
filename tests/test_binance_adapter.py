from __future__ import annotations

import hashlib
import hmac
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from acb_api_service.binance_adapter import BinanceAdapter


class BinanceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = BinanceAdapter("key", "secret")

    def tearDown(self) -> None:
        self.adapter.close()

    def test_signature_matches_binance_hmac_sha256(self) -> None:
        params = {"coin": "USDT", "limit": 100, "timestamp": 123}
        expected = hmac.new(b"secret", urlencode(params).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.adapter._sign(params), expected)

    def test_pay_normalization(self) -> None:
        row = {"transactionId": "tx-1", "orderId": "order-1", "amount": "10.5", "currency": "USDT", "note": "VOC42"}
        normalized = self.adapter._normalize_pay(row)
        self.assertEqual(normalized["transactionNumber"], "tx-1")
        self.assertEqual(normalized["description"], "VOC42")
        self.assertEqual(normalized["type"], "BINANCE_PAY")
        self.assertEqual(normalized["raw"], row)

    def test_deposit_normalization(self) -> None:
        row = {"txId": "tx-2", "amount": "25", "insertTime": 1000, "network": "BSC", "status": 1}
        normalized = self.adapter._normalize_deposit(row, "bsc")
        self.assertEqual(normalized["transactionNumber"], "tx-2")
        self.assertEqual(normalized["currency"], "USDT")
        self.assertEqual(normalized["type"], "BINANCE_BSC_DEPOSIT")
        self.assertEqual(normalized["status"], "SUCCESS")

    @patch("acb_api_service.binance_adapter.time.time", return_value=100.0)
    def test_signed_request_adds_timestamp_signature_and_api_key(self, _time: Mock) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"serverTime": 100000}
        self.adapter.session.get = Mock(return_value=response)
        payload = self.adapter._request("/test", {"coin": "USDT"})
        self.assertEqual(payload, {"serverTime": 100000})
        request = self.adapter.session.get.call_args
        params = request.kwargs["params"]
        self.assertEqual(params["timestamp"], 100000)
        self.assertIn("signature", params)
        self.assertEqual(request.kwargs["timeout"], 20)
        self.assertEqual(self.adapter.session.headers["X-MBX-APIKEY"], "key")


if __name__ == "__main__":
    unittest.main()
