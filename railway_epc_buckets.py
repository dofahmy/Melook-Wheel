#!/usr/bin/env python3
"""Count Railway records by stored expected revenue per click (EPC).

Defaults to the golden_questions table. Environment variables can override:
  DATABASE_URL / DATABASE_PUBLIC_URL  PostgreSQL connection string
  PGHOST, PGPORT, PGDATABASE,
  PGUSER, PGPASSWORD                  alternative Railway PostgreSQL variables
  DATABASE_PATH                      Railway SQLite database file
  TABLE_NAME                          default: golden_questions
  EPC_COLUMN                          auto-detected if omitted
  DATE_COLUMN                         auto-detected if omitted
  START_AT                            optional inclusive ISO timestamp/date
  END_AT                              optional exclusive ISO timestamp/date

Examples:
  python railway_epc_buckets.py
  START_AT=2026-09-10 END_AT=2026-09-18 python railway_epc_buckets.py
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CursorAdapter:
    def __init__(self, cursor, dialect: str):
        self._cursor = cursor
        self.dialect = dialect

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._cursor.close()

    def execute(self, sql, params=()):
        if self.dialect == "sqlite":
            sql = sql.replace("%s", "?")
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()


class ConnectionAdapter:
    def __init__(self, connection, dialect: str):
        self._connection = connection
        self.dialect = dialect

    def cursor(self):
        return CursorAdapter(self._connection.cursor(), self.dialect)

    def close(self):
        self._connection.close()


def connect():
    database_path = os.getenv("DATABASE_PATH")
    if database_path:
        import sqlite3

        return ConnectionAdapter(sqlite3.connect(database_path), "sqlite")

    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("DATABASE_PUBLIC_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRESQL_URL")
    )

    pg_kwargs = {
        "host": os.getenv("PGHOST"),
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB"),
        "user": os.getenv("PGUSER") or os.getenv("POSTGRES_USER"),
        "password": os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD"),
    }
    required = ["host", "dbname", "user", "password"]
    has_separate_pg_vars = all(pg_kwargs.get(key) for key in required)
    if not url and not has_separate_pg_vars:
        visible_names = sorted(
            name
            for name in os.environ
            if any(token in name.upper() for token in ("DATABASE", "POSTGRES", "PGHOST"))
        )
        visible_text = ", ".join(visible_names) if visible_names else "none"
        raise RuntimeError(
            "PostgreSQL connection variables are missing. Link the PostgreSQL "
            "service to this Railway service, or add DATABASE_URL. "
            f"Related variable names currently visible: {visible_text}"
        )

    # Railway projects commonly have one of these drivers already installed.
    try:
        import psycopg  # type: ignore

        connection = psycopg.connect(url) if url else psycopg.connect(**pg_kwargs)
        return ConnectionAdapter(connection, "postgres")
    except ImportError:
        try:
            import psycopg2  # type: ignore

            connection = psycopg2.connect(url) if url else psycopg2.connect(**pg_kwargs)
            return ConnectionAdapter(connection, "postgres")
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL driver missing. Install psycopg[binary] or psycopg2-binary."
            ) from exc


def safe_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise RuntimeError(f"Invalid {label}: {value!r}")
    return value


def quote(name: str) -> str:
    return f'"{name}"'


def choose_column(
    columns: Iterable[str], explicit: str | None, candidates: list[str], label: str
) -> str | None:
    available = list(columns)
    lower_map = {column.lower(): column for column in available}
    if explicit:
        safe_identifier(explicit, label)
        if explicit not in available:
            raise RuntimeError(
                f"{label} {explicit!r} not found. Available columns: {', '.join(available)}"
            )
        return explicit
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


def main() -> int:
    table = safe_identifier(os.getenv("TABLE_NAME", "golden_questions"), "TABLE_NAME")
    start_at = os.getenv("START_AT")
    end_at = os.getenv("END_AT")

    conn = connect()
    try:
        with conn.cursor() as cur:
            if conn.dialect == "sqlite":
                cur.execute(f"PRAGMA table_info({quote(table)})")
                columns = [row[1] for row in cur.fetchall()]
            else:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table,),
                )
                columns = [row[0] for row in cur.fetchall()]
            if not columns:
                location = table if conn.dialect == "sqlite" else f"public.{table}"
                raise RuntimeError(f"Table {location} was not found")

            epc_col = choose_column(
                columns,
                os.getenv("EPC_COLUMN"),
                [
                    "expected_revenue_per_click",
                    "expectedRevenuePerClick",
                    "expected_epc",
                    "epc",
                ],
                "EPC_COLUMN",
            )
            if not epc_col:
                raise RuntimeError(
                    "Could not auto-detect the EPC column. Set EPC_COLUMN. "
                    f"Available columns: {', '.join(columns)}"
                )

            date_col = choose_column(
                columns,
                os.getenv("DATE_COLUMN"),
                ["created_at", "createdAt", "clicked_at", "event_time", "timestamp"],
                "DATE_COLUMN",
            )
            if (start_at or end_at) and not date_col:
                raise RuntimeError("A date filter was requested, but DATE_COLUMN was not found")

            asin_col = choose_column(
                columns, None, ["asin", "product_asin", "amazon_asin"], "ASIN column"
            )
            user_col = choose_column(
                columns,
                None,
                ["user_id", "telegram_user_id", "telegram_id", "chat_id"],
                "user column",
            )

            where_parts: list[str] = []
            params: list[object] = []
            if start_at:
                where_parts.append(f"{quote(date_col)} >= %s")
                params.append(start_at)
            if end_at:
                where_parts.append(f"{quote(date_col)} < %s")
                params.append(end_at)
            date_filter = " AND ".join(where_parts) if where_parts else "TRUE"

            epc = f"CAST({quote(epc_col)} AS NUMERIC)"
            cases = [
                ("أقل من 0.01 جنيه", f"{epc} < 0.01"),
                ("أقل من 0.10 جنيه", f"{epc} < 0.10"),
                ("أقل من 0.50 جنيه", f"{epc} < 0.50"),
            ]
            bands = [
                ("أقل من 0.01", f"{epc} < 0.01"),
                ("من 0.01 إلى أقل من 0.10", f"{epc} >= 0.01 AND {epc} < 0.10"),
                ("من 0.10 إلى أقل من 0.50", f"{epc} >= 0.10 AND {epc} < 0.50"),
                ("0.50 فأكثر", f"{epc} >= 0.50"),
                ("EPC غير موجود", f"{quote(epc_col)} IS NULL"),
            ]

            def run_group(group_defs):
                output = []
                for label, condition in group_defs:
                    select = ["COUNT(*)"]
                    if asin_col:
                        select.append(f"COUNT(DISTINCT {quote(asin_col)})")
                    if user_col:
                        select.append(f"COUNT(DISTINCT {quote(user_col)})")
                    sql = (
                        f"SELECT {', '.join(select)} FROM {quote(table)} "
                        f"WHERE ({date_filter}) AND ({condition})"
                    )
                    cur.execute(sql, params)
                    output.append((label, cur.fetchone()))
                return output

            print("\nتقرير EPC من Railway")
            table_label = table if conn.dialect == "sqlite" else f"public.{table}"
            print(f"Database: {conn.dialect} | Table: {table_label} | EPC column: {epc_col}")
            if start_at or end_at:
                print(
                    f"Period: {start_at or 'beginning'} inclusive -> "
                    f"{end_at or 'now'} exclusive | Date column: {date_col}"
                )
            else:
                print("Period: all stored records")

            headers = ["السجلات"]
            if asin_col:
                headers.append("منتجات مختلفة")
            if user_col:
                headers.append("مستخدمون مختلفون")

            print("\nالأعداد التراكمية (كل صف يشمل الصفوف الأقل منه):")
            print("الشريحة | " + " | ".join(headers))
            for label, values in run_group(cases):
                print(label + " | " + " | ".join(str(value) for value in values))

            print("\nالشرائح المنفصلة (لا يوجد جمع مزدوج):")
            print("الشريحة | " + " | ".join(headers))
            for label, values in run_group(bands):
                print(label + " | " + " | ".join(str(value) for value in values))

            print(
                "\nتنبيه: إذا كان الجدول golden_questions، فالسجلات هي مرات "
                "إدخال/عرض المنتج في البوت وليست Amazon clicks مؤكدة."
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
