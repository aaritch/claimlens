"""Minimal bearer-token auth with roles.

This is deliberately simple (static tokens from the environment/secret store) so the AUTHORIZATION logic
is testable now. Phase 5/6 swaps the token lookup for Cognito/OIDC or IAM without touching the endpoints.

Roles are ordered: reader < uploader < reviewer < admin.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

ROLE_LEVEL = {"reader": 1, "uploader": 2, "reviewer": 3, "admin": 4}
MIN_TOKEN_LENGTH = 16


class AuthConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Actor:
    name: str
    role: str

    def allows(self, needed: str) -> bool:
        return ROLE_LEVEL[self.role] >= ROLE_LEVEL[needed]


class Authenticator:
    def __init__(self, tokens_spec: str = "", disabled: bool = False):
        self.disabled = disabled
        self._tokens: list[tuple[str, Actor]] = []
        for entry in filter(None, (e.strip() for e in tokens_spec.split(","))):
            parts = entry.split(":")
            if len(parts) != 3 or parts[2] not in ROLE_LEVEL:
                raise AuthConfigError("each token must look like token:username:role (role: %s)" % "/".join(ROLE_LEVEL))
            token, user, role = parts
            if len(token) < MIN_TOKEN_LENGTH:
                raise AuthConfigError(f"token for '{user}' is shorter than {MIN_TOKEN_LENGTH} characters")
            self._tokens.append((token, Actor(user, role)))
        if not self._tokens and not disabled:
            # Secure by default: refuse to start with no way to authenticate (instead of silently being open).
            raise AuthConfigError("no API tokens configured. Set CLAIMLENS_API_TOKENS (or CLAIMLENS_API_AUTH=disabled for local dev ONLY)")

    def authenticate(self, authorization: Optional[str]) -> Actor:
        if self.disabled:
            return Actor("dev", "admin")
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not presented:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        match: Optional[Actor] = None
        for token, actor in self._tokens:  # check every token (no early exit) with constant-time compares
            if hmac.compare_digest(token.encode(), presented.encode()):
                match = actor
        if match is None:
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
        return match
