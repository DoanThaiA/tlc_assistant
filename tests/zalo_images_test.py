"""Kiểm thử ảnh trong tin Zalo: máy chủ tải ảnh (chỉ tên miền cho phép) -> OCR nền -> tìm được chữ trong ảnh;
ảnh không tải được -> images_to_read -> Claude tự đọc (save_zalo_image_text).

Dùng server OCR.space GIẢ + "CDN ảnh" GIẢ chạy cục bộ (không tốn lượt thật).
Chạy:  uv run python tests/zalo_images_test.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8776
OCR_PORT = 8777
IMG_PORT = 8778
KEY = "zimg-key"
OCR_KEY = "test-ocr-key"
BASE = f"http://127.0.0.1:{PORT}"
IMG = f"http://127.0.0.1:{IMG_PORT}"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
OCR_CALLS: list[int] = []
IMG_HITS: list[str] = []


def at(minutes: int = 0) -> str:
    return (datetime.now(TZ) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def png(w: int = 400, h: int = 300) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(buf, "PNG")
    return buf.getvalue()


PNG = png()


class FakeOcr(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # noqa: D401
        pass

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        OCR_CALLS.append(1)
        body = {"ParsedResults": [{"ParsedText": "PHIẾU GIAO HÀNG\r\nKhách: Hoà Phát — 120 tấn thép, giao 18/11\r\n",
                                   "FileParseExitCode": 1}], "OCRExitCode": 1, "IsErroredOnProcessing": False}
        out = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


class FakeCdn(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # noqa: D401
        pass

    def do_GET(self) -> None:  # noqa: N802
        IMG_HITS.append(self.path)
        if self.path.startswith("/ok"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG)
        elif self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{IMG_PORT}/ok.png")
            self.end_headers()
        elif self.path.startswith("/html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>login</html>")
        else:
            self.send_response(403)
            self.end_headers()


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


def api(path: str) -> dict:
    r = httpx.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def wait_ocr(timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if api("/api/overview")["ocr"]["queued"] == 0:
            return
        time.sleep(0.3)
    raise AssertionError("hàng đợi OCR không chạy hết")


async def run() -> None:
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        await call(c, "upsert_zalo_conversation", name="Kho vận TLC", kind="group")

        print("1. Lưu tin kèm ảnh: tải ảnh từ CDN được phép -> OCR nền")
        r = await call(c, "save_zalo_messages", conversation="Kho vận TLC", messages=[
            {"sender": "Lê Văn Kho", "sent_at": at(-30), "text": "[Ảnh: phiếu giao hàng] Em gửi phiếu giao chiều nay",
             "category": "khac", "summary": "Phiếu giao hàng cho Hoà Phát",
             "images": [{"url": f"{IMG}/ok.png?x=1", "description": "Ảnh chụp phiếu giao hàng"}]},
            {"sender": "Trần Thủ Kho", "sent_at": at(-20), "text": "[Ảnh: bảng tồn kho] Tồn kho cuối ngày",
             "category": "khac",
             "images": [{"url": f"{IMG}/forbidden.jpg", "description": "Bảng tồn kho"},
                        {"url": f"{IMG}/html", "description": "Ảnh 2"},
                        {"url": f"{IMG}/redir", "description": "Ảnh 3"},
                        {"url": "https://evil.example.com/a.png", "description": "Ảnh lạ"},
                        {"url": "blob:https://chat.zalo.me/abc", "description": "Ảnh blob"}]},
            {"sender": "Lê Văn Kho", "sent_at": at(-10), "text": "[Ảnh: biên bản] Biên bản kiểm kê", "category": "khac",
             "images": [{"description": "Biên bản kiểm kê kho", "text": "Biên bản kiểm kê: thiếu 3 cuộn tôn mạ màu"}]},
        ], last_message={"sender": "Lê Văn Kho", "sent_at": at(-10), "text": "Biên bản kiểm kê"})
        check(r["inserted"] == 3 and not r["errors"], "lưu 3 tin")
        im = r["images"]
        check(im["queued_ocr"] == 1 and im["read_by_claude"] == 1 and im["not_downloaded"] == 5,
              f"1 ảnh vào hàng đợi OCR, 1 ảnh Claude tự đọc, 5 ảnh không tải được ({im})")
        reasons = {x["description"]: x["reason"] for x in r["images_to_read"]}
        check(len(r["images_to_read"]) == 5 and "next_step" in r, "trả images_to_read để Claude tự đọc")
        check("403" in reasons["Bảng tồn kho"], "link cần đăng nhập (HTTP 403) -> báo lý do")
        check("không trả về ảnh" in reasons["Ảnh 2"], "link trả về trang HTML -> không phải ảnh")
        check("chuyển hướng" in reasons["Ảnh 3"], "chuyển hướng sang tên miền khác -> chặn")
        check("tên miền" in reasons["Ảnh lạ"] and not any("evil" in h for h in IMG_HITS),
              "link ngoài tên miền Zalo -> không tải")
        check("blob" in reasons["Ảnh blob"], "link blob: -> Claude tự đọc")
        check("/ok.png" not in IMG_HITS and IMG_HITS.count("/redir") == 1, "không đi theo chuyển hướng")

        wait_ocr()
        check(len(OCR_CALLS) == 1, "OCR đúng 1 ảnh")
        s = await call(c, "search_zalo", query="hoa phat 120 tan thep")
        check(s["count"] == 1 and "120 tấn thép" in s["results"][0]["text"] and s["results"][0]["images"],
              "search_zalo tìm thấy chữ trong ảnh (OCR)")
        s = await call(c, "search_zalo", query="ton ma mau")
        check(s["count"] == 1, "search_zalo tìm thấy chữ Claude tự đọc trong ảnh")
        msgs = (await call(c, "get_zalo_messages", conversation="Kho vận"))["messages"]
        first = msgs[0]["images"][0]
        check(first["status"] == "ok" and first["source"] == "ocr" and "PHIẾU GIAO HÀNG" in first["text"],
              "get_zalo_messages trả chữ OCR trong images")

        print("2. Claude tự đọc ảnh máy chủ không tải được")
        iid = next(x["image_id"] for x in r["images_to_read"] if x["description"] == "Bảng tồn kho")
        await call(c, "save_zalo_image_text", image_id=iid, text="Tồn kho: thép cuộn 450 tấn, tôn 80 tấn")
        s = await call(c, "search_zalo", query="thep cuon 450")
        check(s["count"] == 1 and s["results"][0]["sender"] == "Trần Thủ Kho", "chữ Claude đọc được đưa vào tìm kiếm")
        try:
            await c.call_tool("save_zalo_image_text", {"image_id": 99999, "text": "x"})
            raise AssertionError("lẽ ra phải lỗi")
        except ToolError as e:
            check("image_id" in str(e), "image_id sai -> báo lỗi")

        print("3. Gửi lại cùng tin + thêm ảnh mới: không nhân đôi")
        r2 = await call(c, "save_zalo_messages", conversation="Kho vận TLC", messages=[
            {"sender": "Lê Văn Kho", "sent_at": at(-30), "text": "[Ảnh: phiếu giao hàng] Em gửi phiếu giao chiều nay",
             "category": "khac", "images": [{"url": f"{IMG}/ok.png?x=1", "description": "Ảnh chụp phiếu giao hàng"},
                                            {"url": f"{IMG}/ok.png?x=2", "description": "Phiếu trang 2"}]}])
        check(r2["duplicate"] == 1 and r2["images"]["duplicate"] == 1 and r2["images"]["queued_ocr"] == 1,
              "tin trùng: bỏ ảnh cũ, thêm ảnh mới")
        wait_ocr()
        msgs = (await call(c, "get_zalo_messages", conversation="Kho vận"))["messages"]
        check(len(msgs[0]["images"]) == 2, "tin có đủ 2 ảnh")

        print("4. Xoá hội thoại -> xoá luôn ảnh")
        convs = api("/api/zalo/conversations")
        cid = next(x["id"] for x in (convs.get("conversations") or convs.get("items") or convs) if x["name"] == "Kho vận TLC")
        httpx.delete(f"{BASE}/api/zalo/conversations/{cid}", headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    import sqlite3

    n = sqlite3.connect(os.environ["ZIMG_DB"]).execute("SELECT COUNT(*) FROM zalo_images").fetchone()[0]
    check(n == 0, "không còn ảnh mồ côi")


def main() -> None:
    servers = [ThreadingHTTPServer(("127.0.0.1", OCR_PORT), FakeOcr), ThreadingHTTPServer(("127.0.0.1", IMG_PORT), FakeCdn)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    os.environ["ZIMG_DB"] = str(Path(tmp) / "t.db")
    env = {**os.environ, "MCP_API_KEYS": KEY, "MCP_PORT": str(PORT), "MCP_HOST": "127.0.0.1",
           "DB_PATH": os.environ["ZIMG_DB"], "PYTHONIOENCODING": "utf-8",
           "OCR_SPACE_API_KEY": OCR_KEY, "OCR_SPACE_URL": f"http://127.0.0.1:{OCR_PORT}/parse/image",
           "OCR_POLL_SECONDS": "0.2", "ZALO_IMAGE_HOSTS": "127.0.0.1", "ZALO_IMAGE_TIMEOUT": "5"}
    log_path = Path(tmp) / "server.log"  # ghi ra file: log dài (traceback) không làm đầy pipe, treo máy chủ
    log_file = open(log_path, "wb")  # noqa: SIM115
    ok = False
    proc = subprocess.Popen([sys.executable, "-m", "email_report_mcp", "serve"], env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        asyncio.run(run())
        print("\nTẤT CẢ ĐỀU QUA ✅")
        ok = True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        if not ok:
            print(log_path.read_text("utf-8", "replace")[-4000:])
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
