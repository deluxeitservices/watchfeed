"""Browser logins (HTTP Basic). Staff can have individual names so tracking shows who did what."""
import secrets

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings

basic = HTTPBasic(auto_error=False)


def staff_accounts() -> dict:
    users = {}
    for pair in settings.STAFF_USERS.split(","):
        if ":" in pair:
            u, p = pair.split(":", 1)
            if u.strip() and p.strip():
                users[u.strip()] = p.strip()
    if settings.STAFF_PASSWORD:
        users.setdefault(settings.STAFF_USER, settings.STAFF_PASSWORD)
    return users


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _deny():
    raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


def _admin_ok(c: HTTPBasicCredentials) -> bool:
    return bool(settings.ADMIN_PASSWORD) and _eq(c.username, settings.ADMIN_USER) and _eq(c.password, settings.ADMIN_PASSWORD)


def is_admin(creds: Optional[HTTPBasicCredentials] = Depends(basic)) -> str:
    if not creds:
        _deny()
    if settings.DEMO_MODE:
        raise HTTPException(403, "Admin actions are disabled in demo mode")
    if not _admin_ok(creds):
        _deny()
    return creds.username


def is_staff(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(basic)) -> str:
    """Returns the logged-in username."""
    if settings.DEMO_MODE:
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(403, "Demo preview is read-only")
        request.state.is_admin = False
        return "demo"
    if not creds:
        _deny()
    if _admin_ok(creds):
        request.state.is_admin = True
        return creds.username
    pw = staff_accounts().get(creds.username)
    if pw is None or not _eq(creds.password, pw):
        _deny()
    request.state.is_admin = False
    return creds.username
