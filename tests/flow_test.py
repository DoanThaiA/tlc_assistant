"""Kiểm thử luồng chính: Apps Script đẩy mail -> chờ duyệt -> Claude lọc qua MCP -> hỏi đáp.

Chạy:  uv run python tests/flow_test.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8768
KEY = "flow-key"
BASE = f"http://127.0.0.1:{PORT}"

MAILS = [
    {"sender_email": "Nguyễn Văn An <an.nguyen@congty.vn>", "subject": "Báo cáo ngày 10/11/2026",
     "sent_at": "2026-11-10T17:45:00+07:00", "message_id": "<r1@congty.vn>", "thread_id": "th-r1",
     "body": "Chốt hợp đồng ABC giai đoạn 2. Vướng mắc: khách chậm thanh toán đợt 1."},
    {"sender_email": "mua.hang@khachhang.vn", "sender_name": "Lê Thị Mua", "subject": "Báo giá thiết bị kho",
     "sent_at": "2026-11-10T09:00:00+07:00", "message_id": "<k1@khachhang.vn>", "thread_id": "th-k",
     "body": "Đề nghị gửi báo giá 20 kệ kho trước 15/11.", "labels": ["Khách hàng"]},
    {"sender_email": "an.nguyen@congty.vn", "subject": "Re: Báo giá thiết bị kho",
     "sent_at": "2026-11-10T10:30:00+07:00", "message_id": "<k2@congty.vn>", "thread_id": "th-k",
     "body": "Em gửi báo giá 20 kệ, tổng 180 triệu, giao trong 2 tuần."},
    {"sender_email": "deals@shopee.vn", "subject": "Siêu sale 11.11 giảm 90%",
     "sent_at": "2026-11-11T08:00:00+07:00", "message_id": "<ads1@shopee>", "body": "Mua ngay kẻo lỡ!"},
    {"sender_email": "no-reply@accounts.google.com", "subject": "Cảnh báo bảo mật",
     "sent_at": "2026-11-11T08:05:00+07:00", "message_id": "<sec1@google>", "body": "Mã OTP của bạn là 123456"},
    {"sender_email": "binh.tran@congty.vn", "subject": "Báo cáo ngày 11/11/2026",
     "sent_at": "2026-11-11T17:30:00+07:00", "message_id": "<r2@congty.vn>",
     "body": "Hoàn thành module thanh toán, kiểm thử hợp đồng ABC trên app."},
]


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


def api(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def ingest(reports: list[dict]) -> dict:
    r = httpx.post(f"{BASE}/ingest", json={"source": "gmail", "reports": reports},
                   headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    r.raise_for_status()
    return r.json()


async def run() -> None:
    print("1. Apps Script đẩy mail vào")
    r = ingest(MAILS)
    check(r["inserted"] == 6 and all(x["review_status"] == "pending" for x in r["results"]), "6 email vào trạng thái chờ duyệt")

    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        api("POST", "/api/employees", {"email": "an.nguyen@congty.vn", "name": "Nguyễn Văn An",
                                       "department": "Kinh doanh"})
        api("POST", "/api/employees", {"email": "binh.tran@congty.vn", "name": "Trần Thị Bình",
                                       "department": "Kỹ thuật"})
        sc = await call(c, "start_cycle")
        check(sc["email"]["pending"] == 6 and "6 email chờ lọc" in sc["next_steps"][0],
              "start_cycle: báo có 6 email chờ lọc")

        ov = await call(c, "get_overview")
        check(ov["pending"] == 6 and ov["total_reports"] == 0, "overview: 6 chờ duyệt, 0 đã lưu")
        s = await call(c, "search_reports", query="hop dong")
        check(s["count"] == 0, "hỏi đáp chưa thấy email chờ duyệt")

        print("2. Claude lọc")
        p = await call(c, "get_pending_emails", limit=4, max_chars_each=300)
        check(p["total_pending"] == 6 and p["returned"] == 4, "get_pending_emails trả lô 4/6")
        check("LOẠI BỎ" in p["filter_rules"] and any(x["code"] == "khach_hang" for x in p["categories"]),
              "kèm quy tắc lọc + danh mục")
        first = p["emails"][0]
        check(first["subject"] == "Báo giá thiết bị kho" and first["labels"] == "Khách hàng"
              and first["is_employee"] is False, "email cũ nhất trước, có nhãn Gmail, nhận diện người ngoài")
        ids = {m["message_id"]: None for m in MAILS}
        lst = api("GET", "/api/reports?status=pending&limit=50")
        by_subject = {x["subject"]: x["id"] for x in lst["reports"]}
        ids = by_subject

        res = await call(c, "review_emails", decisions=[
            {"id": ids["Báo cáo ngày 10/11/2026"], "action": "keep", "category": "bao_cao_ngay",
             "summary": "An chốt HĐ ABC GĐ2; khách chậm thanh toán đợt 1.", "tags": ["Hợp đồng ABC", "thanh toán"]},
            {"id": ids["Báo giá thiết bị kho"], "action": "keep", "category": "khach_hang",
             "summary": "Khách hàng xin báo giá 20 kệ kho, hạn 15/11.", "tags": ["kệ kho", "báo giá"]},
            {"id": ids["Re: Báo giá thiết bị kho"], "action": "keep", "category": "khach_hang",
             "summary": "An gửi báo giá 180 triệu, giao 2 tuần.", "tags": ["kệ kho", "báo giá"]},
            {"id": ids["Siêu sale 11.11 giảm 90%"], "action": "discard", "note": "quảng cáo"},
            {"id": ids["Cảnh báo bảo mật"], "action": "discard", "note": "OTP"},
            {"id": ids["Báo cáo ngày 11/11/2026"], "action": "keep", "category": "khong_ton_tai"},
        ])
        check(res["kept"] == 3 and res["discarded"] == 2 and res["error"] == 1 and res["pending_remaining"] == 1,
              "review: giữ 3, loại 2, báo lỗi nhóm không tồn tại, còn 1 chờ")
        await call(c, "review_emails", decisions=[
            {"id": ids["Báo cáo ngày 11/11/2026"], "action": "keep", "category": "bao_cao_ngay",
             "summary": "Bình xong module thanh toán.", "tags": ["Hợp đồng ABC"]}])
        check((await call(c, "get_pending_emails"))["total_pending"] == 0, "hết email chờ duyệt")

        d = await call(c, "get_report", report_id=ids["Siêu sale 11.11 giảm 90%"])
        check(d["status"] == "discarded" and d["body_text"] == "", "email bị loại đã xoá nội dung")
        r = ingest([MAILS[3]])
        check(r["duplicate"] == 1, "Apps Script gửi lại email đã loại -> bỏ qua (trùng)")

        print("3. Hỏi đáp")
        s = await call(c, "search_reports", query="hop dong abc")
        check(s["total_matching"] == 2, "search: tìm cả trong tóm tắt/tag, không dấu")
        s = await call(c, "search_reports", query="sale")
        check(s["count"] == 0, "search: không thấy email đã loại")
        s = await call(c, "search_reports", query="bao gia", sort="oldest")
        check([x["subject"] for x in s["results"]][:2] == ["Báo giá thiết bị kho", "Re: Báo giá thiết bị kho"],
              "search sort=oldest theo dòng thời gian")
        s = await call(c, "search_reports", query="ke kho", sender_domain="khachhang.vn")
        check(s["count"] == 1, "lọc theo tên miền người gửi")

        t = await call(c, "get_thread", report_id=ids["Báo giá thiết bị kho"])
        check(t["count"] == 2 and t["emails"][1]["subject"].startswith("Re:"), "get_thread: đủ luồng theo thời gian")

        g = await call(c, "get_digest", date_from="2026-11-01", date_to="2026-11-30")
        cats = {x["category"]: x["count"] for x in g["groups"]}
        check(cats == {"bao_cao_ngay": 2, "khach_hang": 2}, "get_digest nhóm theo phân loại")
        check(g["groups"][0]["items"][0]["summary"], "digest dùng tóm tắt")

        n = await call(c, "count_emails", group_by="category")
        check({x["key"]: x["count"] for x in n["groups"]} == {"bao_cao_ngay": 2, "khach_hang": 2}, "count_emails theo nhóm")
        n = await call(c, "count_emails", group_by="tag")
        check(n["groups"][0] == {"key": "hop-dong-abc", "count": 2} or n["groups"][0]["count"] == 2, "count_emails theo tag")
        n = await call(c, "count_emails", group_by="status", status="all")
        check({x["key"]: x["count"] for x in n["groups"]} == {"kept": 4, "discarded": 2}, "count_emails theo trạng thái")
        n = await call(c, "count_emails", group_by="sender", query="bao gia")
        check(n["total"] == 2, f"count_emails kèm từ khoá {n}")

        rc = await call(c, "get_reports_content", category="bao_cao_ngay", date_from="2026-11-10", date_to="2026-11-11")
        check(rc["count"] == 2 and rc["reports"][0]["summary"], "get_reports_content lọc theo nhóm")
        l = await call(c, "get_digest", tag="ke kho", date_from="2026-11-01", date_to="2026-11-30")
        check(l["total_matching"] == 2, "get_digest lọc theo tag")

        sub = await call(c, "check_submissions", date="2026-11-10")
        check(sub["submitted_count"] == 1 and sub["total_reports"] == 1,
              "check_submissions chỉ tính nhóm bao_cao_ngay (email báo giá của An không tính)")

        a = await call(c, "find_attention_items", date_from="2026-11-01", date_to="2026-11-30")
        check(a["count"] == 1 and a["items"][0]["subject"] == "Báo cáo ngày 10/11/2026"
              and {"vuong mac", "cham thanh toan"} <= set(a["items"][0]["matched"]),
              "find_attention_items: thấy vướng mắc / chậm thanh toán, bỏ email bình thường")
        a = await call(c, "find_attention_items", date_from="2026-11-01", date_to="2026-11-30",
                       keywords=["kệ kho"], only_keywords=True)
        check(a["count"] == 2, "find_attention_items với từ khoá tự chọn")

        p = await call(c, "get_sender_profile", sender="khachhang.vn")
        check(p["matched_as"] == "domain" and p["total"] == 1 and p["recent_emails"][0]["summary"],
              "get_sender_profile theo tên miền khách hàng")
        p = await call(c, "get_sender_profile", sender="nguyen van an")
        check(p["total"] == 2 and {x["category"] for x in p["categories"]} == {"bao_cao_ngay", "khach_hang"}
              and p["top_tags"], "get_sender_profile theo tên không dấu")

        pg = api("POST", "/api/reports/purge-discarded", {"older_than_days": 0})
        check(pg["purged"] == 2, "trang quản trị dọn dấu vết 2 email đã loại")

        print("4. Quy tắc lọc")
        u = await call(c, "update_filter_rules", rules="- Luôn giữ email từ @khachhang.vn")
        check(u["filter_rules"].endswith("- Luôn giữ email từ @khachhang.vn") and "GIỮ LẠI" in u["filter_rules"],
              "update_filter_rules append")
        g = await call(c, "update_filter_rules")
        check("@khachhang.vn" in g["filter_rules"], "update_filter_rules không tham số = xem quy tắc")

        prompts = [p.name for p in await c.list_prompts()]
        check(set(prompts) == {"tro_ly_dinh_ky", "tong_hop_bao_cao_ngay", "diem_tin_ceo", "thong_ke_bao_cao"},
              "4 prompt: tro_ly_dinh_ky, tong_hop_bao_cao_ngay, diem_tin_ceo, thong_ke_bao_cao")
        pr = await c.get_prompt("thong_ke_bao_cao", {"date_from": "2026-11-01", "date_to": "2026-11-30"})
        check("từ 2026-11-01 đến 2026-11-30" in pr.messages[0].content.text, "prompt thống kê nhận khoảng ngày")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "DB_PATH": str(tmp / "flow.db"), "MCP_PORT": str(PORT), "MCP_API_KEYS": KEY,
           "PYTHONIOENCODING": "utf-8"}
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
    except Exception:
        proc.terminate()
        print("---- log server ----\n" + proc.communicate(timeout=10)[0].decode("utf-8", "replace")[-3000:])
        raise
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
