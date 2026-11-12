"""MCP server (FastMCP, transport HTTP): lọc, lưu trữ & hỏi đáp email công việc.

Luồng: Gmail --(Apps Script)--> POST /ingest (trạng thái "pending")
       Claude Desktop (tác vụ định kỳ) --> get_pending_emails --> review_emails (giữ + phân loại / loại bỏ)
       Hỏi đáp: các tool đọc dữ liệu chỉ dùng email đã giữ ("kept").
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import date as _date, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

import anyio
from fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import admin, db, ingest
from .config import settings

log = logging.getLogger("email_report_mcp")

INSTRUCTIONS = f"""
Máy chủ lưu trữ EMAIL CÔNG VIỆC (báo cáo nhân viên, khách hàng, dự án, tài chính, tin ngành...) để hỏi đáp.
Múi giờ: {settings.timezone.key}. Ngày dạng YYYY-MM-DD. Nếu không chắc "hôm nay" là ngày nào, gọi get_overview.

LỌC EMAIL MỚI (tác vụ định kỳ; Gmail được Apps Script đẩy vào ở trạng thái chờ duyệt):
1. get_pending_emails -> nhận lô email chờ duyệt + QUY TẮC LỌC + danh mục phân loại.
2. Với từng email: liên quan công việc -> action "keep" kèm category, summary (1-3 câu tiếng Việt nêu ý chính,
   số liệu, hạn chót, người liên quan) và tags (dự án, khách hàng, chủ đề). Không liên quan -> "discard"
   (nội dung bị xoá). Phân vân -> giữ với category "khac".
3. review_emails cho cả lô, lặp lại tới khi total_pending = 0.
Người dùng muốn đổi tiêu chí lọc ("từ nay bỏ email của X") -> update_filter_rules.

