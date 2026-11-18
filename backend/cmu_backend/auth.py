"""Ověřování přístupu: statické API tokeny (stroje) a přihlášení přes Keycloak (OIDC, lidé).

Keycloak vydá frontendu přístupový token (JWT). Backend ho ověří podpisem proti veřejným klíčům
realmu (JWKS), zkontroluje vydavatele, platnost a klienta, a roli určí ze skupin/rolí v tokenu.
Skupiny typicky pocházejí z LDAPu: Keycloak LDAP federace + group-ldap-mapper, a do tokenu je
dostane mapper „Group Membership“ (claim `groups`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import httpx
import jwt

from .settings import AuthSettings, OidcSettings

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class Role(IntEnum):
    none = 0
    viewer = 1
    operator = 2


@dataclass
class Principal:
    role: Role
    user: str | None = None      # uživatelské jméno z Keycloaku; u statického tokenu None
    via: str = "none"            # none | token | oidc | open (bez přihlášení)
    groups: tuple[str, ...] = ()
    error: str | None = None     # proč token neprošel (pro odpověď 401)


class AuthError(Exception):
    pass


def _claim(claims: dict[str, Any], path: str) -> list[str]:
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return []
        cur = cur[part]
    if isinstance(cur, str):
        return [cur]
    if isinstance(cur, list):
        return [str(x) for x in cur]
    return []


def group_matches(have: str, wanted: str) -> bool:
    """Skupina z Keycloaku přijde jako plná cesta („/it/cmu-operators“) nebo jen jméno.
    Nastavená hodnota s lomítkem = přesná cesta, bez lomítka = poslední část cesty."""
    h, w = have.strip().strip("/").casefold(), wanted.strip().strip("/").casefold()
    return h == w or ("/" not in w and h.rsplit("/", 1)[-1] == w)


class OidcVerifier:
    def __init__(self, cfg: OidcSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=10, verify=cfg.verifyTls, transport=transport)
        self._discovery: dict[str, Any] | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_at = 0.0
        self._forced_at = -1e9
        self._lock = asyncio.Lock()

    @property
    def discovery_url(self) -> str:
        return self.cfg.discoveryUrl or self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            r = await self._client.get(self.discovery_url)
            r.raise_for_status()
            self._discovery = r.json()
        return self._discovery

    async def _load_keys(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._keys and time.monotonic() - self._keys_at < 600:
                return
            if force and time.monotonic() - self._forced_at < 10:
                return  # ochrana před zahlcením Keycloaku tokeny s neznámým kid
            if force:
                self._forced_at = time.monotonic()
            d = await self.discovery()
            r = await self._client.get(d["jwks_uri"])
            r.raise_for_status()
            keys = {}
            for k in r.json().get("keys", []):
                if k.get("use", "sig") != "sig" or "kid" not in k:
                    continue
                try:
                    keys[k["kid"]] = jwt.PyJWK(k)
                except jwt.PyJWTError as e:
                    log.debug("JWKS: klíč %s přeskočen: %s", k.get("kid"), e)
            self._keys, self._keys_at = keys, time.monotonic()

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("neplatný token") from e
        kid = header.get("kid")
        try:
            await self._load_keys()
            if kid not in self._keys:
                await self._load_keys(force=True)  # Keycloak mohl klíče rotovat
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise AuthError(f"Keycloak je nedostupný: {e}") from e
        key = self._keys.get(kid or "")
        if key is None:
            raise AuthError("token podepsaný neznámým klíčem")
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key=key.key, algorithms=ALGORITHMS, issuer=self.cfg.issuer,
                audience=self.cfg.audience, leeway=self.cfg.leewayS,
                options={"require": ["exp", "iat", "iss"], "verify_aud": self.cfg.audience is not None},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("platnost přihlášení vypršela") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"neplatný token: {e}") from e
        if claims.get("typ") not in (None, "Bearer"):
            raise AuthError("token není přístupový (typ Bearer)")
        if self.cfg.audience is None and claims.get("azp") != self.cfg.clientId:
            raise AuthError("token nebyl vydán pro klienta CMU")
        return claims

    def groups(self, claims: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for path in self.cfg.roleClaims:
            out += _claim(claims, path.replace("{clientId}", self.cfg.clientId))
        return out

    def role(self, claims: dict[str, Any]) -> Role:
        have = self.groups(claims)
        if any(group_matches(h, w) for h in have for w in self.cfg.operatorRoles):
            return Role.operator
        if any(group_matches(h, w) for h in have for w in self.cfg.viewerRoles):
            return Role.viewer
        return Role.none

    async def close(self) -> None:
        await self._client.aclose()


class Authenticator:
    def __init__(self, cfg: AuthSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.oidc = OidcVerifier(cfg.oidc, transport) if cfg.oidc else None

    async def principal(self, authorization: str | None) -> Principal:
        if not self.cfg.enabled:
            return Principal(Role.operator, via="open")
        header = authorization or ""
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token:
            return Principal(Role.none)
        if any(secrets.compare_digest(token, t) for t in self.cfg.operatorTokens):
            return Principal(Role.operator, via="token")
        if any(secrets.compare_digest(token, t) for t in self.cfg.viewerTokens):
            return Principal(Role.viewer, via="token")
        if self.oidc is None or token.count(".") != 2:
            return Principal(Role.none, error="neplatný token")
        try:
            claims = await self.oidc.verify(token)
        except AuthError as e:
            return Principal(Role.none, error=str(e))
        user = str(claims.get(self.oidc.cfg.usernameClaim) or claims.get("sub") or "?")
        groups = tuple(self.oidc.groups(claims))
        role = self.oidc.role(claims)
        return Principal(role, user=user, via="oidc", groups=groups,
                         error=None if role else f"uživatel {user} nemá v CMU žádnou roli")

    async def public_config(self) -> dict[str, Any]:
        """Co frontend potřebuje k přihlášení (bez tajemství)."""
        if self.oidc is None:
            return {"mode": "token" if self.cfg.enabled else "none"}
        out: dict[str, Any] = {"mode": "oidc", "issuer": self.oidc.cfg.issuer, "clientId": self.oidc.cfg.clientId,
                               "scopes": self.oidc.cfg.scopes, "tokensAllowed": bool(
                                   self.cfg.viewerTokens or self.cfg.operatorTokens)}
        try:
            d = await self.oidc.discovery()
            out.update(authorizationEndpoint=d["authorization_endpoint"], tokenEndpoint=d["token_endpoint"],
                       endSessionEndpoint=d.get("end_session_endpoint"))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            out["error"] = f"Keycloak je nedostupný: {e}"
        return out

    async def close(self) -> None:
        if self.oidc:
            await self.oidc.close()
