"""Kiểm thử cấu hình hội thoại Zalo trên trang quản trị (/api/zalo/conversations, /api/zalo/rules).

Chạy:  uv run python tests/zalo_admin_test.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8773
KEY = "zalo-admin-key"
BASE = f"http://127.0.0.1:{PORT}"
H = {"Authorization": f"Bearer {KEY}"}


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def req(method: str, path: str, body: dict | None = None) -> httpx.Response:
    return httpx.request(method, f"{BASE}{path}", json=body, headers=H, timeout=10)


def ok(method: str, path: str, body: dict | None = None) -> dict:
    r = req(method, path, body)
    if r.status_code >= 400:
        raise AssertionError(f"{method} {path} -> {r.status_code} {r.text}")
    return r.json()


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


async def run() -> None:
    print("1. Thêm hội thoại theo 3 chế độ")
    ceo = ok("POST", "/api/zalo/conversations", {"name": "Doãn Quốc Thái", "kind": "direct", "mode": "reply",
                                                 "allowed_askers": ["Doãn Quốc Thái"], "note": "CEO"})
    check(ceo["mode"] == "reply" and ceo["watch"] and ceo["reply"] and ceo["allowed_askers"] == "Doãn Quốc Thái",
          "nhắn riêng CEO: được trả lời, chỉ CEO hỏi")
    grp = ok("POST", "/api/zalo/conversations", {"name": "Trợ lý CEO TLC", "kind": "group", "mode": "reply",
                                                 "allowed_askers": []})
    check(grp["mode"] == "reply" and grp["allowed_askers"] is None, "nhóm trợ lý: mọi thành viên được hỏi")
    ro = ok("POST", "/api/zalo/conversations", {"name": "Ban Lãnh Đạo", "mode": "read"})
    check(ro["mode"] == "read" and ro["watch"] and not ro["reply"] and ro["kind"] == "group", "nhóm chỉ đọc")
    off = ok("POST", "/api/zalo/conversations", {"name": "Nhóm cũ", "mode": "off"})
    check(off["mode"] == "off" and not off["watch"], "ngừng theo dõi")

    print("2. Kiểm tra dữ liệu nhập")
    r = req("POST", "/api/zalo/conversations", {"name": "Ai đó", "kind": "direct", "mode": "reply"})
    check(r.status_code == 400 and "người được hỏi" in r.json()["message"], "nhắn riêng được trả lời mà thiếu CEO -> 400")
    r = req("POST", "/api/zalo/conversations", {"name": "ban lanh dao", "mode": "read"})
    check(r.status_code == 409, "trùng tên (không dấu) -> 409")
    check(req("POST", "/api/zalo/conversations", {"name": "  ", "mode": "read"}).status_code == 400, "tên rỗng -> 400")
    check(req("POST", "/api/zalo/conversations", {"name": "X", "mode": "abc"}).status_code == 400, "chế độ sai -> 400")
    r = req("PUT", f"/api/zalo/conversations/{ro['id']}", {"watch": False, "reply": True})
    check(r.status_code == 400, "được trả lời nhưng không theo dõi -> 400")
    check(req("PUT", "/api/zalo/conversations/9999", {"mode": "read"}).status_code == 404, "không có -> 404")
    r = req("PUT", f"/api/zalo/conversations/{off['id']}", {"name": "Ban Lãnh Đạo"})
    check(r.status_code == 409, "đổi tên trùng hội thoại khác -> 409")
    check(httpx.get(f"{BASE}/api/zalo/conversations").status_code == 401, "chưa đăng nhập -> 401")

    print("3. Trợ lý (MCP) thấy đúng cấu hình")
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        ctx = await call(c, "start_cycle")
        check(sorted(x["name"] for x in ctx["reply_conversations"]) == ["Doãn Quốc Thái", "Trợ lý CEO TLC"]
              and [x["name"] for x in ctx["watched_groups"]] == ["Ban Lãnh Đạo"], "start_cycle: 2 trả lời, 1 chỉ đọc")
        cl = await call(c, "claim_zalo_question", conversation="Ban Lãnh Đạo", sender="A", sent_at=datetime.now(
            ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(), text="Hỏi gì đó?")
        check(cl["allowed"] is False, "nhóm chỉ đọc: trợ lý không được trả lời")
        now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).replace(microsecond=0).isoformat()
        await call(c, "save_zalo_messages", conversation="Ban Lãnh Đạo",
                   messages=[{"sender": "An", "sent_at": now, "text": "Doanh số tuần đạt 1 tỷ", "category": "khac"}])
        cl = await call(c, "claim_zalo_question", conversation="Trợ lý CEO TLC", sender="Minh", sent_at=now,
                        text="Doanh số tuần?")
        await call(c, "record_zalo_reply", reply_id=cl["reply_id"], status="sent", reply_text="1 tỷ ạ")

        print("4. Đổi chế độ / đổi tên / ghi chú")
        u = ok("PUT", f"/api/zalo/conversations/{grp['id']}", {"mode": "read"})
        check(u["mode"] == "read" and not u["reply"], "chuyển nhóm trợ lý về chỉ đọc")
        ctx = await call(c, "start_cycle")
        check([x["name"] for x in ctx["reply_conversations"]] == ["Doãn Quốc Thái"], "trợ lý áp dụng ngay lượt sau")
        cl = await call(c, "claim_zalo_question", conversation="Trợ lý CEO TLC", sender="Minh", sent_at=now,
                        text="Câu khác?")
        check(cl["allowed"] is False, "hết quyền trả lời trong nhóm đó")
        u = ok("PUT", f"/api/zalo/conversations/{ro['id']}", {"name": "Ban Lãnh Đạo Họp Tuần", "note": "Họp thứ 2"})
        check(u["name"] == "Ban Lãnh Đạo Họp Tuần" and u["note"] == "Họp thứ 2" and u["mode"] == "read"
              and u["saved_messages"] == 1, "đổi tên + ghi chú, giữ chế độ và tin đã lưu")
        s = await call(c, "search_zalo", query="doanh so", conversation="Họp Tuần")
        check(s["count"] == 1, "tìm theo tên mới của hội thoại")
        u = ok("PUT", f"/api/zalo/conversations/{ceo['id']}", {"allowed_askers": "Doãn Quốc Thái\nThái CEO"})
        check(u["allowed_askers"] == "Doãn Quốc Thái, Thái CEO", "người được hỏi nhập nhiều dòng")

    print("5. Danh sách + xoá")
    lst = {c["name"]: c for c in ok("GET", "/api/zalo/conversations")["conversations"]}
    check(lst["Trợ lý CEO TLC"]["questions"] == 1 and lst["Trợ lý CEO TLC"]["replies_sent"] == 1
          and lst["Nhóm cũ"]["mode"] == "off", "danh sách có chế độ, số câu hỏi / đã trả lời")
    d = ok("DELETE", f"/api/zalo/conversations/{grp['id']}")
    check(d["replies_deleted"] == 1, "xoá hội thoại kèm lịch sử trả lời")
    d = ok("DELETE", f"/api/zalo/conversations/{ro['id']}")
    check(d["messages_deleted"] == 1 and ok("GET", "/api/replies")["total"] == 0, "xoá kèm tin đã lưu")
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        s = await call(c, "search_zalo", query="doanh so")
        check(s["count"] == 0, "tin đã xoá không còn trong tìm kiếm")
    check(req("DELETE", f"/api/zalo/conversations/{ro['id']}").status_code == 404, "xoá lần 2 -> 404")

    print("6. Quy tắc Zalo")
    r = ok("GET", "/api/zalo/rules")
    check("KHÔNG LƯU" in r["rules"] and r["default_rules"] == r["rules"], "mặc định")
    r = ok("PUT", "/api/zalo/rules", {"rules": "- Chỉ lưu tin về dự án"})
    check(r["rules"] == "- Chỉ lưu tin về dự án", "thay toàn bộ quy tắc")
    check(req("PUT", "/api/zalo/rules", {"rules": " "}).status_code == 400, "quy tắc rỗng -> 400")
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        check((await call(c, "start_cycle"))["rules"] == "- Chỉ lưu tin về dự án", "trợ lý đọc quy tắc mới")
    check("viewZalo" in httpx.get(f"{BASE}/assets/app.js").text and "Nhóm Zalo" in httpx.get(f"{BASE}/").text,
          "giao diện có tab Nhóm Zalo")


def main() -> None:
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "OCR_SPACE_API_KEY": "", "MCP_API_KEYS": KEY, "MCP_PORT": str(PORT), "MCP_HOST": "127.0.0.1",
           "DB_PATH": str(Path(tmp) / "t.db"), "PYTHONIOENCODING": "utf-8"}
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
            print(out[-3000:])


if __name__ == "__main__":
    main()
