from __future__ import annotations

from dataclasses import dataclass, field
import re

from ldap3 import ALL, Connection, Server
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from cnf import CONFIG, get_logger

logger = get_logger()


class LDAPUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class ADUser:
    login: str
    first_name: str
    last_name: str
    display_name: str
    title: str
    active: bool


@dataclass(slots=True)
class UserMatchResult:
    status: str  # found, not_found, ambiguous
    ad_user: ADUser | None = None
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def normalize_value(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = value.replace("ё", "е")
    return value


def fetch_ad_user_with_connection(conn: Connection, ad_base_dn: str, login: str) -> ADUser | None:
    safe_login = escape_filter_chars(login)
    search_filter = f"(&(objectClass=user)(sAMAccountName={safe_login}))"

    conn.search(
        search_base=ad_base_dn,
        search_filter=search_filter,
        attributes=["givenName", "sn", "displayName", "title", "userAccountControl"],
    )

    if not conn.entries:
        return None

    entry = conn.entries[0]
    uac = int(getattr(entry, "userAccountControl", 0).value or 0)
    is_active = not bool(uac & 0x0002)

    return ADUser(
        login=login,
        first_name=str(getattr(entry, "givenName", "").value or ""),
        last_name=str(getattr(entry, "sn", "").value or ""),
        display_name=str(getattr(entry, "displayName", "").value or ""),
        title=str(getattr(entry, "title", "").value or ""),
        active=is_active,
    )


def _open_connection() -> Connection:
    try:
        logger.debug("Connecting to AD host=%s user=%s", CONFIG["ad_host"], CONFIG["ad_user"])
        server = Server(CONFIG["ad_host"], get_info=ALL)
        conn = Connection(server, user=CONFIG["ad_user"], password=CONFIG["ad_password"], auto_bind=True)
        logger.debug("AD connection established")
        return conn
    except LDAPException as exc:
        logger.exception("Active Directory connection failed")
        raise LDAPUnavailableError("Active Directory connection failed") from exc


def fetch_ad_users_batch(logins: list[str]) -> dict[str, ADUser]:
    if not logins:
        return {}

    found: dict[str, ADUser] = {}
    try:
        logger.debug("AD batch lookup started count=%s", len(logins))
        with _open_connection() as conn:
            for login in logins:
                user = fetch_ad_user_with_connection(conn, CONFIG["ad_base_dn"], login)
                if user is not None:
                    found[login] = user
        logger.debug("AD batch lookup completed found=%s", len(found))
    except LDAPUnavailableError:
        raise
    except LDAPException as exc:
        logger.exception("Active Directory lookup failed")
        raise LDAPUnavailableError("Active Directory lookup failed") from exc

    return found


def find_ad_user_by_ktalk_profile(display_name: str, post: str) -> UserMatchResult:
    normalized_display_name = normalize_value(display_name)
    normalized_post = normalize_value(post)

    if not normalized_display_name:
        logger.info(
            "KTalk profile has no displayname displayname=%s post=%s",
            display_name,
            post,
        )
        return UserMatchResult(status="not_found", reason="displayname_missing")

    parts = [item for item in normalized_display_name.split(" ") if item]
    if len(parts) < 2:
        logger.info("KTalk displayname has insufficient parts for AD lookup displayname=%s", display_name)
        return UserMatchResult(status="not_found", reason="displayname_has_single_part")

    first_part = parts[0]
    last_part = parts[-1]
    candidate_pairs = {(first_part, last_part), (last_part, first_part)}
    name_filters = []
    for first_name, last_name in candidate_pairs:
        safe_first = escape_filter_chars(first_name)
        safe_last = escape_filter_chars(last_name)
        name_filters.append(f"(&(givenName={safe_first})(sn={safe_last}))")

    safe_title = escape_filter_chars(normalized_post)
    combined_name_filter = "".join(name_filters)
    name_filter = f"(&(objectClass=user)(|{combined_name_filter}))"
    title_filter = f"(&(objectClass=user)(title={safe_title})(|{combined_name_filter}))" if normalized_post else name_filter
    search_filter = title_filter
    logger.debug("AD lookup by KTalk identity search prepared displayname=%s post=%s", display_name, post)

    strict_candidates: list[ADUser] = []
    name_only_candidates: list[ADUser] = []
    try:
        with _open_connection() as conn:
            conn.search(
                search_base=CONFIG["ad_base_dn"],
                search_filter=search_filter,
                attributes=["sAMAccountName", "givenName", "sn", "displayName", "title", "userAccountControl"],
            )

            for entry in conn.entries:
                uac = int(getattr(entry, "userAccountControl", 0).value or 0)
                is_active = not bool(uac & 0x0002)
                if not is_active:
                    continue

                login = str(getattr(entry, "sAMAccountName", "").value or "").strip().lower()
                first_name = str(getattr(entry, "givenName", "").value or "").strip()
                last_name = str(getattr(entry, "sn", "").value or "").strip()
                ad_display_name = str(getattr(entry, "displayName", "").value or "").strip()
                title = str(getattr(entry, "title", "").value or "").strip()
                if not login:
                    continue

                ad_title_normalized = normalize_value(title)
                variant_1 = normalize_value(f"{first_name} {last_name}")
                variant_2 = normalize_value(f"{last_name} {first_name}")
                variant_display = normalize_value(ad_display_name)
                if normalized_display_name not in {variant_1, variant_2, variant_display}:
                    continue

                ad_user = ADUser(
                    login=login,
                    first_name=first_name,
                    last_name=last_name,
                    display_name=ad_display_name,
                    title=title,
                    active=True,
                )
                if normalized_post and ad_title_normalized:
                    if ad_title_normalized == normalized_post:
                        strict_candidates.append(ad_user)
                else:
                    name_only_candidates.append(ad_user)
    except LDAPUnavailableError:
        raise
    except LDAPException as exc:
        logger.exception("Active Directory lookup by KTalk profile failed")
        raise LDAPUnavailableError("Active Directory lookup failed") from exc

    if len(strict_candidates) == 1:
        return UserMatchResult(status="found", ad_user=strict_candidates[0], reason="name_and_title")
    if len(strict_candidates) > 1:
        logger.warning(
            "AD lookup by KTalk identity is ambiguous displayname=%s post=%s candidates=%s",
            display_name,
            post,
            len(strict_candidates),
        )
        return UserMatchResult(
            status="ambiguous",
            reason="multiple_strict_candidates",
            candidates=[
                {"ad_login": c.login, "ad_name": f"{c.first_name} {c.last_name}".strip() or c.display_name, "ad_title": c.title}
                for c in strict_candidates
            ],
        )
    if len(name_only_candidates) == 1:
        return UserMatchResult(status="found", ad_user=name_only_candidates[0], reason="name_only_title_missing")
    if len(name_only_candidates) > 1:
        logger.warning(
            "AD lookup by KTalk identity is ambiguous displayname=%s post=%s candidates=%s",
            display_name,
            post,
            len(name_only_candidates),
        )
        return UserMatchResult(
            status="ambiguous",
            reason="matched by name only, multiple AD candidates, title missing in AD or KTalk",
            candidates=[
                {"ad_login": c.login, "ad_name": f"{c.first_name} {c.last_name}".strip() or c.display_name, "ad_title": c.title}
                for c in name_only_candidates
            ],
        )

    logger.info("AD user not found by KTalk identity displayname=%s post=%s", display_name, post)
    return UserMatchResult(status="not_found", reason="no_candidates")
