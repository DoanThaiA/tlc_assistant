"""Tầng lưu trữ SQLite + tìm kiếm toàn văn FTS5 (không phân biệt dấu tiếng Việt).

Vòng đời một email:  pending (Apps Script vừa đẩy vào)  ->  kept (Claude/người duyệt giữ lại, có phân loại + tóm tắt)
                                                       ->  discarded (không liên quan: xoá nội dung, chỉ giữ dấu vết
                                                           message_id để không bị nhập lại)
Hỏi đáp mặc định chỉ dùng email "kept".
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    department  TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY,
    message_id    TEXT NOT NULL UNIQUE,
    source        TEXT,                 -- vd: gmail, outlook, manual
    sender_email  TEXT NOT NULL,
    sender_name   TEXT,
    subject       TEXT,
    report_date   TEXT NOT NULL,        -- YYYY-MM-DD (ngày của nội dung email)
    sent_at       TEXT,                 -- ISO 8601 theo múi giờ cấu hình
    body_text     TEXT,
    attachments   TEXT,                 -- JSON [{name, ...}]
    to_addrs      TEXT,
    cc_addrs      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date);
CREATE INDEX IF NOT EXISTS idx_reports_sender ON reports(sender_email, report_date);

-- Bảng FTS lưu văn bản đã bỏ dấu, rowid = reports.id (chỉ chứa email chờ duyệt / đã giữ)
CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(
    subject, body, sender, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    source      TEXT,
    received    INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    duplicate   INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- ===== ZALO: hội thoại theo dõi, công việc được giao, nhật ký trả lời (xem zalo.py) =====
CREATE TABLE IF NOT EXISTS zalo_conversations (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,     -- tên nhóm / người đúng như hiển thị trên Zalo
    name_norm             TEXT NOT NULL UNIQUE,     -- tên không dấu, để tra cứu
    kind                  TEXT NOT NULL DEFAULT 'direct',   -- group | direct | self
    watch                 INTEGER NOT NULL DEFAULT 1,       -- 1 = quét hội thoại này
    auto_reply            INTEGER NOT NULL DEFAULT 0,       -- 1 = được tự trả lời
    note                  TEXT,                     -- người này là ai, cách xưng hô...
    last_message_key      TEXT,                     -- mốc tin cuối đã xử lý
    last_message_at       TEXT,
    last_message_preview  TEXT,
    last_scanned_at       TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER REFERENCES zalo_conversations(id) ON DELETE SET NULL,
    msg_key             TEXT UNIQUE,                -- mã tin giao việc (chống lưu trùng)
    source              TEXT NOT NULL DEFAULT 'zalo',
    assigner            TEXT,                       -- người giao
    title               TEXT NOT NULL,              -- tóm tắt ngắn
    content             TEXT,                       -- nguyên văn tin giao việc
    kind                TEXT NOT NULL DEFAULT 'info',     -- info (Claude tự trả lời) | action (người làm, Claude nhắc)
    priority            TEXT NOT NULL DEFAULT 'binh_thuong',
    status              TEXT NOT NULL DEFAULT 'open',     -- open | waiting | done | cancelled
    assigned_at         TEXT,
    due_at              TEXT,                       -- ISO 8601 theo múi giờ cấu hình
    due_text            TEXT,                       -- nguyên văn hạn: "trước 5h chiều mai"
    lead_minutes        INTEGER NOT NULL DEFAULT 30,
    fire_at             TEXT,                       -- thời điểm xử lý = due_at - lead_minutes
    fired_at            TEXT,                       -- đã xử lý (trả lời / nhắc) lúc nào
    replied_at          TEXT,
    done_at             TEXT,
    result              TEXT,                       -- kết quả / câu trả lời đã gửi
    related_report_ids  TEXT,                       -- JSON [id email liên quan]
    tags                TEXT,                       -- ",tag-1,tag-2,"
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_fire ON tasks(status, fire_at);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_conv ON tasks(conversation_id);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, content, assigner, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS task_events (
    id       INTEGER PRIMARY KEY,
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at       TEXT NOT NULL,
    type     TEXT NOT NULL,     -- created | updated | reminded | replied | done | cancelled | note
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);

CREATE TABLE IF NOT EXISTS zalo_actions (
    id                 INTEGER PRIMARY KEY,
    conversation_id    INTEGER REFERENCES zalo_conversations(id) ON DELETE SET NULL,
    msg_key            TEXT NOT NULL UNIQUE,        -- chống trả lời 2 lần cùng một tin
    sender             TEXT,
    message_at         TEXT,
    message_text       TEXT,                        -- tin gốc (cắt 500 ký tự)
    action             TEXT NOT NULL,               -- answered | task_saved | task_reply | reminder
    reply_text         TEXT,
    task_id            INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    source_report_ids  TEXT,                        -- JSON [id email đã dùng làm nguồn]
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zalo_actions_conv ON zalo_actions(conversation_id, created_at);
"""

# Cột thêm sau (tự migrate CSDL cũ). Email cũ mặc định "kept".
_NEW_REPORT_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'kept'",
    "category": "TEXT",
    "tags": "TEXT",          # dạng ",tag-1,tag-2," để lọc bằng LIKE
    "summary": "TEXT",       # tóm tắt 1-3 câu do Claude viết khi duyệt
    "thread_id": "TEXT",     # id luồng hội thoại (Gmail thread)
    "labels": "TEXT",        # nhãn Gmail, phân tách dấu phẩy
    "reviewed_at": "TEXT",
    "review_note": "TEXT",
}

STATUSES = ("pending", "kept", "discarded")

DEFAULT_CATEGORIES = [
    {"code": "bao_cao_ngay", "name": "Báo cáo công việc hằng ngày/tuần của nhân viên"},
    {"code": "giao_viec", "name": "Giao việc, chỉ đạo, trao đổi công việc nội bộ"},
    {"code": "khach_hang", "name": "Khách hàng, đối tác: báo giá, hợp đồng, đơn hàng, phản hồi"},
    {"code": "du_an", "name": "Tiến độ, vướng mắc, kế hoạch dự án"},
    {"code": "tai_chinh", "name": "Hoá đơn, thanh toán, công nợ, ngân sách"},
    {"code": "nhan_su", "name": "Nhân sự: tuyển dụng, nghỉ phép, chính sách"},
    {"code": "hop_lich", "name": "Lịch họp, sự kiện, biên bản họp"},
    {"code": "thi_truong", "name": "Tin tức, bản tin, báo cáo thị trường/ngành phục vụ công việc"},
    {"code": "he_thong", "name": "Thông báo hệ thống/công cụ làm việc quan trọng"},
    {"code": "khac", "name": "Liên quan công việc nhưng chưa rõ nhóm"},
]

