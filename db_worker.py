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
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (zabbix_event_id, recipient_mention_id)
    );
    """
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
    except Exception as exc:  # noqa: BLE001
        logger.exception("init push table failed")
        raise DatabaseUnavailableError("Database query failed") from exc


def get_push_message(zabbix_event_id: str, recipient_mention_id: str) -> dict[str, Any] | None:
    sql = f"SELECT * FROM {CONFIG['push_messages_table']} WHERE zabbix_event_id=%s AND recipient_mention_id=%s"
    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (zabbix_event_id, recipient_mention_id))
            return cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database query failed") from exc


def save_push_start_message(zabbix_event_id: str, recipient_mention_id: str, ktalk_room_id: str, ktalk_start_event_id: str, trigger_name: str, host_name: str, severity: str, event_time: datetime) -> None:
    sql = f"""
    INSERT INTO {CONFIG['push_messages_table']} (
        zabbix_event_id,event_value,recipient_mention_id,ktalk_room_id,ktalk_start_event_id,trigger_name,host_name,severity,event_time,updated_at
    ) VALUES (%s,'1',%s,%s,%s,%s,%s,%s,%s,now())
    ON CONFLICT (zabbix_event_id, recipient_mention_id)
    DO UPDATE SET ktalk_room_id=EXCLUDED.ktalk_room_id, ktalk_start_event_id=EXCLUDED.ktalk_start_event_id,
                  trigger_name=EXCLUDED.trigger_name, host_name=EXCLUDED.host_name, severity=EXCLUDED.severity,
                  event_time=EXCLUDED.event_time, event_value='1', updated_at=now();
    """
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (zabbix_event_id, recipient_mention_id, ktalk_room_id, ktalk_start_event_id, trigger_name, host_name, severity, event_time))
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database upsert failed") from exc


def save_push_resolve_message(zabbix_event_id: str, recipient_mention_id: str, ktalk_resolve_event_id: str) -> None:
    sql = f"""
    UPDATE {CONFIG['push_messages_table']}
    SET ktalk_resolve_event_id=%s, event_value='0', updated_at=now()
    WHERE zabbix_event_id=%s AND recipient_mention_id=%s
    """
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (ktalk_resolve_event_id, zabbix_event_id, recipient_mention_id))
            if cur.rowcount == 0:
                cur.execute(
                    f"INSERT INTO {CONFIG['push_messages_table']} (zabbix_event_id,event_value,recipient_mention_id,ktalk_room_id,ktalk_resolve_event_id,updated_at) VALUES (%s,'0',%s,'',%s,now()) ON CONFLICT (zabbix_event_id,recipient_mention_id) DO UPDATE SET ktalk_resolve_event_id=EXCLUDED.ktalk_resolve_event_id, event_value='0', updated_at=now()",
                    (zabbix_event_id, recipient_mention_id, ktalk_resolve_event_id),
                )
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database upsert failed") from exc

# existing functions

def fetch_mapped_users_by_logins(logins: list[str]) -> dict[str, DBMappedUser]:
    if not logins:
        return {}
    sql = f"SELECT lower(ad_login) ad_login, ktalk_mention_id, COALESCE(NULLIF(TRIM(ad_first_name || ' ' || ad_last_name), ''), NULLIF(TRIM(ad_display_name), '')) ad_name FROM {CONFIG['table_name']} WHERE lower(ad_login) = ANY(%(logins)s) AND ad_active=TRUE AND ktalk_matched=TRUE AND COALESCE(ktalk_deactivated,FALSE)=FALSE AND ktalk_mention_id IS NOT NULL"
    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"logins": logins})
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database query failed") from exc
    return {str(r['ad_login']).strip().lower(): DBMappedUser(ad_login=str(r['ad_login']).strip().lower(), ktalk_mention_id=str(r['ktalk_mention_id']).strip(), ad_name=str(r.get('ad_name') or '').strip()) for r in rows}


def fetch_mapped_users_by_mention_ids(mention_ids: list[str]) -> dict[str, DBMappedUser]:
    if not mention_ids:
        return {}
    sql = f"SELECT lower(ad_login) ad_login, ktalk_mention_id, COALESCE(NULLIF(TRIM(ad_first_name || ' ' || ad_last_name), ''), NULLIF(TRIM(ad_display_name), '')) ad_name FROM {CONFIG['table_name']} WHERE ktalk_mention_id = ANY(%(mention_ids)s) AND ad_active=TRUE AND ktalk_matched=TRUE AND COALESCE(ktalk_deactivated,FALSE)=FALSE AND ktalk_mention_id IS NOT NULL"
    try:
        with get_db_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, {"mention_ids": mention_ids})
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database query failed") from exc
    return {str(r['ktalk_mention_id']).strip(): DBMappedUser(ad_login=str(r['ad_login']).strip().lower(), ktalk_mention_id=str(r['ktalk_mention_id']).strip(), ad_name=str(r.get('ad_name') or '').strip()) for r in rows}


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
    sql = f"""INSERT INTO {CONFIG['table_name']} (ad_login,ad_first_name,ad_last_name,ad_display_name,ad_title,ad_active,ktalk_mention_id,ktalk_display_name,ktalk_post,ktalk_matched,ktalk_deactivated,last_sync_at,updated_at) VALUES (%(ad_login)s,%(ad_first_name)s,%(ad_last_name)s,%(ad_display_name)s,%(ad_title)s,%(ad_active)s,%(ktalk_mention_id)s,%(ktalk_display_name)s,%(ktalk_post)s,%(ktalk_matched)s,%(ktalk_deactivated)s,%(last_sync_at)s,%(updated_at)s) ON CONFLICT (ad_login) DO UPDATE SET ad_first_name=EXCLUDED.ad_first_name,ad_last_name=EXCLUDED.ad_last_name,ad_display_name=EXCLUDED.ad_display_name,ad_title=EXCLUDED.ad_title,ad_active=EXCLUDED.ad_active,ktalk_mention_id=EXCLUDED.ktalk_mention_id,ktalk_display_name=EXCLUDED.ktalk_display_name,ktalk_post=EXCLUDED.ktalk_post,ktalk_matched=EXCLUDED.ktalk_matched,ktalk_deactivated=EXCLUDED.ktalk_deactivated,last_sync_at=EXCLUDED.last_sync_at,updated_at=EXCLUDED.updated_at"""
    prepared = [validate_record(item) for item in records]
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, prepared, page_size=100)
            return len(prepared)
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailableError("Database upsert failed") from exc
