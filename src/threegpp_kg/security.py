from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import httpx
import jwt
from fastapi import Request
from jwt import PyJWKSet
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from .config import SecurityConfig


class AuthenticationError(ValueError):
    pass


class TokenValidator(Protocol):
    async def validate(self, token: str) -> dict[str, Any]: ...


class OidcTokenValidator:
    def __init__(
        self,
        config: SecurityConfig,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: int = 3600,
    ) -> None:
        if not config.oidc_issuer:
            raise ValueError("OIDC issuer is required")
        self.issuer = config.oidc_issuer.rstrip("/")
        self.audience = config.oidc_audience
        self.client = client
        self.cache_seconds = cache_seconds
        self._jwks: PyJWKSet | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def validate(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationError("invalid bearer token") from exc
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
            raise AuthenticationError("token algorithm is not allowed")
        keys = await self._keys()
        key = next((item.key for item in keys.keys if item.key_id == key_id), None)
        if key is None:
            self._expires_at = 0
            keys = await self._keys()
            key = next((item.key for item in keys.keys if item.key_id == key_id), None)
        if key is None:
            raise AuthenticationError("token signing key was not found")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("bearer token validation failed") from exc
        return dict(claims)

    async def _keys(self) -> PyJWKSet:
        if self._jwks is not None and time.monotonic() < self._expires_at:
            return self._jwks
        async with self._lock:
            if self._jwks is not None and time.monotonic() < self._expires_at:
                return self._jwks
            owns_client = self.client is None
            client = self.client or httpx.AsyncClient(timeout=10)
            try:
                discovery = await client.get(f"{self.issuer}/.well-known/openid-configuration")
                discovery.raise_for_status()
                jwks_uri = discovery.json().get("jwks_uri")
                if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
                    raise AuthenticationError("OIDC discovery returned an invalid JWKS URI")
                response = await client.get(jwks_uri)
                response.raise_for_status()
                self._jwks = PyJWKSet.from_dict(response.json())
                self._expires_at = time.monotonic() + self.cache_seconds
                return self._jwks
            except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                raise AuthenticationError("OIDC metadata could not be loaded") from exc
            finally:
                if owns_client:
                    await client.aclose()


class OidcAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, validator: TokenValidator) -> None:
        super().__init__(app)
        self.validator = validator

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in {"/health", "/docs", "/openapi.json"}:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse(
                {"detail": "bearer authentication is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            request.state.identity = await self.validator.validate(header[7:])
        except AuthenticationError:
            return JSONResponse(
                {"detail": "bearer token is invalid"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return await call_next(request)
