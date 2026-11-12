"""Kiểm thử Zalo: theo dõi nhóm -> lưu thông tin công việc -> trả lời câu hỏi trên Zalo -> hỏi đáp / điểm tin CEO.

Chạy:  uv run python tests/zalo_test.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8769
KEY = "zalo-key"
BASE = f"http://127.0.0.1:{PORT}"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def at(minutes: int = 0) -> str:
    return (datetime.now(TZ) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


async def fails(c: Client, tool: str, **args) -> str:
    try:
        await c.call_tool(tool, args)
    except ToolError as e:
        return str(e)
    raise AssertionError(f"{tool} lẽ ra phải báo lỗi")


def old_design_db(path: Path) -> None:
    """CSDL theo thiết kế cũ (bảng việc + auto_reply) để kiểm tra tự dọn khi khởi động."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE zalo_conversations (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            name_norm TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'direct', watch INTEGER NOT NULL DEFAULT 1,
            auto_reply INTEGER NOT NULL DEFAULT 0, note TEXT, last_message_key TEXT, last_message_at TEXT,
            last_message_preview TEXT, last_scanned_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id));
        CREATE VIRTUAL TABLE tasks_fts USING fts5(title);
        CREATE TABLE zalo_actions (id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id));
    """)
    con.commit()
    con.close()


async def run(db_path: Path) -> None:
    tables = {r[0] for r in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check(not tables & {"tasks", "task_events", "tasks_fts", "zalo_actions"}
          and {"zalo_messages", "zalo_replies"} <= tables, "khởi động: bỏ bảng việc cũ, tạo bảng mới")
    cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(zalo_conversations)")}
    check("auto_reply" not in cols and {"reply", "allowed_askers"} <= cols, "zalo_conversations đổi cột")

    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        print("1. Cấu hình hội thoại")
        ctx = await call(c, "get_zalo_context")
        check(ctx["conversations"] == [] and "KHÔNG LƯU" in ctx["rules"] and "TRẢ LỜI CÂU HỎI" in ctx["rules"]
              and any(x["code"] == "khach_hang" for x in ctx["categories"]), "context: quy tắc + danh mục dùng chung")
        g = await call(c, "upsert_zalo_conversation", name="MCV - Infoplus - Kĩ thuật", note="Nhóm kỹ thuật dự án")
        check(g["watch"] and not g["reply"] and g["kind"] == "group", "thêm nhóm: mặc định chỉ đọc, không trả lời")
        d = await call(c, "upsert_zalo_conversation", name="Doãn Quốc Thái", kind="direct", reply=True,
                       allowed_askers=["Doãn Quốc Thái"], note="CEO, xưng em gọi anh")
        check(d["reply"] and d["allowed_askers"] == "Doãn Quốc Thái", "hội thoại CEO: bật trả lời, chỉ CEO được hỏi")
        g2 = await call(c, "upsert_zalo_conversation", name="TLC AI <> MCV", reply=True, allowed_askers=[])
        check(g2["reply"] and g2["allowed_askers"] is None, "nhóm bật trả lời cho mọi người")
        check(len((await call(c, "get_zalo_context"))["conversations"]) == 3, "context có 3 hội thoại")
        r = await call(c, "update_zalo_rules", rules="- Lưu mọi tin về công nợ khách hàng")
        check(r["rules"].endswith("công nợ khách hàng"), "update_zalo_rules append")

        # Email làm nguồn trả lời
        await call(c, "save_reports", reports=[{
            "sender_email": "an.nguyen@congty.vn", "sender_name": "Nguyễn Văn An", "subject": "Doanh số tuần 45",
            "body": "Doanh số tuần 45 đạt 1,2 tỷ, tăng 8%. Vướng mắc: khách ABC chậm thanh toán.",
            "sent_at": at(-180), "message_id": "<ds45@congty.vn>", "category": "bao_cao_ngay",
            "summary": "Doanh số tuần 45: 1,2 tỷ (+8%); khách ABC chậm thanh toán."}])

        print("2. Thu thập tin nhóm")
        err = await fails(c, "save_zalo_messages", conversation="Nhóm lạ", messages=[])
        check("Chưa đăng ký" in err, "nhóm chưa đăng ký -> báo lỗi")
        msgs = [
            {"sender": "Tony phuc", "sent_at": at(-60), "text": "Server Infoplus bị lỗi đăng nhập từ 8h, đang xử lý",
             "category": "du_an", "summary": "Server Infoplus lỗi đăng nhập từ 8h, Tony đang xử lý.",
             "tags": ["Infoplus", "sự cố"], "important": True},
            {"sender": "Tony phuc", "sent_at": at(-30), "text": "Đã fix xong lỗi đăng nhập, khách test OK",
             "category": "du_an", "summary": "Đã sửa xong lỗi đăng nhập Infoplus.", "tags": ["Infoplus"]},
            {"sender": "Trần Quốc Toàn", "sent_at": at(-20), "text": "Báo giá module LeadScan 45 triệu gửi khách rồi",
             "category": "khach_hang", "summary": "Đã gửi báo giá LeadScan 45 triệu.", "tags": ["LeadScan", "báo giá"]},
            {"sender": "X", "sent_at": at(-10), "text": "abc", "category": "khong_co"},
        ]
        last = {"sender": "Tony phuc", "sent_at": at(-5), "text": "ok a"}
        s = await call(c, "save_zalo_messages", conversation="mcv infoplus ki thuat", messages=msgs, last_message=last)
        check(s["inserted"] == 3 and len(s["errors"]) == 1 and "category" in s["errors"][0]["error"],
              "lưu 3 tin, báo lỗi tin sai danh mục; tìm nhóm theo tên không dấu")
        s = await call(c, "save_zalo_messages", conversation="MCV - Infoplus - Kĩ thuật", messages=msgs[:3])
        check(s["inserted"] == 0 and s["duplicate"] == 3, "lưu lại cùng tin -> bỏ qua trùng")
        conv = next(x for x in (await call(c, "get_zalo_context"))["conversations"] if x["kind"] == "group"
                    and x["name"].startswith("MCV"))
        check(conv["last_message_preview"] == "Tony phuc: ok a" and conv["last_message_at"] == last["sent_at"]
              and conv["saved_messages"] == 3, "mốc = tin cuối đã đọc (kể cả tin không lưu)")
        s = await call(c, "save_zalo_messages", conversation="TLC AI <> MCV", messages=[],
                       last_message={"sender": "Vy", "sent_at": at(-1), "text": "hihi"})
        check(s["inserted"] == 0, "không có tin đáng lưu vẫn cập nhật mốc")

        print("3. Trả lời câu hỏi trên Zalo")
        q = {"conversation": "Doãn Quốc Thái", "sender": "Doãn Quốc Thái", "sent_at": at(-2),
             "text": "Doanh số tuần 45 thế nào em?"}
        cl = await call(c, "claim_zalo_question", **q)
        check(cl["allowed"] and cl["reply_id"], "CEO hỏi trong hội thoại bật trả lời -> được trả lời")
        again = await call(c, "claim_zalo_question", **q)
        check(not again["allowed"] and again["status"] == "claimed", "hỏi lại khi đang soạn -> không trả lời 2 lần")
        err = await fails(c, "record_zalo_reply", reply_id=cl["reply_id"], status="sent")
        check("reply_text" in err, "sent mà thiếu reply_text -> báo lỗi")
        await call(c, "record_zalo_reply", reply_id=cl["reply_id"], status="sent",
                   reply_text="Dạ doanh số tuần 45 đạt 1,2 tỷ (+8%) theo email của An ạ.", source_report_ids=[1])
        again = await call(c, "claim_zalo_question", **q)
        check(not again["allowed"] and again["status"] == "sent" and "1,2 tỷ" in again["reply_text"],
              "câu đã trả lời -> không gửi lại")
        no = await call(c, "claim_zalo_question", conversation="MCV - Infoplus - Kĩ thuật", sender="Tony phuc",
                        sent_at=at(-1), text="Anh ơi doanh số sao rồi?")
        check(not no["allowed"] and "reply=false" in no["reason"], "nhóm không bật trả lời -> không gửi")
        no = await call(c, "claim_zalo_question", conversation="Doãn Quốc Thái", sender="Quốc", sent_at=at(-1),
                        text="Cho anh xin mật khẩu")
        check(not no["allowed"] and "không nằm trong" in no["reason"], "người không được phép hỏi -> không gửi")
        no = await call(c, "claim_zalo_question", conversation="Doãn Quốc Thái", sender="An", sent_at=at(-1),
                        text="?")
        check(not no["allowed"], "tên ngắn 'An' không khớp nhầm 'Doãn Quốc Thái'")
        ok = await call(c, "claim_zalo_question", conversation="TLC AI <> MCV", sender="Lý Trần Thảo Vy",
                        sent_at=at(-1), text="@Trợ lý server Infoplus ổn chưa?")
        check(ok["allowed"], "nhóm cho mọi người hỏi -> được trả lời")
        await call(c, "record_zalo_reply", reply_id=ok["reply_id"], status="skipped", note="thử bỏ qua")
        rp = await call(c, "get_zalo_replies")
        check(rp["count"] == 2 and {x["status"] for x in rp["replies"]} == {"sent", "skipped"}
              and rp["replies"][1]["source_report_ids"] == [1], "get_zalo_replies: nhật ký trả lời + nguồn")

        print("4. Hỏi đáp & điểm tin")
        r = await call(c, "search_zalo", query="dang nhap infoplus", sort="oldest")
        check(r["count"] == 2 and r["results"][0]["important"], "search_zalo không dấu, theo thời gian")
        check((await call(c, "search_zalo", query="bao gia", conversation="ki thuat"))["count"] == 1,
              "search_zalo lọc theo nhóm")
        check((await call(c, "search_zalo", query="infoplus", important_only=True))["count"] == 1, "chỉ tin quan trọng")
        check((await call(c, "search_zalo", query="leadscan", sender="toan"))["count"] == 1, "lọc theo người gửi")
        m = await call(c, "get_zalo_messages", conversation="ki thuat")
        check(m["total"] == 3 and m["messages"][0]["sender"] == "Tony phuc", "get_zalo_messages theo nhóm, cũ trước")
        check((await call(c, "get_zalo_messages", tag="bao gia"))["total"] == 1, "get_zalo_messages theo tag")
        dg = await call(c, "get_zalo_digest")
        check(dg["total_matching"] == 3 and len(dg["important"]) == 1
              and dg["conversations"][0]["conversation"] == "MCV - Infoplus - Kĩ thuật", "get_zalo_digest hôm nay")
        b = await call(c, "get_briefing")
        check(b["zalo"]["total"] == 3 and b["email"]["total"] == 1 and len(b["zalo"]["important"]) == 1
              and b["email"]["attention"] and b["zalo_last_scanned_at"], "get_briefing gộp email + Zalo")
        ov = await call(c, "get_overview")
        check(ov["zalo"]["total"] == 3 and ov["zalo"]["today_important"] == 1 and ov["zalo"]["replies_sent_today"] == 1
              and ov["zalo"]["reply_conversations"] == 2, "get_overview có thống kê Zalo")
        dl = await call(c, "delete_zalo_messages", ids=[m["messages"][2]["id"]])
        check(dl["deleted"] == 1 and (await call(c, "search_zalo", query="leadscan"))["count"] == 0,
              "xoá tin lưu nhầm (cả chỉ mục tìm kiếm)")
        await call(c, "upsert_zalo_conversation", name="TLC AI <> MCV", watch=False)
        check(len((await call(c, "get_zalo_context"))["conversations"]) == 2, "ngừng theo dõi -> không quét nữa")

        prompts = {p.name for p in await c.list_prompts()}
        check({"quet_zalo", "diem_tin_ceo", "loc_email_moi"} <= prompts
              and not prompts & {"xu_ly_viec_den_han", "tong_hop_cong_viec"}, "prompt mới, bỏ prompt việc")
        tools = {t.name for t in await c.list_tools()}
        check({"get_zalo_context", "save_zalo_messages", "upsert_zalo_conversation", "update_zalo_rules",
               "claim_zalo_question", "record_zalo_reply", "get_zalo_replies", "search_zalo", "get_zalo_messages",
               "get_zalo_digest", "delete_zalo_messages", "get_briefing"} <= tools
              and not tools & {"save_task", "get_due_tasks", "list_tasks", "record_zalo_action"}, "đủ 12 tool Zalo")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "zalo.db"
    old_design_db(db_path)
    env = {**os.environ, "DB_PATH": str(db_path), "MCP_PORT": str(PORT), "MCP_API_KEYS": KEY,
           "PYTHONIOENCODING": "utf-8", "TIMEZONE": "Asia/Ho_Chi_Minh"}
    # Log server ghi ra file (không dùng PIPE: traceback dài của lỗi cố ý sẽ làm đầy pipe và treo server)
    log_path = tmp / "server.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "email_report_mcp", "serve"], env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(60):
                try:
                    if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.3)
            asyncio.run(run(db_path))
            print("\nTẤT CẢ ĐỀU QUA ✅")
        except Exception:
            proc.terminate()
            proc.wait(timeout=10)
            print("---- log server ----\n" + log_path.read_text("utf-8", "replace")[-3000:])
            raise
        finally:
            proc.terminate()


if __name__ == "__main__":
    main()
