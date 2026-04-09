from __future__ import annotations

from dataclasses import dataclass

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
        server = Server(CONFIG["ad_host"], get_info=ALL)
        conn = Connection(server, user=CONFIG["ad_user"], password=CONFIG["ad_password"], auto_bind=True)
        return conn
    except LDAPException as exc:
        logger.exception("Active Directory connection failed")
        raise LDAPUnavailableError("Active Directory connection failed") from exc


def fetch_ad_users_batch(logins: list[str]) -> dict[str, ADUser]:
    if not logins:
        return {}

    found: dict[str, ADUser] = {}
    try:
        with _open_connection() as conn:
            for login in logins:
                user = fetch_ad_user_with_connection(conn, CONFIG["ad_base_dn"], login)
                if user is not None:
                    found[login] = user
    except LDAPUnavailableError:
        raise
    except LDAPException as exc:
        logger.exception("Active Directory lookup failed")
        raise LDAPUnavailableError("Active Directory lookup failed") from exc

    return found
