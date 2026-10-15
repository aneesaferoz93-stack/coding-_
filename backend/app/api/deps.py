"""
deps.py — authentication and shared dependencies.

Two credential types, because there are two very different callers:

* **Service API keys** — e-Fard, the USSD gateway, dashboards. Static, rotatable,
  checked against ``settings.service_api_keys``.
* **Device tokens** — issued to enrolled Patwari handsets. HMAC-signed,
  time-limited, bound to a device id and an officer id. The PWA sends one with
  every sync so that ground evidence can be attributed to a named human.

There is no password login for field staff: the device *is* the credential, and
it is enrolled in person by the Tehsildar. That matches how handsets are
actually issued in a revenue circle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class Role(str, Enum):
    SERVICE = "SERVICE"
    PATWARI = "PATWARI"
    QANOONGO = "QANOONGO"
    TEHSILDAR = "TEHSILDAR"
    DEPUTY_COMMISSIONER = "DEPUTY_COMMISSIONER"
    BOARD_OF_REVENUE = "BOARD_OF_REVENUE"
    AUDITOR = "AUDITOR"


ROLE_RANK: dict[str, int] = {
    Role.SERVICE.value: 0,
    Role.PATWARI.value: 1,
    Role.QANOONGO.value: 2,
    Role.TEHSILDAR.value: 3,
    Role.DEPUTY_COMMISSIONER.value: 4,
    Role.BOARD_OF_REVENUE.value: 5,
    Role.AUDITOR.value: 3,
}


@dataclass(slots=True)
class Principal:
    subject: str
    role: Role
    district: str | None = None
    device_id: str | None = None

    def at_least(self, role: Role) -> bool:
        return ROLE_RANK.get(self.role.value, -1) >= ROLE_RANK.get(role.value, 99)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_device_token(*, device_id: str, officer_id: str, role: str,
                       district: str | None, settings: Settings,
                       ttl_h: int | None = None) -> str:
    """HMAC-signed device token: ``b64(payload).b64(sig)``."""
    ttl = ttl_h or settings.device_token_ttl_h
    payload = {
        "dev": device_id, "off": officer_id, "role": role,
        "dist": district, "exp": int(time.time()) + ttl * 3600,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(settings.device_token_secret.encode("utf-8"),
                        body.encode("utf-8"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_device_token(token: str, settings: Settings) -> Principal:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed device token") from exc
    expected = _b64(hmac.new(settings.device_token_secret.encode("utf-8"),
                             body.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device token signature invalid")
    data = json.loads(_b64d(body))
    if int(data.get("exp", 0)) < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device token expired")
    try:
        role = Role(str(data.get("role", "PATWARI")).upper())
    except ValueError:
        role = Role.PATWARI
    return Principal(subject=str(data.get("off", "unknown")), role=role,
                     district=data.get("dist"), device_id=str(data.get("dev")))


async def current_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        return verify_device_token(authorization.split(" ", 1)[1].strip(), settings)

    if x_api_key:
        for name, key in settings.service_api_keys.items():
            if hmac.compare_digest(x_api_key, key):
                return Principal(subject=f"service:{name}", role=Role.SERVICE)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")

    if settings.env == "dev":
        # Open by default in dev so the demo and the PWA work out of the box.
        # Production requires credentials — enforced in main.py at startup.
        return Principal(subject="dev-anonymous", role=Role.BOARD_OF_REVENUE)

    raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                        "provide X-API-Key or a Bearer device token")


def require(role: Role):
    """Role gate. Service keys pass everything (they are our own systems)."""

    def _dep(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if p.role is Role.SERVICE:
            return p
        if not p.at_least(role):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires {role.value} or above; caller is {p.role.value}")
        return p

    return _dep
