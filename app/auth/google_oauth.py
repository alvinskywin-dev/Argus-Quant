"""
P11 — Google OAuth 2.0 (authorization-code flow, server-side).

Feature-flagged: inert unless GOOGLE_OAUTH_ENABLED=true AND client id/secret/
redirect are configured. This module is pure protocol plumbing — it builds the
authorization URL, validates the CSRF `state`, exchanges the code for an
id_token, and extracts a verified identity. It performs NO persistence and
stores NO Google access token (the code-exchange access_token is discarded).

Security notes:
  * `state` is a random opaque token mirrored in a short-lived HTTPOnly,
    SameSite=Lax cookie and compared on callback (CSRF defence).
  * The id_token is fetched directly from Google's token endpoint over TLS
    (server-to-server), so we trust the channel for authenticity and validate
    the claims: `aud` == our client id, issuer is Google, not expired, and
    `email_verified` is true. We never log the token or its raw claims.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from jose import jwt

from app.auth import security
from app.config import settings

# Google OAuth endpoints.
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

STATE_COOKIE = "oauth_state"
STATE_COOKIE_MAX_AGE = 600  # 10 minutes — long enough for the consent screen
_SCOPES = "openid email profile"


class OAuthError(Exception):
    """Expected OAuth failure carrying an HTTP status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def enabled() -> bool:
    """True only when the flag is on AND every required credential is present."""
    return bool(
        settings.google_oauth_enabled
        and settings.google_client_id
        and settings.google_client_secret
        and settings.google_redirect_uri
    )


def generate_state() -> str:
    return security.generate_opaque_token(24)


def authorization_url(state: str) -> str:
    """Build the Google consent-screen URL the browser is redirected to."""
    if not enabled():
        raise OAuthError(503, "Google login is not configured")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "state": state,
        "access_type": "online",  # no refresh token — we never act on the user's behalf
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def validate_state(cookie_state: Optional[str], query_state: Optional[str]) -> None:
    """CSRF guard: the state echoed back by Google must match our cookie."""
    if not cookie_state or not query_state:
        raise OAuthError(400, "Missing OAuth state")
    if not security.constant_time_equals(cookie_state, query_state):
        raise OAuthError(400, "Invalid OAuth state")


async def exchange_code(code: str) -> str:
    """Swap the authorization code for an id_token (JWT). Returns the raw JWT.

    The access_token Google also returns is intentionally ignored and never
    stored — we only need the identity assertion in the id_token.
    """
    if not enabled():
        raise OAuthError(503, "Google login is not configured")
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)
    except httpx.HTTPError as exc:
        raise OAuthError(502, "Could not reach Google token endpoint") from exc
    if resp.status_code != 200:
        # Do not echo Google's body — it can contain the code/secret context.
        raise OAuthError(502, "Google token exchange failed")
    id_token = resp.json().get("id_token")
    if not id_token:
        raise OAuthError(502, "Google response missing id_token")
    return id_token


def extract_identity(id_token: str) -> dict[str, Any]:
    """Decode the id_token and return a validated identity.

    Returns {sub, email, email_verified, name, picture}. Raises OAuthError on
    any claim that fails validation (audience, issuer, expiry, missing/
    unverified email).
    """
    try:
        claims = jwt.get_unverified_claims(id_token)
    except Exception as exc:  # noqa: BLE001 — malformed token
        raise OAuthError(400, "Malformed Google id_token") from exc
    return validate_claims(claims)


def validate_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Validate decoded id_token claims (split out for unit testing)."""
    aud = claims.get("aud")
    if aud != settings.google_client_id:
        raise OAuthError(401, "OAuth token audience mismatch")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise OAuthError(401, "OAuth token issuer mismatch")
    exp = claims.get("exp")
    if not exp or float(exp) < time.time():
        raise OAuthError(401, "OAuth token expired")

    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub:
        raise OAuthError(401, "OAuth token missing subject")
    if not email:
        raise OAuthError(400, "Google account has no email")
    # email_verified can arrive as bool or the string "true".
    ev = claims.get("email_verified")
    email_verified = ev is True or str(ev).lower() == "true"
    if not email_verified:
        raise OAuthError(403, "Google email is not verified")

    return {
        "sub": str(sub),
        "email": email,
        "email_verified": True,
        "name": claims.get("name") or None,
        "picture": claims.get("picture") or None,
    }
