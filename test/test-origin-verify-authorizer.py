#!/usr/bin/env python3
"""Local tests for the origin-verify Lambda authorizer — run before deploying.

    python3.12 test/test-origin-verify-authorizer.py

No AWS credentials and no network: the authorizer only compares a header against
an environment variable and returns an IAM policy.

(배포 전 로컬 테스트 — AWS 자격 증명 불필요)
"""

import importlib.util
import os
import sys
from pathlib import Path

HANDLER = (
    Path(__file__).resolve().parent.parent
    / "lambda"
    / "origin-verify-authorizer"
    / "index.py"
)
SECRET = "origin-verify-secret"
METHOD_ARN = (
    "arn:aws:execute-api:us-east-1:111122223333:abc123/prod/GET/oauth2/authorize"
)


def load(origin_verify_secret: str | None):
    """Imports the authorizer fresh — it reads the secret at call time, but the
    module-level import keeps the two tests independent either way."""
    if origin_verify_secret is None:
        os.environ.pop("ORIGIN_VERIFY_SECRET", None)
    else:
        os.environ["ORIGIN_VERIFY_SECRET"] = origin_verify_secret
    sys.modules.pop("authorizer", None)
    spec = importlib.util.spec_from_file_location("authorizer", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(headers=None, method_arn=METHOD_ARN):
    """A REQUEST-authorizer event, trimmed to the fields the handler reads."""
    return {
        "type": "REQUEST",
        "methodArn": method_arn,
        "headers": headers or {},
    }


def effect(response: dict) -> str:
    return response["policyDocument"]["Statement"][0]["Effect"]


def check(label: str, condition: bool) -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    return condition


def main() -> int:
    results = []
    auth = load(SECRET)

    response = auth.handler(event(headers={"x-origin-verify": SECRET}), None)
    results.append(check("matching header -> Allow", effect(response) == "Allow"))
    results.append(
        check(
            "policy is scoped to the invoked method and the invoke action",
            response["policyDocument"]["Statement"][0]["Resource"] == METHOD_ARN
            and response["policyDocument"]["Statement"][0]["Action"]
            == "execute-api:Invoke",
        )
    )
    results.append(check("principalId is set", response["principalId"] == "cloudfront"))

    response = auth.handler(event(headers={"X-Origin-Verify": SECRET}), None)
    results.append(
        check("header name matched case-insensitively", effect(response) == "Allow")
    )

    results.append(
        check("missing header -> Deny", effect(auth.handler(event(), None)) == "Deny")
    )
    results.append(
        check(
            "wrong header -> Deny",
            effect(auth.handler(event(headers={"x-origin-verify": "wrong"}), None))
            == "Deny",
        )
    )
    results.append(
        check(
            "header that is a prefix of the secret -> Deny",
            effect(auth.handler(event(headers={"x-origin-verify": SECRET[:-1]}), None))
            == "Deny",
        )
    )

    # A client controls this header, so it can hold any bytes. Comparing it as
    # `str` raises TypeError on anything non-ASCII, which API Gateway would
    # surface as a 500 instead of a clean 403.
    # (non-ASCII 헤더가 500이 아니라 Deny가 되는지 확인)
    results.append(
        check(
            "non-ASCII header -> Deny, not a crash",
            effect(auth.handler(event(headers={"x-origin-verify": "비ASCII-값"}), None))
            == "Deny",
        )
    )

    results.append(
        check(
            "headers explicitly null -> Deny",
            effect(auth.handler({"methodArn": METHOD_ARN, "headers": None}, None))
            == "Deny",
        )
    )

    # An empty secret must fail closed: otherwise a misconfigured deployment
    # would allow every request, including a request that sends no header at all.
    auth = load(None)
    results.append(
        check(
            "unset ORIGIN_VERIFY_SECRET -> Deny even with an empty header",
            effect(auth.handler(event(headers={"x-origin-verify": ""}), None)) == "Deny",
        )
    )
    results.append(
        check(
            "unset ORIGIN_VERIFY_SECRET -> Deny with no header",
            effect(auth.handler(event(), None)) == "Deny",
        )
    )

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
