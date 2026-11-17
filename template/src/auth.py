"""Env-var credential loader. Never logs or returns secrets in messages."""
import os

from errors import AuthError


def load_secret(env_var: str, required: bool = True) -> str | None:
    value = os.environ.get(env_var)
    if not value:
        if required:
            raise AuthError(f"Missing required environment variable {env_var}.")
        return None
    return value


def auth_headers(auth_type: str = "none", env_var: str = "API_KEY",
                 header: str = "Authorization") -> dict[str, str]:
    if auth_type == "none":
        return {}
    secret = load_secret(env_var)
    if auth_type == "api_key":
        return {header: str(secret)}
    return {header: f"Bearer {secret}"}  # bearer / oauth2 access token
