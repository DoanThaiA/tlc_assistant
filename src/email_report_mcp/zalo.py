"""Zalo: theo dõi các nhóm chat, thu thập THÔNG TIN CÔNG VIỆC và trả lời câu hỏi ngay trên Zalo.

Claude Desktop đọc / gửi tin trên Zalo Web (Claude in Chrome). Chỉ GỬI tin để trả lời câu hỏi / yêu cầu trong hội
thoại bật reply: (1) nhắn riêng với CEO (kind=direct, allowed_askers = CEO) hoặc (2) NHÓM TRỢ LÝ (kind=group,
reply=true; allowed_askers trống = mọi thành viên được hỏi). Hội thoại không bật reply là CHỈ ĐỌC.
Máy chủ lưu:
  - zalo_conversations: nhóm / hội thoại được theo dõi + mốc tin cuối đã đọc + quyền trả lời
  - zalo_messages (+ zalo_messages_fts): tin có thông tin công việc (tiến độ, số liệu, vướng mắc, quyết định,
    lịch hẹn, khách hàng...) kèm tóm tắt / phân loại / tag / cờ quan trọng do Claude gắn
  - zalo_replies: câu hỏi đã / đang trả lời trên Zalo (chống trả lời 2 lần, kiểm tra lại)
Tin trò chuyện thường (chào hỏi, "ok", sticker...) KHÔNG được lưu.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from . import db
from .config import settings
from .db import connect, nfc, norm

KINDS = ("group", "direct")

DEFAULT_ZALO_RULES = """LƯU (thông tin công việc CEO có thể cần hỏi lại):
- Báo cáo tiến độ, kết quả, số liệu (doanh số, đơn hàng, công nợ, tồn kho, KPI...).
- Vướng mắc, sự cố, chậm trễ, rủi ro, khiếu nại của khách hàng; đề xuất, xin ý kiến, xin duyệt.
- Quyết định, chỉ đạo, phân công (ai làm gì, hạn khi nào), thay đổi kế hoạch.
- Thông tin khách hàng / đối tác / dự án: báo giá, hợp đồng, đơn hàng, lịch giao, lịch họp.
- Tin có file / ảnh chứng từ quan trọng: ghi lại tên file và ý chính đi kèm; ảnh có thông tin công việc (bảng tính,
  chứng từ, hoá đơn, phiếu, màn hình số liệu) lưu kèm images (mô tả + link) để đọc được chữ trong ảnh.

KHÔNG LƯU:
- Chào hỏi, cảm ơn, "ok", "dạ vâng", sticker, emoji, tin đùa vui, chuyện cá nhân.
- Tin hệ thống (thêm/rời nhóm, đổi tên nhóm), quảng cáo, link không rõ nội dung.

GỘP: nhiều tin liên tiếp cùng một người, cùng một ý -> lưu thành MỘT mục (text = nối các tin, sent_at = tin đầu).
QUAN TRỌNG (important = true): vướng mắc, sự cố, gấp, chậm trễ, khiếu nại, cần CEO quyết định / duyệt, số liệu
bất thường.

TRẢ LỜI TRÊN ZALO — CHỈ trong hội thoại bật reply (start_cycle.reply_conversations), CHỈ khi có người hỏi / yêu cầu.
Mọi nhóm / hội thoại khác là CHỈ ĐỌC: KHÔNG BAO GIỜ nhắn.
- Hội thoại nhắn riêng với CEO (kind = direct): chỉ trả lời CEO (allowed_askers).
- NHÓM TRỢ LÝ (kind = group, reply = true): trả lời câu hỏi của mọi thành viên (hoặc chỉ những người trong
  allowed_askers nếu có khai báo). Chỉ trả lời tin HỎI TRỢ LÝ (có @ tên tài khoản này, hoặc rõ ràng nhờ tra cứu /
  tổng hợp thông tin); KHÔNG chen vào cuộc trao đổi giữa người với người, không chủ động nhắn khi chưa ai hỏi,
  không nhắn cho người / nhóm khác. Trả lời đúng nhóm nơi câu hỏi được hỏi, mở đầu bằng tên người hỏi.
- Tin bên PHẢI là của chính tài khoản này, không phải câu hỏi; tin bên TRÁI là của người khác.
- Câu hỏi thông tin: tra cả email (get_briefing, search_reports, get_digest, get_reports_content, count_emails...)
  và Zalo (search_zalo, get_zalo_digest, get_zalo_messages).
- Yêu cầu tổng hợp / thống kê / điểm tin: làm như prompt tong_hop_bao_cao_ngay / diem_tin_ceo / thong_ke_bao_cao
  nhưng viết gọn cho Zalo.
- TRONG NHÓM: chỉ chia sẻ thông tin công việc chung (tiến độ, kết quả, số liệu tổng hợp, lịch họp, vướng mắc dự án).
  KHÔNG tiết lộ: lương thưởng, thông tin cá nhân, chi tiết hợp đồng / giá / công nợ từng khách, nội dung email riêng
  của người khác, tài khoản / mật khẩu. Câu hỏi như vậy: từ chối lịch sự và mời hỏi trực tiếp CEO.
- Yêu cầu theo dõi thêm / bỏ theo dõi một nhóm (upsert_zalo_conversation, chỉ watch, không bật reply), đổi tiêu chí
  lọc email (update_filter_rules) / lưu tin Zalo (update_zalo_rules): CHỈ làm khi CEO yêu cầu trong hội thoại NHẮN
  RIÊNG. Trong nhóm: từ chối lịch sự, mời nhắn riêng CEO. Làm xong thì nhắn xác nhận.
- Văn phong: ngắn gọn, lịch sự, xưng "em" gọi "anh/chị"; Zalo không hiển thị markdown -> không dùng bảng, **, #;
  dùng gạch đầu dòng "-" và số thứ tự. Có số liệu thì kèm nguồn ngắn (vd "theo email của An 10/11",
  "theo nhóm Kỹ thuật 9h30 hôm nay"). Dài quá ~1.500 ký tự thì tách thành vài tin.
