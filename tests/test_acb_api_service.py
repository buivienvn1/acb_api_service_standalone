from __future__ import annotations

import html
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from acb_api_service.bootstrap import prompt_admin_password
from acb_api_service.core import Crypto, Database, Settings, load_env_file, password_hash, utcnow
from acb_api_service.worker import Poller


UTC = timezone.utc


class FakeAdapter:
    calls = 0
    fail = False

    def __init__(self, username, password, account_number, timeout=20):
        self.account_number = account_number

    def fetch_transactions(self, rows=200, days_back=3):
        type(self).calls += 1
        if type(self).fail:
            raise RuntimeError("upstream credential must not leak")
        return [{"transactionNumber": "TX-1", "amount": "12000", "description": "<script>alert(1)</script>", "type": "IN", "activeDatetime": 1_700_000_000_000}]

    def close(self):
        pass


class BootstrapTest(unittest.TestCase):
    @patch("acb_api_service.bootstrap.getpass.getpass", side_effect=["strong-password", "strong-password"])
    def test_interactive_password_confirmation(self, getpass_mock):
        self.assertEqual(prompt_admin_password(), "strong-password")
        self.assertEqual(getpass_mock.call_count, 2)

    @patch("acb_api_service.bootstrap.getpass.getpass", side_effect=["strong-password", "different-password"])
    def test_rejects_mismatched_confirmation(self, _getpass_mock):
        with self.assertRaises(SystemExit):
            prompt_admin_password()

    @patch("acb_api_service.bootstrap.getpass.getpass", side_effect=["too-short", "too-short"])
    def test_rejects_short_password(self, _getpass_mock):
        with self.assertRaises(SystemExit):
            prompt_admin_password()


