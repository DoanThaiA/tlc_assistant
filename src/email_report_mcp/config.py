"""Cấu hình đọc từ biến môi trường / file .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # --- Server HTTP ---
    host: str = os.getenv("MCP_HOST", "127.0.0.1")
    port: int = int(os.getenv("MCP_PORT", "8000"))
    path: str = os.getenv("MCP_PATH", "/mcp")
    # Khoá truy cập: client gửi qua header "Authorization: Bearer <key>" hoặc query "?key=<key>"
    api_keys: list[str] = field(default_factory=lambda: _list("MCP_API_KEYS"))
    # Host header được phép (khi chạy sau domain/reverse proxy). Để trống = không kiểm tra.
    allowed_hosts: list[str] = field(default_factory=lambda: _list("MCP_ALLOWED_HOSTS"))

    # --- Trang quản trị (giao diện web ở "/") ---
    # Mật khẩu đăng nhập; để trống = đăng nhập bằng một trong các MCP_API_KEYS
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    # Khoá ký cookie phiên; để trống = tự suy ra từ API key/mật khẩu
    session_secret: str = os.getenv("SESSION_SECRET", "")
    session_hours: int = int(os.getenv("ADMIN_SESSION_HOURS", "12"))

    # --- CSDL ---
    db_path: Path = Path(os.getenv("DB_PATH", "data/reports.db"))

    # --- Nghiệp vụ ---
    # Múi giờ dùng để xác định "ngày báo cáo"
    timezone: ZoneInfo = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))
    # Ngày làm việc phải nộp báo cáo (1=Thứ 2 ... 7=Chủ nhật)
    work_days: list[int] = field(
        default_factory=lambda: [int(x) for x in _list("WORK_DAYS", "1,2,3,4,5")]
    )


settings = Settings()
