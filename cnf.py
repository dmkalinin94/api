# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlparse, unquote

CONFIG: dict[str, Any] = {
    # PostgreSQL
    "pg_dsn": "postgresql://user:password@localhost:5432/trmetrics",

    # Полное имя таблицы для чтения/записи соответствий
    "table_name": "trmetrics.availconf.ad_ktalk_user_map",

    # AD / LDAP
    "ad_host": "ad.example.local",
    "ad_user": "EXAMPLE\\svc_account",
    "ad_password": "change_me",
    "ad_base_dn": "DC=example,DC=local",

    # KTalk
    "ktalk_base_url": "https://chat.ktalk.ru/api/...",

    # Можно указывать как полный заголовок ("Bearer ..."),
    # так и только значение токена — префикс будет добавлен автоматически.
    "ktalk_bearer_token": "change_me",
    "ktalk_host": "chat.ktalk.ru",
    "ktalk_talk_host": "https://samoletgroup.ktalk.ru",
    "ktalk_homeserver": "https://matrix-9.ktalk.ru",

    # Runtime
    "verify_ssl": False,
    "request_timeout": 15,
    "log_file": "/tmp/ktolkapi.log",
    "log_file_size": "10Mb",
    "log_backup_count": 5,
    "log_level": "DEBUG",
    "log_format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    "log_datefmt": "%Y-%m-%d %H:%M:%S",

    # API
    "api_title": "AD -> KTalk mention resolver",
    "api_version": "1.0.0",
    "api_resolve_path": "/resolve",
    "api_query_param_login": "ad_login",
    "api_query_param_mention_id": "ktalk_mention_id",
    "api_bad_request_message": "at least one ad_login or ktalk_mention_id query parameter is required",
    "database_unavailable_message": "Database connection failed",
    "ldap_unavailable_message": "Active Directory connection failed",
    "ktalk_unavailable_message": "Kontur Talk lookup failed",

    # KTalk API request defaults
    "ktalk_limit": 15,
    "ktalk_user_agent": "autoalerter/1.0",
}


def parse_log_file_size(value: str) -> int:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)\s*([kKmMgG]?[bB])", text)
    if not match:
        return 10 * 1024 * 1024

    number = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "kb":
        return number * 1024
    if unit == "mb":
        return number * 1024 * 1024
    if unit == "gb":
        return number * 1024 * 1024 * 1024
    return number


def parse_pg_dsn(dsn: str) -> dict[str, Any]:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("Unsupported PostgreSQL DSN format")

    dbname = parsed.path.lstrip("/")
    return {
        "dbname": dbname,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname or "localhost",
        "port": int(parsed.port or 5432),
    }


def build_safe_pg_dsn_for_logs(dsn: str) -> str:
    parsed = urlparse(dsn)
    username = unquote(parsed.username or "")
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/")
    user_part = f"{username}@" if username else ""
    return f"postgresql://{user_part}{host}:{port}/{dbname}"


PG_CONNECT_PARAMS = parse_pg_dsn(CONFIG["pg_dsn"])


LOGGER_NAME = "ktolkapi"


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    configured_level = str(CONFIG.get("log_level", "DEBUG")).strip().upper()
    logger_level = getattr(logging, configured_level, logging.DEBUG)
    logger.setLevel(logger_level)
    max_bytes = parse_log_file_size(str(CONFIG.get("log_file_size", "10Mb")))

    handler = RotatingFileHandler(
        filename=str(CONFIG.get("log_file", "/tmp/ktolkapi.log")),
        maxBytes=max_bytes,
        backupCount=int(CONFIG.get("log_backup_count", 5)),
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        fmt=str(CONFIG.get("log_format", "%(asctime)s | %(levelname)s | %(name)s | %(message)s")),
        datefmt=str(CONFIG.get("log_datefmt", "%Y-%m-%d %H:%M:%S")),
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