class EnvironmentLoaderTest(unittest.TestCase):
    def test_load_env_file_preserves_process_values_and_unquotes(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# ignored\nACB_TEST_FROM_FILE='quoted value'\nACB_TEST_PRESERVED=file-value\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ACB_TEST_PRESERVED": "process-value"}, clear=False):
                os.environ.pop("ACB_TEST_FROM_FILE", None)
                load_env_file(env_path)
                self.assertEqual(os.environ["ACB_TEST_FROM_FILE"], "quoted value")
                self.assertEqual(os.environ["ACB_TEST_PRESERVED"], "process-value")
                os.environ.pop("ACB_TEST_FROM_FILE", None)

    def test_poll_interval_defaults_to_20_seconds_and_enforces_15_second_minimum(self):
        required = {
            "ACB_API_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "ACB_API_ADMIN_PASSWORD_HASH": password_hash("correct-password"),
            "ACB_API_SESSION_SECRET": "s" * 64,
        }
        with patch.dict(os.environ, required, clear=True):
            self.assertEqual(Settings.from_env().poll_interval_seconds, 20)
        with patch.dict(os.environ, {**required, "ACB_API_POLL_INTERVAL_SECONDS": "15"}, clear=True):
            self.assertEqual(Settings.from_env().poll_interval_seconds, 15)
        with patch.dict(os.environ, {**required, "ACB_API_POLL_INTERVAL_SECONDS": "5"}, clear=True):
            self.assertEqual(Settings.from_env().poll_interval_seconds, 15)


class ACBAPIServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_path=Path(self.temp.name) / "service.sqlite3",
            encryption_key=Fernet.generate_key().decode(), admin_username="admin",
            admin_password_hash=password_hash("correct-password"), session_secret="s" * 64,
            public_base_url="https://api.example.test", cookie_secure=False, poll_interval_seconds=30,
            poll_concurrency=2, stale_after_seconds=60, api_rate_limit_per_minute=100,
            session_ttl_seconds=3600, acb_timeout_seconds=5,
        )
        self.db = Database(self.settings.database_path)
        self.crypto = Crypto(self.settings.encryption_key, self.settings.session_secret)
        self.customer_ids = [self._customer("Tenant A", "11112222"), self._customer("Tenant B", "33334444")]
        FakeAdapter.calls, FakeAdapter.fail = 0, False

    def tearDown(self):
        self.temp.cleanup()

    def _customer(self, name, account, expires=None):
        now = utcnow()
        return self.db.execute(
            "INSERT INTO customers(name,account_number,credential_ciphertext,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (name, account, self.crypto.encrypt_json({"username": name, "password": "secret"}), expires, now, now),
        )

    def _key(self, customer_id):
        raw = self.crypto.new_api_key()
        self.db.execute("INSERT INTO api_keys(customer_id,key_prefix,key_hash,created_at) VALUES(?,?,?,?)", (customer_id, raw[:16], self.crypto.hash_api_key(raw), utcnow()))
        return raw

    def _feed(self, customer_id):
        raw = self.crypto.new_feed_token()
        self.db.execute(
            "INSERT INTO feed_links(customer_id,token_prefix,token_hash,token_ciphertext,created_at) VALUES(?,?,?,?,?)",
            (customer_id, raw[:18], self.crypto.hash_api_key(raw), self.crypto.encrypt_string(raw), utcnow()),
        )
        return raw

    def _app_client(self):
        with patch.dict(os.environ, {
            "ACB_API_ENCRYPTION_KEY": self.settings.encryption_key,
            "ACB_API_ADMIN_PASSWORD_HASH": self.settings.admin_password_hash,
            "ACB_API_SESSION_SECRET": self.settings.session_secret,
        }):
            from acb_api_service.app import create_app
        return TestClient(create_app(self.settings))

    def _admin(self, client):
        response = client.post("/admin/login", data={"username": "admin", "password": "correct-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        page = client.get("/admin")
        return page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

    def test_additive_migration_and_feed_ciphertext_not_plaintext(self):
        # Re-initializing an existing database is additive and idempotent.
        Database(self.settings.database_path)
        columns = {row["name"] for row in self.db.all("PRAGMA table_info(feed_links)")}
        self.assertTrue({"token_hash", "token_ciphertext", "token_prefix", "created_at", "revoked_at", "last_used_at"} <= columns)
        raw = self._feed(self.customer_ids[0])
        row = self.db.one("SELECT * FROM feed_links WHERE customer_id=?", (self.customer_ids[0],))
        self.assertNotIn(raw, row["token_ciphertext"])
        self.assertNotEqual(raw, row["token_hash"])
        self.assertEqual(Crypto(self.settings.encryption_key, "restart-secret").decrypt_string(row["token_ciphertext"]), raw)

    def test_worker_scheduler_checks_due_customers_every_five_seconds(self):
        poller = Poller(self.settings, self.db, self.crypto, FakeAdapter)
        with patch.object(poller, "run_once") as run_once, \
                patch.object(poller.stop_event, "is_set", side_effect=[False, True]), \
                patch.object(poller.stop_event, "wait") as wait:
            poller.run_forever()
        run_once.assert_called_once_with()
        wait.assert_called_once_with(5)

    def test_dedupe_survives_worker_restart_and_failure_is_redacted(self):
        Poller(self.settings, self.db, self.crypto, FakeAdapter).sync_customer(self.customer_ids[0], force=True)
        Poller(self.settings, self.db, self.crypto, FakeAdapter).sync_customer(self.customer_ids[0], force=True)
        count = self.db.one("SELECT COUNT(*) count FROM transactions WHERE customer_id=?", (self.customer_ids[0],))["count"]
        self.assertEqual(count, 1)
        FakeAdapter.fail = True
        with self.assertRaises(RuntimeError):
            Poller(self.settings, self.db, self.crypto, FakeAdapter).sync_customer(self.customer_ids[0], force=True)
        state = self.db.one("SELECT last_sync_error,failure_count FROM customers WHERE id=?", (self.customer_ids[0],))
        self.assertEqual(state["last_sync_error"], "RuntimeError")
        self.assertEqual(state["failure_count"], 1)

    def test_api_tenant_isolation_rotation_expiry_and_stale(self):
        poller = Poller(self.settings, self.db, self.crypto, FakeAdapter)
        poller.sync_customer(self.customer_ids[0], force=True)
        poller.sync_customer(self.customer_ids[1], force=True)
        key_a, key_b = self._key(self.customer_ids[0]), self._key(self.customer_ids[1])
        client = self._app_client()
        rows = client.get("/api/v1/transactions", headers={"Authorization": f"Bearer {key_a}"}).json()["data"]
        self.assertEqual(len(rows), 1)
        foreign_id = client.get("/api/v1/transactions", headers={"X-API-Key": key_b}).json()["data"][0]["id"]
        self.assertEqual(client.get(f"/api/v1/transactions/{foreign_id}", headers={"X-API-Key": key_a}).status_code, 404)
        self.db.execute("UPDATE customers SET last_sync_ok_at=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=1)).isoformat(), self.customer_ids[0]))
        self.assertTrue(client.get("/api/v1/status", headers={"X-API-Key": key_a}).json()["stale"])
        self.db.execute("UPDATE api_keys SET revoked_at=? WHERE customer_id=?", (utcnow(), self.customer_ids[0]))
        self.assertEqual(client.get("/api/v1/status", headers={"X-API-Key": key_a}).status_code, 401)
        expired_id = self._customer("Expired", "55556666", (datetime.now(UTC)-timedelta(days=1)).isoformat())
        self.assertEqual(client.get("/api/v1/status", headers={"X-API-Key": self._key(expired_id)}).status_code, 403)

    def test_feed_compatible_pagination_tenant_state_and_stale_fallback(self):
        poller = Poller(self.settings, self.db, self.crypto, FakeAdapter)
        poller.sync_customer(self.customer_ids[0], force=True)
        feed_a = self._feed(self.customer_ids[0])
        client = self._app_client()
        response = client.get(f"/api/v1/feed?token={feed_a}&limit=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["transactions"][0]["transactionID"], "TX-1")
        self.assertEqual(payload["transactions"][0]["transactionNumber"], "TX-1")
        self.assertEqual(payload["transactions"][0]["type"], "IN")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertNotIn(feed_a, str(self.db.all("SELECT details FROM audit_logs")))

        # Unknown remains UNKNOWN and pagination is stable.
        now = utcnow()
        self.db.execute("INSERT INTO transactions(customer_id,external_id,amount,description,transaction_type,transacted_at,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (self.customer_ids[0], "TX-2", 2, "x", "UNKNOWN", now, "{}", now))
        first = client.get(f"/api/v1/feed?token={feed_a}&limit=1").json()
        self.assertEqual(first["transactions"][0]["type"], "UNKNOWN")
        self.assertIsNotNone(first["next_cursor"])
        second = client.get(f"/api/v1/feed?token={feed_a}&limit=1&cursor={first['next_cursor']}").json()
        self.assertEqual(second["transactions"][0]["transactionID"], "TX-1")

        self.db.execute("UPDATE customers SET last_sync_ok_at=?,last_sync_error=? WHERE id=?", ((datetime.now(UTC)-timedelta(hours=1)).isoformat(), "RuntimeError", self.customer_ids[0]))
        stale = client.get(f"/api/v1/feed?token={feed_a}").json()
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["incomplete"])
        unsynced = self._feed(self.customer_ids[1])
        self.assertEqual(client.get(f"/api/v1/feed?token={unsynced}").status_code, 503)
        self.db.execute("UPDATE customers SET locked=1 WHERE id=?", (self.customer_ids[0],))
        self.assertEqual(client.get(f"/api/v1/feed?token={feed_a}").status_code, 423)

    def test_transactions_are_newest_first_with_stable_cursor_pagination(self):
        customer_id = self.customer_ids[0]
        now = datetime.now(UTC).replace(microsecond=0)
        self.db.execute(
            "UPDATE customers SET last_sync_at=?,last_sync_ok_at=? WHERE id=?",
            (now.isoformat(), now.isoformat(), customer_id),
        )
        inserted = [
            ("NEWEST", now),
            ("OLDEST", now - timedelta(hours=2)),
            ("TIE-FIRST", now - timedelta(hours=1)),
            ("TIE-SECOND", now - timedelta(hours=1)),
        ]
        ids = {}
        for external_id, transacted_at in inserted:
            ids[external_id] = self.db.execute(
                "INSERT INTO transactions(customer_id,external_id,amount,description,"
                "transaction_type,transacted_at,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (customer_id, external_id, 1, external_id, "IN", transacted_at.isoformat(), "{}", utcnow()),
            )

        feed = self._feed(customer_id)
        api_key = self._key(customer_id)
        client = self._app_client()
        first_feed = client.get(f"/api/v1/feed?token={feed}&limit=2").json()
        second_feed = client.get(
            f"/api/v1/feed?token={feed}&limit=2&cursor={first_feed['next_cursor']}"
        ).json()
        first_ids = [row["transactionID"] for row in first_feed["transactions"]]
        second_ids = [row["transactionID"] for row in second_feed["transactions"]]
        self.assertEqual(first_ids, ["NEWEST", "TIE-SECOND"])
        self.assertEqual(second_ids, ["TIE-FIRST", "OLDEST"])
        self.assertFalse(set(first_ids) & set(second_ids))
        self.assertIsNone(second_feed["next_cursor"])

        api_first = client.get(
            "/api/v1/transactions?limit=2", headers={"X-API-Key": api_key}
        ).json()
        api_second = client.get(
            f"/api/v1/transactions?limit=2&cursor={api_first['next_cursor']}",
            headers={"X-API-Key": api_key},
        ).json()
        self.assertEqual([row["external_id"] for row in api_first["data"]], first_ids)
        self.assertEqual([row["external_id"] for row in api_second["data"]], second_ids)

        foreign_cursor = self.db.execute(
            "INSERT INTO transactions(customer_id,external_id,amount,description,"
            "transaction_type,transacted_at,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.customer_ids[1], "FOREIGN", 1, "foreign", "IN", now.isoformat(), "{}", utcnow()),
        )
        self.assertEqual(
            client.get(f"/api/v1/feed?token={feed}&cursor={foreign_cursor}").status_code,
            400,
        )

        self._admin(client)
        admin_page = client.get(f"/admin/customers/{customer_id}").text
        self.assertLess(admin_page.index("NEWEST"), admin_page.index("TIE-SECOND"))
        self.assertLess(admin_page.index("TIE-SECOND"), admin_page.index("TIE-FIRST"))
        self.assertLess(admin_page.index("TIE-FIRST"), admin_page.index("OLDEST"))
        self.assertGreater(ids["OLDEST"], ids["NEWEST"])

    def test_admin_feed_create_reveal_rotate_revoke_and_api_key_has_no_query_secret(self):
        client = self._app_client()
        csrf = self._admin(client)
        customer_id = self.customer_ids[0]
        created = client.post(f"/admin/customers/{customer_id}/feed", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(created.status_code, 200)
        raw = self.crypto.decrypt_string(self.db.one("SELECT token_ciphertext FROM feed_links WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))["token_ciphertext"])
        self.assertIn(f"https://api.example.test/api/v1/feed?token={raw}", created.text)
        self.assertNotIn("evil.example", created.text)
        masked = client.get(f"/admin/customers/{customer_id}")
        self.assertNotIn(raw, masked.text)
        revealed = client.post(f"/admin/customers/{customer_id}/feed/reveal", data={"csrf_token": csrf})
        self.assertIn(raw, revealed.text)
        rotated = client.post(f"/admin/customers/{customer_id}/feed", data={"csrf_token": csrf})
        new_raw = self.crypto.decrypt_string(self.db.one("SELECT token_ciphertext FROM feed_links WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))["token_ciphertext"])
        self.assertNotEqual(raw, new_raw)
        self.assertEqual(client.get(f"/api/v1/feed?token={raw}").status_code, 401)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM feed_links WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))["n"], 1)
        key_response = client.post(f"/admin/customers/{customer_id}/key", data={"csrf_token": csrf}, follow_redirects=False)
        self.assertEqual(key_response.status_code, 200)
        self.assertNotIn("location", key_response.headers)
        self.assertNotIn("?new_key=", key_response.text)
        self.assertEqual(client.post(f"/admin/customers/{customer_id}/feed/revoke", data={"csrf_token": csrf}, follow_redirects=False).status_code, 303)
        self.assertEqual(client.get(f"/api/v1/feed?token={new_raw}").status_code, 401)
        self.assertNotIn(raw, str(self.db.all("SELECT details FROM audit_logs")))
        self.assertNotIn(new_raw, str(self.db.all("SELECT details FROM audit_logs")))
        self.assertEqual(rotated.headers["cache-control"], "no-store")

    def test_feed_expiry_and_cross_tenant_data_isolation(self):
        Poller(self.settings, self.db, self.crypto, FakeAdapter).sync_customer(self.customer_ids[0], force=True)
        expired_id = self._customer("Expired feed", "77778888", (datetime.now(UTC)-timedelta(days=1)).isoformat())
        expired = self._feed(expired_id)
        self.assertEqual(self._app_client().get(f"/api/v1/feed?token={expired}").status_code, 403)
        feed_b = self._feed(self.customer_ids[1])
        payload = self._app_client().get(f"/api/v1/feed?token={feed_b}")
        self.assertEqual(payload.status_code, 503)
        self.assertNotIn("TX-1", payload.text)

    def test_admin_csrf_xss_login_throttle_and_logout_revokes(self):
        client = self._app_client()
        for _ in range(5):
            client.post("/admin/login", data={"username": "admin", "password": "wrong"})
        self.assertEqual(client.post("/admin/login", data={"username": "admin", "password": "wrong"}).status_code, 429)
        client = self._app_client()
        response = client.post("/admin/login", data={"username": "admin", "password": "correct-password"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        page = client.get("/admin")
        csrf_marker = 'name="csrf_token" value="'
        token = page.text.split(csrf_marker, 1)[1].split('"', 1)[0]
        self.assertEqual(client.post(f"/admin/customers/{self.customer_ids[0]}/key", data={"csrf_token": "bad"}).status_code, 403)
        Poller(self.settings, self.db, self.crypto, FakeAdapter).sync_customer(self.customer_ids[0], force=True)
        detail = client.get(f"/admin/customers/{self.customer_ids[0]}")
        self.assertNotIn("<script>alert(1)</script>", detail.text)
        self.assertIn(html.escape("<script>alert(1)</script>"), detail.text)
        self.assertEqual(client.post("/admin/logout", data={"csrf_token": token}, follow_redirects=False).status_code, 303)
        self.assertEqual(client.get("/admin", follow_redirects=False).status_code, 303)


if __name__ == "__main__":
    unittest.main()