DEFAULT_RULES = """GIỮ LẠI (liên quan công việc):
- Email từ nhân viên, đồng nghiệp, cấp trên; báo cáo công việc ngày/tuần.
- Trao đổi với khách hàng, đối tác, nhà cung cấp: báo giá, hợp đồng, đơn hàng, khiếu nại, phản hồi.
- Hoá đơn, chứng từ, thanh toán, công nợ của công ty.
- Lịch họp, biên bản, kế hoạch, tiến độ dự án.
- Bản tin / báo cáo thị trường, ngành nghề mà công ty theo dõi.
- Thông báo quan trọng từ hệ thống làm việc (đơn hàng mới, lỗi hệ thống, hạn nộp...).

LOẠI BỎ (không liên quan):
- Quảng cáo, khuyến mãi, marketing, bản tin đăng ký không phục vụ công việc.
- Mạng xã hội, diễn đàn, thông báo ứng dụng cá nhân.
- Mã OTP, cảnh báo đăng nhập, đặt lại mật khẩu, xác minh tài khoản.
- Email cá nhân, gia đình, mua sắm cá nhân.
- Spam, lừa đảo, email tự động không có nội dung công việc.

KHI PHÂN VÂN: giữ lại với nhóm "khac" và ghi lý do vào note."""


# ---------------------------------------------------------------------------
# Chuẩn hoá tiếng Việt
# ---------------------------------------------------------------------------
def _fold_char(c: str) -> str:
    if c in "đĐ":
        return "d"
    base = unicodedata.normalize("NFD", c)[0]
    if unicodedata.combining(base):
        return " "
    return base.lower()


def norm(text: str | None) -> str:
    """Bỏ dấu + chữ thường, GIỮ NGUYÊN độ dài so với chuỗi NFC (để cắt snippet)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    return "".join(_fold_char(c) for c in text)


def nfc(text: str | None) -> str:
    return unicodedata.normalize("NFC", text or "")


def norm_tags(tags: Any) -> str | None:
    """['Hợp đồng ABC', 'gấp'] hoặc 'a, b' -> ',hop-dong-abc,gap,' (không dấu, gạch nối)."""
    if tags is None:
        return None
    items = tags.split(",") if isinstance(tags, str) else list(tags)
    out: list[str] = []
    for t in items:
        slug = re.sub(r"[^a-z0-9]+", "-", norm(str(t)).strip()).strip("-")
        if slug and slug not in out:
            out.append(slug)
    return ("," + ",".join(out) + ",") if out else None


def tags_list(raw: str | None) -> list[str]:
    return [t for t in (raw or "").split(",") if t]


# ---------------------------------------------------------------------------
# Kết nối
# ---------------------------------------------------------------------------
@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("norm", 1, norm, deterministic=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
        for col, ddl in _NEW_REPORT_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {col} {ddl}")
        conn.executescript(
            """CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, report_date);
               CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
               CREATE INDEX IF NOT EXISTS idx_reports_thread ON reports(thread_id);"""
        )


def today() -> str:
    return datetime.now(settings.timezone).date().isoformat()


def _now() -> str:
    return datetime.now(settings.timezone).isoformat(timespec="seconds")


def _check_date(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as e:
        raise ValueError(f"{name} phải có dạng YYYY-MM-DD, nhận được: {value!r}") from e


class Conflict(Exception):
    """Dữ liệu trùng (vd email đã tồn tại)."""


# ---------------------------------------------------------------------------
# Cài đặt: quy tắc lọc + danh mục phân loại
# ---------------------------------------------------------------------------
def get_setting(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def set_setting(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def filter_rules() -> str:
    return get_setting("filter_rules", DEFAULT_RULES)


def categories() -> list[dict]:
    cats = get_setting("categories", None)
    return cats if isinstance(cats, list) and cats else DEFAULT_CATEGORIES


def save_rules(rules: str | None = None, cats: list[dict] | None = None) -> dict:
    if rules is not None:
        rules = nfc(rules).strip()
        if not rules:
            raise ValueError("Quy tắc lọc không được để trống")
        set_setting("filter_rules", rules)
    if cats is not None:
        clean: list[dict] = []
        for c in cats:
            code = re.sub(r"[^a-z0-9_]+", "_", norm(str(c.get("code", ""))).strip()).strip("_")
            if code and code not in {x["code"] for x in clean}:
                clean.append({"code": code, "name": nfc(str(c.get("name") or code)).strip()})
        if not clean:
            raise ValueError("Cần ít nhất một nhóm phân loại")
        set_setting("categories", clean)
    return {"filter_rules": filter_rules(), "categories": categories()}


# ---------------------------------------------------------------------------
# Nhân viên
# ---------------------------------------------------------------------------
def upsert_employee(email: str, name: str, department: str | None = None, active: bool = True) -> dict:
    email = email.strip().lower()
    with connect() as conn:
        conn.execute(
            """INSERT INTO employees(email, name, department, active) VALUES (?,?,?,?)
               ON CONFLICT(email) DO UPDATE SET name=excluded.name,
                   department=COALESCE(excluded.department, employees.department),
                   active=excluded.active""",
            (email, nfc(name).strip(), department, 1 if active else 0),
        )
        row = conn.execute("SELECT * FROM employees WHERE email=?", (email,)).fetchone()
    return dict(row)


def list_employees(include_inactive: bool = False) -> list[dict]:
    sql = "SELECT id, email, name, department, active FROM employees"
    if not include_inactive:
        sql += " WHERE active=1"
    sql += " ORDER BY department, name"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql)]


def known_employee_emails() -> set[str]:
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT email FROM employees")}


_EMAIL_OK = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _clean_employee(data: dict[str, Any]) -> dict[str, Any]:
    email = (data.get("email") or "").strip().lower()
    if not _EMAIL_OK.match(email):
        raise ValueError(f"Email không hợp lệ: {email!r}")
    name = nfc(data.get("name")).strip()
    if not name:
        raise ValueError("Họ tên không được để trống")
    dept = nfc(data.get("department")).strip() or None
    return {"email": email, "name": name, "department": dept, "active": 1 if data.get("active", True) else 0}


def admin_list_employees(q: str | None = None, include_inactive: bool = True) -> list[dict]:
    where, params = [], []
    if not include_inactive:
        where.append("e.active = 1")
    if q and q.strip():
        where.append("(e.email LIKE ? OR norm(e.name) LIKE ? OR norm(COALESCE(e.department,'')) LIKE ?)")
        params += [f"%{q.strip().lower()}%", f"%{norm(q.strip())}%", f"%{norm(q.strip())}%"]
    w = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT e.id, e.email, e.name, e.department, e.active, e.created_at,
                     (SELECT COUNT(*) FROM reports r WHERE r.sender_email = e.email AND r.status='kept') AS report_count,
                     (SELECT MAX(report_date) FROM reports r WHERE r.sender_email = e.email AND r.status='kept')
                        AS last_report_date
              FROM employees e{w} ORDER BY e.active DESC, e.department, e.name"""
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_employee(emp_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    return dict(row) if row else None


def create_employee(data: dict[str, Any]) -> dict:
    e = _clean_employee(data)
    with connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO employees(email, name, department, active) VALUES (?,?,?,?)",
                (e["email"], e["name"], e["department"], e["active"]),
            )
        except sqlite3.IntegrityError as ex:
            raise Conflict(f"Email {e['email']} đã có trong danh sách") from ex
        new_id = cur.lastrowid
    return get_employee(new_id)  # type: ignore[return-value]


