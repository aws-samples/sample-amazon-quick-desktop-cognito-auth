#!/usr/bin/env python3
"""Local tests for the auth-proxy Lambda — run before deploying.

    python3.12 test/test-auth-proxy.py

The origin-verify check is not tested here: it moved out of this Lambda and into
the API Gateway REQUEST authorizer, covered by test-origin-verify-authorizer.py.

Needs Python 3.12+ (the handler uses `match` and `StrEnum`, same as the Lambda
runtime) and no AWS credentials: the one outbound call is stubbed. Invoked as
`python3.12` rather than `python3`, which on macOS is still the 3.9 the OS ships.

(배포 전 로컬 테스트 — AWS 자격 증명 불필요)
"""

import importlib.util
import os
import sys
from pathlib import Path

HANDLER = Path(__file__).resolve().parent.parent / "lambda" / "auth-proxy" / "index.py"
COGNITO_DOMAIN = "https://example.auth.us-east-1.amazoncognito.com"


def load():
    """Imports the handler fresh, since it reads its config at import time."""
    os.environ["COGNITO_DOMAIN"] = COGNITO_DOMAIN
    sys.modules.pop("index", None)
    spec = importlib.util.spec_from_file_location("index", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(path="/oauth2/authorize", method="GET", headers=None, query=None, body=None):
    return {
        "path": path,
        "httpMethod": method,
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body,
    }


class _FakeResponse:
    """Stands in for the Cognito token endpoint response."""

    status = 200

    def read(self):
        return b'{"access_token": "stub"}'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    return condition


def main() -> int:
    results = []
    proxy = load()

    # --- Authorize: scope stripped, everything else forwarded -----------------
    # The origin-verify check is no longer here — it is the API Gateway Lambda
    # authorizer's job (test/test-origin-verify-authorizer.py), so this handler
    # only ever sees requests that already passed it.
    response = proxy.handler(
        event(query={"scope": "openid offline_access email", "client_id": "abc"}), None
    )
    location = response["headers"]["Location"]
    results.append(
        check(
            "authorize -> 302 with offline_access stripped",
            response["statusCode"] == 302 and "offline_access" not in location,
        )
    )
    results.append(
        check(
            "authorize -> other query parameters preserved",
            "client_id=abc" in location and "email" in location,
        )
    )

    response = proxy.handler(event(query=None), None)
    results.append(
        check("authorize without a scope parameter -> 302", response["statusCode"] == 302)
    )

    # A percent-encoded parameter name must not slip the scope past the filter.
    # (인코딩된 파라미터 이름으로 우회 불가)
    response = proxy.handler(
        event(query={"sc%6fpe": "openid+offline_access", "client_id": "abc"}), None
    )
    location = response["headers"]["Location"]
    results.append(
        check(
            "authorize -> offline_access stripped from an encoded scope key",
            response["statusCode"] == 302
            and "offline_access" not in location
            and "scope=openid" in location,
        )
    )

    response = proxy.handler(event(path="/nope"), None)
    results.append(check("unknown path -> 404", response["statusCode"] == 404))

    # --- Token endpoint: scope stripped, Authorization forwarded --------------
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        captured["headers"] = dict(request.headers)
        return _FakeResponse()

    proxy.urllib.request.urlopen = fake_urlopen
    response = proxy.handler(
        event(
            path="/oauth2/token",
            method="POST",
            headers={"AUTHORIZATION": "Basic stub"},
            body="grant_type=authorization_code&scope=openid+offline_access&code=1",
        ),
        None,
    )
    results.append(
        check(
            "token: forwarded to Cognito with offline_access stripped",
            response["statusCode"] == 200
            and captured["url"] == f"{COGNITO_DOMAIN}/oauth2/token"
            and "offline_access" not in captured["body"]
            and "grant_type=authorization_code" in captured["body"],
        )
    )
    results.append(
        check(
            "token: Authorization header forwarded regardless of case",
            captured["headers"].get("Authorization") == "Basic stub",
        )
    )

    # Same bypass attempt on the token body: the old substring test for `scope=`
    # saw nothing to clean and forwarded offline_access to Cognito.
    proxy.handler(
        event(
            path="/oauth2/token",
            method="POST",
            body="grant_type=authorization_code&sc%6fpe=openid+offline_access&code=1",
        ),
        None,
    )
    results.append(
        check(
            "token: offline_access stripped from an encoded scope key",
            "offline_access" not in captured["body"]
            and "scope=openid" in captured["body"],
        )
    )

    # Nothing to strip -> the body reaches Cognito exactly as the client sent it.
    body = "grant_type=refresh_token&refresh_token=r%2Ft&client_id=abc"
    proxy.handler(event(path="/oauth2/token", method="POST", body=body), None)
    results.append(
        check("token: a body with no scope is forwarded unchanged", captured["body"] == body)
    )

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
