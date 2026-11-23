"""Gemini credentials, kept in Windows Credential Manager via keyring.

Two ways to reach Gemini:
  api_key  a key from Google AI Studio            -> `start.bat --set-key`
  oauth    sign in with your Google account (once) -> `start.bat --login-google`
"""

from __future__ import annotations

import json
import logging

import keyring

from . import paths
from .config import BrainCfg

log = logging.getLogger(__name__)

SERVICE = "Amolex-9026"
KEY_NAME = "gemini_api_key"
TOKEN_NAME = "gemini_oauth_token"


def get_api_key() -> str | None:
    return keyring.get_password(SERVICE, KEY_NAME)


def set_api_key(key: str) -> None:
    keyring.set_password(SERVICE, KEY_NAME, key.strip())


def clear_all() -> None:
    for name in (KEY_NAME, TOKEN_NAME):
        try:
            keyring.delete_password(SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            pass


def login_google(cfg: BrainCfg):
    """Run the one-time browser sign-in and store the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret_file = paths.expand(cfg.oauth_client_secret_file)
    if not secret_file.is_absolute():
        secret_file = paths.PROJECT_DIR / secret_file
    if not secret_file.exists():
        raise FileNotFoundError(
            f"{secret_file} not found. Download the OAuth client (Desktop app) JSON from "
            "Google Cloud Console > APIs & Services > Credentials and save it there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), cfg.oauth_scopes)
    creds = flow.run_local_server(port=0)
    keyring.set_password(SERVICE, TOKEN_NAME, creds.to_json())
    return creds


def load_oauth_creds(cfg: BrainCfg):
    """Return refreshed Google credentials, or None if the user never signed in."""
    raw = keyring.get_password(SERVICE, TOKEN_NAME)
    if not raw:
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(json.loads(raw), cfg.oauth_scopes)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        keyring.set_password(SERVICE, TOKEN_NAME, creds.to_json())
    if cfg.oauth_quota_project:
        creds = creds.with_quota_project(cfg.oauth_quota_project)
    return creds if creds.valid else None
