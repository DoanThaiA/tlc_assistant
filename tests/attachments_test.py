"""Kiểm thử đọc file đính kèm email + trang lịch sử trả lời của trợ lý (/api/replies).

Chạy:  uv run python tests/attachments_test.py
"""

from __future__ import annotations

import asyncio
import base64
import io
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

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8771
KEY = "att-key"
BASE = f"http://127.0.0.1:{PORT}"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def at(minutes: int = 0) -> str:
    return (datetime.now(TZ) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def api(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, f"{BASE}{path}", json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=30)
    r.raise_for_status()
    return r.json()


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


# ---------------------------------------------------------------------------
# Tạo file mẫu
# ---------------------------------------------------------------------------
def make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("Báo cáo tuần dự án Đà Nẵng")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Hạng mục", "Tiến độ"
    t.cell(1, 0).text, t.cell(1, 1).text = "Móng nhà xưởng", "80%"
    d.add_paragraph("Vướng mắc: chậm giao thép")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Doanh số"
    ws.append(["Khách hàng", "Tháng 10", "Tháng 11"])
    ws.append(["Công ty Hoà Phát", 1250, 1400.5])
    ws.append([None, None, None])
    ws.append(["Tổng", 1250, 1400.5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Kế hoạch quý 4"
    s.placeholders[1].text = "Mở rộng thị trường miền Trung"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_pdf(text: str | None) -> bytes:
    """PDF 1 trang tối giản; text=None -> trang trắng (giống PDF scan, không có lớp chữ)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def att(name: str, data: bytes, mime: str = "") -> dict:
    return {"name": name, "mime_type": mime, "size": len(data), "data": base64.b64encode(data).decode()}


# ---------------------------------------------------------------------------
def unit() -> None:
    print("1. Trích văn bản từng định dạng (không cần server)")
    from email_report_mcp import attachments as A

    r = A.extract("bc.docx", "", make_docx())
    check(r["status"] == "ok" and "Móng nhà xưởng | 80%" in r["text"] and r["text"].index("Báo cáo tuần")
          < r["text"].index("Móng") < r["text"].index("Vướng mắc"), "docx: đoạn văn + bảng đúng thứ tự")
    r = A.extract("ds.xlsx", "", make_xlsx())
    check(r["status"] == "ok" and "## Sheet: Doanh số" in r["text"] and "Công ty Hoà Phát | 1250 | 1400.5" in r["text"],
          "xlsx: tên sheet + dòng dữ liệu, số nguyên không có .0")
    r = A.extract("kh.pptx", "", make_pptx())
    check(r["status"] == "ok" and "Kế hoạch quý 4" in r["text"] and "miền Trung" in r["text"], "pptx: chữ trong slide")
    r = A.extract("bc.pdf", "application/pdf", make_pdf("Doanh so thang 10 dat 1250 trieu"))
    check(r["status"] == "ok" and "Doanh so thang 10" in r["text"], "pdf có lớp chữ")
    r = A.extract("scan.pdf", "application/pdf", make_pdf(None))
    check(r["status"] == "no_text" and "OCR" in r["note"], "pdf scan (không có chữ) -> no_text, gợi ý OCR")
    r = A.extract("ds.csv", "", "Tên;Số lượng\nBàn;12\n".encode("utf-8-sig"))
    check(r["status"] == "ok" and "Bàn | 12" in r["text"], "csv dấu chấm phẩy, có BOM")
    check(A.extract("a.zip", "", b"PK")["status"] == "unsupported", "zip -> chưa hỗ trợ")
    r = A.extract("hong.xlsx", "", b"khong phai file excel")
    check(r["status"] == "error" and r["note"], "file hỏng -> error, không làm hỏng cả lô")
    check(A.process("chi-ten.pdf") == {"name": "chi-ten.pdf", "status": "name_only"}, "chỉ có tên (client cũ)")
    check(A.process({"name": "to.pdf", "size": 30_000_000, "skipped": "too_large"})["status"] == "too_large",
          "Apps Script bỏ qua file quá lớn")


async def flow() -> None:
    print("2. /ingest kèm file -> chờ duyệt -> giữ -> tìm kiếm trong file")
    res = api("POST", "/ingest", {"source": "gmail", "reports": [{
        "message_id": "<att-1@test>", "sender_email": "An <an@congty.vn>", "subject": "Báo cáo tuần 46",
        "body": "Em gửi anh báo cáo tuần, chi tiết trong file đính kèm.", "sent_at": at(-60),
        "attachments": [att("BC tuan 46.docx", make_docx()), att("Doanh so.xlsx", make_xlsx()),
                        att("ban-ve.pdf", make_pdf(None), "application/pdf"),
                        {"name": "anh.jpg", "mime_type": "image/jpeg", "size": 1000, "skipped": "unsupported"}],
    }]})
    check(res["inserted"] == 1 and res["failed"] == 0, "nhận email có 4 file đính kèm")
    rid = res["results"][0]["id"]

    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        p = await call(c, "get_pending_emails")
        atts = {a["name"]: a for a in p["emails"][0]["attachments"]}
        check("Móng nhà xưởng" in atts["BC tuan 46.docx"]["text"] and atts["BC tuan 46.docx"]["read_status"] == "đã đọc",
              "get_pending_emails: có văn bản file để Claude lọc")
        check("ảnh scan" in atts["ban-ve.pdf"]["read_status"] and "text" not in atts["ban-ve.pdf"]
              and "chưa hỗ trợ" in atts["anh.jpg"]["read_status"], "read_status nói rõ file không đọc được vì sao")
        await call(c, "review_emails", decisions=[{"id": rid, "action": "keep", "category": "bao_cao_ngay",
                                                    "summary": "Báo cáo tuần 46"}])
        s = await call(c, "search_reports", query="mong nha xuong")
        check(s["count"] == 1 and s["results"][0]["matched_in_attachment"] == "BC tuan 46.docx"
              and "[Đính kèm BC tuan 46.docx]" in s["results"][0]["snippet"], "search_reports tìm thấy chữ trong file docx")
        s = await call(c, "search_reports", query="Hoà Phát")
        check(s["count"] == 1 and s["results"][0]["matched_in_attachment"] == "Doanh so.xlsx", "tìm thấy trong file Excel")
        s = await call(c, "search_reports", query="chi tiet file dinh kem")
        check(s["count"] == 1 and "matched_in_attachment" not in s["results"][0], "khớp thân email -> đoạn trích từ thân")
        r = await call(c, "get_report", report_id=rid)
        check(len(r["attachments"]) == 4 and "1400.5" in r["attachments"][1]["text"], "get_report: văn bản đầy đủ từng file")
        g = await call(c, "get_reports_content", query="hoa phat")
        check("Hoà Phát" in g["reports"][0]["attachments"][1]["text"], "get_reports_content kèm văn bản file")
        att_hits = await call(c, "find_attention_items", date_from=at(-24 * 60)[:10])
        check(any(x["id"] == rid for x in att_hits["items"]), "find_attention_items thấy 'vướng mắc' trong file")

    print("3. Bổ sung nội dung file cho email cũ (backfillAttachments)")
    old = {"message_id": "<att-2@test>", "sender_email": "binh@congty.vn", "subject": "Kế hoạch Q4",
           "body": "Gửi anh kế hoạch.", "sent_at": at(-30), "attachments": ["KH Q4.pptx"]}
    res = api("POST", "/ingest", {"source": "gmail", "reports": [old]})
    rid2 = res["results"][0]["id"]
    check(api("GET", f"/api/reports/{rid2}")["attachments"][0]["status"] == "name_only", "email cũ chỉ có tên file")
    res = api("POST", "/ingest", {"source": "gmail", "reports": [{**old, "attachments": [att("KH Q4.pptx", make_pptx())]}]})
    check(res["duplicate"] == 1 and res["attachments_updated"] == 1, "gửi lại kèm file -> trùng nhưng bổ sung văn bản")
    a = api("GET", f"/api/reports/{rid2}")["attachments"]
    check(len(a) == 1 and "miền Trung" in a[0]["text"], "file được thay bằng bản có văn bản, không nhân đôi")
    res = api("POST", "/ingest", {"source": "gmail", "reports": [{**old, "attachments": [att("KH Q4.pptx", make_pptx())]}]})
    check(res["attachments_updated"] == 0, "gửi lại lần nữa: không đổi gì")
    api("POST", "/api/reports/review", {"decisions": [{"id": rid2, "action": "discard"}]})
    res = api("POST", "/ingest", {"source": "gmail", "reports": [{**old, "attachments": [att("KH Q4.pptx", make_pptx())]}]})
    check(res["attachments_updated"] == 0 and api("GET", f"/api/reports/{rid2}")["attachments"] == [],
          "email đã loại: không lưu lại nội dung file")


async def replies() -> None:
    print("4. Lịch sử trả lời của trợ lý (/api/replies)")
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        await call(c, "upsert_zalo_conversation", name="Doãn Quốc Thái", kind="direct", reply=True,
                   allowed_askers=["Doãn Quốc Thái"])
        await call(c, "upsert_zalo_conversation", name="Trợ lý CEO TLC", kind="group", reply=True)
        q1 = await call(c, "claim_zalo_question", conversation="Doãn Quốc Thái", sender="Doãn Quốc Thái",
                        sent_at=at(-20), text="Hôm nay ai chưa nộp báo cáo?")
        await call(c, "record_zalo_reply", reply_id=q1["reply_id"], status="sent",
                   reply_text="Dạ anh, hôm nay còn 2 bạn chưa nộp: An, Bình.", source_report_ids=[1])
        q2 = await call(c, "claim_zalo_question", conversation="Trợ lý CEO TLC", sender="Lê Minh",
                        sent_at=at(-10), text="Lương tháng này của Bình bao nhiêu?")
        await call(c, "record_zalo_reply", reply_id=q2["reply_id"], status="skipped", note="Thông tin lương — từ chối")
        await call(c, "claim_zalo_question", conversation="Trợ lý CEO TLC", sender="Lê Minh",
                   sent_at=at(-5), text="Tiến độ dự án Đà Nẵng thế nào?")

    lst = api("GET", "/api/replies")
    check(lst["total"] == 3 and [r["status"] for r in lst["replies"]] == ["claimed", "skipped", "sent"],
          "danh sách mới nhất trước, đủ 3 trạng thái")
    sent = lst["replies"][2]
    check(sent["sources"] == 1 and sent["response_minutes"] is not None and sent["response_minutes"] >= 19,
          "có số nguồn + thời gian phản hồi (phút)")
    check(api("GET", "/api/replies?q=chua nop")["total"] == 1, "tìm không dấu trong câu hỏi")
    check(api("GET", "/api/replies?q=tu choi")["total"] == 1, "tìm trong ghi chú")
    check(api("GET", "/api/replies?status=sent")["total"] == 1, "lọc theo trạng thái")
    check(api("GET", "/api/replies?asker=le minh")["total"] == 2, "lọc theo người hỏi")
    conv_id = next(x["id"] for x in api("GET", "/api/zalo/conversations")["conversations"] if x["name"] == "Trợ lý CEO TLC")
    check(api("GET", f"/api/replies?conversation_id={conv_id}")["total"] == 2, "lọc theo hội thoại")
    st = api("GET", "/api/replies/stats")
    check(st["total"] == 3 and st["by_status"] == {"claimed": 1, "sent": 1, "skipped": 1}
          and st["response_minutes"]["count"] == 1 and st["per_day"] and st["by_conversation"][0]["name"] == "Trợ lý CEO TLC"
          and st["top_askers"][0] == {"asker": "Lê Minh", "total": 2, "sent": 0}, "thống kê: trạng thái, ngày, hội thoại, người hỏi")
    d = api("GET", f"/api/replies/{sent['id']}")
    check(d["reply_text"].startswith("Dạ anh") and d["source_reports"][0]["id"] == 1
          and d["source_reports"][0]["subject"] == "Báo cáo tuần 46", "chi tiết: câu trả lời + email nguồn")
    r = httpx.get(f"{BASE}/api/replies/99999", headers={"Authorization": f"Bearer {KEY}"})
    check(r.status_code == 404, "không có câu hỏi -> 404")
    check(httpx.get(f"{BASE}/api/replies").status_code == 401, "chưa đăng nhập -> 401")
    js = httpx.get(f"{BASE}/assets/app.js").text
    check("viewReplies" in js and "Trợ lý trả lời" in httpx.get(f"{BASE}/").text, "giao diện có tab Trợ lý trả lời")


def main() -> None:
    unit()
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "MCP_API_KEYS": KEY, "MCP_PORT": str(PORT), "MCP_HOST": "127.0.0.1",
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
        asyncio.run(flow())
        asyncio.run(replies())
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
