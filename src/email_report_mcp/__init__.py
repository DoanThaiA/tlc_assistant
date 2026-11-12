"""Email Report MCP server."""

from __future__ import annotations

import argparse
import json
import secrets
import sys


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(prog="email-report-mcp", description="MCP server lưu trữ email báo cáo")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Chạy MCP server (HTTP) — mặc định")
    sub.add_parser("gen-key", help="Sinh API key ngẫu nhiên")
    imp = sub.add_parser("import-json", help="Nạp báo cáo từ file JSON (mảng các object như tool save_reports)")
    imp.add_argument("file")
    imp.add_argument("--source", default="json-import")
    emp = sub.add_parser("import-employees", help="Nạp nhân viên từ CSV: email,name,department")
    emp.add_argument("file")
    args = parser.parse_args()

    if args.cmd == "gen-key":
        print(secrets.token_urlsafe(32))
        return

    from . import db

    db.init_db()

    if args.cmd == "import-json":
        from . import ingest

        with open(args.file, encoding="utf-8") as f:
            items = json.load(f)
        counts = {"inserted": 0, "duplicate": 0, "failed": 0}
        for it in items:
            try:
                counts["inserted" if db.insert_report(ingest.normalize(it, args.source)) else "duplicate"] += 1
            except Exception as e:  # noqa: BLE001
                counts["failed"] += 1
                print("Lỗi:", e, file=sys.stderr)
        db.log_import(args.source, len(items), counts["inserted"], counts["duplicate"], counts["failed"])
        print(counts)
        return

    if args.cmd == "import-employees":
        import csv

        with open(args.file, encoding="utf-8-sig", newline="") as f:
            n = 0
            for row in csv.DictReader(f):
                db.upsert_employee(row["email"], row["name"], row.get("department") or None)
                n += 1
        print(f"Đã nạp {n} nhân viên")
        return

    from .server import serve

    serve()
