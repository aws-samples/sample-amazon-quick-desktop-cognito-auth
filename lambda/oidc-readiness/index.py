"""CloudFormation gate for public Cognito metadata, not Quick login readiness.

No credentials or tokens are sent. Create/update retries for at most 120 seconds;
Delete performs no endpoint checks. Uses the same source in the workshop template.
"""
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_WAIT_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 5
MAX_DOCUMENT_BYTES = 1024 * 1024


class NotReady(Exception):
    """The message is a fixed reason code, never an upstream response or URL."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(url, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise NotReady("metadata_too_large")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise NotReady("metadata_not_an_object")
        return document
    except NotReady:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise NotReady("metadata_unavailable") from None


def check_once(issuer, domain, deadline):
    def fetch(url):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NotReady("deadline_exceeded")
        return _json(url, min(REQUEST_TIMEOUT_SECONDS, remaining))

    discovery = fetch(issuer + "/.well-known/openid-configuration")
    expected = {
        "issuer": issuer,
        "jwks_uri": issuer + "/.well-known/jwks.json",
    }
    for name, value in expected.items():
        if discovery.get(name) != value:
            raise NotReady(name + "_mismatch")
    # A pool can advertise its custom domain while the proxy uses its prefix
    # domain. Different endpoint origins are valid; do not require same-origin.
    for name, path in (("authorization_endpoint", "/oauth2/authorize"),
                       ("token_endpoint", "/oauth2/token")):
        value = discovery.get(name)
        endpoint = urllib.parse.urlsplit(value) if isinstance(value, str) else None
        if not endpoint or endpoint.scheme != "https" or not endpoint.hostname or endpoint.path != path:
            raise NotReady(name + "_invalid")
    # Desktop uses the ID token at Quick; Cognito userInfo is not a readiness dependency.
    scopes = discovery.get("scopes_supported")
    if not isinstance(scopes, list) or not all(scope in scopes for scope in ("openid", "email", "profile")):
        raise NotReady("required_scopes_missing")
    # Fetch the expected URL, never an arbitrary URL supplied by a response.
    jwks = fetch(expected["jwks_uri"])
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not any(
        isinstance(key, dict)
        and key.get("kty") == "RSA"
        and key.get("use", "sig") == "sig"
        and key.get("alg", "RS256") == "RS256"
        and all(isinstance(key.get(field), str) and key[field] for field in ("kid", "n", "e"))
        for key in keys
    ):
        raise NotReady("signing_keys_missing")


def wait_until_ready(issuer, domain, budget):
    for value in (issuer, domain):
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise NotReady("invalid_endpoint_configuration")
    deadline = time.monotonic() + min(MAX_WAIT_SECONDS, budget)
    last_reason = "deadline_exceeded"
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            check_once(issuer, domain, deadline)
            return attempts
        except NotReady as error:
            last_reason = str(error)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(5, remaining))
    raise NotReady(last_reason)


def _send_response(event, context, status, reason, data):
    # The presigned ResponseURL is sensitive: never print it or exception text.
    payload = json.dumps({
        "Status": status,
        "Reason": reason,
        "PhysicalResourceId": event.get("PhysicalResourceId") or "oidc-readiness-" + hashlib.sha256((event.get("StackId", "") + event.get("LogicalResourceId", "")).encode()).hexdigest()[:16],
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data,
    }).encode()
    request = urllib.request.Request(event["ResponseURL"], data=payload, headers={"Content-Type": ""}, method="PUT")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=5):
                return
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise RuntimeError("cloudformation_response_failed") from None
            time.sleep(1)


def handler(event, context):
    status, reason, data = "SUCCESS", "Cognito metadata ready; Quick login must be tested separately", {}
    if event.get("RequestType") == "Delete":
        reason = "Readiness resource removed; no endpoint calls made"
    else:
        try:
            props = event["ResourceProperties"]
            remaining = context.get_remaining_time_in_millis() / 1000 - 25
            attempts = wait_until_ready(props["IssuerUrl"], props["CognitoDomain"], max(0, remaining))
            data = {"Status": "READY", "Attempts": attempts}
        except NotReady as error:
            status, reason = "FAILED", "Cognito readiness check failed: " + str(error)
        except Exception:
            status, reason = "FAILED", "Cognito readiness check failed: invalid_configuration_or_internal_error"
    print(json.dumps({"event": "oidc_readiness", "status": status, "reason": reason, **data}))
    _send_response(event, context, status, reason, data)
