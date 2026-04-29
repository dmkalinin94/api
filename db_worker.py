from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras

from cnf import CONFIG, PG_CONNECT_PARAMS, get_logger

logger = get_logger()


class DatabaseUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class DBMappedUser:
    ad_login: str
    ktalk_mention_id: str
    ad_name: str


def get_db_connection():
    try:
        return psycopg2.connect(**PG_CONNECT_PARAMS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Database connection failed")
        raise DatabaseUnavailableError("Database connection failed") from exc


def fetch_mapped_users_by_logins(logins: list[str]) -> dict[str, DBMappedUser]:
    if not logins:
        return {}

    table_name = CONFIG["table_name"]
    sql = f"""
    SELECT
        lower(ad_login) AS ad_login,
        ktalk_mention_id,
        COALESCE(
            NULLIF(TRIM(ad_first_name || ' ' || ad_last_name), ''),
            NULLIF(TRIM(ad_display_name), '')
        ) AS ad_name
    FROM {table_name}
    WHERE lower(ad_login) = ANY(%(logins)s)
      AND ad_active = TRUE
      AND ktalk_matched = TRUE
      AND COALESCE(ktalk_deactivated, FALSE) = FALSE
      AND ktalk_mention_id IS NOT NULL;
    """

    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"logins": logins})
            rows = cur.fetchall()
    except DatabaseUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read mapped users")
        raise DatabaseUnavailableError("Database query failed") from exc

    result: dict[str, DBMappedUser] = {}
    for row in rows:
        login = str(row.get("ad_login") or "").strip().lower()
        mention_id = str(row.get("ktalk_mention_id") or "").strip()
        ad_name = str(row.get("ad_name") or "").strip()
        if login and mention_id:
            result[login] = DBMappedUser(ad_login=login, ktalk_mention_id=mention_id, ad_name=ad_name)
    return result


def fetch_mapped_users_by_mention_ids(mention_ids: list[str]) -> dict[str, DBMappedUser]:
    if not mention_ids:
        return {}

    table_name = CONFIG["table_name"]
    sql = f"""
    SELECT
        lower(ad_login) AS ad_login,
        ktalk_mention_id,
        COALESCE(
            NULLIF(TRIM(ad_first_name || ' ' || ad_last_name), ''),
            NULLIF(TRIM(ad_display_name), '')
        ) AS ad_name
    FROM {table_name}
    WHERE ktalk_mention_id = ANY(%(mention_ids)s)
      AND ad_active = TRUE
      AND ktalk_matched = TRUE
      AND COALESCE(ktalk_deactivated, FALSE) = FALSE
      AND ktalk_mention_id IS NOT NULL;
    """

    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"mention_ids": mention_ids})
            rows = cur.fetchall()
    except DatabaseUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read mapped users by mention_id")
        raise DatabaseUnavailableError("Database query failed") from exc

    result: dict[str, DBMappedUser] = {}
    for row in rows:
        login = str(row.get("ad_login") or "").strip().lower()
        mention_id = str(row.get("ktalk_mention_id") or "").strip()
        ad_name = str(row.get("ad_name") or "").strip()
        if login and mention_id:
            result[mention_id] = DBMappedUser(ad_login=login, ktalk_mention_id=mention_id, ad_name=ad_name)
    return result


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["ad_login"] = str(normalized.get("ad_login") or "").strip().lower()
    if not normalized["ad_login"]:
        raise ValueError("ad_login must not be empty")

    if "ad_active" not in normalized or normalized["ad_active"] is None:
        raise ValueError("ad_active must be provided")

    normalized["ktalk_mention_id"] = str(normalized.get("ktalk_mention_id") or "").strip() or None
    if not normalized["ktalk_mention_id"]:
        normalized["ktalk_matched"] = False

    normalized.setdefault("ad_first_name", "")
    normalized.setdefault("ad_last_name", "")
    normalized.setdefault("ad_display_name", "")
    normalized.setdefault("ad_title", "")
    normalized.setdefault("ktalk_display_name", "")
    normalized.setdefault("ktalk_post", "")
    normalized.setdefault("ktalk_deactivated", False)
    normalized.setdefault("ktalk_matched", False)

    now = datetime.now(timezone.utc)
    normalized.setdefault("last_sync_at", now)
    normalized.setdefault("updated_at", now)
    return normalized


def upsert_user_mappings(records: list[dict[str, Any]]) -> int:
    if not records:
        return 0

    table_name = CONFIG["table_name"]
    sql = f"""
    INSERT INTO {table_name} (
        ad_login,
        ad_first_name,
        ad_last_name,
        ad_display_name,
        ad_title,
        ad_active,
        ktalk_mention_id,
        ktalk_display_name,
        ktalk_post,
        ktalk_matched,
        ktalk_deactivated,
        last_sync_at,
        updated_at
    )
    VALUES (
        %(ad_login)s,
        %(ad_first_name)s,
        %(ad_last_name)s,
        %(ad_display_name)s,
        %(ad_title)s,
        %(ad_active)s,
        %(ktalk_mention_id)s,
        %(ktalk_display_name)s,
        %(ktalk_post)s,
        %(ktalk_matched)s,
        %(ktalk_deactivated)s,
        %(last_sync_at)s,
        %(updated_at)s
    )
    ON CONFLICT (ad_login)
    DO UPDATE SET
        ad_first_name = EXCLUDED.ad_first_name,
        ad_last_name = EXCLUDED.ad_last_name,
        ad_display_name = EXCLUDED.ad_display_name,
        ad_title = EXCLUDED.ad_title,
        ad_active = EXCLUDED.ad_active,
        ktalk_mention_id = EXCLUDED.ktalk_mention_id,
        ktalk_display_name = EXCLUDED.ktalk_display_name,
        ktalk_post = EXCLUDED.ktalk_post,
        ktalk_matched = EXCLUDED.ktalk_matched,
        ktalk_deactivated = EXCLUDED.ktalk_deactivated,
        last_sync_at = EXCLUDED.last_sync_at,
        updated_at = EXCLUDED.updated_at;
    """

    prepared = [validate_record(item) for item in records]

    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, prepared, page_size=100)
        return len(prepared)
    except DatabaseUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to upsert user mappings")
        raise DatabaseUnavailableError("Database upsert failed") from exc
