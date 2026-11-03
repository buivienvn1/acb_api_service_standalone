"""Generate bootstrap values locally without persisting or logging production configuration."""
from __future__ import annotations

import getpass
import secrets

from cryptography.fernet import Fernet

from acb_api_service.core import password_hash


def prompt_admin_password() -> str:
    password = getpass.getpass("Mật khẩu admin mới: ")
    confirmation = getpass.getpass("Nhập lại mật khẩu: ")
    if not password:
        raise SystemExit("Mật khẩu không được để trống")
    if len(password) < 12:
        raise SystemExit("Mật khẩu phải có ít nhất 12 ký tự")
    if password != confirmation:
        raise SystemExit("Hai lần nhập mật khẩu không khớp")
    return password


def main() -> None:
    password = prompt_admin_password()
    print("ACB_API_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
    print("ACB_API_SESSION_SECRET=" + secrets.token_urlsafe(48))
    print("ACB_API_ADMIN_PASSWORD_HASH=" + password_hash(password))


if __name__ == "__main__":
    main()