def update_employee(emp_id: int, data: dict[str, Any]) -> dict | None:
    cur_emp = get_employee(emp_id)
    if not cur_emp:
        return None
    e = _clean_employee({**cur_emp, **data})
    with connect() as conn:
        try:
            conn.execute(
                "UPDATE employees SET email=?, name=?, department=?, active=? WHERE id=?",
                (e["email"], e["name"], e["department"], e["active"], emp_id),
            )
        except sqlite3.IntegrityError as ex:
            raise Conflict(f"Email {e['email']} đã thuộc nhân viên khác") from ex
    return get_employee(emp_id)


def delete_employee(emp_id: int) -> bool:
    """Chỉ xoá khỏi danh sách nhân viên; email đã lưu vẫn giữ nguyên."""
    with connect() as conn:
        return conn.execute("DELETE FROM employees WHERE id=?", (emp_id,)).rowcount > 0


# ---------------------------------------------------------------------------
# Lưu / sửa email
# ---------------------------------------------------------------------------
def _index_fts(conn: sqlite3.Connection, rid: int) -> None:
    """Đánh chỉ mục lại 1 email (email đã loại thì chỉ xoá khỏi chỉ mục)."""
    conn.execute("DELETE FROM reports_fts WHERE rowid=?", (rid,))
    r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] == "discarded":
        return
    att = " ".join(a.get("name", "") for a in json.loads(r["attachments"] or "[]"))
    conn.execute(
        "INSERT INTO reports_fts(rowid, subject, body, sender) VALUES (?,?,?,?)",
        (
            rid,
            norm(" ".join(filter(None, [r["subject"], att, (r["tags"] or "").replace(",", " "),
                                        (r["category"] or "").replace("_", " ")]))),
            norm(" ".join(filter(None, [r["summary"], r["body_text"]]))),
            norm(f"{r['sender_name'] or ''} {r['sender_email']}"),
        ),
    )


def insert_report(r: dict[str, Any]) -> int | None:
    """Lưu 1 email. Trả về id mới, hoặc None nếu đã tồn tại (trùng message_id)."""
    status = r.get("status") or "kept"
    if status not in STATUSES:
        raise ValueError(f"status không hợp lệ: {status}")
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO reports
               (message_id, source, sender_email, sender_name, subject, report_date, sent_at,
                body_text, attachments, to_addrs, cc_addrs, status, category, tags, summary,
                thread_id, labels, reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["message_id"],
                r.get("source"),
                r["sender_email"].lower(),
                nfc(r.get("sender_name")) or None,
                nfc(r.get("subject")),
                r["report_date"],
                r.get("sent_at"),
                nfc(r.get("body_text")),
                json.dumps(r.get("attachments") or [], ensure_ascii=False),
                r.get("to_addrs"),
                r.get("cc_addrs"),
                status,
                r.get("category") or None,
                norm_tags(r.get("tags")),
                nfc(r.get("summary")) or None,
                r.get("thread_id") or None,
                r.get("labels") or None,
                _now() if status == "kept" and r.get("category") else None,
            ),
        )
        if cur.rowcount == 0:
            return None
        rid = cur.lastrowid
        _index_fts(conn, rid)
        return rid


def find_by_message_id(message_id: str) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM reports WHERE message_id=?", (message_id,)).fetchone()
    return row[0] if row else None


# Các trường được phép sửa từ trang quản trị
EDITABLE_REPORT_FIELDS = ("sender_email", "sender_name", "subject", "report_date", "sent_at", "body_text",
                          "source", "to_addrs", "cc_addrs", "category", "tags", "summary", "review_note")


def update_report(report_id: int, data: dict[str, Any]) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            return None
        new = {k: data[k] for k in EDITABLE_REPORT_FIELDS if k in data}
        if "sender_email" in new:
            email = (new["sender_email"] or "").strip().lower()
            if not _EMAIL_OK.match(email):
                raise ValueError(f"Email người gửi không hợp lệ: {email!r}")
            new["sender_email"] = email
        if "report_date" in new:
            new["report_date"] = _check_date(new["report_date"], "report_date")
            if not new["report_date"]:
                raise ValueError("Ngày không được để trống")
        if "tags" in new:
            new["tags"] = norm_tags(new["tags"])
        if "category" in new:
            new["category"] = (new["category"] or "").strip() or None
        for k in ("sender_name", "subject", "body_text", "summary", "review_note"):
            if k in new:
                new[k] = nfc(new[k]) or (None if k in ("summary", "review_note") else "")
        if new:
            sets = ", ".join(f"{k}=?" for k in new)
            conn.execute(f"UPDATE reports SET {sets} WHERE id=?", [*new.values(), report_id])
            _index_fts(conn, report_id)
    return get_report(report_id)


