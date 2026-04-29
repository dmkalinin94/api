from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests
from requests import RequestException

from ad_worker import ADUser
from cnf import CONFIG, get_logger

logger = get_logger()


class KTalkUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class KTalkUser:
    mention_id: str
    display_name: str
    post: str
    deactivated: bool


@dataclass(slots=True)
class UserMatchResult:
    status: str  # found, not_found, ambiguous
    ad_user: ADUser | None = None
    ktalk_user: KTalkUser | None = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def normalize_value(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = value.replace("ё", "е")
    return value


def build_ktalk_bearer(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def _build_ktalk_headers(talk_host: str, host: str, bearer: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "authorization": bearer,
        "talk-host": talk_host,
        "host": host,
        "user-agent": str(CONFIG.get("ktalk_user_agent", "autoalerter/1.0")),
    }


def search_ktalk_users(
    query: str,
    base_url: str,
    talk_host: str,
    host: str,
    bearer_token: str,
    verify_ssl: bool = True,
    request_timeout: int = 15,
    limit: int = 15,
) -> list[KTalkUser]:
    bearer = build_ktalk_bearer(bearer_token)
    if not base_url or not talk_host or not bearer:
        return []

    headers = _build_ktalk_headers(talk_host=talk_host, host=host, bearer=bearer)

    try:
        logger.debug("KTalk search request started query=%s base_url=%s", query, base_url)
        response = requests.get(
            base_url,
            headers=headers,
            params={"query": query, "limit": int(limit)},
            verify=verify_ssl,
            timeout=request_timeout,
        )
    except RequestException as exc:
        logger.exception("Kontur Talk request failed")
        raise KTalkUnavailableError("Kontur Talk lookup failed") from exc

    if not response.ok:
        logger.error("Kontur Talk API non-OK status: %s", response.status_code)
        raise KTalkUnavailableError("Kontur Talk lookup failed")

    try:
        payload = response.json()
        logger.debug("KTalk search response payload query=%s payload=%s", query, payload)
    except ValueError as exc:
        logger.exception("Kontur Talk response is not valid JSON")
        raise KTalkUnavailableError("Kontur Talk lookup failed") from exc

    items = payload.get("items", [])
    if not isinstance(items, list):
        return []

    return [
        KTalkUser(
            mention_id=str(item.get("user_id", "") or "").strip(),
            display_name=str(item.get("display_name", "") or "").strip(),
            post=str(item.get("post", "") or "").strip(),
            deactivated=bool(item.get("general_deactivated", False)),
        )
        for item in items
    ]


def match_ktalk_user(ad_user: ADUser, candidates: list[KTalkUser]) -> UserMatchResult:
    ad_name = normalize_value(f"{ad_user.first_name} {ad_user.last_name}")
    ad_name_rev = normalize_value(f"{ad_user.last_name} {ad_user.first_name}")
    ad_title = normalize_value(ad_user.title)

    strict_candidates: list[KTalkUser] = []
    name_only_candidates: list[KTalkUser] = []
    for candidate in candidates:
        if candidate.deactivated:
            continue

        name = normalize_value(candidate.display_name)
        post = normalize_value(candidate.post)

        if name not in {ad_name, ad_name_rev, normalize_value(ad_user.display_name)}:
            continue
        if ad_title and post:
            if post == ad_title:
                strict_candidates.append(candidate)
        else:
            name_only_candidates.append(candidate)

    if len(strict_candidates) == 1:
        return UserMatchResult(status="found", ad_user=ad_user, ktalk_user=strict_candidates[0], reason="name_and_title")
    if len(strict_candidates) > 1:
        return UserMatchResult(
            status="ambiguous",
            ad_user=ad_user,
            reason="multiple_strict_candidates",
            candidates=[
                {"ktalk_mention_id": c.mention_id, "ktalk_display_name": c.display_name, "ktalk_post": c.post}
                for c in strict_candidates
            ],
        )
    if len(name_only_candidates) == 1:
        return UserMatchResult(status="found", ad_user=ad_user, ktalk_user=name_only_candidates[0], reason="name_only_title_missing")
    if len(name_only_candidates) > 1:
        return UserMatchResult(
            status="ambiguous",
            ad_user=ad_user,
            reason="matched by name only, multiple KTalk candidates, title missing in AD or KTalk",
            candidates=[
                {"ktalk_mention_id": c.mention_id, "ktalk_display_name": c.display_name, "ktalk_post": c.post}
                for c in name_only_candidates
            ],
        )
    return UserMatchResult(status="not_found", ad_user=ad_user, reason="no_candidates")


def find_ktalk_match_for_ad_user(ad_user: ADUser) -> UserMatchResult:
    candidates = search_ktalk_users(
        query=ad_user.login,
        base_url=CONFIG["ktalk_base_url"],
        talk_host=CONFIG["ktalk_talk_host"],
        host=CONFIG["ktalk_host"],
        bearer_token=CONFIG["ktalk_bearer_token"],
        verify_ssl=bool(CONFIG.get("verify_ssl", True)),
        request_timeout=int(CONFIG.get("request_timeout", 15)),
        limit=int(CONFIG.get("ktalk_limit", 15)),
    )
    return match_ktalk_user(ad_user, candidates)


def fetch_ktalk_profile_by_mention_id(mention_id: str) -> dict:
    bearer = build_ktalk_bearer(str(CONFIG.get("ktalk_bearer_token", "")))
    homeserver = str(CONFIG.get("ktalk_homeserver", "")).rstrip("/")
    talk_host = str(CONFIG.get("ktalk_talk_host", ""))
    host = str(CONFIG.get("ktalk_host", ""))
    if not bearer or not homeserver:
        return {}

    encoded_mention_id = quote(str(mention_id or "").strip(), safe="")
    url = f"{homeserver}/_matrix/client/r0/profile/{encoded_mention_id}"
    headers = _build_ktalk_headers(talk_host=talk_host, host=host, bearer=bearer)

    try:
        logger.debug("KTalk profile request started mention_id=%s homeserver=%s", mention_id, homeserver)
        response = requests.get(
            url,
            headers=headers,
            verify=bool(CONFIG.get("verify_ssl", True)),
            timeout=int(CONFIG.get("request_timeout", 15)),
        )
    except RequestException as exc:
        logger.exception("Kontur Talk profile request failed mention_id=%s", mention_id)
        raise KTalkUnavailableError("Kontur Talk lookup failed") from exc

    if not response.ok:
        logger.error("Kontur Talk profile non-OK status=%s mention_id=%s", response.status_code, mention_id)
        return {}

    try:
        payload = response.json()
        logger.debug("KTalk profile response mention_id=%s payload=%s", mention_id, payload)
    except ValueError as exc:
        logger.exception("Kontur Talk profile invalid JSON mention_id=%s", mention_id)
        raise KTalkUnavailableError("Kontur Talk lookup failed") from exc

    return payload if isinstance(payload, dict) else {}


def resolve_ad_login_by_mention_id(mention_id: str, profile: dict | None = None) -> str | None:
    profile = profile if profile is not None else fetch_ktalk_profile_by_mention_id(mention_id)
    talk_domain_login = str(profile.get("talk_domain_login", "") or "").strip().lower()
    if talk_domain_login:
        return talk_domain_login

    email = str(profile.get("email", "") or "").strip().lower()
    if "@" in email:
        candidate = email.split("@", 1)[0].strip().lower()
        if candidate:
            return candidate
    return None
