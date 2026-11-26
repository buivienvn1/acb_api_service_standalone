# ACB API Service Standalone

Gói độc lập gồm FastAPI web API/admin UI và polling worker, không phụ thuộc bot chính.

## Cài đặt

```bash
sudo mkdir -p /opt/acb-api
sudo unzip acb_api_service_standalone.zip -d /opt/acb-api
cd /opt/acb-api
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cp .env.example .env
mkdir -p data/acb_api
chmod 700 data data/acb_api
./venv/bin/python -m acb_api_service.bootstrap
```

Đưa ba giá trị bootstrap vào `.env`: `ACB_API_ENCRYPTION_KEY`, `ACB_API_SESSION_SECRET`, `ACB_API_ADMIN_PASSWORD_HASH`. Không commit hoặc gửi các giá trị này qua log/chat. Encryption key phải được backup riêng.

## Binance (tùy chọn)

Có thể tạo tenant Binance trong Admin UI mà không cần chạy service riêng. Chọn provider `Binance`, nhập API key/secret chỉ có quyền đọc và chọn kênh `Binance Pay`, `BSC / USDT`, `TRX` hoặc tất cả. API key/secret được Fernet mã hóa trong SQLite; không xuất hiện trong feed URL.

Các biến `BINANCE_*` trong `.env.example` điều khiển endpoint, timeout và khoảng lịch sử polling. Feed chuẩn vẫn cấp cho khách hàng dạng:

```text
https://your-domain.example/api/v1/feed?token=<feed_token>
```

Để tương thích bot Binance cũ, service cũng hỗ trợ:

```text
https://your-domain.example/api/v1/<tenant_id>/<kind>/feed?token=<feed_token>
https://your-domain.example/api/v1/<tenant_id>/status?token=<feed_token>
```

Trong đó `kind` là `pay`, `bsc` hoặc `trx`. Không đặt API secret vào URL, query log hoặc mã nguồn.

## Chống sử dụng lại giao dịch giữa nhiều bot

Mỗi giao dịch trong feed có thêm `claimed` và `claimed_by`. `claimed: false` chỉ để tham khảo; trước khi cộng tiền hoặc giao hàng, bot bắt buộc phải claim nguyên tử qua API trung tâm.

Tạo một consumer key riêng cho từng bot trong trang chi tiết khách hàng. Key chỉ hiển thị một lần và phải được lưu như secret. Bot gửi đúng `transactionID` lấy từ feed:

```bash
curl -X POST https://your-domain.example/api/v1/claims \
  -H "Authorization: Bearer <consumer_key>" \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"TX123"}'
```

Quy ước phản hồi:

- HTTP `200`, `claimed: true`: bot hiện tại đã claim thành công, hoặc chính bot này đang retry giao dịch đã claim trước đó. Bot được phép hoàn tất đơn.
- HTTP `409`, `claimed: true`: một bot khác đã claim giao dịch. Bot không được hoàn tất đơn.
- HTTP `404`: giao dịch không tồn tại trong tenant gắn với consumer key.
- HTTP `401`: consumer key sai hoặc đã bị thu hồi.

Ví dụ thành công:

```json
{"transaction_id":"TX123","claimed":true,"claimed_by":"bot-a"}
```

Bot không được chỉ dựa vào `claimed: false` trong feed vì hai bot có thể đọc cùng lúc. Quyền sử dụng giao dịch được quyết định duy nhất bởi kết quả của `POST /api/v1/claims`.

## Chạy thử

```bash
set -a; . ./.env; set +a
./venv/bin/python -m unittest tests/test_acb_api_service.py
./venv/bin/python -m uvicorn acb_api_service.app:app --host 127.0.0.1 --port 8091 --workers 1 --no-access-log
```

Worker chạy ở process riêng:

```bash
./venv/bin/python -m acb_api_service.worker
```

Kiểm tra: `curl http://127.0.0.1:8091/healthz`. Production phải đặt Nginx HTTPS phía trước, không expose port 8091 trực tiếp.

## Supervisor

Sửa `directory` và đường dẫn `command` trong `deploy/acb-api.conf`, sau đó cài vào Supervisor và chạy `supervisorctl reread && supervisorctl update`. Chỉ chạy một web worker vì rate limiter nằm trong memory process.

## Dữ liệu và backup

SQLite nằm tại `ACB_API_DATABASE_PATH`. Dừng cả web/worker trước khi copy database, hoặc dùng SQLite online backup. Backup database không thay thế backup `ACB_API_ENCRYPTION_KEY`.
