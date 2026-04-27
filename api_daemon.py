from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ad_worker import LDAPUnavailableError, fetch_ad_users_batch
from cnf import CONFIG, build_safe_pg_dsn_for_logs, get_logger
from db_worker import (
    DatabaseUnavailableError,
    fetch_mapped_users_by_logins,
    fetch_mapped_users_by_mention_ids,
    upsert_user_mappings,
)
from ktalk_worker import (
    KTalkUnavailableError,
    fetch_ktalk_profile_by_mention_id,
    find_ktalk_match_for_ad_user,
    resolve_ad_login_by_mention_id,
)

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


def normalize_mention_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        mention_id = str(value or "").strip().lower()
        if mention_id and mention_id not in result:
            result.append(mention_id)
    return result


def _upsert_record_from_resolved(ad_login: str, ad_user, mention_id: str, ktalk_display_name: str, ktalk_post: str) -> dict:
    return {
        "ad_login": ad_login,
        "ad_first_name": ad_user.first_name,
        "ad_last_name": ad_user.last_name,
        "ad_display_name": ad_user.display_name,
        "ad_title": ad_user.title,
        "ad_active": ad_user.active,
        "ktalk_mention_id": mention_id,
        "ktalk_display_name": ktalk_display_name,
        "ktalk_post": ktalk_post,
        "ktalk_matched": True,
        "ktalk_deactivated": False,
    }


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
    mention_param = str(CONFIG.get("api_query_param_mention_id", "ktalk_mention_id"))
    raw_logins = request.query_params.getlist(login_param)
    raw_mention_ids = request.query_params.getlist(mention_param)
    if not raw_logins and not raw_mention_ids:
        return error_response(
            "bad_request",
            str(
                CONFIG.get(
                    "api_bad_request_message",
                    "at least one ad_login or ktalk_mention_id query parameter is required",
                )
            ),
            400,
        )

    normalized_logins = normalize_logins(raw_logins)
    normalized_mention_ids = normalize_mention_ids(raw_mention_ids)
    if not normalized_logins and not normalized_mention_ids:
        return error_response(
            "bad_request",
            str(
                CONFIG.get(
                    "api_bad_request_message",
                    "at least one ad_login or ktalk_mention_id query parameter is required",
                )
            ),
            400,
        )

    client_ip = _extract_client_ip(request)
    logger.info(
        "Incoming request ip=%s endpoint=%s normalized_logins=%s",
        client_ip,
        request.url.path,
        normalized_logins,
    )

    try:
        db_found_by_login = fetch_mapped_users_by_logins(normalized_logins)
        db_found_by_mention = fetch_mapped_users_by_mention_ids(normalized_mention_ids)
    except DatabaseUnavailableError:
        return error_response(
            "database_unavailable",
            str(CONFIG.get("database_unavailable_message", "Database connection failed")),
            503,
        )

    found_users: dict[str, dict[str, str]] = {}
    for row in list(db_found_by_login.values()) + list(db_found_by_mention.values()):
        found_users[row.ad_login] = {
            "ad_login": row.ad_login,
            "ktalk_mention_id": row.ktalk_mention_id,
            "ad_name": row.ad_name,
        }

    found_ad_logins = {row.ad_login for row in db_found_by_login.values()}
    found_mention_ids = set(db_found_by_mention.keys())
    missing_ad_logins = [item for item in normalized_logins if item not in found_ad_logins]
    missing_mention_ids = [item for item in normalized_mention_ids if item not in found_mention_ids]
    without_ktalk_mention_id: list[str] = []
    records_to_upsert: list[dict] = []

    logger.info(
        "DB search complete found_by_login=%s found_by_mention=%s missing_logins=%s missing_mentions=%s",
        len(db_found_by_login),
        len(db_found_by_mention),
        len(missing_ad_logins),
        len(missing_mention_ids),
    )

    logger.info("Launching AD lookup for missing ad_login values count=%s", len(missing_ad_logins))
    try:
        ad_users = fetch_ad_users_batch(missing_ad_logins)
    except LDAPUnavailableError:
        return error_response(
            "ldap_unavailable",
            str(CONFIG.get("ldap_unavailable_message", "Active Directory connection failed")),
            503,
        )

    logger.info("Launching KTalk lookup")
    for login in missing_ad_logins:
        ad_user = ad_users.get(login)
        if not ad_user:
            continue

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
                found_users[login] = {
                    "ad_login": login,
                    "ktalk_mention_id": match.mention_id,
                    "ad_name": make_ad_name(ad_user.first_name, ad_user.last_name, ad_user.display_name),
                }
                records_to_upsert.append(
                    _upsert_record_from_resolved(login, ad_user, match.mention_id, match.display_name, match.post)
                )
                logger.info("Strict match success login=%s", login)
            else:
                without_ktalk_mention_id.append(login)
                logger.info("Strict match failed login=%s", login)
        else:
            without_ktalk_mention_id.append(login)

    logger.info("Launching KTalk profile lookup for missing mention_id values count=%s", len(missing_mention_ids))
    for mention_id in missing_mention_ids:
        try:
            profile = fetch_ktalk_profile_by_mention_id(mention_id)
            resolved_login = resolve_ad_login_by_mention_id(mention_id, profile=profile)
        except KTalkUnavailableError:
            return error_response(
                "ktalk_unavailable",
                str(CONFIG.get("ktalk_unavailable_message", "Kontur Talk lookup failed")),
                503,
            )

        if not resolved_login:
            continue

        ad_users_for_mention = {}
        try:
            ad_users_for_mention = fetch_ad_users_batch([resolved_login])
        except LDAPUnavailableError:
            return error_response(
                "ldap_unavailable",
                str(CONFIG.get("ldap_unavailable_message", "Active Directory connection failed")),
                503,
            )

        ad_user = ad_users_for_mention.get(resolved_login)
        if not ad_user:
            continue

        found_users[resolved_login] = {
            "ad_login": resolved_login,
            "ktalk_mention_id": mention_id,
            "ad_name": make_ad_name(ad_user.first_name, ad_user.last_name, ad_user.display_name),
        }
        records_to_upsert.append(
            _upsert_record_from_resolved(
                resolved_login,
                ad_user,
                mention_id,
                str(profile.get("displayname", "") or "").strip(),
                str(profile.get("post", "") or "").strip(),
            )
        )
        found_mention_ids.add(mention_id)

    try:
        upsert_count = upsert_user_mappings(records_to_upsert)
        logger.info("DB upsert done affected=%s", upsert_count)
    except DatabaseUnavailableError:
        return error_response(
            "database_unavailable",
            str(CONFIG.get("database_unavailable_message", "Database connection failed")),
            503,
        )

    not_found_ad_logins = [item for item in normalized_logins if item not in found_users and item not in without_ktalk_mention_id]
    not_found_ktalk_mention_ids = [item for item in normalized_mention_ids if item not in found_mention_ids]

    response = {
        "count_requested_ad_logins": len(raw_logins),
        "count_requested_ktalk_mention_ids": len(raw_mention_ids),
        "count_found": len(found_users),
        "users": list(found_users.values()),
        "not_found_ad_logins": not_found_ad_logins,
        "not_found_ktalk_mention_ids": not_found_ktalk_mention_ids,
        "without_ktalk_mention_id": without_ktalk_mention_id,
    }
    logger.info(
        "Response success status=200 found=%s not_found_ad=%s not_found_mentions=%s without_mention=%s",
        len(found_users),
        len(not_found_ad_logins),
        len(not_found_ktalk_mention_ids),
        len(without_ktalk_mention_id),
    )
    return response
