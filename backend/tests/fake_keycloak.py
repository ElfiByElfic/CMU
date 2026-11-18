"""Simulovaný Keycloak realm pro testy: discovery, JWKS a vydávání podepsaných tokenů (RS256).

Používají ho testy backendu (přes httpx.MockTransport) i E2E test v prohlížeči (jako HTTP server,
viz serve()). Tokeny mají stejný tvar jako z Keycloaku: iss, azp, typ=Bearer, preferred_username,
realm_access.roles a groups (mapper „Group Membership“ s plnou cestou).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

USERS = {
    "operator": {"name": "Olga Operátorová", "groups": ["/cmu-operators"], "roles": ["offline_access"]},
    "viewer": {"name": "Viktor Čtenář", "groups": ["/it/cmu-viewers"], "roles": []},
    "nobody": {"name": "Nikdo", "groups": ["/hr"], "roles": []},
}


class FakeKeycloak:
    def __init__(self, base: str = "https://kc.test", realm: str = "cmu", client_id: str = "cmu",
                 token_ttl: int = 300) -> None:
        self.base, self.realm, self.client_id, self.token_ttl = base.rstrip("/"), realm, client_id, token_ttl
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.old_keys: list[tuple[str, Any]] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, str] = {}
        self.jwks_calls = 0
        self.refresh_grants = 0

    @property
    def issuer(self) -> str:
        return f"{self.base}/realms/{self.realm}"

    def rotate(self) -> None:
        self.old_keys.append((self.kid, self.key))
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"k{len(self.old_keys) + 1}"

    def token(self, user: str = "operator", *, expires_in: int | None = None, issuer: str | None = None,
              azp: str | None = None, typ: str = "Bearer", key: Any = None, kid: str | None = None) -> str:
        now = int(time.time())
        u = USERS[user]
        claims = {
            "exp": now + (self.token_ttl if expires_in is None else expires_in), "iat": now, "jti": str(uuid.uuid4()),
            "iss": issuer or self.issuer, "aud": "account", "sub": f"sub-{user}", "typ": typ,
            "azp": azp or self.client_id, "preferred_username": user, "name": u["name"],
            "realm_access": {"roles": u["roles"]}, "groups": u["groups"], "scope": "openid profile",
        }
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})

    def jwks(self) -> dict[str, Any]:
        self.jwks_calls += 1
        keys = []
        for kid, k in [(self.kid, self.key), *self.old_keys[-1:]]:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def discovery(self) -> dict[str, Any]:
        p = f"{self.issuer}/protocol/openid-connect"
        return {"issuer": self.issuer, "authorization_endpoint": f"{p}/auth", "token_endpoint": f"{p}/token",
                "end_session_endpoint": f"{p}/logout", "jwks_uri": f"{p}/certs"}

    # ------------------------------------------------------------------ pro httpx.MockTransport
    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/.well-known/openid-configuration"):
                return httpx.Response(200, json=self.discovery())
            if req.url.path.endswith("/certs"):
                return httpx.Response(200, json=self.jwks())
            return httpx.Response(404)
        return httpx.MockTransport(handle)

    # ------------------------------------------------------------------ jako HTTP server (prohlížeč)
    def _token_response(self, user: str, nonce: str | None) -> dict[str, Any]:
        rt = secrets.token_urlsafe(24)
        self.refresh[rt] = user
        id_claims = {"iss": self.issuer, "aud": self.client_id, "sub": f"sub-{user}", "exp": int(time.time()) + 300,
                     "iat": int(time.time()), "nonce": nonce, "preferred_username": user}
        return {"access_token": self.token(user), "token_type": "Bearer", "expires_in": self.token_ttl,
                "refresh_token": rt, "refresh_expires_in": 1800,
                "id_token": jwt.encode(id_claims, self.key, algorithm="RS256", headers={"kid": self.kid})}

    def serve(self, port: int) -> ThreadingHTTPServer:
        kc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def send(self, code: int, body: Any, ctype: str = "application/json") -> None:
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.cors()
                self.end_headers()

            def do_GET(self) -> None:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path.endswith("/.well-known/openid-configuration"):
                    return self.send(200, kc.discovery())
                if u.path.endswith("/certs"):
                    return self.send(200, kc.jwks())
                if u.path.endswith("/auth"):
                    if q.get("client_id") != kc.client_id or q.get("code_challenge_method") != "S256":
                        return self.send(400, "invalid_request", "text/plain")
                    if "user" not in q:  # přihlašovací stránka
                        links = "".join(f'<li><a href="?{urlencode({**q, "user": n})}">Přihlásit jako {n}</a></li>'
                                        for n in USERS)
                        return self.send(200, f"<html><body><h1>Keycloak (test)</h1><ul>{links}</ul></body></html>",
                                         "text/html; charset=utf-8")
                    code = secrets.token_urlsafe(16)
                    kc.codes[code] = {**q}
                    loc = f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q.get('state', '')})}"
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return None
                if u.path.endswith("/logout"):
                    self.send_response(302)
                    self.send_header("Location", q.get("post_logout_redirect_uri", "/"))
                    self.end_headers()
                    return None
                return self.send(404, {"error": "not found"})

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                f = {k: v[0] for k, v in parse_qs(body).items()}
                if not self.path.endswith("/token"):
                    return self.send(404, {"error": "not found"})
                if f.get("grant_type") == "authorization_code":
                    c = kc.codes.pop(f.get("code", ""), None)
                    if c is None or f.get("redirect_uri") != c["redirect_uri"]:
                        return self.send(400, {"error": "invalid_grant"})
                    digest = hashlib.sha256(f.get("code_verifier", "").encode()).digest()
                    if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != c["code_challenge"]:
                        return self.send(400, {"error": "invalid_grant", "error_description": "PKCE"})
                    return self.send(200, kc._token_response(c["user"], c.get("nonce")))
                if f.get("grant_type") == "refresh_token":
                    kc.refresh_grants += 1
                    user = kc.refresh.pop(f.get("refresh_token", ""), None)
                    if user is None:
                        return self.send(400, {"error": "invalid_grant"})
                    return self.send(200, kc._token_response(user, None))
                return self.send(400, {"error": "unsupported_grant_type"})

        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
