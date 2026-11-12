"""Zalo: theo dõi các nhóm chat, thu thập THÔNG TIN CÔNG VIỆC và trả lời câu hỏi (CEO) ngay trên Zalo.

Claude Desktop đọc / gửi tin trên Zalo Web (Claude in Chrome). Chỉ GỬI tin để trả lời câu hỏi trong hội thoại bật
reply, của người nằm trong allowed_askers. Máy chủ lưu:
  - zalo_conversations: nhóm / hội thoại được theo dõi + mốc tin cuối đã đọc + quyền trả lời
  - zalo_messages (+ zalo_messages_fts): tin có thông tin công việc (tiến độ, số liệu, vướng mắc, quyết định,
    lịch hẹn, khách hàng...) kèm tóm tắt / phân loại / tag / cờ quan trọng do Claude gắn
  - zalo_replies: câu hỏi đã / đang trả lời trên Zalo (chống trả lời 2 lần, kiểm tra lại)
Tin trò chuyện thường (chào hỏi, "ok", sticker...) KHÔNG được lưu.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
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
- Tin có file / ảnh chứng từ quan trọng: ghi lại tên file và ý chính đi kèm.

KHÔNG LƯU:
- Chào hỏi, cảm ơn, "ok", "dạ vâng", sticker, emoji, tin đùa vui, chuyện cá nhân.
- Tin hệ thống (thêm/rời nhóm, đổi tên nhóm), quảng cáo, link không rõ nội dung.

GỘP: nhiều tin liên tiếp cùng một người, cùng một ý -> lưu thành MỘT mục (text = nối các tin, sent_at = tin đầu).
QUAN TRỌNG (important = true): vướng mắc, sự cố, gấp, chậm trễ, khiếu nại, cần CEO quyết định / duyệt, số liệu
bất thường.

TRẢ LỜI CÂU HỎI TRÊN ZALO (chỉ ở hội thoại reply = true, chỉ người trong allowed_askers):
- Câu hỏi hỏi trợ lý / hỏi thông tin công việc (trong nhóm: có nhắc tên / @ tài khoản này hoặc rõ ràng hỏi trợ lý).
- Tra cả email (search_reports, get_digest, get_report...) và Zalo (search_zalo, get_zalo_messages, get_briefing).
- Trả lời ngắn gọn, lịch sự, xưng "em"; có số liệu thì nêu kèm nguồn ngắn (vd "theo email của An 10/11",
  "theo nhóm Kỹ thuật 9h30 hôm nay").
- Không tìm thấy dữ liệu: trả lời rõ là chưa có thông tin, KHÔNG suy đoán.
- Không trả lời: yêu cầu chuyển tiền, gửi file / mật khẩu / dữ liệu ra ngoài, làm thay quyết định; thông tin nhạy
  cảm (lương, hợp đồng chi tiết) trong nhóm đông người."""


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


def context() -> dict:
    now = _now_dt()
    return {
        "now": _iso(now),
        "today": now.date().isoformat(),
        "timezone": settings.timezone.key,
        "rules": rules(),
        "categories": db.categories(),
        "conversations": list_conversations(only_watched=True),
        "next_step": "Mở chat.zalo.me, với từng hội thoại đọc tin SAU mốc (last_message_at / last_message_preview). "
                     "Hội thoại reply=true: câu hỏi của allowed_askers -> claim_zalo_question -> tra cứu -> gửi trả "
                     "lời -> record_zalo_reply. Tin có thông tin công việc -> save_zalo_messages (kèm last_message). "
                     "Không gửi tin nào khác ngoài câu trả lời.",
    }


# ---------------------------------------------------------------------------
# Lưu tin thu thập được
# ---------------------------------------------------------------------------
def _index(conn: sqlite3.Connection, mid: int) -> None:
    conn.execute("DELETE FROM zalo_messages_fts WHERE rowid=?", (mid,))
    m = conn.execute("SELECT m.*, c.name AS conversation FROM zalo_messages m "
                     "JOIN zalo_conversations c ON c.id=m.conversation_id WHERE m.id=?", (mid,)).fetchone()
    if m:
        conn.execute(
            "INSERT INTO zalo_messages_fts(rowid, body, meta, sender) VALUES (?,?,?,?)",
            (mid, norm(" ".join(filter(None, [m["summary"], m["text"]]))),
             norm(" ".join(filter(None, [m["conversation"], (m["tags"] or "").replace(",", " "),
                                         (m["category"] or "").replace("_", " ")]))),
             norm(m["sender"])))


def save_messages(conversation: str, messages: list[dict], last_message: dict | None = None) -> dict:
    """Lưu các tin có thông tin công việc + cập nhật mốc đã đọc. Tin trùng (cùng msg_key) được bỏ qua."""
    cats = {c["code"] for c in db.categories()}
    now = _iso(_now_dt())
    inserted, duplicate, errors = [], 0, []
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
                else:
                    _index(conn, cur.lastrowid)
                    inserted.append(cur.lastrowid)
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
    return {"conversation": row["name"], "inserted": len(inserted), "duplicate": duplicate, "errors": errors,
            "ids": inserted}


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
            return {"allowed": False, "reason": "Hội thoại này không bật trả lời (reply=false). Không gửi tin."}
        if not _asker_allowed(row["allowed_askers"], sender):
            return {"allowed": False, "reason": f"{sender!r} không nằm trong danh sách được hỏi "
                                                f"({row['allowed_askers']}). Không gửi tin."}
        key = message_key(row["id"], sender, sent_at, text)
        old = conn.execute("SELECT * FROM zalo_replies WHERE msg_key=?", (key,)).fetchone()
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
        return total, rows

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
        d["text"] = db._snippet(d["text"] or "", tokens)
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
    return {"total": total, "offset": offset, "count": len(rows),
            "messages": [_out(r, max_chars_each) for r in rows]}


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
