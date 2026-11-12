"""Kiểm thử trang quản trị + REST API /api/* trên server thật với CSDL tạm.

Chạy:  uv run python tests/admin_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2 as httpx

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

PORT = 8766
KEY = "test-key-admin"
PW = "mat-khau-quan-tri"
BASE = f"http://127.0.0.1:{PORT}"


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


def main() -> None:
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "MCP_API_KEYS": KEY, "ADMIN_PASSWORD": PW, "MCP_PORT": str(PORT),
           "MCP_HOST": "127.0.0.1", "DB_PATH": str(Path(tmp) / "t.db"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "email_report_mcp", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        run()
        print("\nTẤT CẢ ĐỀU QUA ✅")
    finally:
        proc.terminate()
        proc.wait(10)


def run() -> None:
    anon = httpx.Client(base_url=BASE, timeout=10)
    print("Giao diện & xác thực")
    r = anon.get("/")
    check(r.status_code == 200 and "Quản lý email công việc" in r.text, "GET / trả trang giao diện (công khai)")
    check("frame-ancestors 'none'" in r.headers.get("content-security-policy", ""), "có header CSP")
    check(anon.get("/assets/app.js").status_code == 200, "GET /assets/app.js")
    check(anon.get("/assets/server.py").status_code == 404 and anon.get("/assets/../server.py").status_code in (401, 404), "không đọc được file ngoài danh sách asset")
    check(anon.get("/api/me").status_code == 401, "/api/me chưa đăng nhập -> 401")
    check(anon.get("/mcp").status_code == 401, "/mcp vẫn cần API key")
    check(anon.post("/api/login", json={"password": "sai"}).status_code == 401, "sai mật khẩu -> 401")
    check(anon.post("/api/login", content="password=" + PW,
                    headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 415,
          "login dạng form (không phải JSON) bị chặn -> 415")
    r = anon.post("/api/login", json={"password": PW})
    check(r.status_code == 200 and "erm_session" in r.headers.get("set-cookie", ""), "đăng nhập đúng -> có cookie")
    sc = r.headers["set-cookie"].lower()
    check("httponly" in sc and "samesite=strict" in sc, "cookie HttpOnly + SameSite=Strict")
    me = anon.get("/api/me").json()
    check(me["authenticated"] and me["mcp_path"] == "/mcp", "/api/me sau đăng nhập")
    check(anon.post("/mcp", json={}).status_code == 401, "cookie phiên KHÔNG mở được /mcp")
    bad = httpx.Client(base_url=BASE, cookies={"erm_session": "9999999999.x.giamao"})
    check(bad.get("/api/me").status_code == 401, "cookie giả mạo bị từ chối")
    keyc = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {KEY}"})
    check(keyc.get("/api/me").status_code == 200, "API cũng nhận API key (Bearer)")

    c = anon
    print("Nhân viên")
    r = c.post("/api/employees", json={"email": "An.Nguyen@congty.vn", "name": "Nguyễn Văn An", "department": "Kinh doanh"})
    check(r.status_code == 201 and r.json()["email"] == "an.nguyen@congty.vn", "thêm nhân viên")
    an_id = r.json()["id"]
    check(c.post("/api/employees", json={"email": "an.nguyen@congty.vn", "name": "X"}).status_code == 409, "trùng email -> 409")
    check(c.post("/api/employees", json={"email": "khong-hop-le", "name": "X"}).status_code == 400, "email sai -> 400")
    r = c.post("/api/employees/import", json={"csv": "email,name,department\nbinh.tran@congty.vn,Trần Thị Bình,Kỹ thuật\nloi,,\n"})
    check(r.json()["imported"] == 1 and len(r.json()["errors"]) == 1, "nhập CSV: 1 thành công, 1 lỗi")
    r = c.put(f"/api/employees/{an_id}", json={"department": "Kinh doanh 2", "active": False})
    check(r.json()["department"] == "Kinh doanh 2" and r.json()["active"] == 0, "sửa nhân viên")
    lst = c.get("/api/employees", params={"q": "binh"}).json()
    check(len(lst) == 1 and lst[0]["name"] == "Trần Thị Bình", "tìm nhân viên không dấu")
    check(len(c.get("/api/employees", params={"include_inactive": 0}).json()) == 1, "lọc bỏ người ngừng theo dõi")

    print("Báo cáo")
    r = c.post("/api/reports", json={"sender_email": "binh.tran@congty.vn", "subject": "Báo cáo ngày 11/11/2026",
                                     "sent_at": "2026-11-11T17:00:00+07:00", "body": "Sửa lỗi đăng nhập, hợp đồng ABC"})
    check(r.status_code == 201 and r.json()["report_date"] == "2026-11-11" and r.json()["source"] == "manual", "thêm báo cáo thủ công")
    rid = r.json()["id"]
    r = c.post("/api/reports", json={"sender_email": "an.nguyen@congty.vn", "message_id": "<x@1>",
                                     "sent_at": "2026-11-10T17:00:00+07:00", "body": "Gặp khách Đà Nẵng"})
    rid2 = r.json()["id"]
    check(c.post("/api/reports", json={"sender_email": "a@b.vn", "message_id": "<x@1>", "body": "x"}).status_code == 409,
          "trùng message_id -> 409")
    check(c.post("/api/reports", json={"sender_email": "a@b.vn", "body": "  "}).status_code == 400, "nội dung trống -> 400")
    d = c.get("/api/reports", params={"q": "hop dong"}).json()
    check(d["total"] == 1 and d["reports"][0]["id"] == rid, "tìm báo cáo không dấu qua q")
    r = c.put(f"/api/reports/{rid}", json={"body_text": "Nội dung mới: triển khai Kho Bình Dương", "report_date": "2026-11-09"})
    check(r.json()["report_date"] == "2026-11-09", "sửa báo cáo")
    check(c.get("/api/reports", params={"q": "hop dong"}).json()["total"] == 0, "chỉ mục tìm kiếm cập nhật (từ cũ hết khớp)")
    check(c.get("/api/reports", params={"q": "binh duong"}).json()["total"] == 1, "chỉ mục tìm kiếm cập nhật (từ mới khớp)")
    check(c.put(f"/api/reports/{rid}", json={"report_date": "09/11"}).status_code == 400, "ngày sai định dạng -> 400")
    check(c.get("/api/reports", params={"source": "manual", "date_from": "2026-11-10"}).json()["total"] == 1, "lọc nguồn + ngày")
    check(c.get("/api/reports/999999").status_code == 404, "báo cáo không tồn tại -> 404")

    print("Thống kê")
    check(c.get("/api/overview").json()["total_reports"] == 2, "overview")
    s = c.get("/api/submissions", params={"date": "2026-11-10"}).json()
    check(s["submitted_count"] == 0 and len(s["senders_not_in_employee_list"]) == 0, "submissions (An đã ngừng theo dõi)")
    check("per_day" in c.get("/api/stats").json(), "stats mặc định 14 ngày")

    print("Xoá")
    check(c.delete(f"/api/reports/{rid}").status_code == 200, "xoá báo cáo")
    check(c.delete(f"/api/reports/{rid}").status_code == 404, "xoá lần 2 -> 404")
    check(c.post("/api/reports/delete", json={"ids": [rid2]}).json()["deleted"] == 1, "xoá hàng loạt")
    check(c.get("/api/reports", params={"q": "da nang"}).json()["total"] == 0, "FTS đã dọn sau khi xoá")
    check(c.delete(f"/api/employees/{an_id}").status_code == 200, "xoá nhân viên")
    check(c.get("/api/imports").status_code == 200, "nhật ký nhập")

    c.post("/api/logout", json={})
    check(c.get("/api/me").status_code == 401, "đăng xuất xong -> 401")


if __name__ == "__main__":
    main()