HỎI ĐÁP (mặc định chỉ dùng email đã giữ):
- Bức tranh chung, có những nhóm/người gửi nào: get_overview.
- Tổng hợp/điểm tin một khoảng thời gian, nhiều email: get_digest (gọn, dùng tóm tắt) rồi get_report để xem chi tiết.
- Đọc nguyên văn nhiều email (vd báo cáo hôm nay của cả nhóm): get_reports_content.
- Tìm theo chủ đề/từ khoá (không cần dấu): search_reports; diễn biến theo thời gian: search_reports sort="oldest".
- Đếm/thống kê/xếp hạng (bao nhiêu, ai gửi nhiều nhất, theo tháng, theo khách hàng...): count_emails.
- Lịch sử một cuộc trao đổi: get_thread.
- Việc cần chú ý/xử lý (vướng mắc, chậm trễ, gấp, sự cố, khiếu nại, xin ý kiến): find_attention_items.
- Tình hình với một người / khách hàng (tên miền) / phòng ban: get_sender_profile.
- Danh sách có lọc + phân trang: list_reports. Xem đủ 1 email: get_report.
- Ai đã/chưa nộp báo cáo ngày: check_submissions; mức độ nộp theo khoảng ngày: report_stats.
Bộ lọc dùng chung: date_from/date_to, employee (email, tên không dấu hoặc phòng ban), category, tag,
sender_domain (vd "khachhang.vn"). Khi trả lời, trích dẫn người gửi + ngày; không suy đoán ngoài dữ liệu.
""".strip()

mcp = FastMCP(name="email-reports", instructions=INSTRUCTIONS, version="0.2.0")

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

DateStr = Annotated[str | None, Field(description="Ngày YYYY-MM-DD", default=None)]
Employee = Annotated[
    str | None,
    Field(description="Lọc theo người gửi: email, một phần tên (không dấu được) hoặc phòng ban", default=None),
]
Category = Annotated[
    str | None, Field(description="Mã phân loại (xem get_overview), nhiều mã cách nhau dấu phẩy", default=None)
]
Tag = Annotated[str | None, Field(description="Lọc theo tag, vd 'hop-dong-abc' (không dấu)", default=None)]
Domain = Annotated[str | None, Field(description="Tên miền người gửi, vd 'khachhang.vn'", default=None)]
Status = Annotated[
    str,
    Field(description="kept (đã giữ, mặc định) | pending (chờ duyệt) | discarded (đã loại) | all"),
]


# ---------------------------------------------------------------------------
# NHẬP & LỌC
# ---------------------------------------------------------------------------
class ReportInput(BaseModel):
    sender_email: str = Field(description="Email người gửi")
    sender_name: str | None = Field(default=None, description="Tên hiển thị người gửi")
    subject: str = Field(default="", description="Tiêu đề email")
    body: str = Field(description="Nội dung email (văn bản hoặc HTML) — truyền nguyên văn, không tóm tắt")
    sent_at: str | None = Field(
        default=None, description="Thời điểm gửi, ISO 8601 (vd 2026-11-11T17:30:00+07:00) hoặc header Date"
    )
    message_id: str | None = Field(default=None, description="Message-ID hoặc id email từ hộp thư, dùng chống trùng")
    report_date: str | None = Field(
        default=None, description="Ngày của nội dung YYYY-MM-DD. Bỏ trống để tự suy ra từ tiêu đề hoặc ngày gửi"
    )
    attachments: list[str] | None = Field(default=None, description="Tên các file đính kèm")
    to: str | None = Field(default=None, description="Người nhận")
    cc: str | None = Field(default=None, description="CC")
    thread_id: str | None = Field(default=None, description="Id luồng hội thoại (Gmail thread id)")
    labels: list[str] | None = Field(default=None, description="Nhãn trong hộp thư")
    category: str | None = Field(default=None, description="Mã phân loại nếu đã biết")
    tags: list[str] | None = Field(default=None, description="Tag")
    summary: str | None = Field(default=None, description="Tóm tắt 1-3 câu")


class ReviewDecision(BaseModel):
    id: int = Field(description="id email (từ get_pending_emails)")
    action: Literal["keep", "discard", "pending"] = Field(
        description="keep = giữ (liên quan công việc), discard = loại bỏ (xoá nội dung), pending = để lại chờ"
    )
    category: str | None = Field(default=None, description="Mã phân loại khi keep (mặc định 'khac')")
    summary: str | None = Field(
        default=None, description="Khi keep: tóm tắt 1-3 câu tiếng Việt — việc gì, ai, số liệu, hạn chót, vướng mắc"
    )
    tags: list[str] | None = Field(
        default=None, description="Khi keep: 1-5 tag ngắn (tên dự án, khách hàng, sản phẩm, chủ đề)"
    )
    report_date: str | None = Field(default=None, description="Sửa ngày của nội dung nếu khác ngày gửi (YYYY-MM-DD)")
    note: str | None = Field(default=None, description="Lý do ngắn (nên có khi discard hoặc khi phân vân)")


class CategoryItem(BaseModel):
    code: str = Field(description="Mã không dấu, vd 'khach_hang'")
    name: str = Field(description="Mô tả nhóm")


@mcp.tool(annotations=READ_ONLY)
def get_pending_emails(
    limit: Annotated[int, Field(ge=1, le=50, description="Số email mỗi lô")] = 20,
    max_chars_each: Annotated[int, Field(ge=300, le=10000, description="Cắt bớt nội dung mỗi email")] = 2000,
) -> dict:
    """Lấy lô email CHỜ DUYỆT (cũ nhất trước) kèm quy tắc lọc và danh mục phân loại.
    Mỗi email có is_employee, sender_kept_before / sender_discarded_before (lịch sử duyệt người gửi này)
    để hỗ trợ quyết định. Sau khi quyết định, gọi review_emails."""
    data = db.pending_emails(limit, max_chars_each)
    return {
        **data,
        "filter_rules": db.filter_rules(),
        "categories": db.categories(),
        "next_step": "Gọi review_emails với quyết định cho từng id. Lặp lại tới khi total_pending = 0."
        if data["total_pending"] else "Không còn email chờ duyệt.",
    }


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def review_emails(
    decisions: Annotated[list[ReviewDecision], Field(description="Quyết định cho từng email (tối đa 100)")],
) -> dict:
    """Áp dụng quyết định duyệt. keep: lưu vào kho hỏi đáp với category/summary/tags. discard: loại bỏ,
    XOÁ nội dung (chỉ giữ dấu vết để không bị nhập lại). Cũng dùng được để phân loại lại email đã giữ (keep)."""
    if len(decisions) > 100:
        raise ValueError("Tối đa 100 quyết định mỗi lần")
    return db.review([d.model_dump() for d in decisions])


@mcp.tool(annotations=READ_ONLY)
def get_filter_rules() -> dict:
    """Xem quy tắc lọc email công việc và danh mục phân loại hiện hành."""
    return {"filter_rules": db.filter_rules(), "categories": db.categories()}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def update_filter_rules(
    rules: Annotated[str | None, Field(description="Nội dung quy tắc (tiếng Việt, dạng gạch đầu dòng)")] = None,
    mode: Annotated[Literal["append", "replace"], Field(description="append = thêm vào cuối, replace = thay toàn bộ")]
    = "append",
    categories: Annotated[list[CategoryItem] | None, Field(description="Thay TOÀN BỘ danh mục phân loại")] = None,
) -> dict:
    """Cập nhật tiêu chí lọc khi người dùng yêu cầu (vd: 'luôn giữ email từ @doitac.vn', 'bỏ bản tin X')."""
    if rules is not None and mode == "append":
        rules = db.filter_rules().rstrip() + "\n" + rules.strip()
    return db.save_rules(rules, [c.model_dump() for c in categories] if categories is not None else None)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def save_reports(
    reports: Annotated[list[ReportInput], Field(description="Danh sách email (tối đa 50/lần)")],
    source: Annotated[str | None, Field(description="Nguồn, vd gmail / outlook / manual")] = None,
    status: Annotated[Literal["kept", "pending"], Field(description="kept = lưu thẳng (đã lọc), pending = chờ duyệt")]
    = "kept",
) -> dict:
    """Lưu một lô email do Claude tự đọc từ hộp thư. Email trùng (cùng message_id) được bỏ qua.
    (Luồng chính là Apps Script đẩy vào; tool này dùng khi nhập tay/nguồn khác.)"""
    return ingest.save_batch([r.model_dump() for r in reports], source, status)


@mcp.tool(annotations=READ_ONLY)
def get_import_checkpoint() -> dict:
    """Mốc nhập dữ liệu: email mới nhất đã nhận, số email chờ duyệt, 5 lần nhập gần nhất."""
    return {"today": db.today(), "timezone": settings.timezone.key, **db.checkpoint()}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def delete_report(report_id: Annotated[int, Field(description="id email")]) -> dict:
    """Xoá hẳn một email khỏi CSDL."""
    return {"deleted": db.delete_report(report_id), "id": report_id}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
def upsert_employee(
    email: Annotated[str, Field(description="Email nhân viên")],
    name: Annotated[str, Field(description="Họ tên")],
    department: Annotated[str | None, Field(description="Phòng ban")] = None,
    active: Annotated[bool, Field(description="Còn phải nộp báo cáo hay không")] = True,
) -> dict:
    """Thêm/cập nhật nhân viên (dùng để nhận diện đồng nghiệp và kiểm tra ai chưa nộp báo cáo)."""
    return db.upsert_employee(email, name, department, active)


# ---------------------------------------------------------------------------
# HỎI ĐÁP
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def get_overview() -> dict:
    """Bức tranh chung: hôm nay, tổng email đã giữ, khoảng ngày có dữ liệu, số chờ duyệt, các nhóm phân loại
    (mã + số lượng), người gửi nhiều nhất, số nhân viên. Gọi đầu tiên khi chưa rõ dữ liệu có gì."""
    return {"today": db.today(), "timezone": settings.timezone.key,
            "work_days": settings.work_days, **db.overview()}


@mcp.tool(annotations=READ_ONLY)
def list_reports(
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    tag: Tag = None,
    sender_domain: Domain = None,
    status: Status = "kept",
    order: Annotated[Literal["newest", "oldest"], Field(description="Thứ tự")] = "newest",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Danh sách email (id, ngày, người gửi, tiêu đề, phân loại, tóm tắt, đoạn đầu) có lọc + phân trang."""
    return db.list_reports(date_from, date_to, employee, limit, offset, status=status, category=category,
                           tag=tag, sender_domain=sender_domain, order=order)


