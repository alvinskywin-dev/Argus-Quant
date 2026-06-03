"""P11 — Google OAuth: protocol guards + user create/link logic (no network/DB).

The stateless protocol pieces (enabled-gate, state CSRF, claim validation) are
tested directly. The create/link/dedup logic in service.login_or_link_google is
exercised against a tiny fake session with the lookup helpers monkeypatched,
since the suite has no async-sqlite driver available.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import app.auth.service as service
from app.auth import google_oauth, security
from app.auth.service import OAUTH_NO_PASSWORD
from app.config import settings
from app.database.models import AuthUser


@pytest.fixture
def oauth_cfg():
    """Save/restore the OAuth-relevant settings around a test."""
    keys = (
        "google_oauth_enabled",
        "google_client_id",
        "google_client_secret",
        "google_redirect_uri",
    )
    saved = {k: getattr(settings, k) for k in keys}
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _configure(cfg, *, enabled=True):
    cfg.google_oauth_enabled = enabled
    cfg.google_client_id = "client-123.apps.googleusercontent.com"
    cfg.google_client_secret = "secret"
    cfg.google_redirect_uri = "https://app.example/api/auth/google/callback"


def _claims(**over):
    base = {
        "aud": "client-123.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
        "exp": time.time() + 600,
        "sub": "google-sub-1",
        "email": "user@example.com",
        "email_verified": True,
        "name": "Test User",
        "picture": "https://lh3.googleusercontent.com/a/x",
    }
    base.update(over)
    return base


# ── enabled gate ──────────────────────────────────────────────────


def test_disabled_provider(oauth_cfg):
    _configure(oauth_cfg, enabled=False)
    assert google_oauth.enabled() is False


def test_missing_config_disables_and_blocks_url(oauth_cfg):
    _configure(oauth_cfg, enabled=True)
    oauth_cfg.google_client_id = ""  # incomplete config
    assert google_oauth.enabled() is False
    with pytest.raises(google_oauth.OAuthError) as ei:
        google_oauth.authorization_url("state")
    assert ei.value.status_code == 503


def test_authorization_url_built_when_configured(oauth_cfg):
    _configure(oauth_cfg)
    url = google_oauth.authorization_url("xyz-state")
    assert url.startswith(google_oauth.GOOGLE_AUTH_ENDPOINT)
    assert "state=xyz-state" in url
    assert "client-123" in url
    assert "scope=openid" in url


# ── state CSRF ────────────────────────────────────────────────────


def test_missing_state_rejected():
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.validate_state(None, "q")
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.validate_state("c", None)


def test_invalid_state_rejected():
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.validate_state("cookie-state", "different-state")


def test_matching_state_ok():
    google_oauth.validate_state("same", "same")  # must not raise


# ── claim validation ──────────────────────────────────────────────


def test_missing_email_rejected(oauth_cfg):
    _configure(oauth_cfg)
    with pytest.raises(google_oauth.OAuthError) as ei:
        google_oauth.validate_claims(_claims(email=""))
    assert ei.value.status_code == 400


def test_unverified_email_rejected(oauth_cfg):
    _configure(oauth_cfg)
    with pytest.raises(google_oauth.OAuthError) as ei:
        google_oauth.validate_claims(_claims(email_verified=False))
    assert ei.value.status_code == 403


def test_audience_mismatch_rejected(oauth_cfg):
    _configure(oauth_cfg)
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.validate_claims(_claims(aud="someone-else"))


def test_expired_token_rejected(oauth_cfg):
    _configure(oauth_cfg)
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.validate_claims(_claims(exp=time.time() - 10))


def test_valid_claims_extracted(oauth_cfg):
    _configure(oauth_cfg)
    ident = google_oauth.validate_claims(_claims())
    assert ident["sub"] == "google-sub-1"
    assert ident["email"] == "user@example.com"
    assert ident["email_verified"] is True


# ── create / link logic ───────────────────────────────────────────


class _FakeDB:
    """Minimal async session: records adds and assigns ids on flush."""

    def __init__(self):
        self.added: list = []
        self._next = 500

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, AuthUser) and getattr(obj, "id", None) is None:
                obj.id = self._next
                self._next += 1


def _patch_lookups(monkeypatch, *, by_provider=None, by_email=None):
    async def _prov(db, provider, pid):
        return by_provider

    async def _email(db, email):
        return by_email

    async def _sess(db, user, ip, device):
        return "refresh-token"

    async def _rec(*a, **k):
        return None

    monkeypatch.setattr(service, "get_user_by_provider", _prov)
    monkeypatch.setattr(service, "get_user_by_email", _email)
    monkeypatch.setattr(service, "_create_session", _sess)
    monkeypatch.setattr(service, "_record_login", _rec)


def _run_google_login(db, **over):
    kw = dict(
        sub="google-sub-1",
        email="user@example.com",
        email_verified=True,
        name="Test User",
        picture="https://lh3.googleusercontent.com/a/x",
        ip="1.2.3.4",
        device="pytest",
    )
    kw.update(over)
    return asyncio.run(service.login_or_link_google(db, **kw))


def test_new_google_user_becomes_free(monkeypatch):
    _patch_lookups(monkeypatch, by_provider=None, by_email=None)
    db = _FakeDB()
    user, access, refresh = _run_google_login(db)
    assert user.role == "FREE"
    assert user.status == "ACTIVE"
    assert user.provider == "google"
    assert user.provider_user_id == "google-sub-1"
    assert user.timezone == "UTC"
    assert user.is_verified is True
    assert user.password_hash == OAUTH_NO_PASSWORD  # no usable password
    assert access and refresh == "refresh-token"
    # exactly one AuthUser created
    assert sum(isinstance(o, AuthUser) for o in db.added) == 1


def test_existing_email_links_provider(monkeypatch):
    pw_hash = security.hash_password("OriginalPass1!")
    existing = AuthUser(
        id=42,
        email="user@example.com",
        password_hash=pw_hash,
        role="PREMIUM",
        status="ACTIVE",
        is_verified=False,
        provider="email",
        provider_user_id=None,
    )
    _patch_lookups(monkeypatch, by_provider=None, by_email=existing)
    db = _FakeDB()
    user, _access, _refresh = _run_google_login(db)
    assert user is existing
    assert user.provider == "google"
    assert user.provider_user_id == "google-sub-1"
    assert user.is_verified is True
    assert user.role == "PREMIUM"  # role preserved
    # no new AuthUser row created
    assert sum(isinstance(o, AuthUser) for o in db.added) == 0
    # password preserved -> email/password login still works
    assert user.password_hash == pw_hash
    assert security.verify_password("OriginalPass1!", user.password_hash)


def test_duplicate_provider_id_does_not_duplicate_user(monkeypatch):
    linked = AuthUser(
        id=7,
        email="user@example.com",
        password_hash=OAUTH_NO_PASSWORD,
        role="FREE",
        status="ACTIVE",
        is_verified=True,
        provider="google",
        provider_user_id="google-sub-1",
    )
    _patch_lookups(monkeypatch, by_provider=linked, by_email=None)
    db = _FakeDB()
    user, _a, _r = _run_google_login(db)
    assert user is linked
    assert sum(isinstance(o, AuthUser) for o in db.added) == 0


def test_unverified_email_blocked_at_service(monkeypatch):
    _patch_lookups(monkeypatch, by_provider=None, by_email=None)
    db = _FakeDB()
    with pytest.raises(service.AuthError) as ei:
        _run_google_login(db, email_verified=False)
    assert ei.value.status_code == 403


def test_suspended_account_blocked(monkeypatch):
    suspended = AuthUser(
        id=9,
        email="user@example.com",
        password_hash=OAUTH_NO_PASSWORD,
        role="FREE",
        status="SUSPENDED",
        provider="google",
        provider_user_id="google-sub-1",
    )
    _patch_lookups(monkeypatch, by_provider=suspended, by_email=None)
    db = _FakeDB()
    with pytest.raises(service.AuthError) as ei:
        _run_google_login(db)
    assert ei.value.status_code == 403
