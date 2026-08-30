from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

from threegpp_kg.config import SecurityConfig
from threegpp_kg.parsers.documents import (
    UnsafeDocumentError,
    sanitize_office_package,
    validate_office_package,
)
from threegpp_kg.security import (
    AuthenticationError,
    OidcAuthMiddleware,
    OidcTokenValidator,
)


class FakeValidator:
    async def validate(self, token: str) -> dict[str, str]:
        if token != "valid":
            raise AuthenticationError("invalid")
        return {"sub": "architect-1"}


@pytest.mark.asyncio
async def test_oidc_middleware_protects_api_but_not_health() -> None:
    app = FastAPI()
    app.add_middleware(OidcAuthMiddleware, validator=FakeValidator())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/private")
    async def private() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/private")).status_code == 401
        assert (
            await client.get("/private", headers={"Authorization": "Bearer invalid"})
        ).status_code == 401
        assert (
            await client.get("/private", headers={"Authorization": "Bearer valid"})
        ).status_code == 200


def test_office_package_rejects_macros_and_local_external_relationships() -> None:
    macro = io.BytesIO()
    with zipfile.ZipFile(macro, "w") as archive:
        archive.writestr("word/vbaProject.bin", b"macro")
    with pytest.raises(UnsafeDocumentError, match="macro"):
        validate_office_package(macro.getvalue())

    relationship = io.BytesIO()
    with zipfile.ZipFile(relationship, "w") as archive:
        archive.writestr(
            "word/_rels/document.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="r1" TargetMode="External" Target="file:///etc/passwd" />
            </Relationships>""",
        )
    with pytest.raises(UnsafeDocumentError, match="external relationship"):
        validate_office_package(relationship.getvalue())


def test_office_package_strips_inert_external_hyperlinks_and_templates() -> None:
    package = io.BytesIO()
    relationship_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "word/_rels/document.xml.rels",
            f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="r1" Type="{relationship_type}/hyperlink"
              TargetMode="External" Target="file:///Users/author/private.docx" />
            <Relationship Id="r2" Type="{relationship_type}/attachedTemplate"
              TargetMode="External" Target="file:///templates/3gpp.dot" />
            </Relationships>""",
        )
    sanitized = sanitize_office_package(package.getvalue())
    with zipfile.ZipFile(io.BytesIO(sanitized)) as archive:
        relationships = archive.read("word/_rels/document.xml.rels")
    assert b"TargetMode" not in relationships
    assert b"private.docx" not in relationships


def test_office_package_strips_local_external_ole_objects() -> None:
    package = io.BytesIO()
    relationship_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "word/charts/_rels/chart1.xml.rels",
            f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="r1" Type="{relationship_type}/oleObject"
              TargetMode="External" Target="file:///C:/author/source.xlsx" />
            <Relationship Id="r2" Type="{relationship_type}/image"
              TargetMode="External" Target="cid:image001.png@example" />
            </Relationships>""",
        )
    sanitized = sanitize_office_package(package.getvalue())
    with zipfile.ZipFile(io.BytesIO(sanitized)) as archive:
        relationships = archive.read("word/charts/_rels/chart1.xml.rels")
    assert b"TargetMode" not in relationships
    assert b"source.xlsx" not in relationships
    assert b"image001.png" not in relationships


def test_office_package_strips_missing_internal_relationship_targets() -> None:
    package = io.BytesIO()
    relationship_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("word/document.xml", b"document")
        archive.writestr(
            "word/_rels/document.xml.rels",
            f"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="r1" Type="{relationship_type}/image" Target="../NULL" />
            </Relationships>""",
        )
    sanitized = sanitize_office_package(package.getvalue())
    with zipfile.ZipFile(io.BytesIO(sanitized)) as archive:
        relationships = archive.read("word/_rels/document.xml.rels")
    assert b"NULL" not in relationships


@pytest.mark.asyncio
async def test_oidc_validator_uses_discovery_jwks_issuer_and_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "key-1", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "architect-1",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": "https://identity.internal",
            "aud": "threegpp-evidence-graph",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    config = SecurityConfig(
        allowed_source_hosts=["www.3gpp.org"],
        oidc_issuer="https://identity.internal",
        oidc_audience="threegpp-evidence-graph",
        auth_required=True,
    )
    async with httpx.AsyncClient() as client:
        validator = OidcTokenValidator(config, client=client)
        with respx.mock:
            respx.get("https://identity.internal/.well-known/openid-configuration").mock(
                return_value=httpx.Response(
                    200, json={"jwks_uri": "https://identity.internal/keys"}
                )
            )
            respx.get("https://identity.internal/keys").mock(
                return_value=httpx.Response(200, json={"keys": [public_jwk]})
            )
            claims = await validator.validate(token)
            assert claims["sub"] == "architect-1"
