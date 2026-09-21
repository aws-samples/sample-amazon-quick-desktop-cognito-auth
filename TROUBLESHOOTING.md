# Quick Desktop sign-in: customer troubleshooting

The new readiness output and structured proxy fields are available only after
this version is deployed. Their absence on an older stack is not a failed check;
use its existing API Gateway access logs and Desktop logs until it is upgraded.

Treat infrastructure readiness and Quick sign-in acceptance as two separate checks.
`CREATE_COMPLETE` and the `CognitoReadiness=READY` output mean Cognito's discovery
and signing-key metadata passed the deployment check. They do not mean Quick's
separately created extension access has accepted a user's token.

The stack now checks public Cognito discovery, issuer/JWKS consistency, required
scope metadata, and the presence of RSA signing-key material before publishing the
proxy. It retries metadata checks for at most 120 seconds, fails the custom resource
with a reason code if they do not pass, and makes no endpoint checks on deletion.
The check runs on creation and when its properties change. `CheckRevision` must be
bumped when the probe implementation changes and should run again on existing stacks.
The timeout is a deployment budget, not a Quick propagation SLA.

The check permits a Cognito custom domain in discovery even when the proxy uses
the Cognito prefix domain. Different origins for issuer, hosted login and proxy
endpoints are intentional in this design.

## Before handing the deployment to a customer

1. Wait for stack completion and check `CognitoReadiness=READY`.
2. Copy **all five fields from the same stack** into Quick's extension access:
   `IssuerUrl`, `ClientId`, `AuthEndpoint`, `TokenEndpoint`, and `JwksUri`.
   Keep the Cognito issuer/JWKS and the proxy auth/token endpoints as published.
3. Create and activate the separate Quick desktop extension. Confirm the customer
   user is active and their IdP email exactly matches their Quick email, including
   case. Extension access and the extension itself are separate resources.
4. Complete one fresh Desktop sign-in, explicitly selecting the intended Quick
   account. Successful token exchange alone is not acceptance: confirm the
   Desktop finishes loading the signed-in profile. Only then is setup verified.

## Identify the failing stage

| Evidence | Investigate next |
|---|---|
| Authorization never reaches the proxy | CloudFront/WAF, the origin header authorizer, and API Gateway access logs. Check the actual destination and HTTP status. |
| Proxy `stage=token`, status 400, `oauth_error=invalid_grant` | Authorization-code reuse/expiry, callback URI, PKCE and client configuration. Keep the original error code and Cognito request ID. |
| Proxy `stage=token`, status 502 | Connectivity from Lambda to Cognito, DNS/TLS/timeouts, and Lambda execution errors. |
| Token status 200 but `contract_ok=false` | Inspect the individual claim-check booleans. Confirm the expected issuer/client environment, requested `openid`, ID-token email presence and token expiry. Do not change or swap token fields. |
| Token status 200 and claim checks pass, then Desktop reports enterprise user-info 401 | Check Quick's saved trust configuration (especially JWKS), chosen account/tenant, user matching, extension activation and Quick's validation response. The status alone does not establish a CloudFormation defect or a propagation delay. |

Distinguish a Cognito OAuth error from Quick's post-token identity validation.
Record which service returned the status before changing endpoints or credentials.
Cognito's OIDC `GET /oauth2/userInfo` takes an **access token**. A successful
Cognito call alone does not prove that Quick has accepted the user's identity.
Do not swap token fields to address a later service error.

## Correlate customer logs without collecting tokens

Ask for the Desktop version, UTC failure time, selected Quick account, displayed error, and the
Desktop `token_fp` value if present in the exported application logs. The proxy's
`id_token_fp` uses the same convention: the last eight hex characters of SHA-256.
Use it together with the timestamp and request IDs; it is not a unique user ID.
No raw token needs to leave the customer's environment.

Get the diagnostics output values with the customer's normal read-only AWS
profile. These commands return only diagnostic outputs:

```sh
AUTH_REGION=us-east-1
AUTH_STACK=QuickDesktopCognitoIdcStack
aws cloudformation describe-stacks --region "$AUTH_REGION" --stack-name "$AUTH_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoReadiness' || OutputKey=='ProxyLogGroup' || OutputKey=='AccessLogGroup']" \
  --output table
AUTH_PROXY_LOG=$(aws cloudformation describe-stacks --region "$AUTH_REGION" --stack-name "$AUTH_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ProxyLogGroup'].OutputValue | [0]" --output text)
AUTH_SINCE_MS=$(python3 -c 'import time; print(int((time.time() - 1800) * 1000))')
aws logs filter-log-events --region "$AUTH_REGION" --log-group-name "$AUTH_PROXY_LOG" \
  --start-time "$AUTH_SINCE_MS" \
  --filter-pattern '{ $.id_token_fp = "REPLACE_WITH_DESKTOP_FINGERPRINT" }' \
  --query 'events[].{Timestamp:timestamp,Message:message}' --output json
```