def review(decisions: list[dict[str, Any]]) -> dict:
    """Áp dụng quyết định duyệt: keep (giữ + phân loại), discard (loại, xoá nội dung), pending (đưa về chờ)."""
    valid_cats = {c["code"] for c in categories()}
    results = []
    counts = {"kept": 0, "discarded": 0, "pending": 0, "error": 0}
    with connect() as conn:
        for d in decisions:
            rid = d.get("id")
            action = (d.get("action") or "").strip().lower()
            try:
                row = conn.execute("SELECT id, status FROM reports WHERE id=?", (rid,)).fetchone()
                if not row:
                    raise ValueError("không tồn tại")
                note = nfc(d.get("note")) or None
                if action == "keep":
                    cat = (d.get("category") or "khac").strip()
                    if cat not in valid_cats:
                        raise ValueError(f"category '{cat}' không có trong danh mục: {sorted(valid_cats)}")
                    if row["status"] == "discarded":
                        raise ValueError("email đã bị loại, nội dung đã xoá nên không thể giữ lại")
                    fields: dict[str, Any] = {"status": "kept", "category": cat, "reviewed_at": _now(),
                                              "review_note": note}
                    if d.get("summary") is not None:
                        fields["summary"] = nfc(d["summary"]).strip() or None
                    if d.get("tags") is not None:
                        fields["tags"] = norm_tags(d["tags"])
                    if d.get("report_date"):
                        fields["report_date"] = _check_date(d["report_date"], "report_date")
                    status = "kept"
                elif action == "discard":
                    fields = {"status": "discarded", "body_text": "", "attachments": "[]", "summary": None,
                              "category": None, "tags": None, "to_addrs": None, "cc_addrs": None,
                              "reviewed_at": _now(), "review_note": note}
                    status = "discarded"
                elif action == "pending":
                    if row["status"] == "discarded":
                        raise ValueError("email đã bị loại, không thể đưa về chờ duyệt")
                    fields = {"status": "pending", "reviewed_at": None}
                    status = "pending"
                else:
                    raise ValueError("action phải là keep / discard / pending")
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(f"UPDATE reports SET {sets} WHERE id=?", [*fields.values(), rid])
                _index_fts(conn, rid)
                counts[status] += 1
                results.append({"id": rid, "status": status})
            except Exception as e:  # noqa: BLE001
                counts["error"] += 1
                results.append({"id": rid, "status": "error", "error": str(e)})
        remaining = conn.execute("SELECT COUNT(*) FROM reports WHERE status='pending'").fetchone()[0]
    return {**counts, "pending_remaining": remaining, "results": results}


def delete_report(report_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
        conn.execute("DELETE FROM reports_fts WHERE rowid=?", (report_id,))
        return cur.rowcount > 0


def delete_reports(ids: list[int]) -> int:
    ids = [int(i) for i in ids][:1000]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        n = conn.execute(f"DELETE FROM reports WHERE id IN ({marks})", ids).rowcount
        conn.execute(f"DELETE FROM reports_fts WHERE rowid IN ({marks})", ids)
    return n


def purge_discarded(older_than_days: int = 0) -> int:
    """Xoá hẳn dấu vết email đã loại (khi đó nếu Apps Script gửi lại, email sẽ vào chờ duyệt lần nữa)."""
    cutoff = (datetime.now(settings.timezone) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "DELETE FROM reports WHERE status='discarded' AND COALESCE(reviewed_at,'') <= ?", (cutoff,)
        ).rowcount


# ---------------------------------------------------------------------------
# Truy vấn
# ---------------------------------------------------------------------------
_REPORT_COLS = """
    r.id, r.report_date, r.sent_at, r.sender_email,
    COALESCE(e.name, r.sender_name, r.sender_email) AS employee_name,
    e.department, r.subject, r.source, r.status, r.category, r.tags, r.summary, r.thread_id
"""
_JOIN = "FROM reports r LEFT JOIN employees e ON e.email = r.sender_email"


def _out(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    if "tags" in d:
        d["tags"] = tags_list(d["tags"])
    if "attachments" in d and isinstance(d["attachments"], str):
        d["attachments"] = json.loads(d["attachments"] or "[]")
    return d


def _filters(
    where: list[str],
    params: list[Any],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    employee: str | None = None,
    status: str | None = "kept",
    category: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    sender_domain: str | None = None,
    thread_id: str | None = None,
    q: str | None = None,
) -> None:
    """Bộ lọc dùng chung. status: kept (mặc định) | pending | discarded | all."""
    df = _check_date(date_from, "date_from")
    dt = _check_date(date_to, "date_to")
    if df:
        where.append("r.report_date >= ?")
        params.append(df)
    if dt:
        where.append("r.report_date <= ?")
        params.append(dt)
    if employee and employee.strip():
        e = employee.strip()
        where.append(
            "(r.sender_email = lower(?) OR r.sender_email LIKE ? "
            "OR norm(COALESCE(e.name, r.sender_name, '')) LIKE ? OR norm(COALESCE(e.department,'')) = ?)"
        )
        params += [e, f"%{e.lower()}%", f"%{norm(e)}%", norm(e)]
    if status and status != "all":
        sts = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in sts if s not in STATUSES]
        if bad:
            raise ValueError(f"status không hợp lệ: {bad}. Dùng kept, pending, discarded hoặc all")
        where.append(f"r.status IN ({','.join('?' * len(sts))})")
        params += sts
    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()]
        where.append(f"r.category IN ({','.join('?' * len(cats))})")
        params += cats
    if tag:
        for t in tags_list(norm_tags(tag)):
            where.append("r.tags LIKE ?")
            params.append(f"%,{t}%")
    if source:
        where.append("r.source = ?")
        params.append(source)
    if sender_domain:
        where.append("r.sender_email LIKE ?")
        params.append(f"%@%{sender_domain.strip().lower().lstrip('@')}")
    if thread_id:
        where.append("r.thread_id = ?")
        params.append(thread_id)
    tokens = re.findall(r"\w+", norm(q)) if q else []
    if tokens:
        where.append("r.id IN (SELECT rowid FROM reports_fts WHERE reports_fts MATCH ?)")
        params.append(" AND ".join(fts_term(t) for t in tokens))


