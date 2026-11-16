"""OCR file đính kèm (PDF scan, ảnh chụp) bằng OCR.space — chạy NỀN theo hàng đợi.

Luồng: /ingest trích văn bản như thường; file không có lớp chữ (PDF scan) hoặc ảnh -> attachments.status =
"ocr_pending", nội dung file được giữ TẠM trong bảng ocr_queue. Luồng nền lấy từng file, gửi OCR.space, ghi văn bản
vào email, đánh chỉ mục tìm kiếm lại rồi XOÁ file tạm.

Giới hạn gói miễn phí OCR.space: 1 MB / lần gửi, 3 trang PDF / lần, 500 lượt / ngày / IP. Vì vậy:
  - PDF được tách thành từng phần OCR_PDF_PAGES trang; trang quá lớn thì lấy ảnh trong trang, nén lại rồi OCR.
  - Ảnh quá lớn được thu nhỏ / nén JPEG cho vừa OCR_MAX_BYTES.
  - Đếm số lượt mỗi ngày, chạm OCR_DAILY_LIMIT thì chờ sang ngày sau; bị OCR.space chặn (403/429) thì tạm dừng.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from . import db
from .config import settings

log = logging.getLogger(__name__)

MAX_TRIES = 4
_BACKOFF_MIN = [2, 15, 60, 240]
_FILETYPE = {"JPEG": ("image/jpeg", "JPG"), "PNG": ("image/png", "PNG"), "GIF": ("image/gif", "GIF"),
             "BMP": ("image/bmp", "BMP"), "TIFF": ("image/tiff", "TIF")}
_paused_until = 0.0
_worker: threading.Thread | None = None


class QuotaError(Exception):
    """OCR.space từ chối vì hết lượt / sai key -> tạm dừng, không tính là lỗi của file."""


class OcrError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.ocr_api_key)


def _now() -> datetime:
    return datetime.now(settings.timezone)


# ---------------------------------------------------------------------------
# Đếm lượt mỗi ngày
# ---------------------------------------------------------------------------
def usage_today() -> int:
    u = db.get_setting("ocr_usage", {}) or {}
    return int(u.get("count", 0)) if u.get("date") == db.today() else 0


def _count_call() -> None:
    db.set_setting("ocr_usage", {"date": db.today(), "count": usage_today() + 1})


def status() -> dict:
    with db.connect() as conn:
        q = conn.execute("SELECT COUNT(*), MIN(created_at) FROM ocr_queue").fetchone()
    return {"enabled": enabled(), "queued": q[0], "oldest_queued_at": q[1], "used_today": usage_today(),
            "daily_limit": settings.ocr_daily_limit,
            "paused": _paused_until > time.time(), "engine": settings.ocr_engine}


# ---------------------------------------------------------------------------
# Gọi OCR.space
# ---------------------------------------------------------------------------
def _call(data: bytes, mime: str, filetype: str) -> str:
    if usage_today() >= settings.ocr_daily_limit:
        raise QuotaError(f"đã dùng {settings.ocr_daily_limit} lượt OCR hôm nay")
    form = {
        "base64Image": f"data:{mime};base64," + base64.b64encode(data).decode(),
        "filetype": filetype,
        "OCREngine": str(settings.ocr_engine),
        "scale": "true",
        "detectOrientation": "true",
        "isOverlayRequired": "false",
    }
    if settings.ocr_language:
        form["language"] = settings.ocr_language
    req = urllib.request.Request(
        settings.ocr_url, data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"apikey": settings.ocr_api_key, "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "email-report-mcp"})
    _count_call()
    try:
        with urllib.request.urlopen(req, timeout=settings.ocr_timeout) as r:  # noqa: S310 (URL từ cấu hình)
            res = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403, 429):
            raise QuotaError(f"OCR.space HTTP {e.code}: {body}") from e
        raise OcrError(f"OCR.space HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OcrError(f"không gọi được OCR.space: {e}") from e
    except ValueError as e:
        raise OcrError("OCR.space trả về dữ liệu không hợp lệ") from e
    if not isinstance(res, dict):  # vd thông báo hết lượt trả về dạng chuỗi
        msg = str(res)
        raise (QuotaError if "maximum" in msg.lower() else OcrError)(msg[:300])
    results = res.get("ParsedResults") or []
    texts = [(p.get("ParsedText") or "").strip() for p in results if p.get("FileParseExitCode") == 1]
    if res.get("IsErroredOnProcessing") and not any(texts):
        msg = res.get("ErrorMessage")
        msg = "; ".join(msg) if isinstance(msg, list) else str(msg or res.get("ErrorDetails") or "lỗi OCR")
        raise (QuotaError if "maximum" in msg.lower() else OcrError)(msg[:300])
    return "\n\n".join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Chuẩn bị file cho vừa giới hạn
# ---------------------------------------------------------------------------
def _fit_image(img: Any) -> tuple[bytes, str, str]:
    """Ảnh PIL -> JPEG/PNG <= OCR_MAX_BYTES (thu nhỏ dần nếu cần)."""
    from PIL import Image

    if img.mode in ("P", "LA", "RGBA", "CMYK", "I;16", "I", "F", "1"):
        img = img.convert("L" if img.mode in ("LA", "I;16", "I", "F", "1") else "RGB")
    if max(img.size) > 3000:
        img.thumbnail((3000, 3000), Image.LANCZOS)
    quality = 85
    for _ in range(12):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= settings.ocr_max_bytes:
            return buf.getvalue(), "image/jpeg", "JPG"
        if quality > 60:
            quality -= 10
        else:
            img = img.resize((max(1, int(img.width * 0.8)), max(1, int(img.height * 0.8))), Image.LANCZOS)
    raise OcrError("ảnh quá lớn, không nén được")


def _image_payload(data: bytes) -> tuple[bytes, str, str]:
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    fmt = (img.format or "").upper()
    if fmt in _FILETYPE and len(data) <= settings.ocr_max_bytes:
        mime, ft = _FILETYPE[fmt]
        return data, mime, ft
    img.load()
    return _fit_image(img)


def _ocr_image(data: bytes) -> str:
    payload, mime, ft = _image_payload(data)
    return _call(payload, mime, ft)


def _ocr_pdf(data: bytes) -> tuple[str, str | None]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    total = len(reader.pages)
    n = min(total, settings.ocr_max_pages)
    notes: list[str] = []
    if total > n:
        notes.append(f"chỉ OCR {n}/{total} trang đầu")

    def chunk(start: int, end: int) -> bytes:
        w = PdfWriter()
        for i in range(start, end):
            w.add_page(reader.pages[i])
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()

    parts: list[str] = []
    step = max(1, settings.ocr_pdf_pages)
    i = 0
    while i < n:
        end = min(n, i + step)
        blob = chunk(i, end)
        if len(blob) <= settings.ocr_max_bytes:
            parts.append(_call(blob, "application/pdf", "PDF"))
            i = end
            continue
        if end - i > 1:  # phần nhiều trang quá lớn -> OCR từng trang
            step = 1
            continue
        # 1 trang vẫn quá lớn (ảnh scan độ phân giải cao): OCR các ảnh trong trang sau khi nén
        texts = []
        try:
            for im in reader.pages[i].images:
                payload, mime, ft = _fit_image(im.image)
                texts.append(_call(payload, mime, ft))
        except QuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            notes.append(f"trang {i + 1}: {str(e)[:80]}")
        parts.append("\n".join(t for t in texts if t))
        i += 1
    return "\n\n".join(t for t in parts if t), "; ".join(notes) or None


def ocr_file(name: str, mime: str, data: bytes) -> tuple[str, str | None]:
    from .attachments import _kind

    if _kind(name, mime) == "pdf":
        return _ocr_pdf(data)
    return _ocr_image(data), None


# ---------------------------------------------------------------------------
# Hàng đợi
# ---------------------------------------------------------------------------
def enqueue(conn: Any, report_id: int, att_index: int, name: str, mime: str, data: bytes) -> None:
    if conn.execute("SELECT 1 FROM ocr_queue WHERE report_id=? AND att_index=?", (report_id, att_index)).fetchone():
        return
    now = _now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO ocr_queue(report_id, att_index, att_name, mime_type, data, next_try_at, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (report_id, att_index, name, mime or "", data, now, now))


def _finish(row: Any, text: str, note: str | None, failed: str | None = None) -> None:
    from .attachments import MAX_TEXT_CHARS, _clean

    with db.connect() as conn:
        rep = conn.execute("SELECT status, attachments FROM reports WHERE id=?", (row["report_id"],)).fetchone()
        conn.execute("DELETE FROM ocr_queue WHERE id=?", (row["id"],))
        if not rep or rep["status"] == "discarded":
            return
        atts = json.loads(rep["attachments"] or "[]")
        idx = row["att_index"]
        if not (0 <= idx < len(atts) and atts[idx].get("name") == row["att_name"]):
            idx = next((k for k, a in enumerate(atts) if a.get("name") == row["att_name"]), -1)
        if idx < 0:
            return
        a = atts[idx]
        text = _clean(text or "")
        old = a.get("text") or ""
        if failed:
            a.update(status="ok" if old else "error", note=f"OCR lỗi: {failed}"[:200])
        elif len(text) > len(old):
            truncated = len(text) > MAX_TEXT_CHARS
            if truncated:
                text = text[:MAX_TEXT_CHARS] + "\n…[đã cắt bớt]"
            a.update(status="ok", text=text, chars=len(text), truncated=truncated, ocr=True,
                     note=note or "đọc bằng OCR")
        elif old:
            a.update(status="ok", note=note)
        else:
            a.update(status="no_text", note="OCR không tìm thấy chữ" + (f" ({note})" if note else ""))
        if a.get("note") is None:
            a.pop("note", None)
        conn.execute("UPDATE reports SET attachments=? WHERE id=?",
                     (json.dumps(atts, ensure_ascii=False), row["report_id"]))
        db._index_fts(conn, row["report_id"])


def process_one() -> bool:
    """Xử lý 1 file trong hàng đợi. Trả về True nếu có làm việc (để gọi tiếp ngay)."""
    global _paused_until
    if not enabled() or _paused_until > time.time() or usage_today() >= settings.ocr_daily_limit:
        return False
    now = _now().isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute("SELECT q.*, r.status AS report_status FROM ocr_queue q "
                           "LEFT JOIN reports r ON r.id = q.report_id "
                           "WHERE q.next_try_at <= ? ORDER BY q.id LIMIT 1", (now,)).fetchone()
        if row and (row["report_status"] is None or row["report_status"] == "discarded"):
            conn.execute("DELETE FROM ocr_queue WHERE id=?", (row["id"],))
            return True
    if not row:
        return False
    try:
        text, note = ocr_file(row["att_name"], row["mime_type"], bytes(row["data"]))
    except QuotaError as e:
        log.warning("OCR tạm dừng 1 giờ: %s", e)
        _paused_until = time.time() + 3600
        return False
    except Exception as e:  # noqa: BLE001
        tries = row["tries"] + 1
        msg = str(e) or e.__class__.__name__
        if tries >= MAX_TRIES:
            log.warning("OCR %s thất bại sau %s lần: %s", row["att_name"], tries, msg)
            _finish(row, "", None, failed=msg)
        else:
            nxt = (_now() + timedelta(minutes=_BACKOFF_MIN[tries - 1])).isoformat(timespec="seconds")
            with db.connect() as conn:
                conn.execute("UPDATE ocr_queue SET tries=?, next_try_at=?, last_error=? WHERE id=?",
                             (tries, nxt, msg[:300], row["id"]))
        return True
    _finish(row, text, note)
    return True


def _loop() -> None:
    while True:
        try:
            did = process_one()
        except Exception:  # noqa: BLE001
            log.exception("Lỗi luồng OCR")
            did = False
        time.sleep(1 if did else settings.ocr_poll_seconds)


def start_worker() -> None:
    global _worker
    if not enabled() or (_worker and _worker.is_alive()):
        return
    _worker = threading.Thread(target=_loop, name="ocr-worker", daemon=True)
    _worker.start()
    log.info("OCR.space đang bật (engine %s, tối đa %s lượt/ngày)", settings.ocr_engine, settings.ocr_daily_limit)
