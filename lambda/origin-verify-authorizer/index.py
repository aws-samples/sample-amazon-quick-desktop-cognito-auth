"""API Gateway REQUEST authorizer that proves a request came through CloudFront.

The CloudFront distribution in front of this API injects a secret
`x-origin-verify` header on every origin request. API Gateway calls this
authorizer before it routes anything, and the IAM policy returned here allows
the invocation only when that header matches `ORIGIN_VERIFY_SECRET` — so a
direct call to the API Gateway URL is rejected with a `403` and the auth-proxy
Lambda behind it is never invoked.

REQUEST type, not TOKEN: a TOKEN authorizer only ever reads a bearer token out
of one named header and validates it against a regex, which is not what a
shared-secret header check is.

The authorizer declares **no identity source**: identity sources are the cache
key for a REQUEST authorizer, and result caching is off (see the construct). With
caching off API Gateway passes every request to this function, so the
missing-header case is handled below rather than assumed away, and rejections
stay a uniform `403`. Naming the header *and* turning caching on would instead
have API Gateway answer a request without it `401` without invoking this
function.

(CloudFront가 주입하는 비밀 헤더를 검사하는 API Gateway 권한 부여자)
"""

import hmac
import os

# Header CloudFront injects on origin requests to prove the request came through
# the distribution and not straight at the API Gateway URL.
ORIGIN_VERIFY_HEADER = "x-origin-verify"


def _header(event: dict, name: str) -> str | None:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return value
    return None


def handler(event: dict, _context: object) -> dict:
    expected = os.environ.get("ORIGIN_VERIFY_SECRET", "")
    supplied = _header(event, ORIGIN_VERIFY_HEADER) or ""
    # Constant-time compare so the secret cannot be recovered byte by byte.
    # Both sides are encoded first: `compare_digest` on `str` raises TypeError
    # for any non-ASCII character, and a client controls the supplied value — so
    # comparing the strings directly turns a bad header into a 500 rather than a
    # clean Deny. An unset secret denies rather than allowing everything.
    # (str 비교는 non-ASCII 헤더에서 TypeError가 발생하므로 bytes로 비교)
    allowed = bool(expected) and hmac.compare_digest(
        expected.encode(), supplied.encode()
    )
    effect = "Allow" if allowed else "Deny"
    print(f"{effect}: {event.get('methodArn', '')}")
    return {
        "principalId": "cloudfront",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    # Scoped to the invoked method. Safe because the authorizer
                    # result is not cached (see the construct): a policy cached
                    # under one method ARN would deny the other route.
                    "Resource": event.get("methodArn", "*"),
                }
            ],
        },
    }