def fts_term(token: str) -> str:
    """Từ ngắn (âm tiết tiếng Việt: gia, bao, tien...) khớp CHÍNH XÁC để 'gia' không khớp 'giai',
    'tien' không khớp 'tieng'; từ dài (tiếng Anh, mã số) khớp tiền tố ('report' khớp 'reports')."""
    return f'"{token}"*' if len(token) >= 5 else f'"{token}"'


def _where(where: list[str]) -> str:
    return (" WHERE " + " AND ".join(where)) if where else ""


def _preview(text: str | None, n: int = 200) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[:n] + "…"


_ORDERS = {
    "newest": "r.report_date DESC, r.sent_at DESC",
    "oldest": "r.report_date ASC, r.sent_at ASC",
}


def list_reports(
    date_from: str | None = None,
    date_to: str | None = None,
    employee: str | None = None,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    source: str | None = None,
    status: str | None = "kept",
    category: str | None = None,
    tag: str | None = None,
    sender_domain: str | None = None,
    order: str = "newest",
) -> dict:
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, date_from=date_from, date_to=date_to, employee=employee, status=status,
             category=category, tag=tag, source=source, sender_domain=sender_domain, q=q)
    w = _where(where)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {_JOIN}{w}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_REPORT_COLS}, r.body_text, r.review_note {_JOIN}{w} "
            f"ORDER BY {_ORDERS.get(order, _ORDERS['newest'])} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        d = _out(row)
        d["preview"] = _preview(d.pop("body_text"))
        items.append(d)
    return {"total": total, "offset": offset, "count": len(items), "reports": items}


def get_report(report_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_REPORT_COLS}, r.body_text, r.attachments, r.to_addrs, r.cc_addrs, r.message_id, "
            f"r.sender_name, r.created_at, r.labels, r.reviewed_at, r.review_note {_JOIN} WHERE r.id = ?",
            (report_id,),
        ).fetchone()
    return _out(row) if row else None


def reports_full(
    date_from: str | None, date_to: str | None, employee: str | None, max_chars_each: int, limit: int,
    status: str | None = "kept", category: str | None = None, tag: str | None = None, q: str | None = None,
    sender_domain: str | None = None,
) -> dict:
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, date_from=date_from, date_to=date_to, employee=employee, status=status,
             category=category, tag=tag, q=q, sender_domain=sender_domain)
    w = _where(where)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {_JOIN}{w}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_REPORT_COLS}, r.body_text, r.attachments {_JOIN}{w} "
            "ORDER BY r.report_date, employee_name, r.sent_at LIMIT ?",
            [*params, limit],
        ).fetchall()
    out = []
    for row in rows:
        d = _out(row)
        body = d.get("body_text") or ""
        if len(body) > max_chars_each:
            d["body_text"] = body[:max_chars_each] + f"\n…[đã cắt, xem đủ bằng get_report id={d['id']}]"
        d["attachments"] = [a["name"] for a in d["attachments"]]
        out.append(d)
    return {"total_matching": total, "reports": out}


def digest(
    date_from: str | None, date_to: str | None, employee: str | None = None, category: str | None = None,
    tag: str | None = None, q: str | None = None, sender_domain: str | None = None, limit: int = 300,
) -> dict:
    """Bản tin gọn: mỗi email 1 dòng (tóm tắt hoặc đoạn đầu), nhóm theo phân loại."""
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, date_from=date_from, date_to=date_to, employee=employee, status="kept",
             category=category, tag=tag, q=q, sender_domain=sender_domain)
    w = _where(where)
    limit = max(1, min(limit, 1000))
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {_JOIN}{w}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_REPORT_COLS}, r.body_text {_JOIN}{w} ORDER BY r.report_date, r.sent_at LIMIT ?",
            [*params, limit],
        ).fetchall()
    names = {c["code"]: c["name"] for c in categories()}
    groups: dict[str, list[dict]] = {}
    for row in rows:
        d = _out(row)
        groups.setdefault(d["category"] or "chua_phan_loai", []).append({
            "id": d["id"], "date": d["report_date"], "from": d["employee_name"], "department": d["department"],
            "subject": d["subject"], "summary": d["summary"] or _preview(d["body_text"], 300), "tags": d["tags"],
        })
    return {
        "total_matching": total, "returned": len(rows), "truncated": total > len(rows),
        "groups": [{"category": k, "category_name": names.get(k, k), "count": len(v), "items": v}
                   for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
    }


def _snippet(text: str, tokens: list[str], width: int = 160) -> str:
    text = nfc(text)
    ntext = norm(text)
    pos = -1
    for t in tokens:
        m = re.search(r"\b" + re.escape(t), ntext)
        if m and (pos < 0 or m.start() < pos):
            pos = m.start()
    if pos < 0:
        return _preview(text, width * 2)
    start = max(0, pos - width)
    end = min(len(text), pos + width)
    s = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def search_reports(
    query: str,
    date_from: str | None = None,
    date_to: str | None = None,
    employee: str | None = None,
    limit: int = 20,
    status: str | None = "kept",
    category: str | None = None,
    tag: str | None = None,
    sender_domain: str | None = None,
    sort: str = "relevance",
) -> dict:
    tokens = re.findall(r"\w+", norm(query))
    if not tokens:
        raise ValueError("Từ khoá tìm kiếm rỗng")
    limit = max(1, min(limit, 100))
    order = {"newest": "r.report_date DESC, r.sent_at DESC", "oldest": "r.report_date ASC, r.sent_at ASC"}.get(
        sort, "score")

    def run(match: str) -> tuple[int, list[sqlite3.Row]]:
        where = ["reports_fts MATCH ?"]
        params: list[Any] = [match]
        _filters(where, params, date_from=date_from, date_to=date_to, employee=employee, status=status,
                 category=category, tag=tag, sender_domain=sender_domain)
        base = (f"FROM reports_fts JOIN reports r ON r.id = reports_fts.rowid "
                f"LEFT JOIN employees e ON e.email = r.sender_email WHERE {' AND '.join(where)}")
        with connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_REPORT_COLS}, r.body_text, bm25(reports_fts, 5.0, 1.0, 2.0) AS score {base} "
                f"ORDER BY {order} LIMIT ?", [*params, limit]).fetchall()
        return total, rows

    quoted = [fts_term(t) for t in tokens]
    mode = "all_words"
    total, rows = run(" AND ".join(quoted))
    if not rows and len(tokens) > 1:
        mode = "any_word"
        total, rows = run(" OR ".join(quoted))
    results = []
    for row in rows:
        d = _out(row)
        d["snippet"] = _snippet(d.pop("body_text") or "", tokens)
        d.pop("score", None)
        results.append(d)
    return {"query": query, "match_mode": mode, "total_matching": total, "count": len(results), "results": results}


