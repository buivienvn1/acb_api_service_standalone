"""FastAPI admin UI and tenant-isolated read-only customer API."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from acb_api_service.core import Crypto, Database, Settings, SlidingWindowLimiter, utcnow, verify_password
from acb_api_service.worker import Poller


UTC = timezone.utc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    crypto = Crypto(settings.encryption_key, settings.session_secret)
    limiter = SlidingWindowLimiter()
    poller = Poller(settings, database, crypto)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app = FastAPI(title="ACB Read-only API", version="1.0.0", docs_url="/api/docs", redoc_url=None)
    app.state.settings, app.state.database, app.state.crypto = settings, database, crypto
    app.state.poller = poller

    secret_headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    @app.middleware("http")
    async def protect_sensitive_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin") or request.url.path == "/api/v1/feed":
            response.headers.update(secret_headers)
        return response

    def remote_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def admin_session(request: Request) -> dict[str, Any]:
        signed = request.cookies.get("acb_admin_session", "")
        session_id = crypto.verify_session_cookie(signed)
        if not session_id:
            raise HTTPException(303, headers={"Location": "/admin/login"})
        row = database.one(
            "SELECT * FROM admin_sessions WHERE id=? AND revoked_at IS NULL AND expires_at>?", (session_id, int(time.time()))
        )
        if not row:
            raise HTTPException(303, headers={"Location": "/admin/login"})
        return dict(row)

    def csrf(request: Request, session: dict[str, Any], token: str) -> None:
        if not token or not secrets.compare_digest(token, session["csrf_token"]):
            database.audit("admin.csrf_rejected", None, remote_ip(request))
            raise HTTPException(403, "CSRF token không hợp lệ")

    def authenticate_api(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        raw_key = x_api_key or bearer
        if not raw_key or (x_api_key and bearer and x_api_key != bearer):
            raise HTTPException(401, "Dùng Authorization: Bearer <key> hoặc X-API-Key")
        key_hash = crypto.hash_api_key(raw_key)
        row = database.one(
            "SELECT c.*,k.id key_id FROM api_keys k JOIN customers c ON c.id=k.customer_id WHERE k.key_hash=? AND k.revoked_at IS NULL",
            (key_hash,),
        )
        now = utcnow()
        if not row:
            raise HTTPException(401, "API key không hợp lệ")
        if row["locked"]:
            raise HTTPException(423, "Tài khoản dịch vụ đang bị khóa")
        if row["expires_at"] and row["expires_at"] <= now:
            raise HTTPException(403, "Gói dịch vụ đã hết hạn")
        if not limiter.allow(f"customer:{row['id']}", settings.api_rate_limit_per_minute):
            raise HTTPException(429, "Vượt giới hạn request", headers={"Retry-After": "60"})
        database.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, row["key_id"]))
        return dict(row)

    def authenticate_feed(request: Request, token: str = Query(..., min_length=32)) -> dict[str, Any]:
        row = database.one(
            "SELECT c.*,f.id feed_id FROM feed_links f JOIN customers c ON c.id=f.customer_id "
            "WHERE f.token_hash=? AND f.revoked_at IS NULL",
            (crypto.hash_api_key(token),),
        )
        now = utcnow()
        if not row:
            raise HTTPException(401, "Feed token không hợp lệ")
        if row["locked"]:
            raise HTTPException(423, "Tài khoản dịch vụ đang bị khóa")
        if row["expires_at"] and row["expires_at"] <= now:
            raise HTTPException(403, "Gói dịch vụ đã hết hạn")
        # Shared with header-authenticated API calls; changing IP or credential does not bypass it.
        if not limiter.allow(f"customer:{row['id']}", settings.api_rate_limit_per_minute):
            raise HTTPException(429, "Vượt giới hạn request", headers={"Retry-After": "60"})
        database.execute("UPDATE feed_links SET last_used_at=? WHERE id=?", (now, row["feed_id"]))
        return dict(row)

    def render_customer(request: Request, customer_id: int, session: dict[str, Any], **secrets_to_show: str):
        customer = database.one("SELECT * FROM customers WHERE id=?", (customer_id,))
        if not customer:
            raise HTTPException(404)
        transactions = []
        for row in database.all(
            "SELECT * FROM transactions WHERE customer_id=? "
            "ORDER BY COALESCE(transacted_at,created_at) DESC,id DESC LIMIT 100",
            (customer_id,),
        ):
            transaction = dict(row)
            try:
                metadata = json.loads(transaction.get("extra_meta") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                transaction.update({
                    "network": metadata.get("network", ""),
                    "address": metadata.get("address", ""),
                })
            else:
                transaction.update({"network": "", "address": ""})
            transactions.append(transaction)
        audits = [dict(row) for row in database.all("SELECT * FROM audit_logs WHERE customer_id=? ORDER BY id DESC LIMIT 50", (customer_id,))]
        active_key = database.one("SELECT key_prefix,created_at,last_used_at FROM api_keys WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))
        active_feed = database.one("SELECT token_prefix,created_at,last_used_at FROM feed_links WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))
        stale = not customer["last_sync_ok_at"] or (datetime.fromisoformat(customer["last_sync_ok_at"]) < datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=settings.stale_after_seconds))
        context = {"customer": dict(customer), "transactions": transactions, "audits": audits,
                   "active_key": dict(active_key) if active_key else None,
                   "active_feed": dict(active_feed) if active_feed else None,
                   "csrf": session["csrf_token"], "stale": stale, "new_key": None, "feed_url": None}
        context.update(secrets_to_show)
        return templates.TemplateResponse(request, "customer.html", context, headers=secret_headers)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        database.one("SELECT 1")
        return {"status": "ok"}

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/admin/login", response_class=HTMLResponse)
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = remote_ip(request)
        if not limiter.allow(f"login:{ip}", 5, 300):
            database.audit("admin.login_throttled", None, ip)
            return templates.TemplateResponse(request, "login.html", {"error": "Thử lại sau."}, status_code=429)
        if username != settings.admin_username or not verify_password(password, settings.admin_password_hash):
            database.audit("admin.login_failed", None, ip)
            return templates.TemplateResponse(request, "login.html", {"error": "Sai thông tin đăng nhập."}, status_code=401)
        session_id, csrf_token, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        database.execute(
            "INSERT INTO admin_sessions(id,csrf_token,created_at,expires_at) VALUES(?,?,?,?)",
            (session_id, csrf_token, now, now + settings.session_ttl_seconds),
        )
        database.audit("admin.login", None, ip)
        response = RedirectResponse("/admin", 303)
        response.set_cookie("acb_admin_session", crypto.session_cookie(session_id), httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=settings.session_ttl_seconds, path="/")
        return response

    @app.post("/admin/logout")
    def logout(request: Request, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        database.execute("UPDATE admin_sessions SET revoked_at=? WHERE id=?", (int(time.time()), session["id"]))
        database.audit("admin.logout", None, remote_ip(request))
        response = RedirectResponse("/admin/login", 303)
        response.delete_cookie("acb_admin_session", path="/")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    def dashboard(request: Request, session: dict[str, Any] = Depends(admin_session)):
        customers = [dict(row) for row in database.all("SELECT * FROM customers ORDER BY id DESC")]
        return templates.TemplateResponse(request, "dashboard.html", {"customers": customers, "csrf": session["csrf_token"], "now": utcnow()})

    @app.post("/admin/customers")
    def create_customer(
        request: Request, name: str = Form(...), account_number: str = Form(...),
        username: str = Form(""), password: str = Form(""), provider: str = Form("acb"),
        channel_type: str = Form(""), api_key: str = Form(""), api_secret: str = Form(""),
        expires_at: str = Form(""), csrf_token: str = Form(...),
        session: dict[str, Any] = Depends(admin_session),
    ):
        csrf(request, session, csrf_token)
        provider = provider.strip().lower()
        channel_type = channel_type.strip().lower() or None
        if provider not in {"acb", "binance"}:
            raise HTTPException(400, "Provider không hợp lệ")
        if provider == "binance":
            if not api_key.strip() or not api_secret.strip():
                raise HTTPException(400, "Binance API key/secret là bắt buộc")
            if channel_type not in {None, "all", "pay", "bsc", "trx"}:
                raise HTTPException(400, "Kênh Binance không hợp lệ")
            credentials = {"api_key": api_key.strip(), "api_secret": api_secret.strip()}
        else:
            if not username.strip() or not password:
                raise HTTPException(400, "ACB username/password là bắt buộc")
            channel_type = None
            credentials = {"username": username.strip(), "password": password}
        now = utcnow()
        customer_id = database.execute(
            "INSERT INTO customers(name,account_number,credential_ciphertext,expires_at,created_at,updated_at,provider,channel_type) VALUES(?,?,?,?,?,?,?,?)",
            (name.strip()[:200], account_number.strip()[:64], crypto.encrypt_json(credentials), expires_at.strip() or None, now, now, provider, channel_type),
        )
        database.audit("customer.created", customer_id, remote_ip(request), {"name": name[:200], "provider": provider})
        return RedirectResponse(f"/admin/customers/{customer_id}", 303)

    @app.get("/admin/customers/{customer_id}", response_class=HTMLResponse)
    def customer_detail(request: Request, customer_id: int, session: dict[str, Any] = Depends(admin_session)):
        return render_customer(request, customer_id, session)

    @app.post("/admin/customers/{customer_id}/update")
    def update_customer(request: Request, customer_id: int, name: str = Form(...), account_number: str = Form(...), username: str = Form(""), password: str = Form(""), api_key: str = Form(""), api_secret: str = Form(""), expires_at: str = Form(""), locked: str | None = Form(None), csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        existing = database.one("SELECT * FROM customers WHERE id=?", (customer_id,))
        if not existing: raise HTTPException(404)
        cipher = existing["credential_ciphertext"]
        if api_key or api_secret or username or password:
            old = crypto.decrypt_json(cipher)
            if existing["provider"] == "binance":
                cipher = crypto.encrypt_json({"api_key": api_key or old.get("api_key", ""), "api_secret": api_secret or old.get("api_secret", "")})
            else:
                cipher = crypto.encrypt_json({"username": username or old.get("username", ""), "password": password or old.get("password", "")})
        database.execute("UPDATE customers SET name=?,account_number=?,credential_ciphertext=?,expires_at=?,locked=?,updated_at=? WHERE id=?", (name.strip()[:200], account_number.strip()[:64], cipher, expires_at.strip() or None, 1 if locked else 0, utcnow(), customer_id))
        database.audit("customer.updated", customer_id, remote_ip(request), {"locked": bool(locked)})
        return RedirectResponse(f"/admin/customers/{customer_id}", 303)

    @app.post("/admin/customers/{customer_id}/key")
    def rotate_key(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        raw = crypto.new_api_key()
        with database.transaction() as connection:
            if not connection.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise HTTPException(404)
            connection.execute("UPDATE api_keys SET revoked_at=? WHERE customer_id=? AND revoked_at IS NULL", (utcnow(), customer_id))
            connection.execute("INSERT INTO api_keys(customer_id,key_prefix,key_hash,created_at) VALUES(?,?,?,?)", (customer_id, raw[:16], crypto.hash_api_key(raw), utcnow()))
        database.audit("api_key.rotated", customer_id, remote_ip(request))
        return render_customer(request, customer_id, session, new_key=raw)

    @app.post("/admin/customers/{customer_id}/key/revoke")
    def revoke_key(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        database.execute("UPDATE api_keys SET revoked_at=? WHERE customer_id=? AND revoked_at IS NULL", (utcnow(), customer_id))
        database.audit("api_key.revoked", customer_id, remote_ip(request))
        return RedirectResponse(f"/admin/customers/{customer_id}", 303)

    @app.post("/admin/customers/{customer_id}/feed")
    def rotate_feed(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        if not settings.public_base_url:
            raise HTTPException(503, "ACB_API_PUBLIC_BASE_URL chưa được cấu hình")
        raw = crypto.new_feed_token()
        now = utcnow()
        with database.transaction() as connection:
            if not connection.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise HTTPException(404)
            connection.execute("UPDATE feed_links SET revoked_at=? WHERE customer_id=? AND revoked_at IS NULL", (now, customer_id))
            connection.execute(
                "INSERT INTO feed_links(customer_id,token_prefix,token_hash,token_ciphertext,created_at) VALUES(?,?,?,?,?)",
                (customer_id, raw[:18], crypto.hash_api_key(raw), crypto.encrypt_string(raw), now),
            )
        database.audit("feed_link.rotated", customer_id, remote_ip(request))
        return render_customer(request, customer_id, session, feed_url=f"{settings.public_base_url}/api/v1/feed?token={raw}")

    @app.post("/admin/customers/{customer_id}/feed/reveal")
    def reveal_feed(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        row = database.one("SELECT token_ciphertext FROM feed_links WHERE customer_id=? AND revoked_at IS NULL", (customer_id,))
        if not row:
            raise HTTPException(404, "Không có feed link active")
        raw = crypto.decrypt_string(row["token_ciphertext"])
        database.audit("feed_link.revealed", customer_id, remote_ip(request))
        return render_customer(request, customer_id, session, feed_url=f"{settings.public_base_url}/api/v1/feed?token={raw}")

    @app.post("/admin/customers/{customer_id}/feed/revoke")
    def revoke_feed(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        database.execute("UPDATE feed_links SET revoked_at=? WHERE customer_id=? AND revoked_at IS NULL", (utcnow(), customer_id))
        database.audit("feed_link.revoked", customer_id, remote_ip(request))
        return RedirectResponse(f"/admin/customers/{customer_id}", 303)

    @app.post("/admin/customers/{customer_id}/test")
    def test_connection(request: Request, customer_id: int, csrf_token: str = Form(...), session: dict[str, Any] = Depends(admin_session)):
        csrf(request, session, csrf_token)
        if not limiter.allow(f"test:{customer_id}", 1, 60): raise HTTPException(429, "Chỉ test một lần/phút")
        try: poller.sync_customer(customer_id, force=True)
        finally: database.audit("customer.connection_test", customer_id, remote_ip(request))
        return RedirectResponse(f"/admin/customers/{customer_id}", 303)

    def _feed_transactions(customer: dict[str, Any], cursor: int | None, limit: int, kind: str | None = None) -> tuple[list[dict[str, Any]], int | None]:
        clauses, params = ["customer_id=?"], [customer["id"]]
        if kind:
            transaction_type = {"pay": "BINANCE_PAY", "bsc": "BINANCE_BSC_DEPOSIT", "trx": "BINANCE_TRX_DEPOSIT"}.get(kind)
            if not transaction_type:
                raise HTTPException(404, "Feed not found")
            clauses.append("transaction_type=?")
            params.append(transaction_type)
        if cursor:
            cursor_row = database.one("SELECT id,COALESCE(transacted_at,created_at) sort_at FROM transactions WHERE id=? AND customer_id=?", (cursor, customer["id"]))
            if not cursor_row:
                raise HTTPException(400, "Cursor không hợp lệ")
            clauses.append("(COALESCE(transacted_at,created_at)<? OR (COALESCE(transacted_at,created_at)=? AND id<?))")
            params.extend((cursor_row["sort_at"], cursor_row["sort_at"], cursor_row["id"]))
        params.append(limit + 1)
        rows = database.all(f"SELECT id,external_id,amount,description,transaction_type,transacted_at,raw_json,currency,extra_meta FROM transactions WHERE {' AND '.join(clauses)} ORDER BY COALESCE(transacted_at,created_at) DESC,id DESC LIMIT ?", tuple(params))
        more, rows = len(rows) > limit, rows[:limit]
        transactions = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            item = {"transactionNumber": row["external_id"], "transactionID": row["external_id"], "amount": row["amount"], "description": row["description"], "transactionDate": raw.get("transactionDate") or row["transacted_at"] or "", "type": row["transaction_type"], "currency": row["currency"]}
            try:
                extra_meta = json.loads(row["extra_meta"] or "{}")
            except (TypeError, json.JSONDecodeError):
                extra_meta = {}
            if isinstance(extra_meta, dict):
                item.update({key: value for key, value in extra_meta.items() if key not in item})
            transactions.append(item)
        return transactions, rows[-1]["id"] if more else None

    @app.get("/api/v1/feed")
    def api_feed(cursor: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=200), customer: dict[str, Any] = Depends(authenticate_feed)):
        if not customer["last_sync_ok_at"]:
            raise HTTPException(503, "Dữ liệu chưa từng đồng bộ thành công")
        stale = time.time() - datetime.fromisoformat(customer["last_sync_ok_at"]).timestamp() > settings.stale_after_seconds
        transactions, next_cursor = _feed_transactions(customer, cursor, limit)
        return {"status": "success", "transactions": transactions,
                "next_cursor": next_cursor, "stale": stale,
                "incomplete": bool(customer["last_sync_error"]), "last_sync_at": customer["last_sync_at"],
                "last_sync_ok_at": customer["last_sync_ok_at"]}

    def _authenticate_legacy_binance(tenant_id: str, token: str) -> dict[str, Any]:
        customer = database.one("SELECT c.*, f.id AS feed_id FROM customers c JOIN feed_links f ON f.customer_id=c.id WHERE c.account_number=? AND f.token_hash=? AND f.revoked_at IS NULL", (tenant_id, crypto.hash_api_key(token)))
        if not customer or customer["provider"] != "binance":
            raise HTTPException(404, "Feed not found")
        now = utcnow()
        if customer["locked"]:
            raise HTTPException(423, "Tài khoản dịch vụ đang bị khóa")
        if customer["expires_at"] and customer["expires_at"] <= now:
            raise HTTPException(403, "Gói dịch vụ đã hết hạn")
        if not limiter.allow(f"customer:{customer['id']}", settings.api_rate_limit_per_minute):
            raise HTTPException(429, "Vượt giới hạn request", headers={"Retry-After": "60"})
        database.execute("UPDATE feed_links SET last_used_at=? WHERE id=?", (now, customer["feed_id"]))
        return dict(customer)

    @app.get("/api/v1/{tenant_id}/{kind}/feed")
    def legacy_binance_feed(tenant_id: str, kind: str, token: str = Query(..., min_length=32), limit: int = Query(100, ge=1, le=200)):
        if kind not in {"pay", "bsc", "trx"}:
            raise HTTPException(404, "Feed not found")
        customer = _authenticate_legacy_binance(tenant_id, token)
        transactions, next_cursor = _feed_transactions(customer, None, limit, kind)
        return {"status": "success", "tenant_id": tenant_id, "feed": kind, "transactions": transactions, "updated_at": customer["last_sync_at"], "next_cursor": next_cursor, "stale": not customer["last_sync_ok_at"], "last_error": customer["last_sync_error"]}

    @app.get("/api/v1/{tenant_id}/status")
    def legacy_binance_status(tenant_id: str, token: str = Query(..., min_length=32)):
        customer = _authenticate_legacy_binance(tenant_id, token)
        return {"status": "success", "tenant_id": tenant_id, "last_sync_at": customer["last_sync_at"], "last_sync_ok_at": customer["last_sync_ok_at"], "stale": not customer["last_sync_ok_at"], "last_error": customer["last_sync_error"]}

    @app.get("/api/v1/status")
    def api_status(customer: dict[str, Any] = Depends(authenticate_api)):
        stale = not customer["last_sync_ok_at"] or (time.time() - datetime.fromisoformat(customer["last_sync_ok_at"]).timestamp() > settings.stale_after_seconds)
        return {"status": "ok", "customer_id": customer["id"], "account_number_masked": "*" * max(0, len(customer["account_number"]) - 4) + customer["account_number"][-4:], "last_sync_at": customer["last_sync_at"], "last_sync_ok_at": customer["last_sync_ok_at"], "stale": stale, "incomplete": bool(customer["last_sync_error"]), "sync_error": customer["last_sync_error"]}

    @app.get("/api/v1/transactions")
    def api_transactions(cursor: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200), transaction_type: str | None = None, min_amount: int | None = None, max_amount: int | None = None, customer: dict[str, Any] = Depends(authenticate_api)):
        clauses, params = ["customer_id=?"], [customer["id"]]
        if cursor:
            cursor_row = database.one(
                "SELECT id,COALESCE(transacted_at,created_at) sort_at FROM transactions WHERE id=? AND customer_id=?",
                (cursor, customer["id"]),
            )
            if not cursor_row:
                raise HTTPException(400, "Cursor không hợp lệ")
            clauses.append("(COALESCE(transacted_at,created_at)<? OR (COALESCE(transacted_at,created_at)=? AND id<?))")
            params.extend((cursor_row["sort_at"], cursor_row["sort_at"], cursor_row["id"]))
        if transaction_type: clauses.append("transaction_type=?"); params.append(transaction_type.upper())
        if min_amount is not None: clauses.append("amount>=?"); params.append(min_amount)
        if max_amount is not None: clauses.append("amount<=?"); params.append(max_amount)
        params.append(limit + 1)
        rows = database.all(
            f"SELECT id,external_id,amount,description,transaction_type,transacted_at,created_at "
            f"FROM transactions WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(transacted_at,created_at) DESC,id DESC LIMIT ?",
            tuple(params),
        )
        more = len(rows) > limit; rows = rows[:limit]
        return {"data": [dict(row) for row in rows], "next_cursor": rows[-1]["id"] if more else None}

    @app.get("/api/v1/transactions/{transaction_id}")
    def api_transaction(transaction_id: int, customer: dict[str, Any] = Depends(authenticate_api)):
        row = database.one("SELECT id,external_id,amount,description,transaction_type,transacted_at,created_at FROM transactions WHERE id=? AND customer_id=?", (transaction_id, customer["id"]))
        if not row: raise HTTPException(404, "Không tìm thấy giao dịch")
        return dict(row)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": {"status": exc.status_code, "message": exc.detail}}, exc.status_code, headers=exc.headers)
        if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
            return RedirectResponse(exc.headers["Location"], 303)
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "message": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
