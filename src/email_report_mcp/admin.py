"""Trang quản trị: giao diện web ở "/" + REST API ở "/api/*" (xem, thêm, sửa, xoá dữ liệu).

Đăng nhập bằng ADMIN_PASSWORD (hoặc API key nếu không đặt) -> cookie phiên ký HMAC, HttpOnly, SameSite=Strict.
Các API cũng nhận API key như MCP (header Bearer / X-API-Key) để gọi từ script.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import secrets
import time
from datetime import date, timedelta
from functools import wraps
from importlib import resources
from typing import Any, Awaitable, Callable

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import db, ingest
from .config import settings

COOKIE = "erm_session"
ASSETS = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# ---------------------------------------------------------------------------
# Phiên đăng nhập
# ---------------------------------------------------------------------------
def auth_enabled() -> bool:
    return bool(settings.api_keys or settings.admin_password)


def _secret() -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode()
    material = "erm-session|" + ",".join(sorted(settings.api_keys)) + "|" + settings.admin_password
    return hashlib.sha256(material.encode()).digest()


def _sign(payload: str) -> str:
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_session() -> str:
    exp = int(time.time()) + settings.session_hours * 3600
    payload = f"{exp}.{secrets.token_urlsafe(9)}"
    return f"{payload}.{_sign(payload)}"


def valid_session(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(payload.split(".", 1)[0]) > time.time()
    except ValueError:
        return False


def session_from_scope(scope: dict) -> bool:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE and valid_session(value):
                    return True
    return False


def _password_ok(pw: str) -> bool:
    if settings.admin_password:
        return hmac.compare_digest(pw.encode(), settings.admin_password.encode())
    return any(hmac.compare_digest(pw.encode(), k.encode()) for k in settings.api_keys)


# Chặn dò mật khẩu: tối đa 10 lần sai / 10 phút / IP
_FAILS: dict[str, list[float]] = {}
_MAX_FAILS, _WINDOW = 10, 600


def _client_ip(request: Request) -> str:
    # X-Real-IP do nginx đặt (client không giả được). Phần tử ĐẦU của X-Forwarded-For thì client tự ghi được
    # -> dùng phần tử CUỐI (do proxy gần nhất thêm vào) để chống lách giới hạn đăng nhập sai.
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def _too_many(ip: str) -> bool:
    now = time.time()
    _FAILS[ip] = [t for t in _FAILS.get(ip, []) if now - t < _WINDOW]
    return len(_FAILS[ip]) >= _MAX_FAILS


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


# ---------------------------------------------------------------------------
# Tiện ích API
# ---------------------------------------------------------------------------
class NotFound(Exception):
    pass


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return _json({"error": code, "message": message}, status)


def api(fn: Callable[[Request], Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
    """Bọc handler: chuyển lỗi thành JSON 4xx, chặn POST/PUT không phải JSON (chống CSRF)."""

    @wraps(fn)
    async def wrapper(request: Request) -> Response:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            ctype = request.headers.get("content-type", "")
            if request.method != "DELETE" and not ctype.startswith("application/json"):
                return _err(415, "unsupported_media_type", "Body phải là JSON (Content-Type: application/json)")
        try:
            result = await fn(request)
        except NotFound as e:
            return _err(404, "not_found", str(e) or "Không tìm thấy")
        except db.Conflict as e:
            return _err(409, "conflict", str(e))
        except (ValueError, TypeError) as e:
            return _err(400, "bad_request", str(e))
        if isinstance(result, Response):
            return result
        return _json(result)

    return wrapper


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as e:  # noqa: BLE001
        raise ValueError("JSON không hợp lệ") from e
    if not isinstance(data, dict):
        raise ValueError("Body phải là object JSON")
    return data


def _q(request: Request, name: str) -> str | None:
    v = request.query_params.get(name)
    return v.strip() if v and v.strip() else None


def _int(request: Request, name: str, default: int) -> int:
    v = request.query_params.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError as e:
        raise ValueError(f"{name} phải là số") from e


def _run(fn: Callable[..., Any], *args: Any) -> Awaitable[Any]:
    return anyio.to_thread.run_sync(lambda: fn(*args))


# ---------------------------------------------------------------------------
# Giao diện
# ---------------------------------------------------------------------------
def _asset(name: str) -> bytes:
    return resources.files(__package__).joinpath("web", name).read_bytes()


def _index_html() -> bytes:
    """index.html với ?v=<hash> cho từng asset để trình duyệt luôn lấy bản mới sau khi cập nhật."""
    html = _asset("index.html").decode()
    for name in ASSETS:
        ver = hashlib.sha256(_asset(name)).hexdigest()[:10]
        html = html.replace(f"/assets/{name}\"", f"/assets/{name}?v={ver}\"")
    return html.encode()


async def index(_: Request) -> Response:
    return Response(_index_html(), media_type="text/html; charset=utf-8",
                    headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})


async def asset(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in ASSETS:
        return Response("Not found", status_code=404)
    cache = "public, max-age=31536000, immutable" if "v" in request.query_params else "no-cache"
    return Response(_asset(name), media_type=ASSETS[name], headers={**SECURITY_HEADERS, "Cache-Control": cache})


# ---------------------------------------------------------------------------
# Đăng nhập
# ---------------------------------------------------------------------------
@api
async def login(request: Request) -> Any:
    ip = _client_ip(request)
    if _too_many(ip):
        return _err(429, "too_many_attempts", "Sai quá nhiều lần, thử lại sau 10 phút")
    data = await _body(request)
    if auth_enabled() and not _password_ok(str(data.get("password") or "")):
        _FAILS.setdefault(ip, []).append(time.time())
        await anyio.sleep(0.5)
        return _err(401, "invalid_password", "Sai mật khẩu")
    _FAILS.pop(ip, None)
    resp = _json({"ok": True})
    resp.set_cookie(COOKIE, make_session(), max_age=settings.session_hours * 3600, path="/",
                    httponly=True, samesite="strict", secure=_is_https(request))
    return resp


@api
async def logout(_: Request) -> Any:
    resp = _json({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@api
async def me(_: Request) -> Any:
    return {
        "authenticated": True,
        "auth_enabled": auth_enabled(),
        "login_with": "password" if settings.admin_password else "api_key",
        "mcp_path": settings.path,
        "today": db.today(),
        "timezone": settings.timezone.key,
        "work_days": settings.work_days,
    }


# ---------------------------------------------------------------------------
# Tổng quan / thống kê
# ---------------------------------------------------------------------------
@api
async def overview(_: Request) -> Any:
    def work() -> dict:
        cp = db.checkpoint()
        return {"today": db.today(), **db.overview(), "latest_sent_at": cp["latest_sent_at"],
                "recent_imports": cp["recent_imports"], "sources": db.sources(),
                "category_list": db.categories()}
    return await _run(work)


@api
async def submissions(request: Request) -> Any:
    return await _run(db.submission_status, _q(request, "date") or db.today())


@api
async def stats(request: Request) -> Any:
    today = date.fromisoformat(db.today())
    df = _q(request, "date_from") or (today - timedelta(days=13)).isoformat()
    dt = _q(request, "date_to") or today.isoformat()
    return await _run(db.stats, df, dt)


@api
async def imports(request: Request) -> Any:
    return await _run(db.list_imports, _int(request, "limit", 50), _int(request, "offset", 0))


# ---------------------------------------------------------------------------
# Báo cáo
# ---------------------------------------------------------------------------
@api
async def reports_list(request: Request) -> Any:
    return await _run(lambda: db.list_reports(
        _q(request, "date_from"), _q(request, "date_to"), _q(request, "employee"),
        _int(request, "limit", 50), _int(request, "offset", 0), _q(request, "q"), _q(request, "source"),
        status=_q(request, "status") or "kept", category=_q(request, "category"), tag=_q(request, "tag"),
        order=_q(request, "order") or ("oldest" if _q(request, "status") == "pending" else "newest"),
    ))


@api
async def report_create(request: Request) -> Any:
    data = await _body(request)
    if not (data.get("body") or data.get("body_text") or "").strip():
        raise ValueError("Nội dung báo cáo không được để trống")
    item = {**data, "body": data.get("body") or data.get("body_text")}

    def work() -> dict:
        r = ingest.normalize(item, (data.get("source") or "manual").strip() or "manual")
        r["status"] = "kept"
        if r.get("category") and r["category"] not in {c["code"] for c in db.categories()}:
            raise ValueError(f"Nhóm '{r['category']}' không có trong danh mục")
        new_id = db.insert_report(r)
        if not new_id:
            raise db.Conflict(f"Báo cáo đã tồn tại (id={db.find_by_message_id(r['message_id'])})")
        return db.get_report(new_id)  # type: ignore[return-value]

    return _json(await _run(work), 201)


async def _report_id(request: Request) -> int:
    return int(request.path_params["id"])


@api
async def report_get(request: Request) -> Any:
    r = await _run(db.get_report, await _report_id(request))
    if not r:
        raise NotFound("Không tìm thấy báo cáo")
    return r


@api
async def report_update(request: Request) -> Any:
    data = await _body(request)
    if "body" in data and "body_text" not in data:
        data["body_text"] = data["body"]
    if data.get("sent_at"):
        data["sent_at"] = ingest.parse_sent_at(str(data["sent_at"])).isoformat(timespec="seconds")
    r = await _run(db.update_report, await _report_id(request), data)
    if not r:
        raise NotFound("Không tìm thấy báo cáo")
    return r


@api
async def report_delete(request: Request) -> Any:
    rid = await _report_id(request)
    if not await _run(db.delete_report, rid):
        raise NotFound("Không tìm thấy báo cáo")
    return {"deleted": 1, "id": rid}


@api
async def reports_bulk_delete(request: Request) -> Any:
    ids = (await _body(request)).get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("Cần danh sách ids")
    return {"deleted": await _run(db.delete_reports, ids)}


@api
async def reports_review(request: Request) -> Any:
    decisions = (await _body(request)).get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Cần danh sách decisions")
    if len(decisions) > 500:
        raise ValueError("Tối đa 500 email mỗi lần")
    return await _run(db.review, decisions)


@api
async def reports_purge(request: Request) -> Any:
    days = (await _body(request)).get("older_than_days", 30)
    if not isinstance(days, int) or days < 0:
        raise ValueError("older_than_days phải là số nguyên >= 0")
    return {"purged": await _run(db.purge_discarded, days), "older_than_days": days}


@api
async def report_thread(request: Request) -> Any:
    return await _run(lambda: db.thread(None, int(request.path_params["id"])))


@api
async def rules_get(_: Request) -> Any:
    return await _run(lambda: {"filter_rules": db.filter_rules(), "categories": db.categories(),
                               "default_rules": db.DEFAULT_RULES})


@api
async def rules_put(request: Request) -> Any:
    data = await _body(request)
    cats = data.get("categories")
    if cats is not None and not isinstance(cats, list):
        raise ValueError("categories phải là danh sách")
    return await _run(db.save_rules, data.get("filter_rules"), cats)


# ---------------------------------------------------------------------------
# Nhân viên
# ---------------------------------------------------------------------------
@api
async def employees_list(request: Request) -> Any:
    inactive = request.query_params.get("include_inactive", "1") not in ("0", "false")
    return await _run(db.admin_list_employees, _q(request, "q"), inactive)


@api
async def employee_create(request: Request) -> Any:
    return _json(await _run(db.create_employee, await _body(request)), 201)


@api
async def employee_get(request: Request) -> Any:
    e = await _run(db.get_employee, int(request.path_params["id"]))
    if not e:
        raise NotFound("Không tìm thấy nhân viên")
    return e


@api
async def employee_update(request: Request) -> Any:
    e = await _run(db.update_employee, int(request.path_params["id"]), await _body(request))
    if not e:
        raise NotFound("Không tìm thấy nhân viên")
    return e


@api
async def employee_delete(request: Request) -> Any:
    eid = int(request.path_params["id"])
    if not await _run(db.delete_employee, eid):
        raise NotFound("Không tìm thấy nhân viên")
    return {"deleted": 1, "id": eid}


@api
async def employees_import(request: Request) -> Any:
    """Nhập CSV: cột email,name,department (dòng tiêu đề tuỳ chọn)."""
    text = str((await _body(request)).get("csv") or "")
    if not text.strip():
        raise ValueError("CSV trống")

    def work() -> dict:
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        if rows and rows[0] and rows[0][0].strip().lower() in ("email", "e-mail"):
            rows = rows[1:]
        ok, errors = 0, []
        for i, row in enumerate(rows, 1):
            if not row or not any(c.strip() for c in row):
                continue
            try:
                email, name = row[0].strip(), (row[1].strip() if len(row) > 1 else "")
                if not name:
                    raise ValueError("thiếu họ tên")
                db._clean_employee({"email": email, "name": name})
                db.upsert_employee(email, name, (row[2].strip() if len(row) > 2 else "") or None)
                ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append({"line": i, "error": str(e)})
        return {"imported": ok, "errors": errors}

    return await _run(work)


# ---------------------------------------------------------------------------
def register(mcp: Any) -> None:
    route = mcp.custom_route
    route("/", methods=["GET"])(index)
    route("/assets/{name}", methods=["GET"])(asset)
    route("/api/login", methods=["POST"])(login)
    route("/api/logout", methods=["POST"])(logout)
    route("/api/me", methods=["GET"])(me)
    route("/api/overview", methods=["GET"])(overview)
    route("/api/submissions", methods=["GET"])(submissions)
    route("/api/stats", methods=["GET"])(stats)
    route("/api/imports", methods=["GET"])(imports)
    route("/api/reports", methods=["GET"])(reports_list)
    route("/api/reports", methods=["POST"])(report_create)
    route("/api/reports/delete", methods=["POST"])(reports_bulk_delete)
    route("/api/reports/review", methods=["POST"])(reports_review)
    route("/api/reports/purge-discarded", methods=["POST"])(reports_purge)
    route("/api/reports/{id:int}/thread", methods=["GET"])(report_thread)
    route("/api/rules", methods=["GET"])(rules_get)
    route("/api/rules", methods=["PUT"])(rules_put)
    route("/api/reports/{id:int}", methods=["GET"])(report_get)
    route("/api/reports/{id:int}", methods=["PUT"])(report_update)
    route("/api/reports/{id:int}", methods=["DELETE"])(report_delete)
    route("/api/employees", methods=["GET"])(employees_list)
    route("/api/employees", methods=["POST"])(employee_create)
    route("/api/employees/import", methods=["POST"])(employees_import)
    route("/api/employees/{id:int}", methods=["GET"])(employee_get)
    route("/api/employees/{id:int}", methods=["PUT"])(employee_update)
    route("/api/employees/{id:int}", methods=["DELETE"])(employee_delete)