# Cụm từ (không dấu) báo hiệu việc cần chú ý: vướng mắc, chậm trễ, gấp, sự cố, khiếu nại...
# Tránh từ đơn dễ nhầm (vd "gap" = gặp/gấp, "loi" = lỗi/lời, "thieu" = thiếu/giới thiệu).
ATTENTION_KEYWORDS = [
    "vuong mac", "kho khan", "tre han", "tre tien do", "cham tien do", "cham tre", "cham thanh toan",
    "qua han", "han chot", "deadline", "khan cap", "khan truong", "can gap", "rat gap", "gap rut",
    "su co", "bi loi", "gap loi", "loi he thong", "khieu nai", "phan nan", "rui ro", "can ho tro",
    "xin y kien", "xin chi dao", "de xuat", "phat sinh", "thieu vat tu", "thieu nhan su", "thieu hut",
    "huy don", "huy hop dong", "cong no", "no qua han", "urgent", "asap",
]


def _phrases(keywords: list[str]) -> list[str]:
    out: list[str] = []
    for k in keywords:
        p = " ".join(re.findall(r"\w+", norm(k)))
        if p and p not in out:
            out.append(p)
    return out


def attention_items(
    date_from: str | None, date_to: str | None, employee: str | None = None, category: str | None = None,
    sender_domain: str | None = None, keywords: list[str] | None = None, only_keywords: bool = False,
    limit: int = 50,
) -> dict:
    """Email đã giữ có nhắc tới vướng mắc / chậm trễ / gấp / sự cố... (theo danh sách cụm từ)."""
    phrases = _phrases((keywords or []) if only_keywords else [*ATTENTION_KEYWORDS, *(keywords or [])])
    if not phrases:
        raise ValueError("Danh sách từ khoá rỗng")
    limit = max(1, min(limit, 200))
    where = ["reports_fts MATCH ?"]
    params: list[Any] = [" OR ".join(f'"{p}"' for p in phrases)]
    _filters(where, params, date_from=date_from, date_to=date_to, employee=employee, status="kept",
             category=category, sender_domain=sender_domain)
    base = (f"FROM reports_fts JOIN reports r ON r.id = reports_fts.rowid "
            f"LEFT JOIN employees e ON e.email = r.sender_email WHERE {' AND '.join(where)}")
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = conn.execute(f"SELECT {_REPORT_COLS}, r.body_text {base} "
                            "ORDER BY r.report_date DESC, r.sent_at DESC LIMIT ?", [*params, limit]).fetchall()
    items = []
    counts: dict[str, int] = {}
    for row in rows:
        d = _out(row)
        body = d.pop("body_text") or ""
        hay = " " + " ".join(re.findall(r"\w+", norm(" ".join(
            filter(None, [d["subject"], d["summary"], body, " ".join(d["tags"])]))))) + " "
        hits = [p for p in phrases if f" {p} " in hay]
        for p in hits:
            counts[p] = counts.get(p, 0) + 1
        d["matched"] = hits
        d["snippet"] = _snippet(body or d["summary"] or "", hits[0].split() if hits else [])
        items.append(d)
    return {"total_matching": total, "count": len(items),
            "keyword_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])), "items": items}


def sender_profile(sender: str, date_from: str | None = None, date_to: str | None = None,
                   recent: int = 10) -> dict:
    """Hồ sơ trao đổi với một người gửi / tên miền / phòng ban: số lượng, thời gian, nhóm, tag, email gần nhất."""
    s = (sender or "").strip()
    if not s:
        raise ValueError("Cần sender (email, tên miền, tên hoặc phòng ban)")
    filt: dict[str, Any] = {"date_from": date_from, "date_to": date_to, "status": "kept"}
    if s.startswith("@") or (re.fullmatch(r"[\w.-]+\.[a-z]{2,}", s.lower()) and "@" not in s):
        filt["sender_domain"] = s
        kind = "domain"
    else:
        filt["employee"] = s
        kind = "email" if "@" in s else "name_or_department"
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, **filt)
    w = _where(where)
    recent = max(1, min(recent, 50))
    with connect() as conn:
        tot = dict(conn.execute(
            f"SELECT COUNT(*) AS total, MIN(r.report_date) AS first_date, MAX(r.report_date) AS last_date, "
            f"COUNT(DISTINCT r.sender_email) AS senders, COUNT(DISTINCT r.thread_id) AS threads {_JOIN}{w}",
            params).fetchone())
        senders = [dict(x) for x in conn.execute(
            f"SELECT r.sender_email, MAX(COALESCE(e.name, r.sender_name)) AS name, MAX(e.department) AS department, "
            f"COUNT(*) AS count, MAX(r.report_date) AS last_date {_JOIN}{w} "
            "GROUP BY r.sender_email ORDER BY count DESC LIMIT 20", params)]
        cats = [dict(x) for x in conn.execute(
            f"SELECT COALESCE(r.category,'(chưa phân loại)') AS category, COUNT(*) AS count {_JOIN}{w} "
            "GROUP BY 1 ORDER BY count DESC", params)]
        tag_counts: dict[str, int] = {}
        for (tags,) in conn.execute(f"SELECT r.tags {_JOIN}{w}", params):
            for t in tags_list(tags):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        rows = conn.execute(f"SELECT {_REPORT_COLS}, r.body_text {_JOIN}{w} "
                            "ORDER BY r.report_date DESC, r.sent_at DESC LIMIT ?", [*params, recent]).fetchall()
        pw: list[str] = []
        pp: list[Any] = []
        _filters(pw, pp, **{**filt, "status": "pending"})
        pending = conn.execute(f"SELECT COUNT(*) {_JOIN}{_where(pw)}", pp).fetchone()[0]
    names = {c["code"]: c["name"] for c in categories()}
    for c in cats:
        c["category_name"] = names.get(c["category"], c["category"])
    emails = []
    for row in rows:
        d = _out(row)
        body = d.pop("body_text")
        d["summary"] = d["summary"] or _preview(body, 300)
        emails.append(d)
    return {"sender": s, "matched_as": kind, **tot, "pending_from_sender": pending, "senders": senders,
            "categories": cats,
            "top_tags": [{"tag": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])[:15]],
            "recent_emails": emails}


