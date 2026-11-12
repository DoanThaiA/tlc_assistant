"""Kiểm thử đầu-cuối: chạy server HTTP thật trên CSDL tạm, nạp dữ liệu mẫu qua tool và gọi các tool hỏi đáp.

Chạy:  uv run python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8765
KEY = "test-key-123"
BASE = f"http://127.0.0.1:{PORT}"

SAMPLE = [
    {"sender_email": "Nguyễn Văn An <an.nguyen@congty.vn>", "subject": "Báo cáo ngày 10/11/2026",
     "sent_at": "2026-11-10T17:45:00+07:00", "message_id": "<m1@congty.vn>",
     "body": "1. Hoàn thành hợp đồng ABC với khách hàng Đà Nẵng\n2. Gặp đối tác XYZ\nVướng mắc: chậm thanh toán đợt 2",
     "attachments": ["bao_cao.xlsx"]},
    {"sender_email": "binh.tran@congty.vn", "sender_name": "Trần Thị Bình", "subject": "Report 10/11",
     "sent_at": "Mon, 10 Nov 2026 18:05:00 +0700", "message_id": "<m2@congty.vn>",
     "body": "<html><body><p>Hôm nay:</p><ul><li>Fix lỗi đăng nhập app mobile</li><li>Review code module thanh toán</li></ul>"
             "<p>On Mon, 10 Nov 2026 at 9:00 Sếp wrote:</p><p>&gt; nhớ gửi báo cáo</p></body></html>"},
    # báo cáo của ngày 10 nhưng gửi sáng 11 -> report_date lấy từ tiêu đề
    {"sender_email": "cuong.le@congty.vn", "sender_name": "Lê Văn Cường", "subject": "BC ngày 10.11.2026",
     "sent_at": "2026-11-11T08:10:00+07:00",
     "body": "Khảo sát kho Bình Dương, dự án trễ tiến độ 3 ngày do thiếu vật tư."},
    {"sender_email": "an.nguyen@congty.vn", "subject": "Báo cáo công việc",
     "sent_at": "2026-11-11T17:30:00+07:00", "message_id": "<m4@congty.vn>",
     "body": "Ký phụ lục hợp đồng ABC. Chuẩn bị demo cho khách Hà Nội."},
]


def wait_health(timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("Server không khởi động")


async def call(c: Client, tool: str, **args):
    res = await c.call_tool(tool, args)
    return res.structured_content if res.structured_content is not None else res.data


async def run_tests() -> None:
    # 1) Không có key -> 401
    req = urllib.request.Request(f"{BASE}/mcp", data=b"{}", method="POST",
                                 headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("Phải bị chặn 401")
    except urllib.error.HTTPError as e:
        assert e.code == 401, e.code
    print("✓ chặn request không có API key (401)")

    # 2) Key qua query string (kiểu URL connector trên claude.ai)
    async with Client(StreamableHttpTransport(f"{BASE}/mcp?key={KEY}")) as c:
        tools = sorted(t.name for t in await c.list_tools())
        print("✓ tools:", tools)

        r = await call(c, "save_reports", reports=SAMPLE, source="test")
        print("✓ save_reports:", {k: r[k] for k in ("inserted", "duplicate", "failed")})
        assert r["inserted"] == 4 and r["failed"] == 0, r
        dates = [x["report_date"] for x in r["results"]]
        assert dates == ["2026-11-10", "2026-11-10", "2026-11-10", "2026-11-11"], dates

        r = await call(c, "save_reports", reports=SAMPLE[:2])
        assert r["duplicate"] == 2 and r["inserted"] == 0, r
        print("✓ gửi lại -> bỏ qua trùng")

        bad = await call(c, "save_reports", reports=[{"sender_email": "khong-hop-le", "body": "x"}])
        assert bad["failed"] == 1
        print("✓ dòng lỗi được báo riêng:", bad["results"][0]["error"])

    # 3) Key qua header Bearer
    async with Client(StreamableHttpTransport(f"{BASE}/mcp", headers={"Authorization": f"Bearer {KEY}"})) as c:
        for e in [("an.nguyen@congty.vn", "Nguyễn Văn An", "Kinh doanh"),
                  ("binh.tran@congty.vn", "Trần Thị Bình", "Kỹ thuật"),
                  ("cuong.le@congty.vn", "Lê Văn Cường", "Vận hành"),
                  ("dung.pham@congty.vn", "Phạm Văn Dũng", "Kinh doanh")]:
            await call(c, "upsert_employee", email=e[0], name=e[1], department=e[2])

        cp = await call(c, "get_import_checkpoint")
        assert cp["latest_sent_at"] == "2026-11-11T17:30:00+07:00", cp
        print("✓ checkpoint:", cp["latest_sent_at"])

        s = await call(c, "search_reports", query="da nang hop dong")
        assert s["count"] == 1 and "Đà Nẵng" in s["results"][0]["snippet"], s
        print("✓ tìm không dấu 'da nang hop dong' ->", s["results"][0]["snippet"][:60])

        s = await call(c, "search_reports", query="tre tien do")
        assert s["count"] == 1 and s["results"][0]["employee_name"] == "Lê Văn Cường", s
        print("✓ tìm 'tre tien do' ->", s["results"][0]["employee_name"])

        d = await call(c, "get_reports_content", date_from="2026-11-10", date_to="2026-11-10")
        assert d["count"] == 3
        binh = next(x for x in d["reports"] if x["sender_email"] == "binh.tran@congty.vn")
        assert "Fix lỗi đăng nhập" in binh["body_text"] and "nhớ gửi" not in binh["body_text"], binh["body_text"]
        print("✓ get_reports_content 10/11: 3 báo cáo, HTML đã làm sạch, bỏ phần trích dẫn")

        d = await call(c, "get_reports_content", date_from="2026-11-01", date_to="2026-11-30", employee="nguyen van an")
        assert d["count"] == 2, d
        d = await call(c, "list_reports", employee="kinh doanh")
        assert d["total"] == 2, d
        print("✓ lọc nhân viên theo tên không dấu / phòng ban")

        sub = await call(c, "check_submissions", date="2026-11-10")
        assert sub["submitted_count"] == 3 and [m["email"] for m in sub["missing"]] == ["dung.pham@congty.vn"], sub
        print("✓ check_submissions 10/11: chưa nộp =", [m["name"] for m in sub["missing"]])

        st = await call(c, "report_stats", date_from="2026-11-09", date_to="2026-11-11")
        dung = next(x for x in st["compliance"] if x["email"] == "dung.pham@congty.vn")
        assert dung["missing_days"] == ["2026-11-09", "2026-11-10", "2026-11-11"], dung
        print("✓ report_stats: ngày làm việc =", st["work_days_in_range"])

        rid = binh["id"]
        full = await call(c, "get_report", report_id=rid)
        assert full["subject"] == "Report 10/11"
        assert (await call(c, "delete_report", report_id=rid))["deleted"] is True
        print("✓ get_report / delete_report")

        ov = await call(c, "get_overview")
        print("✓ overview:", json.dumps(ov, ensure_ascii=False))

        prompts = await c.list_prompts()
        assert "tong_hop_bao_cao_ngay" in [p.name for p in prompts]
        print("✓ prompt:", prompts[0].name)

    # 4) REST /ingest (dùng cho Google Apps Script)
    def post_ingest(payload: dict, key: str | None = KEY) -> tuple[int, dict]:
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"{BASE}/ingest", data=json.dumps(payload).encode(), method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    assert post_ingest({"reports": []}, key=None)[0] == 401
    assert post_ingest({"reports": []}, key="sai")[0] == 401
    code, body = post_ingest({"reports": []})
    assert code == 200 and body["received"] == 0, body
    code, body = post_ingest({"foo": 1})
    assert code == 400, body
    code, body = post_ingest({"reports": [{"subject": "thiếu body"}]})
    assert code == 400, body
    code, body = post_ingest({"source": "gmail", "reports": [
        {"sender_email": "Phạm Văn Dũng <dung.pham@congty.vn>", "subject": "Báo cáo ngày 11/11/2026",
         "body": "Gặp khách hàng Hải Phòng", "sent_at": "2026-11-11T18:00:00.000Z", "message_id": "<g1@mail>"}]})
    assert code == 200 and body["inserted"] == 1, body
    code, body = post_ingest({"reports": [{"sender_email": "dung.pham@congty.vn", "body": "x", "message_id": "<g1@mail>"}]})
    assert body["duplicate"] == 1, body
    print("✓ /ingest: 401 khi sai key, 400 khi sai dữ liệu, lưu + chống trùng OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "DB_PATH": str(tmp / "test.db"), "MCP_PORT": str(PORT), "MCP_API_KEYS": KEY,
           "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "email_report_mcp", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_health()
        asyncio.run(run_tests())
        print("\nTẤT CẢ ĐỀU QUA ✅")
    except Exception:
        proc.terminate()
        out = proc.communicate(timeout=10)[0].decode("utf-8", "replace")
        print("---- log server ----\n" + out[-4000:])
        raise
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