@mcp.tool(annotations=READ_ONLY)
def get_report(report_id: Annotated[int, Field(description="id email")]) -> dict:
    """Xem đầy đủ một email: nội dung, người nhận, CC, đính kèm, phân loại, tóm tắt, luồng."""
    r = db.get_report(report_id)
    if not r:
        raise ValueError(f"Không tìm thấy email id={report_id}")
    return r


@mcp.tool(annotations=READ_ONLY)
def get_reports_content(
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    tag: Tag = None,
    sender_domain: Domain = None,
    query: Annotated[str | None, Field(description="Chỉ lấy email chứa các từ này (không dấu được)")] = None,
    max_chars_each: Annotated[int, Field(ge=200, le=20000, description="Cắt bớt mỗi email")] = 4000,
    limit: Annotated[int, Field(ge=1, le=100)] = 40,
) -> dict:
    """NỘI DUNG ĐẦY ĐỦ các email để đọc kỹ/tổng hợp/so sánh (mặc định: hôm nay).
    Vd: báo cáo hôm nay của cả nhóm (category=bao_cao_ngay), mọi trao đổi với khách X tuần này."""
    if not date_from and not date_to and not query and not tag:
        date_from = date_to = db.today()
    res = db.reports_full(date_from, date_to, employee, max_chars_each, limit, category=category, tag=tag,
                          q=query, sender_domain=sender_domain)
    return {"date_from": date_from, "date_to": date_to, "count": len(res["reports"]),
            "total_matching": res["total_matching"],
            "truncated_list": res["total_matching"] > len(res["reports"]), "reports": res["reports"]}


