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
        logger.debug("Connecting to PostgreSQL host=%s port=%s dbname=%s", PG_CONNECT_PARAMS.get("host"), PG_CONNECT_PARAMS.get("port"), PG_CONNECT_PARAMS.get("dbname"))
        conn = psycopg2.connect(**PG_CONNECT_PARAMS)
        logger.debug("PostgreSQL connection established")
        return conn
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
        logger.debug("DB lookup by ad_login started count=%s", len(logins))
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"logins": logins})
            rows = cur.fetchall()
        logger.debug("DB lookup by ad_login done rows=%s", len(rows))
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
        logger.debug("DB lookup by ktalk_mention_id started count=%s", len(mention_ids))
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"mention_ids": mention_ids})
            rows = cur.fetchall()
        logger.debug("DB lookup by ktalk_mention_id done rows=%s", len(rows))
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
        logger.debug("DB upsert started records=%s", len(prepared))
        with get_db_connection() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, prepared, page_size=100)
        logger.debug("DB upsert completed records=%s", len(prepared))
        return len(prepared)
    except DatabaseUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to upsert user mappings")
        raise DatabaseUnavailableError("Database upsert failed") from exc


def init_push_messages_table() -> None:
    table_name = CONFIG["push_messages_table"]
    sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id BIGSERIAL PRIMARY KEY,
        zabbix_event_id TEXT NOT NULL,
        event_value TEXT NOT NULL,
        recipient_mention_id TEXT NOT NULL,
        ktalk_room_id TEXT NOT NULL,
        ktalk_start_event_id TEXT,
        ktalk_resolve_event_id TEXT,
        trigger_name TEXT,
        host_name TEXT,
        severity TEXT,
        event_time TIMESTAMP,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
        UNIQUE (zabbix_event_id, recipient_mention_id)
    );
    """
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to initialize push messages table")
        raise DatabaseUnavailableError("Database query failed") from exc


def get_push_message(zabbix_event_id: str, recipient_mention_id: str) -> dict | None:
    table_name = CONFIG["push_messages_table"]
    sql = f"""
    SELECT * FROM {table_name}
    WHERE zabbix_event_id = %(zabbix_event_id)s
      AND recipient_mention_id = %(recipient_mention_id)s
    LIMIT 1;
    """
    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"zabbix_event_id": zabbix_event_id, "recipient_mention_id": recipient_mention_id})
            return cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read push message")
        raise DatabaseUnavailableError("Database query failed") from exc


def save_push_start_message(
    zabbix_event_id: str,
    recipient_mention_id: str,
    ktalk_room_id: str,
    ktalk_start_event_id: str,
    trigger_name: str,
    host_name: str,
    severity: str,
    event_time: datetime,
) -> None:
    table_name = CONFIG["push_messages_table"]
    sql = f"""
    INSERT INTO {table_name} (
        zabbix_event_id, event_value, recipient_mention_id, ktalk_room_id,
        ktalk_start_event_id, trigger_name, host_name, severity, event_time, updated_at
    ) VALUES (
        %(zabbix_event_id)s, '1', %(recipient_mention_id)s, %(ktalk_room_id)s,
        %(ktalk_start_event_id)s, %(trigger_name)s, %(host_name)s, %(severity)s, %(event_time)s, now()
    )
    ON CONFLICT (zabbix_event_id, recipient_mention_id)
    DO UPDATE SET
        event_value = '1',
        ktalk_room_id = EXCLUDED.ktalk_room_id,
        ktalk_start_event_id = EXCLUDED.ktalk_start_event_id,
        trigger_name = EXCLUDED.trigger_name,
        host_name = EXCLUDED.host_name,
        severity = EXCLUDED.severity,
        event_time = EXCLUDED.event_time,
        updated_at = now();
    """
    params = locals()
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save push start message")
        raise DatabaseUnavailableError("Database upsert failed") from exc


def save_push_resolve_message(zabbix_event_id: str, recipient_mention_id: str, ktalk_resolve_event_id: str) -> None:
    table_name = CONFIG["push_messages_table"]
    sql = f"""
    UPDATE {table_name}
    SET event_value = '0',
        ktalk_resolve_event_id = %(ktalk_resolve_event_id)s,
        updated_at = now()
    WHERE zabbix_event_id = %(zabbix_event_id)s
      AND recipient_mention_id = %(recipient_mention_id)s;
    """
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, locals())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save push resolve message")
        raise DatabaseUnavailableError("Database update failed") from exc
