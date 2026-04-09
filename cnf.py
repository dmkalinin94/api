# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
=======
import os
>>>>>>> test
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

    # Runtime
    "verify_ssl": False,
    "request_timeout": 15,
    "log_file": "/tmp/ktolkapi.log",
    "log_file_size": "10Mb",
<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
    "log_backup_count": 5,
    "log_format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    "log_datefmt": "%Y-%m-%d %H:%M:%S",

    # API
    "api_title": "AD -> KTalk mention resolver",
    "api_version": "1.0.0",
    "api_resolve_path": "/resolve",
    "api_query_param_login": "ad_login",
    "api_bad_request_message": "at least one ad_login query parameter is required",
    "database_unavailable_message": "Database connection failed",
    "ldap_unavailable_message": "Active Directory connection failed",
    "ktalk_unavailable_message": "Kontur Talk lookup failed",

    # KTalk API request defaults
    "ktalk_limit": 15,
    "ktalk_user_agent": "autoalerter/1.0",
}


=======
}


ENV_TO_CONFIG = {
    "KTOLKAPI_PG_DSN": "pg_dsn",
    "KTOLKAPI_TABLE_NAME": "table_name",
    "KTOLKAPI_AD_HOST": "ad_host",
    "KTOLKAPI_AD_USER": "ad_user",
    "KTOLKAPI_AD_PASSWORD": "ad_password",
    "KTOLKAPI_AD_BASE_DN": "ad_base_dn",
    "KTOLKAPI_KTALK_BASE_URL": "ktalk_base_url",
    "KTOLKAPI_KTALK_BEARER_TOKEN": "ktalk_bearer_token",
    "KTOLKAPI_KTALK_HOST": "ktalk_host",
    "KTOLKAPI_KTALK_TALK_HOST": "ktalk_talk_host",
    "KTOLKAPI_VERIFY_SSL": "verify_ssl",
    "KTOLKAPI_REQUEST_TIMEOUT": "request_timeout",
    "KTOLKAPI_LOG_FILE": "log_file",
    "KTOLKAPI_LOG_FILE_SIZE": "log_file_size",
}


def _cast_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def apply_env_overrides() -> None:
    for env_key, conf_key in ENV_TO_CONFIG.items():
        if env_key not in os.environ:
            continue
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        if conf_key == "verify_ssl":
            CONFIG[conf_key] = _cast_bool(raw)
        elif conf_key == "request_timeout":
            try:
                CONFIG[conf_key] = int(raw)
            except ValueError:
                pass
        else:
            CONFIG[conf_key] = raw


>>>>>>> test
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


<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
=======
apply_env_overrides()
>>>>>>> test
PG_CONNECT_PARAMS = parse_pg_dsn(CONFIG["pg_dsn"])


LOGGER_NAME = "ktolkapi"


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    max_bytes = parse_log_file_size(str(CONFIG.get("log_file_size", "10Mb")))

    handler = RotatingFileHandler(
        filename=str(CONFIG.get("log_file", "/tmp/ktolkapi.log")),
        maxBytes=max_bytes,
<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
        backupCount=int(CONFIG.get("log_backup_count", 5)),
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        fmt=str(CONFIG.get("log_format", "%(asctime)s | %(levelname)s | %(name)s | %(message)s")),
        datefmt=str(CONFIG.get("log_datefmt", "%Y-%m-%d %H:%M:%S")),
=======
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
>>>>>>> test
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