The example searches the last 30 minutes. Adjust the window for an older incident.
If there is no matching token record, inspect the `AccessLogGroup` output for the
same time window; a request rejected by the API authorizer never invokes the proxy.

The structured proxy record contains the API and Lambda request IDs, Cognito
request ID, stage/status, ID-token fingerprint/key ID, and boolean claim checks.
It does **not** log request bodies/authorization headers, tokens, email/subject
values, authorization codes, state, nonce or PKCE verifier/challenge values.
A decoded claim check is **not cryptographic signature verification**;
`claims_decoded_only=true` makes that distinction explicit.

For Logs Insights, select `ProxyLogGroup` and a narrow time window:

```text
fields @timestamp, stage, status, api_request_id, lambda_request_id,
       cognito_request_id, id_token_fp, id_token_kid, contract_ok,
       id_issuer_matches, id_audience_matches, access_issuer_matches,
       access_client_matches, access_openid, id_email_present, id_unexpired
| filter event = "oidc_proxy"
| sort @timestamp asc
```

OAuth responses include `Cache-Control: no-store` and `Pragma: no-cache`.
`X-Quick-Auth-Request-Id` identifies the API Gateway request when it is available.
The token response body remains unchanged; diagnostics do not decide whether a
user is authorized and do not rewrite claims or scopes.

## Recover and escalate with evidence

For a failure immediately after creating Quick extension access, verify the five
saved fields and account selection first. A fresh sign-in after a short delay is
a reasonable check, but no fixed delay is documented here as a guaranteed fix.
If the failure recurs, capture the correlation information before changing anything.
Do not automatically retry every 401 indefinitely or report deployment success as
proof that Quick login is ready.

For escalation, include account/region, UTC timestamps, configured public OIDC
URLs/client ID, deployment revision, proxy/Cognito request IDs, token fingerprint
and signing `kid`, and the Quick response request ID if the client provides one.
Keep the IDs labeled: an API Gateway token-exchange ID is not a Quick user-info
request ID. State explicitly when the failing Quick request ID is unavailable.
Do not ask customers to send passwords, JWTs, authorization headers or an
unredacted HAR. A passing probe later is evidence of recovery, not proof of the
original root cause.

Avoid deleting/recreating the user pool or redeploying blindly as a first response:
it can change issuer/client IDs, introduce another trust-registration transition,
and destroy the state needed to investigate the original failure.

Reference documentation:

- `https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-setup.html`
- `https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-setup-troubleshooting.html`
- `https://docs.aws.amazon.com/cognito/latest/developerguide/userinfo-endpoint.html`
- `https://www.rfc-editor.org/rfc/rfc6749#section-5.1`

## IAM Identity Center provisioning

Provisioning runs asynchronously on deployment and every five minutes by default.
CloudFormation completion does not mean invitations or all queued users are ready.
Check `QuickDesktopIdcReconciler` logs for `scan_queued` and `QuickDesktopIdcSync`
logs for `created`, `member_reconciled`, `disabled`, or `failed`.

- Check the configured `IdentityStoreId` and `IdcGroupId` outputs first, especially
  when multiple Identity Center instances are visible.
- Watch `IdcWorkQueueUrl` for oldest-message age and growing backlog. Failed work
  is redriven to `IdcWorkDlqUrl`; EventBridge delivery and asynchronous coordinator
  failures go to `IdcDeliveryDlqUrl`. Configure alarms for these queues.
- A user with no primary email, an email alias already owned by another Cognito
  account, or a username without the sync ownership marker requires administrator
  review. The worker does not adopt manual accounts solely by matching an email.
- Fix a managed user's email in Identity Center. Reconciliation copies it to
  Cognito. A failed email migration leaves the managed Cognito account disabled.
- Remove the user from the watched group to revoke provisioning. Manually
  disabling a managed Cognito user while leaving them in that group is temporary:
  reconciliation can re-enable them. Disabling an Identity Center sign-in alone
  is not a supported revocation signal in this sample.
- Investigate the source error before redriving messages. Queue messages contain
  directory identifiers; restrict access and do not include them in public issues.

For urgent access removal, remove group membership and disable the Cognito user,
then follow Amazon Quick's session/access administration process. Already issued
tokens and Quick sessions have their own lifetimes; this sample does not promise
instantaneous revocation of existing sessions.
