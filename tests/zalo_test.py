"""Kiểm thử luồng Zalo: quét tin -> lưu việc được giao -> trả lời -> đến hạn thì xử lý.

Chạy:  uv run python tests/zalo_test.py
"""

from __future__ import annotations

import asyncio
import os
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


async def run() -> None:
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        print("1. Cài đặt & hội thoại")
        ctx = await call(c, "get_zalo_context")
        check(ctx["conversations"] == [] and "LƯU VIỆC KHI" in ctx["rules"]
              and ctx["defaults"]["lead_minutes"] == 30, "context ban đầu: chưa có hội thoại, có quy tắc mặc định")
        conv = await call(c, "upsert_zalo_conversation", name="Anh Hùng GĐ", auto_reply=True,
                          note="Giám đốc, xưng em gọi anh")
        check(conv["watch"] and conv["auto_reply"] and conv["kind"] == "direct", "thêm hội thoại nhắn riêng")
        await call(c, "upsert_zalo_conversation", name="Nhóm Kinh doanh", kind="group")
        conv = await call(c, "upsert_zalo_conversation", name="anh hung gd", note="Giám đốc")
        check(conv["name"] == "anh hung gd" and conv["auto_reply"], "sửa hội thoại theo tên không dấu, giữ quyền cũ")
        await call(c, "upsert_zalo_conversation", name="Anh Hùng GĐ")
        s = await call(c, "update_zalo_rules", owner_name="Trợ lý TLC", lead_minutes=30,
                       rules="- Không trả lời về giá bán cho khách lẻ")
        check(s["defaults"]["owner_name"] == "Trợ lý TLC" and s["rules"].endswith("khách lẻ"),
              "update_zalo_rules: cài đặt + thêm quy tắc")
        check(len((await call(c, "get_zalo_context"))["conversations"]) == 2, "context có 2 hội thoại theo dõi")
        err = await fails(c, "check_zalo_messages", conversation="Người lạ", messages=[])
        check("Chưa đăng ký" in err, "hội thoại chưa đăng ký -> báo lỗi")

        # Email làm dữ liệu trả lời
        await call(c, "save_reports", reports=[{
            "sender_email": "an.nguyen@congty.vn", "sender_name": "Nguyễn Văn An",
            "subject": "Doanh số tuần 45", "body": "Doanh số tuần 45 đạt 1,2 tỷ, tăng 8% so với tuần 44.",
            "sent_at": at(-120), "message_id": "<ds45@congty.vn>", "category": "bao_cao_ngay",
            "summary": "Doanh số tuần 45: 1,2 tỷ (+8%)."}])

        print("2. Quét tin mới")
        msgs = [
            {"sender": "Anh Hùng", "sent_at": at(-15), "text": "Chào em"},
            {"sender": "Anh Hùng", "sent_at": at(-10),
             "text": "Em tổng hợp doanh số tuần 45 gửi anh trước 20 phút nữa nhé"},
            {"sender": "Anh Hùng", "sent_at": at(-5), "text": "Hợp đồng ABC ký chưa em?"},
        ]
        r = await call(c, "check_zalo_messages", conversation="Anh Hùng GĐ", messages=msgs)
        check(len(r["new_messages"]) == 3 and r["auto_reply"], "3 tin mới, hội thoại được tự trả lời")

        t = await call(c, "save_task", title="Tổng hợp doanh số tuần 45 gửi anh Hùng", conversation="Anh Hùng GĐ",
                       assigner="Anh Hùng", content=msgs[1]["text"], sent_at=msgs[1]["sent_at"],
                       due_at=at(20), due_text="trước 20 phút nữa", kind="info", tags=["doanh số"])
        check(t["saved"] and t["task"]["status"] == "open" and t["task"]["fire_at"] < t["task"]["due_at"]
              and "task_saved" in t["next_step"], "save_task: lưu việc, fire_at = hạn - 30 phút")
        task_id = t["task"]["id"]
        d = await call(c, "save_task", title="trùng", conversation="Anh Hùng GĐ", assigner="Anh Hùng",
                       content=msgs[1]["text"], sent_at=msgs[1]["sent_at"])
        check(d["duplicate"] and d["task"]["id"] == task_id, "save_task cùng tin -> trùng, không lưu lại")
        await call(c, "record_zalo_action", conversation="Anh Hùng GĐ", action="task_saved", task_id=task_id,
                   sender="Anh Hùng", sent_at=msgs[1]["sent_at"], message_text=msgs[1]["text"],
                   reply_text="Em đã nhận: tổng hợp doanh số tuần 45.")
        a = await call(c, "record_zalo_action", conversation="Anh Hùng GĐ", action="answered", sender="Anh Hùng",
                       sent_at=msgs[2]["sent_at"], message_text=msgs[2]["text"], reply_text="Dạ chưa ạ.")
        check(a["recorded"], "record_zalo_action: ghi câu đã trả lời")
        a = await call(c, "record_zalo_action", conversation="Anh Hùng GĐ", action="answered", sender="Anh Hùng",
                       sent_at=msgs[2]["sent_at"], message_text=msgs[2]["text"], reply_text="lần 2")
        check(a["duplicate"], "trả lời lại cùng tin -> duplicate")

        r = await call(c, "check_zalo_messages", conversation="Anh Hùng GĐ", messages=msgs)
        check([m["index"] for m in r["new_messages"]] == [0], "quét lại: chỉ còn tin chào chưa xử lý")
        await call(c, "mark_zalo_scanned", conversation="Anh Hùng GĐ", last_sender="Anh Hùng",
                   last_sent_at=msgs[2]["sent_at"], last_text=msgs[2]["text"])
        r = await call(c, "check_zalo_messages", conversation="Anh Hùng GĐ", messages=msgs)
        check(r["new_messages"] == [] and len(r["already_handled"]) == 3, "sau mark_zalo_scanned: không còn tin mới")
        newer = {"sender": "Anh Hùng", "sent_at": at(0), "text": "Mai 9h em nhắc anh họp với khách ABC nhé"}
        r = await call(c, "check_zalo_messages", conversation="Anh Hùng GĐ", messages=[*msgs, newer])
        check([m["index"] for m in r["new_messages"]] == [3], "tin mới sau mốc được nhận ra")

        tomorrow = (datetime.now(TZ) + timedelta(days=1)).date().isoformat()
        t2 = await call(c, "save_task", title="Nhắc anh Hùng họp khách ABC", conversation="Anh Hùng GĐ",
                        assigner="Anh Hùng", content=newer["text"], sent_at=newer["sent_at"],
                        due_at=f"{tomorrow}T09:00", kind="action", priority="cao", lead_minutes=15)
        t2_id = t2["task"]["id"]
        check(t2["task"]["due_at"].endswith("09:00:00+07:00") and t2["task"]["fire_at"].endswith("08:45:00+07:00"),
              "giờ không có múi giờ -> giờ Việt Nam, nhắc trước 15 phút")
        t3 = await call(c, "save_task", title="Gửi hồ sơ năng lực", due_at=tomorrow)
        check(t3["task"]["due_at"].endswith("17:00:00+07:00") and t3["next_step"] is None,
              "hạn chỉ có ngày -> 17:00; việc nhập tay không cần nhắn xác nhận")

        print("3. Đến hạn")
        due = await call(c, "get_due_tasks")
        check(due["count"] == 1 and due["tasks"][0]["id"] == task_id
              and due["tasks"][0]["conversation"] == "Anh Hùng GĐ", "get_due_tasks: việc tới fire_at")
        check(any(e["subject"] == "Doanh số tuần 45" for e in due["tasks"][0]["suggested_emails"]),
              "get_due_tasks gợi ý email liên quan")
        email_id = next(e["id"] for e in due["tasks"][0]["suggested_emails"] if e["subject"] == "Doanh số tuần 45")
        mins = (datetime.fromisoformat(t2["task"]["fire_at"]) - datetime.now(TZ)).total_seconds() / 60
        soon = [x["id"] for x in (await call(c, "get_due_tasks", within_minutes=1440))["tasks"]]
        check(task_id in soon and ((t2_id in soon) == (mins <= 1440)), "within_minutes lấy cả việc sắp tới")
        err = await fails(c, "record_zalo_action", action="task_reply")
        check("task_id" in err, "task_reply thiếu task_id -> báo lỗi")
        rr = await call(c, "record_zalo_action", action="task_reply", task_id=task_id,
                        reply_text="Doanh số tuần 45 đạt 1,2 tỷ (+8%) ạ.", source_report_ids=[email_id])
        check(rr["recorded"], "task_reply ghi nhận (tự lấy hội thoại gốc)")
        check((await call(c, "record_zalo_action", action="task_reply", task_id=task_id))["duplicate"],
              "task_reply lần 2 -> duplicate")
        check((await call(c, "get_due_tasks"))["count"] == 0, "việc đã trả lời không còn đến hạn")
        g = await call(c, "get_task", task_id=task_id)
        check(g["status"] == "done" and g["replied_at"] and g["related_report_ids"] == [email_id]
              and [e["type"] for e in g["events"]] == ["created", "replied"]
              and len(g["zalo_messages_sent"]) == 2 and g["related_emails"][0]["id"] == email_id,
              "get_task: xong, có lịch sử, tin đã gửi, email nguồn")

        print("4. Gia hạn, nhắc, quá hạn")
        u = await call(c, "update_task", task_id=t2_id, due_at=at(-60), note="Anh Hùng dời họp lên sớm")
        check(u["is_overdue"] and u["fired_at"] is None, "update_task đổi hạn -> xử lý lại theo hạn mới")
        due = await call(c, "get_due_tasks")
        check([x["id"] for x in due["tasks"]] == [t2_id], "việc action đã tới hạn")
        await call(c, "record_zalo_action", conversation="Cloud của tôi", action="reminder", task_id=t2_id,
                   reply_text="Nhắc việc: họp khách ABC")
        due = await call(c, "get_due_tasks")
        check(due["count"] == 0 and [x["id"] for x in due["still_open_after_fired"]] == [t2_id],
              "đã nhắc: không nhắc lại, nhưng vẫn báo quá hạn chưa xong")
        check([e["type"] for e in (await call(c, "get_task", task_id=t2_id))["events"]]
              == ["created", "updated", "note", "reminded"], "lịch sử: tạo, sửa, ghi chú, nhắc")

        print("5. Hỏi đáp việc")
        lt = await call(c, "list_tasks")
        check([x["id"] for x in lt["tasks"]] == [t2_id, t3["task"]["id"]], "list_tasks: việc chưa xong, hạn gần trước")
        check((await call(c, "list_tasks", overdue_only=True))["total"] == 1, "list_tasks quá hạn")
        check((await call(c, "list_tasks", status="all", query="doanh so"))["total"] == 1, "list_tasks tìm không dấu")
        check((await call(c, "list_tasks", status="all", assigner="hung"))["total"] == 2, "list_tasks theo người giao")
        check((await call(c, "list_tasks", status="all", conversation="hung gd", kind="action"))["total"] == 1,
              "list_tasks theo hội thoại + loại")
        await call(c, "update_task", task_id=t2_id, status="done", result="Đã nhắc, anh Hùng đã họp")
        ov = await call(c, "get_overview")
        check(ov["zalo_tasks"]["by_status"]["done"] == 2 and ov["zalo_tasks"]["open"] == 1
              and ov["zalo_tasks"]["watched_conversations"] == 2, "get_overview có thống kê việc Zalo")
        check(not any(x["name"] == "Cloud của tôi" for x in (await call(c, "get_zalo_context"))["conversations"]),
              "hội thoại tự tạo khi nhắc việc không bị đưa vào danh sách quét")

        prompts = [p.name for p in await c.list_prompts()]
        check({"quet_zalo", "xu_ly_viec_den_han", "tong_hop_cong_viec"} <= set(prompts), "có 3 prompt Zalo")
        tools = {t.name for t in await c.list_tools()}
        check({"get_zalo_context", "check_zalo_messages", "record_zalo_action", "mark_zalo_scanned",
               "upsert_zalo_conversation", "update_zalo_rules", "save_task", "get_due_tasks", "update_task",
               "list_tasks", "get_task"} <= tools, "đủ 11 tool Zalo")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "DB_PATH": str(tmp / "zalo.db"), "MCP_PORT": str(PORT), "MCP_API_KEYS": KEY,
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
            asyncio.run(run())
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
