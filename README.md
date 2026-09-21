# Amazon Quick Desktop authentication with Amazon Cognito

This AWS CDK sample deploys Amazon Cognito as an OIDC provider for Amazon Quick
Desktop. By default, it provisions Cognito users from an IAM Identity Center
group (`useIdc=true`). Use `useIdc=false` to manage Cognito users yourself.
**Neither mode creates a demonstration user or a placeholder customer email.**

This sample is intended for learning and evaluation in non-production environments.
Before adapting it for production, perform your own security review, operational
hardening, and end-to-end validation.

Identity Center mode is **user provisioning, not federated sign-in**. Users set a
separate Cognito password and authenticator-app MFA. Identity Center passwords,
MFA settings and account-disabled state are not copied into Cognito. Read
[Access removal](#access-removal) before deploying.

## Architecture

```text
Identity Center group ── CloudTrail events ── EventBridge ─┐
                                                        ▼
Initial import + periodic schedule ────────────── Reconciler Lambda
                                                        │
                                                        ▼
                                                  SQS FIFO queue
                                                        │
                                                        ▼
                                              Provisioning Lambda
                                                        │
                                                        ▼
                                                   Cognito users

Quick Desktop ── CloudFront + WAF ── API Gateway ── OAuth proxy Lambda
                     │                   ▲                   │
                     └─ x-origin-verify ─┘                   ▼
                                                  Cognito OAuth endpoints
```

The OAuth proxy removes only `offline_access` from the scope on authorization
and token requests. Cognito does not accept this scope. The remaining OAuth
parameters, including PKCE parameters, are forwarded; token response bodies are
returned unchanged. Authorization requests redirect to Cognito's hosted login.

`IssuerUrl` and `JwksUri` refer to Cognito. `AuthEndpoint` and `TokenEndpoint`
refer to CloudFront, or API Gateway when CloudFront is disabled. These different
origins are intentional. Configure all five values from the same deployment.

The app creates two stacks by default:

| Stack | Region | Purpose |
|---|---|---|
| `QuickDesktopCognitoIdcStack` | Deploy region | Cognito, OAuth proxy, CloudFront, optional Identity Center provisioning |
| `QuickDesktopCloudFrontWafStack` | `us-east-1` | CloudFront-scoped WAF web ACL |

Use `--all` when deploying or destroying. With `enableWaf=false`, the WAF stack
is omitted. Deploy into the Identity Center instance's primary region when
using provisioning. This sample targets the standard AWS commercial partition.

## Prerequisites

- An Amazon Quick subscription, an active Quick user, and permissions to configure
  desktop extension access and activate the desktop extension.
- Node.js 22 or later, Python 3.12 for local tests, AWS CLI credentials, and CDK
  bootstrap resources in the deployment region and `us-east-1` when using WAF.
- For the default mode, an accessible IAM Identity Center instance and a group
  of intended users whose email addresses match their Amazon Quick identities.
  The deployment creates the named group if it does not exist. With multiple
  visible instances, set `idcInstanceArn` explicitly.
- Deployment permissions for the resources in the synthesized templates,
  including IAM roles and custom resources. Runtime roles are separate.

In a shared account, check the region's API Gateway CloudWatch logging role.
The default `apiGatewayAccountRole=true` creates an account-wide setting that
can affect other REST APIs. Set it to `false` when the region already has a
suitable role; ensure that role exists before enabling API access logging.

## Install, test and deploy

```bash
git clone https://github.com/aws-samples/sample-amazon-quick-desktop-cognito-auth.git
cd sample-amazon-quick-desktop-cognito-auth
npm ci
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
npm test
npm run build

npx cdk bootstrap aws://ACCOUNT_ID/IDC_REGION aws://ACCOUNT_ID/us-east-1
npx cdk synth --all -c idcGroupName=QuickDesktopUsers
npx cdk deploy --all -c idcGroupName=QuickDesktopUsers
```

Use your actual account and region in the bootstrap command, specifying the
environment only once if both regions are `us-east-1`. Review the synthesized
IAM permissions and the CDK change set before deployment. CloudFront deployment
can take several minutes.

For Cognito-only deployment:

```bash
npx cdk deploy --all -c useIdc=false
```

This omits Identity Center functions, queues, event rules, resolver and
CloudTrail resources. It retains the same OAuth proxy and five-field Quick
configuration. It does not create the workshop template's example user.

Create users in the Cognito console with a non-email username and their actual
Quick email in the Email field. This pool uses email aliases, so email-format
usernames are not accepted. Confirm ownership before marking an email verified,
and send an invitation so the user can replace their temporary password.

## Context parameters

Pass options with `-c name=value`. Persist your selected options for subsequent
synth, deploy and destroy operations.

| Parameter | Default | Meaning |
|---|---|---|
| `useIdc` | `true` | Enable Identity Center group provisioning |
| `idcGroupName` | Required with IDC | Group display name to resolve or create |
| `idcInstanceArn` | Auto-select only one visible instance | Select the intended instance explicitly |
| `reconcileIntervalMinutes` | `5` | Snapshot interval, 1–1440 minutes |
| `onRemove` | `disable` | Explicit removal events: `disable` or `delete`; periodic scans always disable |
| `mfaRequired` | `true` | Require Cognito TOTP MFA; `false` makes it optional |
| `retain` | `true` | Retain the user pool and CloudTrail log bucket when deleting the stack |
| `createTrail` | `true` | Create a regional management-event trail; ignored without IDC |
| `enableCloudFront` | `true` | Publish CloudFront URLs; `false` publishes API Gateway URLs |
| `enableWaf` | `true` | Attach the CloudFront web ACL; ignored without CloudFront |
| `allowedCidrs` | Unrestricted | JSON array of IPv4/IPv6 client CIDRs |
| `wafRateLimit` | `100` | Requests per five minutes per IP, 10–2,000,000,000 |
| `apiRateLimit` | `10` | API Gateway steady-state requests per second |
| `apiBurstLimit` | `20` | API Gateway burst allowance |
| `accessLogging` | `true` | JSON API Gateway access logs |
| `apiGatewayAccountRole` | `true` | Create the region-wide API Gateway CloudWatch logging role |
| `originVerifySecret` | Generated secret | Override the CloudFront origin header value; protect deployment metadata |

Booleans accept `true`/`false`, `yes`/`no`, `on`/`off`, and `1`/`0` without regard
to case. Invalid values fail synthesis. The CIDR allowlist is enforced by WAF
with CloudFront, or by an API Gateway resource policy without CloudFront. A
CloudFront configuration with an allowlist and WAF disabled is rejected because
it would leave the allowlist unenforced. WAF does not accept `/0` CIDRs.

For example, restrict access to your actual corporate NAT or VPN egress ranges:

```bash
npx cdk deploy --all -c idcGroupName=QuickDesktopUsers \
  -c allowedCidrs='["203.0.113.0/24","2001:db8::/32"]'
```

The ranges above are documentation examples; replace them before deploying.
Increase the WAF limit as appropriate when users share a NAT address.

If an existing trail already captures management events in the deployment
region, set `createTrail=false` to avoid duplicate logging. CloudTrail enables
the event fast paths; the periodic snapshot works independently of those
paths and also catches changes that arrive through other API/SCIM workflows.

## Configure and verify Amazon Quick

| Quick OIDC field | Stack output |
|---|---|
| Client ID / audience | `ClientId` |
| Issuer URL | `IssuerUrl` |
| Authorization endpoint | `AuthEndpoint` |
| Token endpoint | `TokenEndpoint` |
| JWKS URI | `JwksUri` |

Follow the [Amazon Quick Desktop enterprise setup guide](https://docs.aws.amazon.com/quick/latest/userguide/desktop-enterprise-setup.html).
Create extension access and activate the separate desktop extension. Use the
correct Quick account and ensure the Cognito email matches the active Quick
user, including case.

The `CognitoReadiness=READY` output means the public discovery and signing-key
metadata passed the deployment probe. It does not certify Quick's trust
registration or user acceptance. The probe retries for up to 120 seconds; this
is not a documented Quick propagation delay or service-level guarantee.

Verify a fresh Desktop sign-in through to the loaded user profile. For IDC
mode, also verify initial member provisioning, invitation delivery, subsequent
membership additions and removals. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
for safe correlation of failures using request IDs and token fingerprints.

Other outputs include `PoolId`, `ProxyLogGroup`, `AccessLogGroup`,
`ApiGatewayUrl`, `DistributionDomainName`, and, with IDC, `IdentityStoreId`,
`IdcGroupId`, `IdcWorkQueueUrl`, `IdcWorkDlqUrl`, and `IdcDeliveryDlqUrl`.

## Identity Center provisioning

Deployment queues an initial asynchronous import of existing group members.
Every five minutes by default, the coordinator compares current group members
with Cognito users marked as managed by this sample. Directory events provide
faster handling of supported membership and user-delete events. The periodic
scan covers changes that do not have a usable event shape, including membership
removal events containing only a membership ID.

Each user is queued separately in an encrypted FIFO queue. The worker checks
current group membership before changing Cognito, so delayed add/remove events
do not override current membership. It uses the stable Identity Center user ID
as the Cognito username and stores `custom:idc_user_id` as an ownership marker.
It does not locate users for removal by email or adopt manual accounts solely
because their emails match.

The worker copies the current primary Identity Center email, marks it verified
as an administrator operation, and sends a Cognito invitation for a new user.
Administrators of the source directory therefore control the provisioned email
identity. Correct a managed user's email in Identity Center, not only in Cognito;
reconciliation will restore the directory value. An email change disables the
managed Cognito account first and re-enables it only after a successful update.

Cognito's built-in email delivery has limits. Configure Amazon SES and validate
invitation/recovery delivery for your expected user volume. The initial import
can send invitations to every previously unprovisioned group member.

Provisioning is asynchronous. Queue backlog, service throttling and the scan
budget can delay convergence. This sample is not load-tested for large
directories; validate expected group size and monitor oldest-message age and
both dead-letter queues before relying on it for access lifecycle operations.

### Access removal

The watched group is authoritative: remove a user from it to revoke provisioning.
Reconciliation disables managed users absent from the group. Explicit supported
removal events can delete users if `onRemove=delete` is selected; scheduled
scans always disable them to avoid destructive actions based only on snapshots.

A manually disabled Cognito user still in the source group may be re-enabled by
reconciliation. Disabling Identity Center sign-in without removing membership
is not a supported revocation signal. Existing Cognito tokens and Quick sessions
have their own lifetimes; disabling a Cognito account does not promise immediate
termination of an established Quick session. For urgent revocation, remove group
membership, disable the Cognito user, and use Quick's access/session controls.

### Updating an existing deployment

Defaults now require TOTP MFA, retain the user pool/log bucket on stack deletion,
and disable users on removal. Review the effects on existing users before
upgrading; set explicit context options if your approved configuration differs.
Existing members are imported automatically, and managed emails are reconciled.

Switching to `useIdc=false` removes provisioning automation but leaves Cognito
users in place. These users become manually managed; membership changes no
longer revoke their access. Switching back may send invitations to previously
unprovisioned members. Manual Cognito users are not automatically adopted.
Review the CDK diff and verify the selected instance/group on each change.

## Security and operations

- CloudFront caching is disabled for OAuth traffic. Proxy responses also carry
  `Cache-Control: no-store` and `Pragma: no-cache`.
- The API Gateway REQUEST authorizer checks the `x-origin-verify` value injected
  by CloudFront, with authorizer caching disabled. It gates origin traffic,
  not individual users. Cognito authenticates users. Protect permissions that
  reveal or modify stack metadata, distribution headers and Lambda settings.
- Secrets Manager generates the origin header credential by default. CloudFormation
  resolves the same secret reference into CloudFront and the authorizer environment.
  Protect access to the secret, distribution configuration and Lambda configuration.
  If rotating it, update both consumers together using a new secret reference or
  version; changing the stored secret alone does not update deployed values.
- The API Gateway origin remains reachable and rejects requests without the
  header. Rejected calls still incur API Gateway and authorizer charges. WAF
  uses a rate rule and managed rules; the API stage has an additional throttle.
  OAuth-compatible Count overrides cover query-string size, missing User-Agent,
  RFI and metadata-SSRF signatures triggered by native loopback callbacks. The
  proxy contacts only the configured Cognito origin and never fetches a supplied
  redirect URI. Cognito enforces its registered callback allowlist.
- The public Cognito app client has no secret and requests `openid`, `email`
  and `profile`. Its attribute permissions exclude the custom ownership marker.
  Email changes require verification; administrator provisioning can set
  verified email. Control administrator permissions in both directories.
- Cognito self-sign-up is disabled and authenticator-app MFA is required by
  default. Review password, session, threat-protection and recovery policies
  against your organization's requirements.
- Runtime Cognito writes are scoped to this pool and SQS operations to their
  queues. Identity Store and instance-discovery permissions use wildcard
  resources with a limited action list; handlers validate the configured store
  and group. Review those grants for your organization's account structure.
- Proxy diagnostics log stage/status, request IDs, token fingerprints and boolean
  claim checks, not raw tokens, passwords, email addresses or OAuth parameters.
  Decoded claims are diagnostic only, not signature validation. API access logs
  contain IP addresses and user agents; provisioning logs/queues contain directory
  IDs. Apply appropriate log access and retention policies.
- Configure alarms for Lambda errors, queue age/dead letters, API errors and WAF
  blocks. WAF full request logging is not configured by this sample.
- WAF, CloudFront, Lambda, API Gateway, Secrets Manager, queues, Cognito, CloudTrail and logs may
  incur charges. Review service pricing for your traffic and user population.

## Local validation

`npm test` exercises scope filtering, token forwarding, origin checks, metadata
readiness, safe diagnostics, directory synchronization with AWS SDK stubbed
responses, and synthesized infrastructure assertions. Tests use dummy credentials
and make no AWS calls. `npm run build` compiles TypeScript.

Synthesize both deployment modes and run `cfn-lint` against the resulting
CloudFormation templates. Successful local tests/synthesis do not replace a real
customer-account deployment and end-to-end Quick/IDC acceptance test.

## Cleanup

Use the same context options as deployment when running `npx cdk destroy --all`.
The default retains the Cognito user pool and CloudTrail log bucket, and the
IDC resolver leaves the source group in place. Retained fixed-name resources
can conflict with a subsequent fresh deployment; review and explicitly clean
up or import retained resources before redeploying.

Remove Quick extension access and the desktop extension separately when they
are no longer needed. Retained log groups and the account-wide API Gateway
logging role may remain; check whether other workloads depend on the role
before changing it. CloudFront deletion can take several minutes.

## License

This sample is licensed under MIT No Attribution (MIT-0). See [LICENSE](LICENSE).
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) for contribution
and vulnerability-reporting guidance.