def thread(thread_id: str | None = None, report_id: int | None = None, max_chars_each: int = 3000) -> dict:
    """Toàn bộ email trong cùng một luồng hội thoại, theo thời gian."""
    with connect() as conn:
        if not thread_id and report_id:
            row = conn.execute("SELECT thread_id, subject FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                raise ValueError(f"Không tìm thấy email id={report_id}")
            thread_id = row["thread_id"]
            if not thread_id:
                # Không có thread_id: ghép theo tiêu đề đã bỏ Re:/Fwd:
                base = re.sub(r"^\s*((re|fw|fwd|tl|chuyển tiếp)\s*:\s*)+", "", row["subject"] or "", flags=re.I)
                if len(norm(base).strip()) < 4:
                    # Tiêu đề rỗng/quá ngắn: không ghép được, chỉ trả chính email đó
                    rows = conn.execute(f"SELECT {_REPORT_COLS}, r.body_text {_JOIN} WHERE r.id=?",
                                        (report_id,)).fetchall()
                    return _thread_out(None, rows, max_chars_each)
                rows = conn.execute(
                    f"SELECT {_REPORT_COLS}, r.body_text {_JOIN} WHERE r.status != 'discarded' "
                    "AND norm(r.subject) LIKE ? ORDER BY r.sent_at", (f"%{norm(base)}%",)).fetchall()
                return _thread_out(None, rows, max_chars_each)
        if not thread_id:
            raise ValueError("Cần thread_id hoặc report_id")
        rows = conn.execute(
            f"SELECT {_REPORT_COLS}, r.body_text {_JOIN} WHERE r.thread_id=? AND r.status != 'discarded' "
            "ORDER BY r.sent_at", (thread_id,)).fetchall()
    return _thread_out(thread_id, rows, max_chars_each)


def _thread_out(thread_id: str | None, rows: list[sqlite3.Row], max_chars: int) -> dict:
    items = []
    for row in rows:
        d = _out(row)
        body = d.get("body_text") or ""
        d["body_text"] = body if len(body) <= max_chars else body[:max_chars] + "\n…[đã cắt]"
        items.append(d)
    return {"thread_id": thread_id, "count": len(items), "emails": items}


_GROUPS = {
    "day": "r.report_date",
    "week": "strftime('%Y-W%W', r.report_date)",
    "month": "substr(r.report_date, 1, 7)",
    "weekday": "strftime('%w', r.report_date)",
    "sender": "r.sender_email",
    "domain": "substr(r.sender_email, instr(r.sender_email, '@') + 1)",
    "department": "COALESCE(e.department, '(ngoài danh sách)')",
    "category": "COALESCE(r.category, '(chưa phân loại)')",
    "source": "COALESCE(r.source, '(không rõ)')",
    "status": "r.status",
}


def aggregate(group_by: str, **filters: Any) -> dict:
    """Đếm email theo một chiều (ngày/tuần/tháng/người gửi/tên miền/phòng ban/phân loại/tag/nguồn/trạng thái)."""
    if group_by not in (*_GROUPS, "tag"):
        raise ValueError(f"group_by phải là một trong: {', '.join([*_GROUPS, 'tag'])}")
    limit = max(1, min(int(filters.pop("limit", 100) or 100), 500))
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, **filters)
    w = _where(where)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) {_JOIN}{w}", params).fetchone()[0]
        if group_by == "tag":
            counts: dict[str, int] = {}
            for (tags,) in conn.execute(f"SELECT r.tags {_JOIN}{w}", params):
                for t in tags_list(tags):
                    counts[t] = counts.get(t, 0) + 1
            rows = [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]
        else:
            expr = _GROUPS[group_by]
            label = ", MAX(COALESCE(e.name, r.sender_name)) AS name" if group_by == "sender" else ""
            order = "key" if group_by in ("day", "week", "month", "weekday") else "count DESC"
            rows = [dict(r) for r in conn.execute(
                f"SELECT {expr} AS key, COUNT(*) AS count, COUNT(DISTINCT r.sender_email) AS senders, "
                f"MIN(r.report_date) AS first_date, MAX(r.report_date) AS last_date{label} "
                f"{_JOIN}{w} GROUP BY key ORDER BY {order} LIMIT ?", [*params, limit])]
            if group_by == "weekday":
                names = ["Chủ nhật", "Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"]
                for r in rows:
                    r["key"] = names[int(r["key"])]
    return {"group_by": group_by, "total": total, "groups": rows}


# ---------------------------------------------------------------------------
# Chờ duyệt
# ---------------------------------------------------------------------------
def pending_emails(limit: int = 20, max_chars_each: int = 2000) -> dict:
    limit = max(1, min(limit, 50))
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM reports WHERE status='pending'").fetchone()[0]
        rows = conn.execute(
            f"""SELECT r.id, r.sent_at, r.report_date, r.sender_email, r.sender_name, r.subject, r.body_text,
                       r.attachments, r.to_addrs, r.cc_addrs, r.labels, r.thread_id,
                       e.name AS employee_name, e.department,
                       (SELECT COUNT(*) FROM reports k WHERE k.sender_email = r.sender_email
                          AND k.status='kept') AS sender_kept_before,
                       (SELECT COUNT(*) FROM reports k WHERE k.sender_email = r.sender_email
                          AND k.status='discarded') AS sender_discarded_before
                {_JOIN} WHERE r.status='pending' ORDER BY r.sent_at LIMIT ?""",
            (limit,),
        ).fetchall()
    emails = []
    for row in rows:
        d = _out(row)
        body = d.pop("body_text") or ""
        d["body"] = body if len(body) <= max_chars_each else body[:max_chars_each] + "\n…[đã cắt]"
        d["body_length"] = len(body)
        d["attachments"] = [a["name"] for a in d["attachments"]]
        d["is_employee"] = d["employee_name"] is not None
        emails.append(d)
    return {"total_pending": total, "returned": len(emails), "emails": emails}


# ---------------------------------------------------------------------------
# Nộp báo cáo ngày (chỉ tính email "kept" thuộc nhóm bao_cao_ngay hoặc chưa phân loại)
# ---------------------------------------------------------------------------
_DAILY = "r.status='kept' AND (r.category IS NULL OR r.category='bao_cao_ngay')"


