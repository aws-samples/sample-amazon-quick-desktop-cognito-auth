"""API Gateway Lambda proxy for Amazon Quick on desktop OAuth requests.

Amazon Quick sends an `offline_access` scope on every OAuth request, but
Amazon Cognito does not support that scope and rejects the request. This proxy
sits in front of the Cognito hosted UI and strips `offline_access` from the
`scope` parameter before forwarding:

  - GET  /oauth2/authorize -> 302 redirect to the Cognito authorize endpoint
  - POST /oauth2/token     -> proxied (server-side) to the Cognito token endpoint

The token request is forwarded server-side (rather than redirected) so the
JSON token response is returned directly to the desktop client.

When the deployment puts CloudFront in front of the API (the default), the
distribution injects a secret `x-origin-verify` header on every origin request
and an API Gateway Lambda authorizer rejects anything that does not carry it —
so calling the API Gateway URL directly gets a 403 and this function is never
invoked. That check lives in `lambda/origin-verify-authorizer`, not here.
"""

import base64
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from enum import Enum, StrEnum


# BEGIN SAFE_DIAGNOSTICS -- kept identical in the CDK and workshop handlers.
import hashlib
import json
import re
import time


def _request_id(event):
    context = event.get("requestContext") or {}
    value = context.get("requestId") if isinstance(context, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+=/-]{1,128}", value) else None


def _response_headers(event, content_type="application/json"):
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if content_type:
        headers["Content-Type"] = content_type
    request_id = _request_id(event)
    if request_id:
        headers["X-Quick-Auth-Request-Id"] = request_id
    return headers


def _emit(event, context, stage, status, **fields):
    # Never pass request bodies/headers/URLs or exception messages to this helper.
    try:
        print(json.dumps({
            "event": "oidc_proxy", "stage": stage, "status": int(status),
            "api_request_id": _request_id(event),
            "lambda_request_id": getattr(context, "aws_request_id", None),
            **fields,
        }, separators=(",", ":")))
    except Exception:
        pass  # Observability must not change the OAuth response.


def _jwt_summary(token):
    if not isinstance(token, str) or len(token) > 65536:
        return {}, {}, None
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[-8:]
    try:
        header, payload, signature = token.split(".")
        decode = lambda value: json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        h, p = decode(header), decode(payload)
        if not isinstance(h, dict) or not isinstance(p, dict):
            return {}, {}, fingerprint
        return h, p, fingerprint
    except (ValueError, TypeError, UnicodeError):
        return {}, {}, fingerprint


def _token_diagnostics(body):
    # These are decoded claim checks, NOT signature verification. The OAuth
    # response is passed through unchanged. Log neither claim values nor tokens.
    try:
        tokens = json.loads(body)
        if not isinstance(tokens, dict):
            return {"contract_ok": False, "response_json_valid": False}
        header, identity, fingerprint = _jwt_summary(tokens.get("id_token"))
        _, access, _ = _jwt_summary(tokens.get("access_token"))
        issuer, client = os.environ.get("COGNITO_ISSUER"), os.environ.get("COGNITO_CLIENT_ID")
        scope = access.get("scope")
        scopes = scope.split() if isinstance(scope, str) else []
        kid = header.get("kid")
        if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,128}", kid):
            kid = None
        exp = identity.get("exp")
        expiry_valid = isinstance(exp, (int, float)) and not isinstance(exp, bool)
        checks = {
            "expected_config_present": bool(issuer and client),
            "id_claims_decoded": bool(identity), "access_claims_decoded": bool(access),
            "id_token_use_matches": identity.get("token_use") == "id",
            "access_token_use_matches": access.get("token_use") == "access",
            "id_issuer_matches": identity.get("iss") == issuer if issuer else None,
            "access_issuer_matches": access.get("iss") == issuer if issuer else None,
            "id_audience_matches": identity.get("aud") == client if client else None,
            "access_client_matches": access.get("client_id") == client if client else None,
            "access_openid": "openid" in scopes,
            "id_email_present": bool(identity.get("email")),
            "id_unexpired": exp > time.time() if expiry_valid else False,
        }
        return {
            **checks, "contract_ok": all(value is True for value in checks.values()),
            "claims_decoded_only": True, "id_token_fp": fingerprint, "id_token_kid": kid,
            "access_email_scope": "email" in scopes, "access_profile_scope": "profile" in scopes,
        }
    except Exception:
        return {"contract_ok": False, "response_json_valid": False}


def _upstream_request_id(response):
    headers = getattr(response, "headers", None) or {}
    for name, value in headers.items():
        if name.lower() == "x-amz-cognito-request-id" and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
            return value
    return None


def _oauth_error(body):
    try:
        error = json.loads(body).get("error")
        return error if error in {
            "invalid_request", "invalid_client", "invalid_grant", "invalid_scope",
            "unauthorized_client", "unsupported_grant_type", "server_error", "temporarily_unavailable",
        } else "upstream_error"
    except Exception:
        return "upstream_error"
# END SAFE_DIAGNOSTICS


class Route(StrEnum):
    AUTHORIZE = "/oauth2/authorize"
    TOKEN = "/oauth2/token"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"


class StatusCode(int, Enum):
    REDIRECT = 302
    NOT_FOUND = 404
    INTERNAL_ERROR = 500
    BAD_GATEWAY = 502


class ContentType(StrEnum):
    JSON = "application/json"
    FORM = "application/x-www-form-urlencoded"


# Scope that Amazon Quick sends but Cognito does not support.
UNSUPPORTED_SCOPE = "offline_access"
# OAuth parameter carrying the scope list, on both routes.
SCOPE_PARAM = "scope"


@dataclass
class ProxyResponse:
    statusCode: int
    body: str = ""
    headers: dict | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.headers is None:
            del result["headers"]
        return result


