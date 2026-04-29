from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ad_worker import LDAPUnavailableError, fetch_ad_users_batch, find_ad_user_by_ktalk_profile
from cnf import CONFIG, build_safe_pg_dsn_for_logs, get_logger
from db_worker import (
    DatabaseUnavailableError,
    fetch_mapped_users_by_logins,
    fetch_mapped_users_by_mention_ids,
    upsert_user_mappings,
)
from ktalk_worker import KTalkUnavailableError, fetch_ktalk_profile_by_mention_id, find_ktalk_match_for_ad_user

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
        "Incoming request ip=%s endpoint=%s normalized_logins=%s normalized_mention_ids=%s",
        client_ip,
        request.url.path,
        normalized_logins,
        normalized_mention_ids,
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
    ambiguous_matches: list[dict] = []

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
        logger.info("User not found in DB by ad_login=%s", login)
        ad_user = ad_users.get(login)
        if not ad_user:
            logger.info("AD user not found by ad_login=%s", login)
            continue

        if ad_user.active:
            try:
                match_result = find_ktalk_match_for_ad_user(ad_user)
            except KTalkUnavailableError:
                return error_response(
                    "ktalk_unavailable",
                    str(CONFIG.get("ktalk_unavailable_message", "Kontur Talk lookup failed")),
                    503,
                )

            if match_result.status == "found" and match_result.ktalk_user and match_result.ktalk_user.mention_id:
                match = match_result.ktalk_user
                found_users[login] = {
                    "ad_login": login,
                    "ktalk_mention_id": match.mention_id,
                    "ad_name": make_ad_name(ad_user.first_name, ad_user.last_name, ad_user.display_name),
                }
                records_to_upsert.append(
                    _upsert_record_from_resolved(login, ad_user, match.mention_id, match.display_name, match.post)
                )
                if match_result.reason == "name_and_title":
                    logger.info("User matched by name and title source=ad_login requested=%s ad_login=%s ktalk_mention_id=%s", login, login, match.mention_id)
                else:
                    logger.info("User matched by name only source=ad_login requested=%s ad_login=%s ktalk_mention_id=%s reason=title_missing", login, login, match.mention_id)
            elif match_result.status == "ambiguous":
                without_ktalk_mention_id.append(login)
                ambiguous_matches.append(
                    {
                        "source": "ad_login",
                        "requested": login,
                        "reason": match_result.reason,
                        "candidates": match_result.candidates,
                    }
                )
                logger.warning("User match ambiguous source=ad_login requested=%s candidates=%s reason=%s", login, len(match_result.candidates), match_result.reason)
            else:
                without_ktalk_mention_id.append(login)
                logger.info("User not found source=ad_login requested=%s reason=%s", login, match_result.reason)
        else:
            without_ktalk_mention_id.append(login)
            logger.info("AD user found but inactive ad_login=%s", login)
            logger.info("User without KTalk mention_id ad_login=%s", login)

    logger.info("Launching KTalk profile lookup for missing mention_id values count=%s", len(missing_mention_ids))
    for mention_id in missing_mention_ids:
        logger.info("User not found in DB by ktalk_mention_id=%s", mention_id)
        try:
            profile = fetch_ktalk_profile_by_mention_id(mention_id)
        except KTalkUnavailableError:
            return error_response(
                "ktalk_unavailable",
                str(CONFIG.get("ktalk_unavailable_message", "Kontur Talk lookup failed")),
                503,
            )

        if not profile:
            logger.info("KTalk profile is empty for ktalk_mention_id=%s", mention_id)
            logger.info("User not found by ktalk_mention_id=%s", mention_id)
            continue

        profile_display_name = str(profile.get("displayname", "") or "").strip()
        profile_post = str(profile.get("post", "") or "").strip()
        if not profile_display_name or not profile_post:
            logger.info(
                "KTalk profile has no displayname/post mention_id=%s displayname=%s post=%s",
                mention_id,
                profile_display_name,
                profile_post,
            )
            logger.info("User not found by ktalk_mention_id=%s", mention_id)
            continue

        try:
            ad_match_result = find_ad_user_by_ktalk_profile(display_name=profile_display_name, post=profile_post)
        except LDAPUnavailableError:
            return error_response(
                "ldap_unavailable",
                str(CONFIG.get("ldap_unavailable_message", "Active Directory connection failed")),
                503,
            )

        if ad_match_result.status == "ambiguous":
            ambiguous_matches.append(
                {
                    "source": "ktalk_mention_id",
                    "requested": mention_id,
                    "reason": ad_match_result.reason,
                    "candidates": ad_match_result.candidates,
                }
            )
            logger.warning("User match ambiguous source=ktalk_mention_id requested=%s candidates=%s reason=%s", mention_id, len(ad_match_result.candidates), ad_match_result.reason)
            logger.info("User not found by ktalk_mention_id=%s", mention_id)
            continue
        if ad_match_result.status != "found" or not ad_match_result.ad_user:
            logger.info(
                "User not found source=ktalk_mention_id requested=%s reason=%s",
                mention_id,
                ad_match_result.reason,
            )
            continue
        ad_user = ad_match_result.ad_user

        found_users[ad_user.login] = {
            "ad_login": ad_user.login,
            "ktalk_mention_id": mention_id,
            "ad_name": make_ad_name(ad_user.first_name, ad_user.last_name, ad_user.display_name),
        }
        records_to_upsert.append(
            _upsert_record_from_resolved(
                ad_user.login,
                ad_user,
                mention_id,
                profile_display_name,
                profile_post,
            )
        )
        found_mention_ids.add(mention_id)
        if ad_match_result.reason == "name_and_title":
            logger.info("User matched by name and title source=ktalk_mention_id requested=%s ad_login=%s ktalk_mention_id=%s", mention_id, ad_user.login, mention_id)
        else:
            logger.info("User matched by name only source=ktalk_mention_id requested=%s ad_login=%s ktalk_mention_id=%s reason=title_missing", mention_id, ad_user.login, mention_id)

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
        "ambiguous_matches": ambiguous_matches,
        "count_ambiguous_matches": len(ambiguous_matches),
    }
    logger.info(
        "Response success status=200 found=%s not_found_ad=%s not_found_mentions=%s without_mention=%s",
        len(found_users),
        len(not_found_ad_logins),
        len(not_found_ktalk_mention_ids),
        len(without_ktalk_mention_id),
    )
    return response
