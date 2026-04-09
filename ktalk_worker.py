from __future__ import annotations

import re
from dataclasses import dataclass

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

    headers = {
        "accept": "application/json",
        "authorization": bearer,
        "talk-host": talk_host,
        "host": host,
<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
        "user-agent": str(CONFIG.get("ktalk_user_agent", "autoalerter/1.0")),
=======
        "user-agent": "autoalerter/1.0",
>>>>>>> test
    }

    try:
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


def match_ktalk_user_strict(ad_user: ADUser, candidates: list[KTalkUser]) -> KTalkUser | None:
    ad_name = normalize_value(f"{ad_user.first_name} {ad_user.last_name}")
    ad_name_rev = normalize_value(f"{ad_user.last_name} {ad_user.first_name}")
    ad_title = normalize_value(ad_user.title)

    for candidate in candidates:
        if candidate.deactivated:
            continue

        name = normalize_value(candidate.display_name)
        post = normalize_value(candidate.post)

        if name in {ad_name, ad_name_rev} and ad_title and post == ad_title:
            return candidate

    return None


def find_ktalk_match_for_ad_user(ad_user: ADUser) -> KTalkUser | None:
    candidates = search_ktalk_users(
        query=ad_user.login,
        base_url=CONFIG["ktalk_base_url"],
        talk_host=CONFIG["ktalk_talk_host"],
        host=CONFIG["ktalk_host"],
        bearer_token=CONFIG["ktalk_bearer_token"],
        verify_ssl=bool(CONFIG.get("verify_ssl", True)),
        request_timeout=int(CONFIG.get("request_timeout", 15)),
<<<<<<< codex/create-python-api-daemon-for-ad-to-ktalk-resolution-idwlnp
        limit=int(CONFIG.get("ktalk_limit", 15)),
=======
>>>>>>> test
    )
    return match_ktalk_user_strict(ad_user, candidates)