def _strip_unsupported_scope(scope_str: str) -> str:
    """Removes the unsupported scope from a space-delimited OAuth scope string.

    `+` separates too, not just whitespace: it is how the delimiter travels in a
    URL, so a scope list that reaches here still encoded (`openid+offline_access`)
    is filtered the same way as a decoded one.
    (URL 인코딩된 `+` 구분자도 공백과 동일하게 처리)
    """
    scopes = scope_str.replace("+", " ").split()
    return " ".join(s for s in scopes if s != UNSUPPORTED_SCOPE)


def _param_name(key: str) -> str:
    """Decodes a query parameter name so `sc%6fpe` is recognized as `scope`."""
    return urllib.parse.unquote_plus(key)


def _header(event: dict, name: str) -> str | None:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return value
    return None


class AuthorizeHandler:
    """Handles GET /oauth2/authorize by redirecting to Cognito with a cleaned scope."""

    def __init__(self, cognito_domain: str) -> None:
        self._cognito_domain = cognito_domain

    def handle(self, event: dict, context=None) -> ProxyResponse:
        params = event.get("queryStringParameters") or {}
        # Every parameter name is decoded before it is compared. API Gateway
        # normally hands over decoded names already, but comparing the raw key
        # would let `sc%6fpe=openid+offline_access` carry the unsupported scope
        # through untouched.
        cleaned = {}
        for key, value in params.items():
            name = _param_name(key)
            cleaned[name] = (
                _strip_unsupported_scope(value) if name == SCOPE_PARAM else value
            )
        qs = urllib.parse.urlencode(cleaned)
        scopes = (cleaned.get(SCOPE_PARAM) or "").split()
        _emit(event, context, "authorize", 302, openid_requested="openid" in scopes,
              email_requested="email" in scopes, profile_requested="profile" in scopes)
        return ProxyResponse(
            statusCode=StatusCode.REDIRECT,
            headers={**_response_headers(event, None), "Location": f"{self._cognito_domain}{Route.AUTHORIZE.value}?{qs}"},
        )


class TokenHandler:
    """Handles POST /oauth2/token by forwarding the request to Cognito server-side."""

    def __init__(self, cognito_domain: str) -> None:
        self._cognito_domain = cognito_domain

    def handle(self, event: dict, context=None) -> ProxyResponse:
        body = event.get("body", "") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()

        # Strip the unsupported scope from the token request body as well, in
        # case the client includes it on the token exchange.
        body = self._clean_body(body)

        headers = {"Content-Type": ContentType.FORM.value}
        # Forward the client's Authorization header (used by confidential
        # clients) if present. Header case varies by caller and by whether the
        # request came through CloudFront, so match case-insensitively.
        authorization = _header(event, "Authorization")
        if authorization:
            headers["Authorization"] = authorization

        req = urllib.request.Request(
            f"{self._cognito_domain}{Route.TOKEN.value}",
            data=body.encode(),
            headers=headers,
            method=HttpMethod.POST.value,
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = resp.read().decode()
                _emit(event, context, "token", resp.status,
                      cognito_request_id=_upstream_request_id(resp),
                      **(_token_diagnostics(result) if resp.status == 200 else {"oauth_error": _oauth_error(result)}))
                return ProxyResponse(statusCode=resp.status, headers=_response_headers(event), body=result)
        except urllib.error.HTTPError as error:
            result = error.read().decode()
            _emit(event, context, "token", error.code,
                  cognito_request_id=_upstream_request_id(error), oauth_error=_oauth_error(result))
            return ProxyResponse(statusCode=error.code, headers=_response_headers(event), body=result)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            _emit(event, context, "token", 502, error_type="upstream_unreachable",
                  network_error_type=type(getattr(error, "reason", error)).__name__)
            return ProxyResponse(statusCode=StatusCode.BAD_GATEWAY,
                                 headers=_response_headers(event), body='{"error": "upstream_unreachable"}')

    def _clean_body(self, body: str) -> str:
        """Removes the unsupported scope from a form-encoded token request body.

        The body is parsed rather than substring-tested for `scope=`, because
        `parse_qsl` decodes each parameter name before it is compared — a client
        sending `sc%6fpe=openid+offline_access` gets filtered too, where the
        substring test saw no `scope=` and passed the body straight through.

        Re-encoding happens only when something actually changed, so a body with
        no unsupported scope reaches Cognito exactly as the client sent it.
        (인코딩된 파라미터 이름도 파싱 후 비교하므로 우회할 수 없음)
        """
        pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
        cleaned = [
            (k, _strip_unsupported_scope(v) if k == SCOPE_PARAM else v)
            for k, v in pairs
        ]
        return urllib.parse.urlencode(cleaned) if cleaned != pairs else body


COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"]
_authorize_handler = AuthorizeHandler(COGNITO_DOMAIN)
_token_handler = TokenHandler(COGNITO_DOMAIN)


def handler(event: dict, context: object) -> dict:
    path = event.get("path", "")
    method = event.get("httpMethod", "")
    try:
        match (path, method):
            case (Route.AUTHORIZE, HttpMethod.GET):
                response = _authorize_handler.handle(event, context)
            case (Route.TOKEN, HttpMethod.POST):
                response = _token_handler.handle(event, context)
            case _:
                _emit(event, context, "routing", 404)
                response = ProxyResponse(statusCode=StatusCode.NOT_FOUND, body="Not found")
    except Exception as error:
        _emit(event, context, "proxy", 500, error_type=type(error).__name__)
        response = ProxyResponse(statusCode=StatusCode.INTERNAL_ERROR,
                                 headers=_response_headers(event), body='{"error": "internal_error"}')
    return response.to_dict()