@mcp.tool(annotations=READ_ONLY)
def get_digest(
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    tag: Tag = None,
    sender_domain: Domain = None,
    query: Annotated[str | None, Field(description="Chỉ lấy email chứa các từ này")] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 300,
) -> dict:
    """Điểm tin GỌN cho khoảng thời gian dài/nhiều email: mỗi email 1 dòng (tóm tắt), nhóm theo phân loại.
    Dùng cho câu hỏi tổng quát: 'tuần này có gì nổi bật', 'tháng 10 làm việc với khách hàng nào',
    'các vướng mắc gần đây'. Mặc định 7 ngày gần nhất."""
    if not date_from and not date_to:
        t = _date.fromisoformat(db.today())
        date_from, date_to = (t - timedelta(days=6)).isoformat(), t.isoformat()
    return {"date_from": date_from, "date_to": date_to,
            **db.digest(date_from, date_to, employee, category, tag, query, sender_domain, limit)}


@mcp.tool(annotations=READ_ONLY)
def search_reports(
    query: Annotated[str, Field(description="Từ khoá, không cần dấu (vd: 'hop dong ABC', 'trễ tiến độ')")],
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    tag: Tag = None,
    sender_domain: Domain = None,
    status: Status = "kept",
    sort: Annotated[Literal["relevance", "newest", "oldest"], Field(
        description="relevance = khớp nhất; oldest = theo dòng thời gian (xem diễn biến)")] = "relevance",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """Tìm kiếm toàn văn (tiêu đề, nội dung, tóm tắt, tag, tên file đính kèm, người gửi), không phân biệt dấu.
    Ưu tiên khớp tất cả từ, nếu không có thì khớp bất kỳ từ nào."""
    return db.search_reports(query, date_from, date_to, employee, limit, status=status, category=category,
                             tag=tag, sender_domain=sender_domain, sort=sort)


@mcp.tool(annotations=READ_ONLY)
def count_emails(
    group_by: Annotated[
        Literal["day", "week", "month", "weekday", "sender", "domain", "department", "category", "tag", "source",
                "status"],
        Field(description="Chiều thống kê"),
    ],
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    tag: Tag = None,
    sender_domain: Domain = None,
    query: Annotated[str | None, Field(description="Chỉ đếm email chứa các từ này")] = None,
    status: Status = "kept",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """Đếm / thống kê / xếp hạng email theo một chiều, có lọc. Vd: số email theo tháng, ai gửi nhiều nhất,
    khách hàng (domain) nào trao đổi nhiều, tag nào nổi bật, bao nhiêu email nhắc tới 'hợp đồng' mỗi tuần."""
    return db.aggregate(group_by, date_from=date_from, date_to=date_to, employee=employee, category=category,
                        tag=tag, sender_domain=sender_domain, q=query, status=status, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def get_thread(
    report_id: Annotated[int | None, Field(description="id một email trong luồng")] = None,
    thread_id: Annotated[str | None, Field(description="Hoặc thread_id")] = None,
    max_chars_each: Annotated[int, Field(ge=200, le=20000)] = 3000,
) -> dict:
    """Toàn bộ email trong cùng cuộc trao đổi, theo thứ tự thời gian (diễn biến, ai trả lời gì)."""
    return db.thread(thread_id, report_id, max_chars_each)


@mcp.tool(annotations=READ_ONLY)
def find_attention_items(
    date_from: DateStr = None,
    date_to: DateStr = None,
    employee: Employee = None,
    category: Category = None,
    sender_domain: Domain = None,
    keywords: Annotated[list[str] | None, Field(
        description="Thêm cụm từ cần dò (vd ['chậm giao hàng', 'đổi trả']), không cần dấu")] = None,
    only_keywords: Annotated[bool, Field(
        description="true = chỉ dò các cụm trong keywords, bỏ danh sách mặc định")] = False,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """Dò email cần CHÚ Ý / XỬ LÝ: vướng mắc, khó khăn, chậm/trễ tiến độ, quá hạn, gấp, sự cố, khiếu nại,
    rủi ro, xin ý kiến/chỉ đạo, đề xuất, công nợ... (mặc định 7 ngày gần nhất, mới nhất trước).
    Mỗi kết quả có 'matched' (cụm từ khớp) và snippet. Dùng cho: 'có vấn đề gì cần tôi xử lý',
    'tuần này nhóm gặp vướng mắc gì', 'khách nào đang phàn nàn'."""
    if not date_from and not date_to:
        t = _date.fromisoformat(db.today())
        date_from, date_to = (t - timedelta(days=6)).isoformat(), t.isoformat()
    return {"date_from": date_from, "date_to": date_to,
            **db.attention_items(date_from, date_to, employee, category, sender_domain, keywords, only_keywords,
                                 limit)}


@mcp.tool(annotations=READ_ONLY)
def get_sender_profile(
    sender: Annotated[str, Field(description="Email người gửi, tên miền ('khachhang.vn' / '@khachhang.vn'), "
                                             "tên (không dấu được) hoặc phòng ban")],
    date_from: DateStr = None,
    date_to: DateStr = None,
    recent: Annotated[int, Field(ge=1, le=50, description="Số email gần nhất trả về")] = 10,
) -> dict:
    """Hồ sơ trao đổi với một người / khách hàng (tên miền) / phòng ban: tổng số email, lần đầu - lần cuối,
    các người gửi, phân bố theo nhóm, tag nổi bật, các email gần nhất kèm tóm tắt.
    Dùng cho: 'tình hình làm việc với khách hàng X', 'anh An gần đây gửi gì', 'phòng Kỹ thuật báo cáo những gì'."""
    return db.sender_profile(sender, date_from, date_to, recent)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def purge_discarded_emails(
    older_than_days: Annotated[int, Field(ge=0, le=3650, description="Chỉ xoá dấu vết email đã loại quá N ngày")]
    = 30,
) -> dict:
    """Dọn dẹp: xoá hẳn dấu vết (message_id, tiêu đề) của email ĐÃ LOẠI lâu hơn N ngày.
    Lưu ý: nếu Apps Script quét lại các email đó, chúng sẽ vào hàng chờ duyệt lần nữa."""
    return {"purged": db.purge_discarded(older_than_days), "older_than_days": older_than_days}


@mcp.tool(annotations=READ_ONLY)
def check_submissions(date: DateStr = None) -> dict:
    """Ai đã nộp / chưa nộp báo cáo ngày (mặc định hôm nay), dựa trên danh sách nhân viên."""
    return db.submission_status(date or db.today())


@mcp.tool(annotations=READ_ONLY)
def report_stats(
    date_from: Annotated[str, Field(description="YYYY-MM-DD")],
    date_to: Annotated[str, Field(description="YYYY-MM-DD")],
) -> dict:
    """Mức độ nộp báo cáo ngày trong khoảng ngày: số báo cáo theo người & theo ngày, ngày làm việc chưa nộp."""
    return db.stats(date_from, date_to)


@mcp.tool(annotations=READ_ONLY)
def list_employees(include_inactive: bool = False) -> list[dict]:
    """Danh sách nhân viên (email, tên, phòng ban)."""
    return db.list_employees(include_inactive)


# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------
FILTER_TASK_PROMPT = (
    "Hãy lọc email mới trong hệ thống email-reports:\n"
    "1. Gọi get_pending_emails (limit 20).\n"
    "2. Đọc filter_rules và categories đi kèm. Với từng email quyết định keep hoặc discard theo quy tắc. "
    "Khi keep: chọn category, viết summary 1-3 câu tiếng Việt (việc gì, ai, số liệu, hạn chót, vướng mắc) "
    "và 1-5 tags. Khi discard: ghi note lý do ngắn. Phân vân thì keep với category 'khac'.\n"
    "3. Gọi review_emails cho cả lô. Lặp lại bước 1-3 tới khi total_pending = 0.\n"
    "4. Cuối cùng báo cáo ngắn: đã giữ bao nhiêu (theo nhóm), loại bao nhiêu, email nào đáng chú ý/cần xử lý gấp."
)


@mcp.prompt(name="loc_email_moi", description="Lọc email mới: giữ email công việc, loại email không liên quan")
def filter_prompt() -> str:
    return FILTER_TASK_PROMPT


@mcp.prompt(name="tong_hop_bao_cao_ngay", description="Tổng hợp báo cáo của cả nhóm trong một ngày")
def daily_summary_prompt(date: str = "") -> str:
    day = date or "hôm nay"
    return (
        f"Hãy tổng hợp báo cáo công việc ngày {day}. Dùng get_reports_content (category=bao_cao_ngay) để đọc "
        "toàn bộ báo cáo và check_submissions để biết ai chưa nộp. Trình bày: (1) tổng quan, (2) việc chính theo "
        "từng người/phòng, (3) vướng mắc/rủi ro cần lãnh đạo chú ý, (4) danh sách chưa nộp."
    )


@mcp.prompt(name="diem_tin_tuan", description="Điểm tin email công việc trong tuần")
def weekly_prompt(date_from: str = "", date_to: str = "") -> str:
    rng = f"từ {date_from} đến {date_to}" if date_from else "7 ngày gần nhất"
    return (
        f"Hãy điểm tin email công việc {rng}. Dùng get_digest để xem toàn cảnh, count_emails (group_by=category "
        "và sender) để thấy quy mô, find_attention_items để thấy việc cần xử lý, get_report cho email quan trọng. "
        "Trình bày theo nhóm: khách hàng/đối tác, "
        "dự án, tài chính, nội bộ, tin thị trường; nêu việc cần xử lý, hạn chót và rủi ro."
    )


@mcp.prompt(name="viec_can_xu_ly", description="Liệt kê việc cần chú ý / xử lý từ email gần đây")
def attention_prompt(date_from: str = "", date_to: str = "") -> str:
    rng = f"từ {date_from} đến {date_to}" if date_from else "7 ngày gần nhất"
    return (
        f"Hãy rà soát email công việc {rng} để tìm việc cần tôi chú ý. Gọi find_attention_items, đọc kỹ email "
        "quan trọng bằng get_report/get_thread (xem đã có ai trả lời/xử lý chưa). Trình bày bảng: việc, người/khách "
        "liên quan, hạn chót, mức độ gấp, đề xuất hành động; trích dẫn người gửi + ngày. Bỏ qua kết quả khớp nhầm."
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/ingest", methods=["POST"])
async def ingest_http(request: Request) -> JSONResponse:
    """REST endpoint cho Google Apps Script. Email vào trạng thái CHỜ DUYỆT (pending) để Claude lọc.
    Body: {"source": "gmail", "reports": [ {cùng cấu trúc ReportInput}, ... ]}"""
    try:
        payload = await request.json()
        items = payload.get("reports") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Body phải là JSON dạng {\"reports\": [...]}")
        parsed = [ReportInput.model_validate(x).model_dump() for x in items]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
    try:
        result = await anyio.to_thread.run_sync(
            ingest.save_batch, parsed, payload.get("source") or "http", "pending")
    except ValueError as e:
        return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
    return JSONResponse(result)


admin.register(mcp)


# ---------------------------------------------------------------------------
# Xác thực API key (header Bearer hoặc ?key=)
# ---------------------------------------------------------------------------
class ApiKeyMiddleware:
    # Trang giao diện + đăng nhập công khai; dữ liệu (/api/*) cần cookie phiên hoặc API key
    PUBLIC_PATHS = {"/health", "/", "/favicon.ico", "/api/login", "/api/logout"}
    PUBLIC_PREFIXES = ("/assets/",)

    def __init__(self, app: Any, keys: list[str], enabled: bool | None = None):
        self.app = app
        self.keys = [k.encode() for k in keys]
        self.enabled = bool(self.keys) if enabled is None else enabled

    def _ok(self, scope: dict) -> bool:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        candidates: list[str] = []
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidates.append(auth[7:].strip())
        if headers.get("x-api-key"):
            candidates.append(headers["x-api-key"].strip())
        qs = parse_qs(scope.get("query_string", b"").decode())
        candidates += qs.get("key", [])
        return any(hmac.compare_digest(c.encode(), k) for c in candidates for k in self.keys)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if (scope["type"] != "http" or not self.enabled or path in self.PUBLIC_PATHS
                or path.startswith(self.PUBLIC_PREFIXES) or self._ok(scope)
                or (path.startswith("/api/") and admin.session_from_scope(scope))):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "unauthorized", "message": "Thiếu hoặc sai API key"}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class RedactKeyFilter(logging.Filter):
    """Che API key (?key=...) trong access log của uvicorn."""

    _re = re.compile(r"([?&]key=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(self._re.sub(r"\1***", a) if isinstance(a, str) else a for a in args)
        if isinstance(record.msg, str) and "key=" in record.msg:
            record.msg = self._re.sub(r"\1***", record.msg)
        return True


def serve() -> None:
    db.init_db()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(RedactKeyFilter())
    if not admin.auth_enabled():
        log.warning("MCP_API_KEYS và ADMIN_PASSWORD trống — server KHÔNG có xác thực. Chỉ dùng khi chạy nội bộ!")
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.path,
        stateless_http=True,
        middleware=[Middleware(ApiKeyMiddleware, keys=settings.api_keys, enabled=admin.auth_enabled())],
        allowed_hosts=settings.allowed_hosts or None,
        show_banner=False,
    )
