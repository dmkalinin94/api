from __future__ import annotations

from datetime import datetime
import hmac
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ad_worker import LDAPUnavailableError, fetch_ad_users_batch, find_ad_user_by_ktalk_profile
from cnf import CONFIG, build_safe_pg_dsn_for_logs, get_logger
from db_worker import (
    DatabaseUnavailableError,
    fetch_mapped_users_by_logins,
    fetch_mapped_users_by_mention_ids,
    get_push_message,
    init_push_messages_table,
    save_push_resolve_message,
    save_push_start_message,
    upsert_user_mappings,
)
from ktalk_worker import (
    KTalkUnavailableError,
    build_resolve_message_text,
    build_start_message_text,
    ensure_direct_room,
    fetch_ktalk_profile_by_mention_id,
    find_ktalk_match_for_ad_user,
    send_ktalk_message,
)

app = FastAPI(title=str(CONFIG.get("api_title", "AD -> KTalk mention resolver")), version=str(CONFIG.get("api_version", "1.0.0")))
logger = get_logger()


class PushPayload(BaseModel):
    event_id: str
    event_value: str
    event_time: str
    trigger_name: str
    host_name: str
    severity: str
    users: list[str]
    host_groups: list[str] | None = None
    operation_data: str | None = None
    trigger_url: str | None = None


def normalize_logins(users: list[str]) -> list[str]:
    result: list[str] = []
    for user in users:
        raw = str(user).strip()
        if not raw:
            continue
        if raw.startswith("@"):
            raw = raw[1:]
        if ":" in raw:
            raw = raw.split(":", 1)[0]
        login = raw.strip().lower()
        if login and login not in result:
            result.append(login)
    return result


def normalize_mention_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        mention_id = str(value or "").strip().lower()
        if mention_id and mention_id not in result:
            result.append(mention_id)
    return result


def normalize_push_users(values: list[str]) -> tuple[list[str], list[str], list[str]]:
    ad_logins: list[str] = []
    mention_ids: list[str] = []
    original: list[str] = []
    for item in values:
        raw = str(item or "").strip()
        if not raw or raw in original:
            continue
        original.append(raw)
        lowered = raw.lower()
        if raw.startswith("@") and ":matrix" in lowered:
            if lowered not in mention_ids:
                mention_ids.append(lowered)
        else:
            login = normalize_logins([raw])
            if login and login[0] not in ad_logins:
                ad_logins.append(login[0])
    return ad_logins, mention_ids, original


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"status": "error", "error": code, "message": message})