def _work_days_between(df: date, dt: date) -> list[str]:
    days = []
    d = df
    while d <= dt:
        if d.isoweekday() in settings.work_days:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def submission_status(day: str) -> dict:
    day = _check_date(day, "date") or today()
    employees = list_employees()
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_REPORT_COLS} {_JOIN} WHERE r.report_date = ? AND {_DAILY} ORDER BY r.sent_at", (day,)
        ).fetchall()
    by_sender: dict[str, list[dict]] = {}
    for r in rows:
        by_sender.setdefault(r["sender_email"], []).append(dict(r))
    known = known_employee_emails()  # gồm cả người đã ngừng theo dõi
    submitted = [
        {**e, "report_ids": [x["id"] for x in by_sender[e["email"]]],
         "first_sent_at": by_sender[e["email"]][0]["sent_at"]}
        for e in employees if e["email"] in by_sender
    ]
    missing = [e for e in employees if e["email"] not in by_sender]
    unknown = [
        {"email": s, "name": v[0]["employee_name"], "report_ids": [x["id"] for x in v]}
        for s, v in by_sender.items() if s not in known
    ]
    weekday = date.fromisoformat(day).isoweekday()
    return {
        "date": day,
        "is_work_day": weekday in settings.work_days,
        "total_reports": len(rows),
        "employees_active": len(employees),
        "submitted_count": len(submitted),
        "missing_count": len(missing),
        "submitted": submitted,
        "missing": missing,
        "senders_not_in_employee_list": unknown,
        "note": None if employees else "Chưa khai báo danh sách nhân viên (dùng upsert_employee) nên không xác định được ai chưa nộp.",
    }


def stats(date_from: str, date_to: str) -> dict:
    df = date.fromisoformat(_check_date(date_from, "date_from"))  # type: ignore[arg-type]
    dt = date.fromisoformat(_check_date(date_to, "date_to"))  # type: ignore[arg-type]
    if dt < df:
        raise ValueError("date_to phải >= date_from")
    work_days = _work_days_between(df, dt)
    rng = (df.isoformat(), dt.isoformat())
    with connect() as conn:
        per_emp = conn.execute(
            f"""SELECT r.sender_email, COALESCE(e.name, MAX(r.sender_name)) AS employee_name, e.department,
                       COUNT(*) AS reports, COUNT(DISTINCT r.report_date) AS days_reported,
                       MIN(r.report_date) AS first_date, MAX(r.report_date) AS last_date
                {_JOIN} WHERE r.report_date BETWEEN ? AND ? AND {_DAILY}
                GROUP BY r.sender_email ORDER BY reports DESC""", rng).fetchall()
        per_day = conn.execute(
            """SELECT report_date, COUNT(*) AS reports, COUNT(DISTINCT sender_email) AS senders
               FROM reports WHERE report_date BETWEEN ? AND ? AND status='kept'
               GROUP BY report_date ORDER BY report_date""", rng).fetchall()
        days_by_emp: dict[str, set[str]] = {}
        for r in conn.execute(
            f"SELECT DISTINCT r.sender_email, r.report_date FROM reports r WHERE r.report_date BETWEEN ? AND ? "
            f"AND {_DAILY}", rng):
            days_by_emp.setdefault(r[0], set()).add(r[1])

    compliance = []
    for e in list_employees():
        got = days_by_emp.get(e["email"], set())
        missing = [d for d in work_days if d not in got]
        compliance.append({
            "email": e["email"], "name": e["name"], "department": e["department"],
            "work_days": len(work_days), "days_reported": len([d for d in work_days if d in got]),
            "missing_days": missing,
        })
    return {
        "date_from": df.isoformat(),
        "date_to": dt.isoformat(),
        "work_days_in_range": len(work_days),
        "per_employee": [dict(r) for r in per_emp],
        "per_day": [dict(r) for r in per_day],
        "compliance": compliance,
    }


# ---------------------------------------------------------------------------
# Nhật ký nhập / điểm mốc / tổng quan
# ---------------------------------------------------------------------------
def log_import(source: str | None, received: int, inserted: int, duplicate: int, failed: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO import_log(at, source, received, inserted, duplicate, failed) VALUES (?,?,?,?,?,?)",
            (_now(), source, received, inserted, duplicate, failed),
        )


def checkpoint() -> dict:
    with connect() as conn:
        latest = conn.execute(
            "SELECT MAX(sent_at) AS latest_sent_at, MAX(report_date) AS latest_report_date, "
            "COUNT(*) AS total_reports, COALESCE(SUM(status='pending'), 0) AS pending FROM reports"
        ).fetchone()
        imports = [dict(r) for r in conn.execute("SELECT * FROM import_log ORDER BY id DESC LIMIT 5")]
    return {**dict(latest), "recent_imports": imports}


def list_imports(limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM import_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, max(0, offset)))]
    return {"total": total, "items": rows}


def sources() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM reports WHERE source IS NOT NULL ORDER BY source")]


def overview() -> dict:
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS total_reports, MIN(report_date) AS first_date, MAX(report_date) AS last_date, "
            "COUNT(DISTINCT sender_email) AS senders FROM reports WHERE status='kept'"
        ).fetchone()
        by_status = {s: 0 for s in STATUSES}
        by_status.update({row[0]: row[1] for row in conn.execute(
            "SELECT status, COUNT(*) FROM reports GROUP BY status")})
        cats = {row[0]: row[1] for row in conn.execute(
            "SELECT COALESCE(category,'(chưa phân loại)'), COUNT(*) FROM reports WHERE status='kept' GROUP BY 1")}
        top = [dict(x) for x in conn.execute(
            f"SELECT r.sender_email, MAX(COALESCE(e.name, r.sender_name)) AS name, COUNT(*) AS count {_JOIN} "
            "WHERE r.status='kept' GROUP BY r.sender_email ORDER BY count DESC LIMIT 10")]
        emp = conn.execute("SELECT COUNT(*) FROM employees WHERE active=1").fetchone()[0]
    names = {c["code"]: c["name"] for c in categories()}
    return {
        **dict(r),
        "active_employees": emp,
        "by_status": by_status,
        "pending": by_status["pending"],
        "categories": [{"code": k, "name": names.get(k, k), "count": v}
                       for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
        "top_senders": top,
    }
