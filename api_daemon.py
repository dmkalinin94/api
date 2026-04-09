from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ad_worker import LDAPUnavailableError, fetch_ad_users_batch
from cnf import CONFIG, build_safe_pg_dsn_for_logs, get_logger
from db_worker import DatabaseUnavailableError, fetch_mapped_users_by_logins, upsert_user_mappings
from ktalk_worker import KTalkUnavailableError, find_ktalk_match_for_ad_user

app = FastAPI(
    title=str(CONFIG.get("api_title", "AD -> KTalk mention resolver")),
    version=str(CONFIG.get("api_version", "1.0.0")),
)
logger = get_logger()


def normalize_logins(users: list[str]) -> list[str]:
    logins: list[str] = []
    for user in users:
        raw = str(user).strip()
        if not raw:
            continue
        if raw.startswith("@"):
            raw = raw[1:]
        if ":" in raw:
            raw = raw.split(":", 1)[0]
        login = raw.strip().lower()
        if login and login not in logins:
            logins.append(login)
    return logins


def make_ad_name(first_name: str, last_name: str, display_name: str) -> str:
    full_name = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    if full_name:
        return full_name
    return str(display_name or "").strip()


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code, "message": message})


def _extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return str(request.client.host)
    return "unknown"


def _to_not_found(requested: Iterable[str], found: dict[str, object]) -> list[str]:
    return [item for item in requested if item not in found]


@app.on_event("startup")
def startup_log() -> None:
    safe_dsn = build_safe_pg_dsn_for_logs(CONFIG["pg_dsn"])
    logger.info(
        "Service started. DB=%s table=%s log_file=%s",
        safe_dsn,
        CONFIG.get("table_name"),
        CONFIG.get("log_file"),
    )


@app.get(str(CONFIG.get("api_resolve_path", "/resolve")))
def resolve_users(request: Request):
    login_param = str(CONFIG.get("api_query_param_login", "ad_login"))
    raw_logins = request.query_params.getlist(login_param)
    if not raw_logins:
        return error_response(
            "bad_request",
            str(CONFIG.get("api_bad_request_message", "at least one ad_login query parameter is required")),
            400,
        )

    normalized = normalize_logins(raw_logins)
    if not normalized:
        return error_response(
            "bad_request",
            str(CONFIG.get("api_bad_request_message", "at least one ad_login query parameter is required")),
            400,
        )

    client_ip = _extract_client_ip(request)
    logger.info(
        "Incoming request ip=%s endpoint=%s normalized_logins=%s",
        client_ip,
        request.url.path,
        normalized,
    )

    try:
        db_found = fetch_mapped_users_by_logins(normalized)
    except DatabaseUnavailableError:
        return error_response(
            "database_unavailable",
            str(CONFIG.get("database_unavailable_message", "Database connection failed")),
            503,
        )

    missing_after_db = _to_not_found(normalized, db_found)
    logger.info("DB search complete found=%s missing=%s", len(db_found), len(missing_after_db))

    if not missing_after_db:
        response = {
            "count_requested": len(raw_logins),
            "count_normalized": len(normalized),
            "count_found": len(db_found),
            "users": [
                {
                    "ad_login": row.ad_login,
                    "ktalk_mention_id": row.ktalk_mention_id,
                    "ad_name": row.ad_name,
                }
                for row in db_found.values()
            ],
            "not_found": [],
        }
        logger.info("Response success status=200 found=%s not_found=0", len(db_found))
        return response

    logger.info("Launching AD lookup for %s users", len(missing_after_db))
    try:
        ad_users = fetch_ad_users_batch(missing_after_db)
    except LDAPUnavailableError:
        return error_response(
            "ldap_unavailable",
            str(CONFIG.get("ldap_unavailable_message", "Active Directory connection failed")),
            503,
        )

    records_to_upsert: list[dict] = []
    newly_found: dict[str, dict[str, str]] = {}

    logger.info("Launching KTalk lookup")
    for login in missing_after_db:
        ad_user = ad_users.get(login)
        if not ad_user:
            continue

        record = {
            "ad_login": ad_user.login,
            "ad_first_name": ad_user.first_name,
            "ad_last_name": ad_user.last_name,
            "ad_display_name": ad_user.display_name,
            "ad_title": ad_user.title,
            "ad_active": ad_user.active,
            "ktalk_mention_id": None,
            "ktalk_display_name": "",
            "ktalk_post": "",
            "ktalk_matched": False,
            "ktalk_deactivated": False,
        }

        if ad_user.active:
            try:
                match = find_ktalk_match_for_ad_user(ad_user)
            except KTalkUnavailableError:
                return error_response(
                    "ktalk_unavailable",
                    str(CONFIG.get("ktalk_unavailable_message", "Kontur Talk lookup failed")),
                    503,
                )

            if match and match.mention_id:
                record["ktalk_mention_id"] = match.mention_id
                record["ktalk_display_name"] = match.display_name
                record["ktalk_post"] = match.post
                record["ktalk_matched"] = True
                record["ktalk_deactivated"] = match.deactivated
                newly_found[login] = {
                    "ad_login": login,
                    "ktalk_mention_id": match.mention_id,
                    "ad_name": make_ad_name(ad_user.first_name, ad_user.last_name, ad_user.display_name),
                }
                logger.info("Strict match success login=%s", login)
            else:
                logger.info("Strict match failed login=%s", login)

        records_to_upsert.append(record)

    try:
        upsert_count = upsert_user_mappings(records_to_upsert)
        logger.info("DB upsert done affected=%s", upsert_count)
    except DatabaseUnavailableError:
        return error_response(
            "database_unavailable",
            str(CONFIG.get("database_unavailable_message", "Database connection failed")),
            503,
        )

    all_found = {
        **{
            k: {"ad_login": v.ad_login, "ktalk_mention_id": v.ktalk_mention_id, "ad_name": v.ad_name}
            for k, v in db_found.items()
        },
        **newly_found,
    }

    not_found = _to_not_found(normalized, all_found)

    response = {
        "count_requested": len(raw_logins),
        "count_normalized": len(normalized),
        "count_found": len(all_found),
        "users": list(all_found.values()),
        "not_found": not_found,
    }
    logger.info("Response success status=200 found=%s not_found=%s", len(all_found), len(not_found))
    return response
