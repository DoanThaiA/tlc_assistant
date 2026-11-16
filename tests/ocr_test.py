"""Kiểm thử OCR file đính kèm (PDF scan, ảnh) với một server OCR.space GIẢ chạy cục bộ (không tốn lượt thật).

Chạy:  uv run python tests/ocr_test.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8774
OCR_PORT = 8775
KEY = "ocr-key"
OCR_KEY = "test-ocr-key"
BASE = f"http://127.0.0.1:{PORT}"
MAX_BYTES = 60_000
CALLS: list[dict] = []
MODE = {"quota": False}


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


class FakeOcr(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # noqa: D401
        pass

    def do_POST(self) -> None:  # noqa: N802
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        data_uri = form["base64Image"][0]
        raw = base64.b64decode(data_uri.split(",", 1)[1])
        CALLS.append({"filetype": form.get("filetype", [""])[0], "size": len(raw), "mime": data_uri[5:data_uri.index(";")],
                      "engine": form.get("OCREngine", [""])[0]})
        if self.headers.get("apikey") != OCR_KEY or MODE["quota"]:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"You may only perform this action upto maximum 500 number of times within 1 day")
            return
        pages = 1
        if CALLS[-1]["filetype"] == "PDF":
            from pypdf import PdfReader

            pages = len(PdfReader(io.BytesIO(raw)).pages)
        body = {"ParsedResults": [{"ParsedText": f"Doanh thu quý 3 đạt 2.500 triệu — trang OCR {i + 1}\r\n",
                                   "FileParseExitCode": 1} for i in range(pages)],
                "OCRExitCode": 1, "IsErroredOnProcessing": False}
        out = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


def api(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    r.raise_for_status()
    return r.json()


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


def att(name: str, data: bytes, mime: str) -> dict:
    return {"name": name, "mime_type": mime, "size": len(data), "data": base64.b64encode(data).decode()}


def noise_png(w: int = 700, h: int = 700) -> bytes:
    from PIL import Image

    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def ingest(mid: str, atts: list[dict]) -> int:
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(microsecond=0).isoformat()
    r = api("POST", "/ingest", {"source": "gmail", "reports": [{
        "message_id": mid, "sender_email": "an@congty.vn", "subject": f"Chứng từ {mid}", "body": "Gửi anh chứng từ.",
        "sent_at": now, "attachments": atts}]})
    assert r["failed"] == 0, r
    return r["results"][0]["id"]


def wait_queue_empty(timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if api("GET", "/api/overview")["ocr"]["queued"] == 0:
            return
        time.sleep(0.3)
    raise AssertionError("hàng đợi OCR không chạy hết")


async def run() -> None:
    print("1. Nhận mail: ảnh / PDF scan vào hàng đợi OCR, lưu mail ngay")
    big = noise_png()
    rid = ingest("<ocr-1@x>", [att("hoa-don.png", big, "image/png"), att("scan.pdf", blank_pdf(5), "application/pdf"),
                              att("logo.png", noise_png(10, 10), "image/png")])
    r = api("GET", f"/api/reports/{rid}")
    a = {x["name"]: x for x in r["attachments"]}
    check(a["logo.png"]["status"] == "unsupported" and "logo" in a["logo.png"]["note"], "ảnh nhỏ (logo) bỏ qua OCR")
    check(all("_ocr" not in x and "data" not in x for x in r["attachments"]), "không lưu nội dung file gốc vào email")

    print("2. Luồng nền OCR xong -> có văn bản, tìm được")
    wait_queue_empty()
    a = {x["name"]: x for x in api("GET", f"/api/reports/{rid}")["attachments"]}
    check(a["hoa-don.png"]["status"] == "ok" and a["hoa-don.png"]["ocr"] and "2.500 triệu" in a["hoa-don.png"]["text"],
          "ảnh: OCR xong, có văn bản")
    check(a["scan.pdf"]["text"].count("Doanh thu quý 3") == 5 and a["scan.pdf"]["ocr"],
          "PDF scan 5 trang: đủ văn bản các trang")
    pdf_calls = [x for x in CALLS if x["filetype"] == "PDF"]
    img_calls = [x for x in CALLS if x["filetype"] != "PDF"]
    check(len(pdf_calls) == 2, "PDF 5 trang tách thành 2 lần gửi (3 + 2 trang)")
    check(img_calls and all(x["size"] <= MAX_BYTES for x in CALLS) and img_calls[0]["mime"] == "image/jpeg",
          f"ảnh lớn ({len(big) // 1024} KB) được nén JPEG cho vừa giới hạn {MAX_BYTES // 1000} KB")
    check(all(x["engine"] == "2" for x in CALLS), "dùng OCR Engine 2")
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        p = await call(c, "get_pending_emails")
        st = {x["name"]: x["read_status"] for x in p["emails"][0]["attachments"]}
        check(st["hoa-don.png"] == "đã đọc bằng OCR", "Claude thấy 'đã đọc bằng OCR'")
        await call(c, "review_emails", decisions=[{"id": rid, "action": "keep", "category": "khac"}])
        s = await call(c, "search_reports", query="doanh thu quy 3")
        check(s["count"] == 1 and s["results"][0]["matched_in_attachment"] in ("hoa-don.png", "scan.pdf"),
              "search_reports tìm thấy chữ OCR")
    ov = api("GET", "/api/overview")["ocr"]
    check(ov["enabled"] and ov["used_today"] == len(CALLS), "đếm lượt OCR hôm nay")

    print("3. Mail bị loại: bỏ file chờ OCR")
    rid2 = ingest("<ocr-2@x>", [att("anh.png", noise_png(300, 300), "image/png")])
    api("POST", "/api/reports/review", {"decisions": [{"id": rid2, "action": "discard"}]})
    wait_queue_empty()
    check(api("GET", f"/api/reports/{rid2}")["attachments"] == [], "mail đã loại không bị ghi lại văn bản OCR")

    print("4. Mail cũ gửi lại kèm ảnh (backfillAttachments) -> OCR bổ sung")
    old = {"message_id": "<ocr-3@x>", "sender_email": "binh@congty.vn", "subject": "Biên bản", "body": "Biên bản.",
           "attachments": ["bien-ban.jpg"]}
    r = api("POST", "/ingest", {"source": "gmail", "reports": [old]})
    rid3 = r["results"][0]["id"]
    r = api("POST", "/ingest", {"source": "gmail", "reports": [{**old, "attachments": [
        att("bien-ban.jpg", noise_png(300, 300), "image/jpeg")]}]})
    check(r["attachments_updated"] == 1, "trùng mail nhưng nhận file để OCR")
    wait_queue_empty()
    a = api("GET", f"/api/reports/{rid3}")["attachments"]
    check(len(a) == 1 and a[0]["ocr"] and "2.500" in a[0]["text"], "mail cũ có văn bản OCR, không nhân đôi file")

    print("5. OCR.space từ chối (hết lượt) -> tạm dừng, giữ file trong hàng đợi")
    MODE["quota"] = True
    rid4 = ingest("<ocr-4@x>", [att("anh2.png", noise_png(300, 300), "image/png")])
    for _ in range(50):
        if api("GET", "/api/overview")["ocr"]["paused"]:
            break
        time.sleep(0.2)
    ov = api("GET", "/api/overview")["ocr"]
    a = api("GET", f"/api/reports/{rid4}")["attachments"][0]
    check(ov["paused"] and ov["queued"] == 1 and a["status"] == "ocr_pending", "tạm dừng, file vẫn chờ, không báo lỗi file")


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", OCR_PORT), FakeOcr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "MCP_API_KEYS": KEY, "MCP_PORT": str(PORT), "MCP_HOST": "127.0.0.1",
           "DB_PATH": str(Path(tmp) / "t.db"), "PYTHONIOENCODING": "utf-8",
           "OCR_SPACE_API_KEY": OCR_KEY, "OCR_SPACE_URL": f"http://127.0.0.1:{OCR_PORT}/parse/image",
           "OCR_POLL_SECONDS": "0.2", "OCR_MAX_BYTES": str(MAX_BYTES), "OCR_MIN_IMAGE_BYTES": "2000"}
    proc = subprocess.Popen([sys.executable, "-m", "email_report_mcp", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        asyncio.run(run())
        print("\nTẤT CẢ ĐỀU QUA ✅")
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            out = ""
        if "Traceback" in out:
            print(out[-4000:])
        srv.shutdown()


if __name__ == "__main__":
    main()
