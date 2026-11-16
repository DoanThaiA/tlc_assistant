"""Trích văn bản từ file đính kèm email (Apps Script gửi nội dung file dạng base64).

Hỗ trợ: PDF (có lớp chữ), Word .docx, Excel .xlsx/.xlsm/.xls, PowerPoint .pptx, CSV/TXT/MD/JSON/XML, HTML.
Chỉ lưu VĂN BẢN đã trích (không lưu file gốc) vào reports.attachments:
    {name, mime_type, size, status, text, chars, truncated, note}
status: ok | no_text (vd PDF scan ảnh) | unsupported | too_large | error | name_only (client chỉ gửi tên)
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import re
import zipfile
from typing import Any

MAX_FILE_BYTES = 15 * 1024 * 1024        # file lớn hơn: không đọc
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024   # chống "zip bomb" với docx/xlsx/pptx
MAX_TEXT_CHARS = 60_000                  # văn bản lưu tối đa mỗi file
MAX_PDF_PAGES = 300
MAX_SHEET_ROWS = 5_000

STATUS_LABEL = {
    "ok": "đã đọc",
    "no_text": "không có chữ",
    "unsupported": "định dạng chưa hỗ trợ",
    "too_large": "file quá lớn",
    "error": "lỗi đọc file",
    "name_only": "chỉ có tên file",
}

_TEXT_EXT = {"txt", "csv", "tsv", "md", "json", "xml", "log"}
_HTML_EXT = {"htm", "html"}


def _ext(name: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", name or "")
    return m.group(1).lower() if m else ""


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    for enc in ("cp1258", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as e:
        raise ValueError("file hỏng hoặc không đúng định dạng") from e
    if total > MAX_UNZIPPED_BYTES:
        raise ValueError("file giải nén quá lớn")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%d/%m/%Y") if not getattr(v, "hour", 0) and not getattr(v, "minute", 0) \
                else v.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return str(v)
    return str(v).strip()


def _rows_text(rows: Any, out: list[str], budget: list[int]) -> None:
    n = 0
    for row in rows:
        cells = [_cell(v) for v in row]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        line = " | ".join(cells)
        out.append(line)
        budget[0] -= len(line) + 1
        n += 1
        if n >= MAX_SHEET_ROWS or budget[0] <= 0:
            out.append("…[còn nữa, đã cắt]")
            break


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    _check_zip(data)
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    budget = [MAX_TEXT_CHARS]
    try:
        for ws in wb.worksheets:
            if budget[0] <= 0:
                break
            out.append(f"## Sheet: {ws.title}")
            _rows_text(ws.iter_rows(values_only=True), out, budget)
    finally:
        wb.close()
    return "\n".join(out)


def _xls(data: bytes) -> str:
    import xlrd

    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    out: list[str] = []
    budget = [MAX_TEXT_CHARS]
    for sh in book.sheets():
        if budget[0] <= 0:
            break
        out.append(f"## Sheet: {sh.name}")
        rows = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                row.append(cell.value)
            rows.append(row)
        _rows_text(rows, out, budget)
    return "\n".join(out)


def _docx(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _check_zip(data)
    d = docx.Document(io.BytesIO(data))
    out: list[str] = []
    # Giữ đúng thứ tự đoạn văn / bảng trong thân văn bản
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = Paragraph(child, d).text.strip()
            if t:
                out.append(t)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells: list[str] = []
                for c in row.cells:
                    t = c.text.strip().replace("\n", " ")
                    if not cells or cells[-1] != t:  # ô gộp lặp lại
                        cells.append(t)
                if any(cells):
                    out.append(" | ".join(cells))
    return "\n".join(out)


def _pptx(data: bytes) -> str:
    from pptx import Presentation

    _check_zip(data)
    prs = Presentation(io.BytesIO(data))
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"## Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    out.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(out)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001
            raise ValueError("PDF có mật khẩu") from e
    out: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES or total > MAX_TEXT_CHARS:
            out.append("…[còn nữa, đã cắt]")
            break
        t = (page.extract_text() or "").strip()
        if t:
            out.append(t)
            total += len(t)
    return "\n\n".join(out)


def _csv(data: bytes) -> str:
    text = _decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        rows = csv.reader(io.StringIO(text), dialect)
        out: list[str] = []
        _rows_text(rows, out, [MAX_TEXT_CHARS])
        return "\n".join(out)
    except csv.Error:
        return text


def _html(data: bytes) -> str:
    from .ingest import html_to_text

    return html_to_text(_decode_text(data))


def _kind(name: str, mime: str) -> str | None:
    ext = _ext(name)
    mime = (mime or "").lower()
    if ext == "pdf" or mime == "application/pdf":
        return "pdf"
    if ext in ("docx", "docm") or "wordprocessingml" in mime:
        return "docx"
    if ext in ("xlsx", "xlsm") or "spreadsheetml" in mime:
        return "xlsx"
    if ext == "xls" or mime == "application/vnd.ms-excel":
        return "xls"
    if ext == "pptx" or "presentationml" in mime:
        return "pptx"
    if ext in ("csv", "tsv") or mime in ("text/csv", "text/tab-separated-values"):
        return "csv"
    if ext in _HTML_EXT or mime == "text/html":
        return "html"
    if ext in _TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    return None


_EXTRACTORS = {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "xls": _xls, "pptx": _pptx, "csv": _csv,
               "html": _html, "text": _decode_text}


def supported(name: str, mime: str = "") -> bool:
    return _kind(name, mime) is not None


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def extract(name: str, mime: str, data: bytes) -> dict[str, Any]:
    """Trích văn bản 1 file. Không bao giờ ném lỗi: lỗi được ghi vào status/note."""
    kind = _kind(name, mime)
    if kind is None:
        return {"status": "unsupported", "note": f"chưa đọc được định dạng .{_ext(name) or '?'}"}
    if len(data) > MAX_FILE_BYTES:
        return {"status": "too_large", "note": f"file {len(data) // 1024 // 1024} MB, giới hạn "
                                                f"{MAX_FILE_BYTES // 1024 // 1024} MB"}
    try:
        text = _clean(_EXTRACTORS[kind](data))
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        return {"status": "error", "note": msg[:200]}
    if not text:
        note = "PDF không có lớp chữ (ảnh scan) — cần OCR" if kind == "pdf" else "file không có nội dung chữ"
        return {"status": "no_text", "note": note}
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS] + "\n…[đã cắt bớt]"
    return {"status": "ok", "text": text, "chars": len(text), "truncated": truncated}


def process(item: Any) -> dict[str, Any]:
    """Chuẩn hoá 1 phần tử attachments từ client: tên (chuỗi) hoặc object {name, mime_type, size, data, skipped}."""
    if not isinstance(item, dict):
        return {"name": str(item), "status": "name_only"}
    name = str(item.get("name") or "(không tên)")
    mime = str(item.get("mime_type") or item.get("content_type") or "")
    out: dict[str, Any] = {"name": name}
    if mime:
        out["mime_type"] = mime
    if isinstance(item.get("size"), int):
        out["size"] = item["size"]
    data_b64 = item.get("data")
    if data_b64:
        try:
            data = base64.b64decode(str(data_b64), validate=False)
        except (binascii.Error, ValueError):
            out.update(status="error", note="dữ liệu base64 không hợp lệ")
            return out
        out.setdefault("size", len(data))
        out.update(extract(name, mime, data))
        return out
    if item.get("text"):  # client đã tự trích văn bản
        text = _clean(str(item["text"]))[:MAX_TEXT_CHARS]
        out.update(status="ok", text=text, chars=len(text), truncated=False)
        return out
    skipped = str(item.get("skipped") or "")
    if skipped == "too_large":
        out.update(status="too_large", note="file quá lớn, Apps Script không gửi nội dung")
    elif skipped == "unsupported" or (skipped == "" and not supported(name, mime)):
        out.update(status="unsupported", note=f"chưa đọc được định dạng .{_ext(name) or '?'}")
    elif skipped:
        out.update(status="error", note=skipped[:200])
    else:
        out["status"] = "name_only"
    return out


def has_text(att: dict) -> bool:
    return bool(att.get("text"))


def describe(att: dict) -> str:
    st = att.get("status") or ("ok" if att.get("text") else "name_only")
    label = STATUS_LABEL.get(st, st)
    return label + (f" — {att['note']}" if att.get("note") and st != "ok" else "")