def resolve_recipients(ad_logins: list[str], mention_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    resolved: dict[str, str] = {}
    failed: list[str] = []
    db_by_login = fetch_mapped_users_by_logins(ad_logins)
    db_by_mention = fetch_mapped_users_by_mention_ids(mention_ids)
    for login, row in db_by_login.items():
        resolved[login] = row.ktalk_mention_id
    for mention, row in db_by_mention.items():
        resolved[mention] = row.ktalk_mention_id

    missing_logins = [x for x in ad_logins if x not in db_by_login]
    if missing_logins:
        ad_users = fetch_ad_users_batch(missing_logins)
        records_to_upsert: list[dict[str, Any]] = []
        for login in missing_logins:
            ad_user = ad_users.get(login)
            if not ad_user or not ad_user.active:
                failed.append(login)
                continue
            match = find_ktalk_match_for_ad_user(ad_user)
            if match.status == "found" and match.ktalk_user and match.ktalk_user.mention_id:
                mention_id = match.ktalk_user.mention_id
                resolved[login] = mention_id
                records_to_upsert.append({
                    "ad_login": login,
                    "ad_first_name": ad_user.first_name,
                    "ad_last_name": ad_user.last_name,
                    "ad_display_name": ad_user.display_name,
                    "ad_title": ad_user.title,
                    "ad_active": ad_user.active,
                    "ktalk_mention_id": mention_id,
                    "ktalk_display_name": match.ktalk_user.display_name,
                    "ktalk_post": match.ktalk_user.post,
                    "ktalk_matched": True,
                    "ktalk_deactivated": False,
                })
            else:
                failed.append(login)
        upsert_user_mappings(records_to_upsert)

    missing_mentions = [x for x in mention_ids if x not in db_by_mention]
    for mention_id in missing_mentions:
        profile = fetch_ktalk_profile_by_mention_id(mention_id)
        if not profile:
            failed.append(mention_id)
            continue
        resolved[mention_id] = mention_id
        ad_login = str(profile.get("talk_domain_login") or "").strip().lower()
        if not ad_login:
            ad_match = find_ad_user_by_ktalk_profile(str(profile.get("displayname") or ""), str(profile.get("post") or ""))
            if ad_match.status == "found" and ad_match.ad_user:
                ad_login = ad_match.ad_user.login
        if ad_login:
            upsert_user_mappings([
                {
                    "ad_login": ad_login,
                    "ad_active": True,
                    "ktalk_mention_id": mention_id,
                    "ktalk_display_name": str(profile.get("displayname") or ""),
                    "ktalk_post": str(profile.get("post") or ""),
                    "ktalk_matched": True,
                    "ktalk_deactivated": False,
                }
            ])
    return resolved, failed


@app.on_event("startup")
def startup_log() -> None:
    logger.info("Service started. DB=%s table=%s", build_safe_pg_dsn_for_logs(CONFIG["pg_dsn"]), CONFIG.get("table_name"))
    init_push_messages_table()


@app.get(str(CONFIG.get("api_resolve_path", "/resolve")))
def resolve_users(request: Request):
    login_param = str(CONFIG.get("api_query_param_login", "ad_login"))
    mention_param = str(CONFIG.get("api_query_param_mention_id", "ktalk_mention_id"))
    logins = normalize_logins(request.query_params.getlist(login_param))
    mentions = normalize_mention_ids(request.query_params.getlist(mention_param))
    if not logins and not mentions:
        return JSONResponse(status_code=400, content={"error": "bad_request", "message": CONFIG.get("api_bad_request_message")})
    try:
        resolved, failed = resolve_recipients(logins, mentions)
    except (DatabaseUnavailableError, LDAPUnavailableError, KTalkUnavailableError):
        return JSONResponse(status_code=503, content={"error": "service_unavailable", "message": "Dependency unavailable"})
    users = [{"ad_login": k, "ktalk_mention_id": v} for k, v in resolved.items()]
    return {"users": users, "without_ktalk_mention_id": failed}


@app.post(str(CONFIG.get("api_push_path", "/push")))
def push_alert(payload: PushPayload, request: Request):
    token_header = str(CONFIG.get("api_push_token_header", "X-Push-Token"))
    received_token = request.headers.get(token_header, "")
    expected_token = str(CONFIG.get("api_push_secret_token", ""))
    if not received_token or not hmac.compare_digest(received_token, expected_token):
        return error_response("unauthorized", "Invalid push token", 401)

    if payload.event_value not in {"0", "1"}:
        return error_response("bad_request", "event_value must be '0' or '1'", 400)
    try:
        parsed_event_time = datetime.strptime(payload.event_time, "%Y.%m.%d %H:%M:%S")
    except ValueError:
        return error_response("bad_request", "event_time has invalid format", 400)

    ad_logins, mention_ids, requested_users = normalize_push_users(payload.users)
    if not requested_users:
        return error_response("bad_request", "users must not be empty", 400)

    logger.info("/push received event_id=%s event_value=%s trigger=%s host=%s users=%s", payload.event_id, payload.event_value, payload.trigger_name, payload.host_name, len(requested_users))
    try:
        resolved_map, failed_to_resolve = resolve_recipients(ad_logins, mention_ids)
        results = []
        failed_users = list(failed_to_resolve)
        sent_count = 0
        for user in requested_users:
            key = user.lower()
            mention_id = resolved_map.get(key)
            if not mention_id:
                mention_id = resolved_map.get(normalize_logins([user])[0]) if normalize_logins([user]) else None
            if not mention_id:
                failed_users.append(user)
                results.append({"user": user, "status": "not_found", "error": "User was not resolved to ktalk_mention_id"})
                continue

            row = get_push_message(payload.event_id, mention_id)
            room_id = row["ktalk_room_id"] if row and row.get("ktalk_room_id") else ensure_direct_room(mention_id)
            if payload.event_value == "1":
                if row and row.get("ktalk_start_event_id"):
                    results.append({"user": user, "ktalk_mention_id": mention_id, "status": "already_sent", "ktalk_room_id": room_id, "ktalk_event_id": row.get("ktalk_start_event_id")})
                    continue
                msg = build_start_message_text(payload.model_dump())
                ktalk_event_id = send_ktalk_message(room_id=room_id, body=msg)
                save_push_start_message(payload.event_id, mention_id, room_id, ktalk_event_id, payload.trigger_name, payload.host_name, payload.severity, parsed_event_time)
                sent_count += 1
                results.append({"user": user, "ktalk_mention_id": mention_id, "status": "sent", "ktalk_room_id": room_id, "ktalk_event_id": ktalk_event_id})
            else:
                if row and row.get("ktalk_resolve_event_id"):
                    results.append({"user": user, "ktalk_mention_id": mention_id, "status": "already_resolved", "ktalk_room_id": room_id, "ktalk_event_id": row.get("ktalk_resolve_event_id")})
                    continue
                reply_to = row.get("ktalk_start_event_id") if row else None
                warning = None
                if not reply_to:
                    warning = "start_message_not_found"
                msg = build_resolve_message_text(payload.model_dump())
                ktalk_event_id = send_ktalk_message(room_id=room_id, body=msg, reply_to_event_id=reply_to)
                save_push_resolve_message(payload.event_id, mention_id, ktalk_event_id)
                sent_count += 1
                item = {"user": user, "ktalk_mention_id": mention_id, "status": "sent", "ktalk_room_id": room_id, "ktalk_event_id": ktalk_event_id}
                if warning:
                    item["warning"] = warning
                results.append(item)

        status_code = 200 if not failed_users else 502
        body = {
            "status": "ok" if status_code == 200 else "error",
            "event_id": payload.event_id,
            "event_value": payload.event_value,
            "count_requested_users": len(requested_users),
            "count_resolved_users": len(requested_users) - len(failed_to_resolve),
            "count_sent": sent_count,
            "count_failed": len(failed_users),
            "results": results,
            "failed_users": failed_users,
        }
        return JSONResponse(status_code=status_code, content=body)
    except DatabaseUnavailableError:
        return error_response("database_unavailable", "Database connection failed", 503)
    except LDAPUnavailableError:
        return error_response("ldap_unavailable", "Active Directory connection failed", 503)
    except KTalkUnavailableError as exc:
        return error_response("ktalk_unavailable", str(exc), 502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled /push error")
        return error_response("internal_error", str(exc), 500)
