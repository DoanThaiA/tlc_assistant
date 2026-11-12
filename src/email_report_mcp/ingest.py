"""Chuẩn hoá dữ liệu email báo cáo do client (Claude) gửi vào trước khi lưu."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from . import db
from .config import settings

# ---------------------------------------------------------------------------
# Làm sạch nội dung
# ---------------------------------------------------------------------------
_QUOTE_MARKERS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.I),
    re.compile(r"^On .{5,200} wrote:\s*$", re.I),
    re.compile(r"^Vào .{5,200} đã viết:\s*$", re.I),
    re.compile(r"^_{20,}\s*$"),
]
_HTML_HINT = re.compile(r"<(html|body|div|p|br|table|span)\b", re.I)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.append(" | ")
    return soup.get_text("\n")


def clean_body(text: str) -> str:
    if _HTML_HINT.search(text or ""):
        text = html_to_text(text)
    lines: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if any(p.match(s) for p in _QUOTE_MARKERS):
            break  # bỏ phần thư cũ được trích dẫn bên dưới
        if s.startswith(">"):
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines)
    out = re.sub(r"[ \t\u00a0]+", " ", out)
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", out)
    return out.strip()


# ---------------------------------------------------------------------------
# Ngày
# ---------------------------------------------------------------------------
_DATE_PATTERNS = [
    (re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2}|\d{2})(?!\d)"), "dmy"),
    (re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})(?![\d/.-])"), "dm"),
]


def date_from_subject(subject: str, sent: date) -> date | None:
    """Tìm ngày trong tiêu đề (vd 'Báo cáo ngày 10/11/2026'). Chỉ nhận nếu gần ngày gửi."""
    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(subject or ""):
            try:
                if kind == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif kind == "dmy":
                    d, mo, y = int(m[1]), int(m[2]), int(m[3])
                    y += 2000 if y < 100 else 0
                else:
                    d, mo, y = int(m[1]), int(m[2]), sent.year
                    if sent.month == 1 and mo == 12:
                        y -= 1
                cand = date(y, mo, d)
            except ValueError:
                continue
            if sent - timedelta(days=31) <= cand <= sent + timedelta(days=1):
                return cand
    return None


def parse_sent_at(value: str | None) -> datetime:
    """Nhận ISO 8601 hoặc định dạng RFC 2822 (header Date của email)."""
    if not value:
        return datetime.now(settings.timezone)
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(v)
        except Exception as e:
            raise ValueError(f"sent_at không hợp lệ: {value!r} (dùng ISO 8601, vd 2026-11-11T17:30:00+07:00)") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings.timezone)
    return dt.astimezone(settings.timezone)


# ---------------------------------------------------------------------------
# Chuẩn hoá 1 bản ghi
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


def normalize(item: dict[str, Any], source: str | None) -> dict[str, Any]:
    sender = (item.get("sender_email") or "").strip()
    m = _EMAIL_RE.search(sender)
    if not m:
        raise ValueError(f"sender_email không hợp lệ: {sender!r}")
    sender_email = m.group(0).lower()

    subject = (item.get("subject") or "").strip()
    sent_dt = parse_sent_at(item.get("sent_at"))
    if item.get("report_date"):
        rdate = date.fromisoformat(str(item["report_date"]).strip())
    else:
        rdate = date_from_subject(subject, sent_dt.date()) or sent_dt.date()

    body = clean_body(item.get("body") or "")
    message_id = (item.get("message_id") or "").strip()
    if not message_id:
        # Không có Message-ID: tạo khoá chống trùng từ người gửi + thời điểm + tiêu đề
        key = f"{sender_email}|{sent_dt.isoformat()}|{db.norm(subject)}"
        message_id = "gen:" + hashlib.sha1(key.encode("utf-8")).hexdigest()

    attachments = [
        {"name": str(a)} if not isinstance(a, dict) else {"name": a.get("name", "(không tên)"), **a}
        for a in (item.get("attachments") or [])
    ]
    return {
        "message_id": message_id,
        "source": source,
        "sender_email": sender_email,
        "sender_name": (item.get("sender_name") or "").strip() or None,
        "subject": subject,
        "report_date": rdate.isoformat(),
        "sent_at": sent_dt.isoformat(timespec="seconds"),
        "body_text": body,
        "attachments": attachments,
        "to_addrs": item.get("to"),
        "cc_addrs": item.get("cc"),
        "thread_id": (str(item.get("thread_id") or "").strip() or None),
        "labels": ", ".join(item["labels"]) if isinstance(item.get("labels"), list) else item.get("labels"),
        "category": item.get("category"),
        "tags": item.get("tags"),
        "summary": item.get("summary"),
    }


MAX_BATCH = 50


def save_batch(items: list[dict[str, Any]], source: str | None, status: str = "kept") -> dict[str, Any]:
    """Lưu một lô email; lỗi từng dòng được báo riêng, không làm hỏng cả lô.
    status="pending": chờ Claude duyệt (luồng Apps Script); "kept": lưu thẳng."""
    if len(items) > MAX_BATCH:
        raise ValueError(f"Tối đa {MAX_BATCH} báo cáo mỗi lần gửi, hãy chia nhỏ lô.")
    results: list[dict[str, Any]] = []
    inserted = duplicate = failed = 0
    for i, item in enumerate(items):
        try:
            r = normalize(item, source)
            r["status"] = status
            if status == "kept" and r.get("category"):
                valid = {c["code"] for c in db.categories()}
                if r["category"] not in valid:
                    raise ValueError(f"category '{r['category']}' không có trong danh mục: {sorted(valid)}")
            new_id = db.insert_report(r)
            if new_id:
                inserted += 1
                results.append({"index": i, "status": "inserted", "id": new_id, "report_date": r["report_date"],
                                "review_status": status})
            else:
                duplicate += 1
                results.append({"index": i, "status": "duplicate", "id": db.find_by_message_id(r["message_id"])})
        except Exception as e:  # noqa: BLE001
            failed += 1
            results.append({"index": i, "status": "error", "error": str(e)})
    if items:
        db.log_import(source, len(items), inserted, duplicate, failed)
    return {"received": len(items), "inserted": inserted, "duplicate": duplicate, "failed": failed,
            "results": results}