- Không tìm thấy dữ liệu: nói rõ là chưa có thông tin, KHÔNG suy đoán.
- Từ chối lịch sự: chuyển tiền, gửi file / mật khẩu / dữ liệu ra ngoài, nhắn hộ cho người / nhóm khác, bật quyền trả lời
  cho người / nhóm khác, làm thay quyết định."""


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------
def _now_dt() -> datetime:
    return datetime.now(settings.timezone).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(settings.timezone).isoformat(timespec="seconds")


def parse_dt(value: str | None, name: str) -> str | None:
    """'2026-11-12T17:00', '2026-11-12 17:00:00+07:00' -> ISO theo múi giờ cấu hình (thiếu múi giờ = giờ VN)."""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    if len(s) > 10:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{name} phải là ngày giờ ISO, vd 2026-11-12T09:30:00+07:00 — nhận được: {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings.timezone)
    return _iso(dt.replace(microsecond=0))


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", norm(text)).strip()


def message_key(conversation_id: int, sender: str | None, sent_at: str | None, text: str | None) -> str:
    """Mã một tin: hội thoại + người gửi + NGÀY gửi + nội dung (không dấu). Dùng ngày (không dùng phút) vì giờ
    đọc trên Zalo Web có thể lệch vài phút giữa các lần quét."""
    raw = f"{conversation_id}|{_squash(sender)}|{(sent_at or '')[:10]}|{_squash(text)[:500]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _default_range(date_from: str | None, date_to: str | None, days: int = 1) -> tuple[str, str]:
    dt = db._check_date(date_to, "date_to") or db.today()
    df = db._check_date(date_from, "date_from") or (date.fromisoformat(dt) - timedelta(days=days - 1)).isoformat()
    return df, dt


# ---------------------------------------------------------------------------
# Quy tắc thu thập
# ---------------------------------------------------------------------------
def rules() -> str:
    return db.get_setting("zalo_rules", DEFAULT_ZALO_RULES)


def save_rules(new_rules: str | None = None, mode: str = "append") -> dict:
    if new_rules is not None:
        text = nfc(new_rules).strip()
        if mode == "append":
            text = rules().rstrip() + "\n" + text
        if not text.strip():
            raise ValueError("Quy tắc thu thập Zalo không được để trống")
        db.set_setting("zalo_rules", text)
    return {"rules": rules(), "categories": db.categories()}


# ---------------------------------------------------------------------------
# Hội thoại theo dõi
# ---------------------------------------------------------------------------
def _conv_out(r: sqlite3.Row, counts: dict[int, int] | None = None) -> dict:
    d = dict(r)
    d.pop("name_norm", None)
    d["watch"] = bool(d["watch"])
    d["reply"] = bool(d.get("reply"))
    if counts is not None:
        d["saved_messages"] = counts.get(d["id"], 0)
    return d


def _words(text: str | None) -> str:
    return " ".join(re.findall(r"\w+", norm(text)))


def _find_conv(conn: sqlite3.Connection, name: str, fuzzy: bool = True) -> sqlite3.Row | None:
    """Tìm hội thoại: đúng tên (không dấu) -> đúng tên bỏ dấu câu -> chứa chuỗi (chỉ khi duy nhất một hội thoại,
    và chỉ khi fuzzy=True; thêm/sửa hội thoại không dùng để tránh sửa nhầm)."""
    row = conn.execute("SELECT * FROM zalo_conversations WHERE name_norm=?", (_squash(name),)).fetchone()
    if row or not _words(name):
        return row
    rows = conn.execute("SELECT * FROM zalo_conversations").fetchall()
    q = _words(name)
    exact = [r for r in rows if _words(r["name"]) == q]
    if len(exact) == 1:
        return exact[0]
    if not fuzzy:
        return None
    partial = [r for r in rows if q in _words(r["name"])]
    return partial[0] if len(partial) == 1 else None


def _askers(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else value
    out = [nfc(x).strip() for x in items if nfc(x).strip()]
    return ", ".join(dict.fromkeys(out)) if out else ""


def upsert_conversation(name: str, kind: str | None = None, watch: bool | None = None,
                        note: str | None = None, reply: bool | None = None,
                        allowed_askers: str | list[str] | None = None) -> dict:
    name = nfc(name).strip()
    if not name:
        raise ValueError("Cần tên hội thoại")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind không hợp lệ: {kind!r}. Dùng group hoặc direct")
    with connect() as conn:
        row = _find_conv(conn, name, fuzzy=False)
        # Hội thoại NHẮN RIÊNG bật trả lời phải chỉ rõ người được hỏi (CEO). NHÓM trợ lý: allowed_askers trống =
        # mọi thành viên được hỏi.
        eff_kind = kind or (row["kind"] if row else "group")
        eff_reply = bool(reply) if reply is not None else bool(row and row["reply"])
        eff_askers = _askers(allowed_askers) if allowed_askers is not None else (row["allowed_askers"] if row else None)
        if eff_reply and eff_kind == "direct" and not (eff_askers or "").strip():
            raise ValueError("Hội thoại nhắn riêng bật trả lời cần allowed_askers = tên hiển thị của CEO trên Zalo.")
        if row is None:
            conn.execute(
                "INSERT INTO zalo_conversations(name, name_norm, kind, watch, reply, allowed_askers, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (name, _squash(name), kind or "group", 1 if watch is None else int(watch), int(bool(reply)),
                 _askers(allowed_askers) or None, nfc(note).strip() or None))
        else:
            sets, params = ["name=?", "name_norm=?"], [name, _squash(name)]
            if kind is not None:
                sets.append("kind=?")
                params.append(kind)
            if watch is not None:
                sets.append("watch=?")
                params.append(int(watch))
            if reply is not None:
                sets.append("reply=?")
                params.append(int(reply))
            if allowed_askers is not None:
                sets.append("allowed_askers=?")
                params.append(_askers(allowed_askers) or None)
            if note is not None:
                sets.append("note=?")
                params.append(nfc(note).strip() or None)
            conn.execute(f"UPDATE zalo_conversations SET {', '.join(sets)} WHERE id=?", [*params, row["id"]])
        return _conv_out(_find_conv(conn, name, fuzzy=False))


def list_conversations(only_watched: bool = False) -> list[dict]:
    with connect() as conn:
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT conversation_id, COUNT(*) FROM zalo_messages GROUP BY conversation_id")}
        rows = conn.execute("SELECT * FROM zalo_conversations" + (" WHERE watch=1" if only_watched else "")
                            + " ORDER BY name").fetchall()
    return [_conv_out(r, counts) for r in rows]


def _is_reply_conv(c: dict) -> bool:
    """Hội thoại được gửi trả lời: nhóm trợ lý (group + reply) hoặc nhắn riêng với CEO (direct + reply + askers)."""
    return bool(c["reply"]) and (c["kind"] == "group" or bool((c.get("allowed_askers") or "").strip()))


def cycle_context() -> dict:
    """Đầu mỗi lượt định kỳ: có việc mới không (email chờ lọc, Zalo) + mọi thứ cần để làm việc."""
    now = _now_dt()
    convs = list_conversations(only_watched=True)
    reply_convs = [c for c in convs if _is_reply_conv(c)]
    groups = [{**c, "reply": False} for c in convs if not _is_reply_conv(c)]
    cp = db.checkpoint()
    with connect() as conn:
        unfinished = [dict(r) for r in conn.execute(
            "SELECT r.id AS reply_id, c.name AS conversation, r.asker, r.asked_at, r.question, r.created_at "
            "FROM zalo_replies r JOIN zalo_conversations c ON c.id=r.conversation_id "
            "WHERE r.status='claimed' ORDER BY r.id")]
    steps = []
    if cp["pending"]:
        steps.append(f"Có {cp['pending']} email chờ lọc -> get_pending_emails -> review_emails tới khi hết.")
    else:
        steps.append("Không có email chờ lọc.")
    steps.append("Mở chat.zalo.me, đọc DANH SÁCH hội thoại bên trái (tin cuối + giờ + số chưa đọc), so với "
                 "last_message_preview / last_message_at. Chỉ mở hội thoại có tin MỚI sau mốc.")
    if unfinished:
        steps.append("Có câu hỏi đã nhận nhưng chưa ghi kết quả (unfinished_replies): mở hội thoại đó xem đã gửi trả "
                     "lời chưa -> record_zalo_reply (sent kèm nguyên văn / skipped).")
    steps.append("Không có email chờ lọc và không hội thoại nào có tin mới -> KẾT THÚC lượt, báo 'Không có việc mới'.")
    return {
        "now": _iso(now),
        "today": now.date().isoformat(),
        "timezone": settings.timezone.key,
        "email": {"pending": cp["pending"], "latest_sent_at": cp["latest_sent_at"],
                  "last_import_at": cp["recent_imports"][0]["at"] if cp["recent_imports"] else None},
        "reply_conversations": reply_convs,
        "watched_groups": groups,
        "unfinished_replies": unfinished,
        "warnings": ([] if reply_convs else [
            "Chưa có hội thoại trả lời (reply=true: nhóm trợ lý, hoặc nhắn riêng với CEO kèm allowed_askers). "
            "Không trả lời trên Zalo cho tới khi cấu hình bằng upsert_zalo_conversation."]),
        "rules": rules(),
        "categories": db.categories(),
        "next_steps": steps,
    }


# ---------------------------------------------------------------------------
# Lưu tin thu thập được
# ---------------------------------------------------------------------------
def _index(conn: sqlite3.Connection, mid: int) -> None:
    conn.execute("DELETE FROM zalo_messages_fts WHERE rowid=?", (mid,))
    m = conn.execute("SELECT m.*, c.name AS conversation FROM zalo_messages m "
                     "JOIN zalo_conversations c ON c.id=m.conversation_id WHERE m.id=?", (mid,)).fetchone()
    if m:
        imgs = [x for i in conn.execute("SELECT description, text FROM zalo_images WHERE message_id=? ORDER BY id",
                                        (mid,)) for x in (i["description"], i["text"])]
        conn.execute(
            "INSERT INTO zalo_messages_fts(rowid, body, meta, sender) VALUES (?,?,?,?)",
            (mid, norm(" ".join(filter(None, [m["summary"], m["text"], *imgs]))),
             norm(" ".join(filter(None, [m["conversation"], (m["tags"] or "").replace(",", " "),
                                         (m["category"] or "").replace("_", " ")]))),
             norm(m["sender"])))


# ---------------------------------------------------------------------------
# Ảnh trong tin Zalo: Claude ghi mô tả ngắn + link ảnh; máy chủ tải ảnh (chỉ từ CDN Zalo) và OCR chạy nền.
# Không tải được (link cần đăng nhập / hết hạn / blob:) -> trả về images_to_read để Claude tự đọc ảnh.
# ---------------------------------------------------------------------------
MAX_IMAGES_PER_MESSAGE = 20
MAX_IMAGES_PER_CALL = 60
IMAGE_TEXT_CHARS = 20000
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/130.0 Safari/537.36")


def _host_ok(url: str | None) -> bool:
    try:
        p = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == h or host.endswith("." + h)
                                                 for h in settings.zalo_image_hosts)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if not _host_ok(newurl):
            raise urllib.error.URLError("link chuyển hướng ra ngoài tên miền ảnh Zalo")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url: str) -> bytes:
    """Tải ảnh từ CDN Zalo (chỉ các tên miền ZALO_IMAGE_HOSTS). Lỗi -> ValueError có lý do dễ hiểu."""
    if not _host_ok(url):
        raise ValueError("link không thuộc tên miền ảnh Zalo (ZALO_IMAGE_HOSTS)")
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://chat.zalo.me/",
                                               "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    try:
        with opener.open(req, timeout=settings.zalo_image_timeout) as r:
            data = r.read(settings.zalo_image_max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"máy chủ ảnh trả về HTTP {e.code} (link cần đăng nhập Zalo hoặc đã hết hạn)") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"không tải được ảnh: {getattr(e, 'reason', e)}") from e
    if len(data) > settings.zalo_image_max_bytes:
        raise ValueError("ảnh quá lớn")
    from PIL import Image

    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception as e:  # noqa: BLE001
        raise ValueError("link không trả về ảnh (có thể cần đăng nhập Zalo / link hết hạn)") from e
    return data


def _clip(text: str | None, n: int = IMAGE_TEXT_CHARS) -> str | None:
    t = nfc(text).strip()
    return (t[:n] + "…") if len(t) > n else (t or None)


def _add_images(conn: sqlite3.Connection, mid: int, images: list[dict], now: str, ocr_on: bool,
                fetch: list[tuple[int, str]], to_read: list[dict], counts: dict[str, int]) -> None:
    for img in images[:MAX_IMAGES_PER_MESSAGE]:
        url = (img.get("url") or "").strip() or None
        desc = _clip(img.get("description"), 500)
        text = _clip(img.get("text"))
        if not (url or desc or text):
            continue
        if url is None and conn.execute("SELECT 1 FROM zalo_images WHERE message_id=? AND url IS NULL AND "
                                        "COALESCE(description,'')=?", (mid, desc or "")).fetchone():
            counts["duplicate"] += 1
            continue
        reason = None
        if text:
            status, source = "ok", "claude"
        elif not url:
            status, source = "described", None
        elif not url.startswith(("http://", "https://")):
            status, source, reason = "download_failed", None, "link không phải http(s) (vd blob:) — máy chủ không tải được"
        elif not _host_ok(url):
            status, source, reason = "download_failed", None, "link không thuộc tên miền ảnh Zalo (ZALO_IMAGE_HOSTS)"
        elif not ocr_on:
            status, source, reason = "no_ocr", None, "máy chủ chưa bật OCR (OCR_SPACE_API_KEY)"
        else:
            status, source = "downloading", None
        cur = conn.execute(
            "INSERT OR IGNORE INTO zalo_images(message_id, url, description, status, text, source, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (mid, url, desc, status, text, source, reason, now))
        if cur.rowcount == 0:
            counts["duplicate"] += 1
            continue
        iid = cur.lastrowid
        if status == "downloading":
            fetch.append((iid, url))  # đếm sau khi tải (queued_ocr / not_downloaded)
            continue
        if reason:
            to_read.append({"image_id": iid, "message_id": mid, "url": url, "description": desc, "reason": reason})
        counts[{"ok": "read_by_claude", "described": "described"}.get(status, "not_downloaded")] += 1


def _fetch_images(fetch: list[tuple[int, str]], to_read: list[dict], counts: dict[str, int]) -> None:
    """Tải song song các ảnh cần OCR (ngoài giao dịch CSDL), rồi đưa vào hàng đợi OCR."""
    if not fetch:
        return

    def get(item: tuple[int, str]) -> tuple[int, str, bytes | None, str | None]:
        try:
            return item[0], item[1], download_image(item[1]), None
        except ValueError as e:
            return item[0], item[1], None, str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(get, fetch))
    now = _iso(_now_dt())
    with connect() as conn:
        for iid, url, data, err in results:
            if data is not None:
                conn.execute("UPDATE zalo_images SET status='ocr_pending', data=?, next_try_at=? WHERE id=?",
                             (data, now, iid))
                counts["queued_ocr"] += 1
            else:
                conn.execute("UPDATE zalo_images SET status='download_failed', note=? WHERE id=?", (err, iid))
                r = conn.execute("SELECT message_id, description FROM zalo_images WHERE id=?", (iid,)).fetchone()
                counts["not_downloaded"] += 1
                if r:
                    to_read.append({"image_id": iid, "message_id": r["message_id"], "url": url,
                                    "description": r["description"], "reason": err})


def save_image_text(image_id: int, text: str, description: str | None = None) -> dict:
    """Claude tự đọc ảnh (máy chủ không tải / OCR được) -> lưu chữ / số liệu đọc được vào ảnh đó."""
    text = _clip(text)
    if not text:
        raise ValueError("text rỗng")
    with connect() as conn:
        r = conn.execute("SELECT message_id, description FROM zalo_images WHERE id=?", (image_id,)).fetchone()
        if not r:
            raise ValueError(f"Không có ảnh image_id={image_id}")
        conn.execute("UPDATE zalo_images SET status='ok', text=?, source='claude', description=?, data=NULL, "
                     "note=NULL WHERE id=?", (text, _clip(description, 500) or r["description"], image_id))
        _index(conn, r["message_id"])
    return {"image_id": image_id, "message_id": r["message_id"], "status": "ok"}


def _images_for(conn: sqlite3.Connection, ids: list[int], text_chars: int | None = None) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    for i in conn.execute(f"SELECT id, message_id, description, status, text, source, note FROM zalo_images "
                          f"WHERE message_id IN ({q}) ORDER BY id", ids):
        d = dict(i)
        mid = d.pop("message_id")
        if text_chars is not None and d["text"] and len(d["text"]) > text_chars:
            d["text"] = d["text"][:text_chars] + "…"
        out.setdefault(mid, []).append({k: v for k, v in d.items() if v is not None})
    return out


def save_messages(conversation: str, messages: list[dict], last_message: dict | None = None) -> dict:
    """Lưu các tin có thông tin công việc + cập nhật mốc đã đọc. Tin trùng (cùng msg_key) được bỏ qua
    (ảnh mới kèm tin trùng vẫn được thêm)."""
    from . import ocr

    cats = {c["code"] for c in db.categories()}
    now = _iso(_now_dt())
    inserted, duplicate, errors = [], 0, []
    if sum(len(m.get("images") or []) for m in messages) > MAX_IMAGES_PER_CALL:
        raise ValueError(f"Tối đa {MAX_IMAGES_PER_CALL} ảnh mỗi lần lưu — chia thành nhiều lần")
    ocr_on = ocr.enabled()
    fetch: list[tuple[int, str]] = []
    to_read: list[dict] = []
    counts = dict.fromkeys(("queued_ocr", "read_by_claude", "described", "not_downloaded", "duplicate"), 0)
    with connect() as conn:
        row = _find_conv(conn, conversation)
        if row is None:
            raise ValueError(f"Chưa đăng ký hội thoại Zalo {conversation!r}. Thêm bằng upsert_zalo_conversation.")
        cid = row["id"]
        for i, m in enumerate(messages):
            try:
                text = nfc(m.get("text")).strip()
                if not text:
                    raise ValueError("text rỗng")
                sent_at = parse_dt(m.get("sent_at"), "sent_at")
                if not sent_at:
                    raise ValueError("thiếu sent_at")
                category = (m.get("category") or "khac").strip()
                if category not in cats:
                    raise ValueError(f"category {category!r} không có trong danh mục")
                key = message_key(cid, m.get("sender"), sent_at, text)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO zalo_messages(conversation_id, msg_key, sender, sent_at, msg_date, text,
                       category, summary, tags, important, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, key, nfc(m.get("sender")).strip() or None, sent_at, sent_at[:10], text, category,
                     nfc(m.get("summary")).strip() or None, db.norm_tags(m.get("tags")),
                     int(bool(m.get("important"))), now))
                if cur.rowcount == 0:
                    duplicate += 1
                    mid = conn.execute("SELECT id FROM zalo_messages WHERE msg_key=?", (key,)).fetchone()[0]
                else:
                    mid = cur.lastrowid
                    inserted.append(mid)
                if m.get("images"):
                    _add_images(conn, mid, m["images"], now, ocr_on, fetch, to_read, counts)
                if cur.rowcount or m.get("images"):
                    _index(conn, mid)
            except ValueError as e:
                errors.append({"index": i, "error": str(e)})
        sets: dict[str, Any] = {"last_scanned_at": now}
        if last_message and (last_message.get("text") or last_message.get("sent_at")):
            last_at = parse_dt(last_message.get("sent_at"), "last_message.sent_at")
            sets.update(
                last_message_key=message_key(cid, last_message.get("sender"), last_at, last_message.get("text")),
                last_message_at=last_at or row["last_message_at"],
                last_message_preview=(f"{nfc(last_message.get('sender'))}: " if last_message.get("sender") else "")
                + nfc(last_message.get("text"))[:150])
        conn.execute(f"UPDATE zalo_conversations SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                     [*sets.values(), cid])
    _fetch_images(fetch, to_read, counts)
    out: dict[str, Any] = {"conversation": row["name"], "inserted": len(inserted), "duplicate": duplicate,
                           "errors": errors, "ids": inserted}
    if any(counts.values()):
        out["images"] = counts
    if to_read:
        out["images_to_read"] = to_read
        out["next_step"] = ("Máy chủ không đọc được các ảnh trong images_to_read: mở ảnh trên Zalo (bấm để xem lớn), "
                            "đọc chữ / số liệu chính rồi gọi save_zalo_image_text (tối đa ~10 ảnh mỗi lượt, ưu "
                            "tiên ảnh quan trọng).")
    return out


def delete_messages(ids: list[int]) -> int:
    with connect() as conn:
        n = 0
        for i in ids:
            conn.execute("DELETE FROM zalo_messages_fts WHERE rowid=?", (i,))
            n += conn.execute("DELETE FROM zalo_messages WHERE id=?", (i,)).rowcount
    return n


# ---------------------------------------------------------------------------
# Trả lời câu hỏi trên Zalo
# ---------------------------------------------------------------------------
CLAIM_TIMEOUT_MINUTES = 30   # câu hỏi "đang soạn" quá lâu (lượt trước bị gián đoạn) thì cho nhận lại
SAME_QUESTION_MINUTES = 15   # cùng nội dung, giờ hỏi lệch trong khoảng này = cùng một câu hỏi (giờ đọc trên Zalo lệch)


def _minutes_apart(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 60


def _asker_allowed(allowed: str | None, sender: str | None) -> bool:
    if not allowed or not allowed.strip():
        return True
    # Khớp theo TỪ: mọi từ của tên được phép phải có trong tên người gửi ("Thái" khớp "Doãn Quốc Thái",
    # nhưng "An" không khớp "Doãn Quốc Thái" vì không cắt giữa từ)
    words = set(_squash(sender).split())
    names = [set(_squash(a).split()) for a in allowed.split(",") if _squash(a)]
    return bool(words) and any(n <= words for n in names)


def claim_question(conversation: str, sender: str | None, sent_at: str | None, text: str) -> dict:
    """Nhận một câu hỏi để trả lời. Chỉ khi allowed=true mới được soạn và gửi trả lời."""
    text = nfc(text).strip()
    if not text:
        raise ValueError("Câu hỏi rỗng")
    sent_at = parse_dt(sent_at, "sent_at")
    now = _now_dt()
    with connect() as conn:
        row = _find_conv(conn, conversation)
        if row is None:
            raise ValueError(f"Chưa đăng ký hội thoại Zalo {conversation!r}")
        if not row["reply"]:
            return {"allowed": False, "reason": "Hội thoại này chỉ đọc (reply=false). Không gửi tin."}
        askers = (row["allowed_askers"] or "").strip()
        if row["kind"] == "direct" and not askers:
            return {"allowed": False, "reason": "Hội thoại riêng chưa khai báo allowed_askers (tên CEO). "
                                                "Không gửi tin."}
        # Nhóm trợ lý không khai báo allowed_askers = mọi thành viên được hỏi
        if askers and not _asker_allowed(askers, sender):
            return {"allowed": False, "reason": f"{sender!r} không nằm trong danh sách được hỏi ({askers}). "
                                                "Không gửi tin."}
        base = message_key(row["id"], sender, sent_at, text)
        # Cùng nội dung có thể được hỏi lại nhiều lần trong ngày ("có gì mới không em?") -> chỉ coi là trùng khi
        # giờ hỏi gần nhau; hỏi lại lúc khác là câu hỏi mới.
        same = conn.execute("SELECT * FROM zalo_replies WHERE msg_key=? OR msg_key LIKE ? ORDER BY id DESC",
                            (base, base + "|%")).fetchall()
        old = next((r for r in same if _minutes_apart(r["asked_at"], sent_at) <= SAME_QUESTION_MINUTES), None)
        key = base if not same else f"{base}|{sent_at}"
        if old:
            limit = _iso(now - timedelta(minutes=CLAIM_TIMEOUT_MINUTES))
            if not (old["status"] == "claimed" and old["created_at"] < limit):
                return {"allowed": False, "reply_id": old["id"], "status": old["status"],
                        "reason": "Câu hỏi này đã được trả lời / đang được xử lý. Không gửi lại.",
                        "reply_text": old["reply_text"]}
            conn.execute("UPDATE zalo_replies SET created_at=? WHERE id=?", (_iso(now), old["id"]))
            return {"allowed": True, "reply_id": old["id"], "conversation": row["name"], "note": row["note"]}
        cur = conn.execute(
            "INSERT INTO zalo_replies(conversation_id, msg_key, asker, asked_at, question, created_at) "
            "VALUES (?,?,?,?,?,?)", (row["id"], key, nfc(sender).strip() or None, sent_at, text, _iso(now)))
        return {"allowed": True, "reply_id": cur.lastrowid, "conversation": row["name"], "note": row["note"],
                "next_step": "Tra cứu email + Zalo, gửi câu trả lời trong hội thoại này rồi gọi record_zalo_reply."}


def record_reply(reply_id: int, status: str, reply_text: str | None = None,
                 source_report_ids: list[int] | None = None, source_zalo_ids: list[int] | None = None,
                 note: str | None = None) -> dict:
    if status not in ("sent", "skipped"):
        raise ValueError("status phải là sent hoặc skipped")
    if status == "sent" and not (reply_text or "").strip():
        raise ValueError("status=sent cần reply_text (nguyên văn đã gửi)")
    now = _iso(_now_dt())
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM zalo_replies WHERE id=?", (reply_id,)).fetchone():
            raise ValueError(f"Không có câu hỏi reply_id={reply_id}")
        conn.execute(
            "UPDATE zalo_replies SET status=?, reply_text=?, source_report_ids=?, source_zalo_ids=?, note=?, "
            "replied_at=? WHERE id=?",
            (status, nfc(reply_text).strip() or None, json.dumps(sorted(set(source_report_ids or []))),
             json.dumps(sorted(set(source_zalo_ids or []))), nfc(note).strip() or None,
             now if status == "sent" else None, reply_id))
    return {"reply_id": reply_id, "status": status}


def list_replies(conversation: str | None = None, date_from: str | None = None, date_to: str | None = None,
                 limit: int = 50) -> dict:
    where: list[str] = []
    params: list[Any] = []
    if conversation:
        where.append("c.name_norm LIKE ?")
        params.append(f"%{_squash(conversation)}%")
    df = db._check_date(date_from, "date_from")
    dt = db._check_date(date_to, "date_to")
    if df:
        where.append("substr(r.created_at,1,10) >= ?")
        params.append(df)
    if dt:
        where.append("substr(r.created_at,1,10) <= ?")
        params.append(dt)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.id, c.name AS conversation, r.asker, r.asked_at, r.question, r.status, r.reply_text, "
            "r.source_report_ids, r.source_zalo_ids, r.note, r.replied_at FROM zalo_replies r "
            f"JOIN zalo_conversations c ON c.id = r.conversation_id{w} ORDER BY r.id DESC LIMIT ?",
            [*params, max(1, min(limit, 200))]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["source_report_ids"] = json.loads(d["source_report_ids"] or "[]")
        d["source_zalo_ids"] = json.loads(d["source_zalo_ids"] or "[]")
        items.append(d)
    return {"count": len(items), "replies": items}


# ---------------------------------------------------------------------------
# Hỏi đáp
# ---------------------------------------------------------------------------
_SELECT = ("SELECT m.id, c.name AS conversation, m.sender, m.sent_at, m.msg_date, m.category, m.summary, m.tags, "
           "m.important, m.text FROM zalo_messages m JOIN zalo_conversations c ON c.id = m.conversation_id")


def _filters(where: list[str], params: list[Any], *, conversation: str | None = None, sender: str | None = None,
             date_from: str | None = None, date_to: str | None = None, category: str | None = None,
             tag: str | None = None, important_only: bool = False) -> None:
    if conversation:
        where.append("c.name_norm LIKE ?")
        params.append(f"%{_squash(conversation)}%")
    if sender:
        where.append("norm(COALESCE(m.sender,'')) LIKE ?")
        params.append(f"%{_squash(sender)}%")
    df = db._check_date(date_from, "date_from")
    dt = db._check_date(date_to, "date_to")
    if df:
        where.append("m.msg_date >= ?")
        params.append(df)
    if dt:
        where.append("m.msg_date <= ?")
        params.append(dt)
    if category:
        cats = [x.strip() for x in category.split(",") if x.strip()]
        where.append(f"m.category IN ({','.join('?' * len(cats))})")
        params += cats
    if tag:
        for t in db.tags_list(db.norm_tags(tag)):
            where.append("m.tags LIKE ?")
            params.append(f"%,{t}%")
    if important_only:
        where.append("m.important = 1")


def _out(r: sqlite3.Row, text_chars: int | None = None) -> dict:
    d = dict(r)
    d["tags"] = db.tags_list(d.get("tags"))
    d["important"] = bool(d["important"])
    if text_chars is not None and d.get("text") and len(d["text"]) > text_chars:
        d["text"] = d["text"][:text_chars] + "…"
    return d


def search(query: str, conversation: str | None = None, sender: str | None = None, date_from: str | None = None,
           date_to: str | None = None, category: str | None = None, tag: str | None = None,
           important_only: bool = False, sort: str = "relevance", limit: int = 30) -> dict:
    tokens = re.findall(r"\w+", norm(query))
    if not tokens:
        raise ValueError("Từ khoá tìm kiếm rỗng")
    order = {"newest": "m.sent_at DESC", "oldest": "m.sent_at ASC"}.get(sort, "score")

    def run(match: str) -> tuple[int, list[sqlite3.Row]]:
        where, params = ["zalo_messages_fts MATCH ?"], [match]
        _filters(where, params, conversation=conversation, sender=sender, date_from=date_from, date_to=date_to,
                 category=category, tag=tag, important_only=important_only)
        base = (" FROM zalo_messages_fts JOIN zalo_messages m ON m.id = zalo_messages_fts.rowid "
                "JOIN zalo_conversations c ON c.id = m.conversation_id WHERE " + " AND ".join(where))
        with connect() as conn:
            total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            rows = conn.execute(
                _SELECT.split(" FROM ")[0] + ", bm25(zalo_messages_fts, 1.0, 3.0, 2.0) AS score" + base
                + f" ORDER BY {order} LIMIT ?", [*params, max(1, min(limit, 100))]).fetchall()
            imgs.update(_images_for(conn, [r["id"] for r in rows]))
        return total, rows

    imgs: dict[int, list[dict]] = {}

    terms = [db.fts_term(t) for t in tokens]
    mode = "all_words"
    total, rows = run(" AND ".join(terms))
    if not rows and len(tokens) > 1:
        mode = "any_word"
        total, rows = run(" OR ".join(terms))
    results = []
    for r in rows:
        d = _out(r)
        d.pop("score", None)
        mi = imgs.get(d["id"], [])
        img_text = "\n".join(f"[Ảnh: {i.get('description') or 'ảnh'}] {i.get('text') or ''}".strip() for i in mi)
        d["text"] = db._snippet("\n".join(filter(None, [d["text"], img_text])), tokens)
        if mi:
            d["images"] = [{k: v for k, v in i.items() if k != "text"} for i in mi]
        results.append(d)
    return {"query": query, "match_mode": mode, "total_matching": total, "count": len(results), "results": results}


def messages(conversation: str | None = None, sender: str | None = None, date_from: str | None = None,
             date_to: str | None = None, category: str | None = None, tag: str | None = None,
             important_only: bool = False, limit: int = 100, offset: int = 0, order: str = "oldest",
             max_chars_each: int = 2000) -> dict:
    where: list[str] = []
    params: list[Any] = []
    _filters(where, params, conversation=conversation, sender=sender, date_from=date_from, date_to=date_to,
             category=category, tag=tag, important_only=important_only)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    base = " FROM zalo_messages m JOIN zalo_conversations c ON c.id = m.conversation_id" + w
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
        rows = conn.execute(
            _SELECT + w + f" ORDER BY m.sent_at {'DESC' if order == 'newest' else 'ASC'} LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(0, offset)]).fetchall()
        imgs = _images_for(conn, [r["id"] for r in rows], max_chars_each)
    out = []
    for r in rows:
        d = _out(r, max_chars_each)
        if r["id"] in imgs:
            d["images"] = imgs[r["id"]]
        out.append(d)
    return {"total": total, "offset": offset, "count": len(rows), "messages": out}


def digest(date_from: str | None = None, date_to: str | None = None, conversation: str | None = None,
           category: str | None = None, important_only: bool = False, limit: int = 500) -> dict:
    """Điểm tin Zalo: nhóm theo hội thoại, mỗi tin một dòng (tóm tắt), kèm danh sách tin quan trọng."""
    df, dt = _default_range(date_from, date_to)
    data = messages(conversation=conversation, date_from=df, date_to=dt, category=category,
                    important_only=important_only, limit=limit, max_chars_each=300)
    names = {c["code"]: c["name"] for c in db.categories()}
    groups: dict[str, list[dict]] = {}
    important = []
    for m in data["messages"]:
        item = {"id": m["id"], "at": m["sent_at"], "from": m["sender"], "category": m["category"],
                "summary": m["summary"] or m["text"], "important": m["important"], "tags": m["tags"]}
        groups.setdefault(m["conversation"], []).append(item)
        if m["important"]:
            important.append({**item, "conversation": m["conversation"]})
    convs = []
    for name, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        cats: dict[str, int] = {}
        for x in items:
            cats[x["category"]] = cats.get(x["category"], 0) + 1
        convs.append({"conversation": name, "count": len(items),
                      "categories": [{"code": k, "name": names.get(k, k), "count": v}
                                     for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
                      "items": items})
    return {"date_from": df, "date_to": dt, "total_matching": data["total"], "returned": data["count"],
            "truncated": data["total"] > data["count"], "important": important, "conversations": convs}


def summary() -> dict:
    today = db.today()
    with connect() as conn:
        r = conn.execute("SELECT COUNT(*) AS total, MIN(msg_date) AS first_date, MAX(msg_date) AS last_date, "
                         "COALESCE(SUM(important),0) AS important FROM zalo_messages").fetchone()
        t = conn.execute("SELECT COUNT(*), COALESCE(SUM(important),0) FROM zalo_messages WHERE msg_date=?",
                         (today,)).fetchone()
        convs = conn.execute("SELECT COUNT(*), MAX(last_scanned_at), COALESCE(SUM(reply),0) "
                             "FROM zalo_conversations WHERE watch=1").fetchone()
        replies = conn.execute("SELECT COUNT(*) FROM zalo_replies WHERE status='sent' "
                               "AND substr(replied_at,1,10)=?", (today,)).fetchone()[0]
    return {**dict(r), "today": t[0], "today_important": t[1], "watched_conversations": convs[0],
            "reply_conversations": convs[2], "last_scanned_at": convs[1], "replies_sent_today": replies}


# ---------------------------------------------------------------------------
# Điểm tin tổng hợp cho CEO: email + Zalo
# ---------------------------------------------------------------------------
def briefing(date_from: str | None = None, date_to: str | None = None, important_only: bool = False) -> dict:
    df, dt = _default_range(date_from, date_to)
    email = db.digest(df, dt, limit=300)
    attention = db.attention_items(df, dt, limit=30)
    z = digest(df, dt, important_only=important_only)
    return {
        "date_from": df, "date_to": dt,
        "email": {"total": email["total_matching"], "truncated": email["truncated"], "groups": email["groups"],
                  "attention": attention.get("items", [])},
        "zalo": {"total": z["total_matching"], "truncated": z["truncated"], "important": z["important"],
                 "conversations": z["conversations"]},
        "pending_emails": db.overview()["pending"],
        "zalo_last_scanned_at": summary()["last_scanned_at"],
    }


# ---------------------------------------------------------------------------
# Trang quản trị: lịch sử trả lời của trợ lý trên Zalo
# ---------------------------------------------------------------------------
_ASKED_DAY = "substr(COALESCE(r.asked_at, r.created_at),1,10)"
REPLY_STATUSES = ("claimed", "sent", "skipped")


def _reply_where(conversation_id: int | None, status: str | None, q: str | None, date_from: str | None,
                 date_to: str | None, asker: str | None = None) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if conversation_id:
        where.append("r.conversation_id = ?")
        params.append(int(conversation_id))
    if status:
        if status not in REPLY_STATUSES:
            raise ValueError(f"status phải là một trong {REPLY_STATUSES}")
        where.append("r.status = ?")
        params.append(status)
    df = db._check_date(date_from, "date_from")
    dt = db._check_date(date_to, "date_to")
    if df:
        where.append(f"{_ASKED_DAY} >= ?")
        params.append(df)
    if dt:
        where.append(f"{_ASKED_DAY} <= ?")
        params.append(dt)
    if asker:
        where.append("norm(COALESCE(r.asker,'')) LIKE ?")
        params.append(f"%{norm(asker.strip())}%")
    for word in re.findall(r"\w+", norm(q or "")):
        where.append("norm(r.question || ' ' || COALESCE(r.reply_text,'') || ' ' || COALESCE(r.asker,'') || ' ' "
                     "|| COALESCE(r.note,'')) LIKE ?")
        params.append(f"%{word}%")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _response_minutes(asked: str | None, created: str | None, replied: str | None) -> float | None:
    start = asked or created
    if not start or not replied:
        return None
    try:
        return round((datetime.fromisoformat(replied) - datetime.fromisoformat(start)).total_seconds() / 60, 1)
    except ValueError:
        return None


def admin_replies(conversation_id: int | None = None, status: str | None = None, q: str | None = None,
                  date_from: str | None = None, date_to: str | None = None, asker: str | None = None,
                  limit: int = 50, offset: int = 0) -> dict:
    w, params = _reply_where(conversation_id, status, q, date_from, date_to, asker)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM zalo_replies r{w}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT r.id, r.conversation_id, c.name AS conversation, c.kind, r.asker, r.asked_at, r.question, "
            "r.status, r.reply_text, r.note, r.created_at, r.replied_at, r.source_report_ids, r.source_zalo_ids "
            f"FROM zalo_replies r JOIN zalo_conversations c ON c.id = r.conversation_id{w} "
            "ORDER BY COALESCE(r.asked_at, r.created_at) DESC, r.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["sources"] = len(json.loads(d.pop("source_report_ids") or "[]")) + \
            len(json.loads(d.pop("source_zalo_ids") or "[]"))
        d["response_minutes"] = _response_minutes(d["asked_at"], d["created_at"], d["replied_at"])
        d["stale"] = d["status"] == "claimed" and d["created_at"] < _iso(
            _now_dt() - timedelta(minutes=CLAIM_TIMEOUT_MINUTES))
        items.append(d)
    return {"total": total, "offset": offset, "count": len(items), "replies": items}


def reply_detail(reply_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT r.*, c.name AS conversation, c.kind FROM zalo_replies r "
            "JOIN zalo_conversations c ON c.id = r.conversation_id WHERE r.id = ?", (reply_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        rids = json.loads(d.pop("source_report_ids") or "[]")
        zids = json.loads(d.pop("source_zalo_ids") or "[]")
        d["source_reports"] = []
        if rids:
            marks = ",".join("?" * len(rids))
            found = {x["id"]: dict(x) for x in conn.execute(
                "SELECT r.id, r.report_date, r.subject, r.status, r.summary, "
                "COALESCE(e.name, r.sender_name, r.sender_email) AS employee_name "
                f"FROM reports r LEFT JOIN employees e ON e.email = r.sender_email WHERE r.id IN ({marks})", rids)}
            d["source_reports"] = [found.get(i) or {"id": i, "missing": True} for i in rids]
        d["source_zalo"] = []
        if zids:
            marks = ",".join("?" * len(zids))
            found = {x["id"]: dict(x) for x in conn.execute(
                "SELECT m.id, c.name AS conversation, m.sender, m.sent_at, m.summary, substr(m.text,1,500) AS text "
                f"FROM zalo_messages m JOIN zalo_conversations c ON c.id = m.conversation_id WHERE m.id IN ({marks})",
                zids)}
            d["source_zalo"] = [found.get(i) or {"id": i, "missing": True} for i in zids]
    d["response_minutes"] = _response_minutes(d["asked_at"], d["created_at"], d["replied_at"])
    return d


def reply_stats(date_from: str, date_to: str) -> dict:
    w, params = _reply_where(None, None, None, date_from, date_to)
    with connect() as conn:
        by_status = {s: 0 for s in REPLY_STATUSES}
        for row in conn.execute(f"SELECT r.status, COUNT(*) FROM zalo_replies r{w} GROUP BY r.status", params):
            by_status[row[0]] = row[1]
        per_day = [dict(x) for x in conn.execute(
            f"SELECT {_ASKED_DAY} AS day, SUM(r.status='sent') AS sent, SUM(r.status='skipped') AS skipped, "
            f"SUM(r.status='claimed') AS claimed FROM zalo_replies r{w} GROUP BY day ORDER BY day", params)]
        by_conv = [dict(x) for x in conn.execute(
            "SELECT c.id, c.name, c.kind, COUNT(*) AS total, SUM(r.status='sent') AS sent, "
            "SUM(r.status='skipped') AS skipped, MAX(COALESCE(r.asked_at, r.created_at)) AS last_at "
            f"FROM zalo_replies r JOIN zalo_conversations c ON c.id = r.conversation_id{w} "
            "GROUP BY c.id ORDER BY total DESC", params)]
        askers = [dict(x) for x in conn.execute(
            f"SELECT COALESCE(r.asker,'(không rõ)') AS asker, COUNT(*) AS total, SUM(r.status='sent') AS sent "
            f"FROM zalo_replies r{w} GROUP BY COALESCE(r.asker,'(không rõ)') ORDER BY total DESC LIMIT 10", params)]
        times = [_response_minutes(a, c, rp) for a, c, rp in conn.execute(
            f"SELECT r.asked_at, r.created_at, r.replied_at FROM zalo_replies r{w}"
            + (" AND" if w else " WHERE") + " r.status='sent'", params)]
    times = sorted(t for t in times if t is not None and t >= 0)
    median = None
    if times:
        mid = len(times) // 2
        median = times[mid] if len(times) % 2 else round((times[mid - 1] + times[mid]) / 2, 1)
    return {
        "date_from": date_from, "date_to": date_to,
        "total": sum(by_status.values()), "by_status": by_status,
        "response_minutes": {"avg": round(sum(times) / len(times), 1) if times else None, "median": median,
                             "max": times[-1] if times else None, "count": len(times)},
        "per_day": per_day, "by_conversation": by_conv, "top_askers": askers,
    }


# ---------------------------------------------------------------------------
# Trang quản trị: cấu hình hội thoại Zalo (trả lời / chỉ đọc / ngừng theo dõi)
# ---------------------------------------------------------------------------
MODES = ("reply", "read", "off")


def conv_mode(c: dict) -> str:
    if not c["watch"]:
        return "off"
    return "reply" if _is_reply_conv(c) else "read"


def admin_conversations() -> list[dict]:
    """Mọi hội thoại + chế độ + số tin đã lưu + số câu đã trả lời / lần cuối trả lời."""
    convs = list_conversations()
    with connect() as conn:
        stats = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT conversation_id, COUNT(*), SUM(status='sent'), MAX(replied_at) FROM zalo_replies "
            "GROUP BY conversation_id")}
    for c in convs:
        total, sent, last = stats.get(c["id"], (0, 0, None))
        c.update(mode=conv_mode(c), questions=total, replies_sent=sent or 0, last_replied_at=last)
    return convs


def _mode_fields(mode: str) -> tuple[int, int]:
    if mode not in MODES:
        raise ValueError(f"mode phải là một trong {MODES}")
    return {"reply": (1, 1), "read": (1, 0), "off": (0, 0)}[mode]


def _admin_values(data: dict, old: sqlite3.Row | None) -> dict:
    name = nfc(str(data.get("name") if data.get("name") is not None else (old["name"] if old else ""))).strip()
    if not name:
        raise ValueError("Cần tên hội thoại (đúng như hiển thị trên Zalo)")
    if len(name) > 200:
        raise ValueError("Tên hội thoại quá dài")
    kind = data.get("kind") or (old["kind"] if old else "group")
    if kind not in KINDS:
        raise ValueError("Loại phải là group (nhóm) hoặc direct (nhắn riêng)")
    if data.get("mode") is not None:
        watch, reply = _mode_fields(str(data["mode"]))
    else:
        watch = int(bool(data["watch"])) if data.get("watch") is not None else (old["watch"] if old else 1)
        reply = int(bool(data["reply"])) if data.get("reply") is not None else (old["reply"] if old else 0)
    if reply and not watch:
        raise ValueError("Hội thoại được trả lời phải đang theo dõi")
    if "allowed_askers" in data:
        askers = _askers(data["allowed_askers"] if data["allowed_askers"] is not None else "") or None
    else:
        askers = old["allowed_askers"] if old else None
    if isinstance(askers, str):
        askers = ", ".join(a for a in (x.strip() for x in re.split(r"[,\n]", askers)) if a) or None
    if reply and kind == "direct" and not askers:
        raise ValueError("Hội thoại nhắn riêng được trả lời phải khai báo người được hỏi (tên hiển thị của CEO trên "
                         "Zalo) — nếu không, ai nhắn riêng cho tài khoản này cũng được trợ lý trả lời.")
    if data.get("note") is not None:
        note = nfc(str(data["note"])).strip() or None
    else:
        note = old["note"] if old else None
    return {"name": name, "name_norm": _squash(name), "kind": kind, "watch": watch, "reply": reply,
            "allowed_askers": askers, "note": note}


def _conv_by_id(conn: sqlite3.Connection, conv_id: int) -> dict:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT conversation_id, COUNT(*) FROM zalo_messages WHERE conversation_id=? GROUP BY conversation_id",
        (conv_id,))}
    row = conn.execute("SELECT * FROM zalo_conversations WHERE id=?", (conv_id,)).fetchone()
    c = _conv_out(row, counts)
    c["mode"] = conv_mode(c)
    return c


def admin_create_conversation(data: dict) -> dict:
    v = _admin_values(data, None)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM zalo_conversations WHERE name_norm=?", (v["name_norm"],)).fetchone():
            raise db.Conflict(f"Đã có hội thoại tên {v['name']!r}")
        cur = conn.execute(
            "INSERT INTO zalo_conversations(name, name_norm, kind, watch, reply, allowed_askers, note) "
            "VALUES (:name,:name_norm,:kind,:watch,:reply,:allowed_askers,:note)", v)
        return _conv_by_id(conn, cur.lastrowid)


def admin_update_conversation(conv_id: int, data: dict) -> dict | None:
    with connect() as conn:
        old = conn.execute("SELECT * FROM zalo_conversations WHERE id=?", (conv_id,)).fetchone()
        if old is None:
            return None
        v = _admin_values(data, old)
        dup = conn.execute("SELECT id FROM zalo_conversations WHERE name_norm=? AND id<>?",
                           (v["name_norm"], conv_id)).fetchone()
        if dup:
            raise db.Conflict(f"Đã có hội thoại khác tên {v['name']!r}")
        conn.execute("UPDATE zalo_conversations SET name=:name, name_norm=:name_norm, kind=:kind, watch=:watch, "
                     "reply=:reply, allowed_askers=:allowed_askers, note=:note WHERE id=:id", {**v, "id": conv_id})
        if v["name"] != old["name"]:  # tên hội thoại nằm trong chỉ mục tìm kiếm của tin đã lưu
            for (mid,) in conn.execute("SELECT id FROM zalo_messages WHERE conversation_id=?", (conv_id,)).fetchall():
                _index(conn, mid)
        return _conv_by_id(conn, conv_id)


def admin_delete_conversation(conv_id: int) -> dict | None:
    """Xoá hẳn hội thoại cùng tin đã lưu và lịch sử trả lời của nó."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM zalo_conversations WHERE id=?", (conv_id,)).fetchone():
            return None
        conn.execute("DELETE FROM zalo_messages_fts WHERE rowid IN "
                     "(SELECT id FROM zalo_messages WHERE conversation_id=?)", (conv_id,))
        msgs = conn.execute("DELETE FROM zalo_messages WHERE conversation_id=?", (conv_id,)).rowcount
        reps = conn.execute("DELETE FROM zalo_replies WHERE conversation_id=?", (conv_id,)).rowcount
        conn.execute("DELETE FROM zalo_conversations WHERE id=?", (conv_id,))
    return {"deleted": 1, "id": conv_id, "messages_deleted": msgs, "replies_deleted": reps}
