#!/usr/bin/env node
import 'source-map-support/register';
import { App, Tags } from 'aws-cdk-lib';
import { QuickDesktopAuthStack } from './stacks/quick-desktop-auth-stack';
import {
  CLOUDFRONT_WAF_REGION,
  CloudFrontWafStack,
} from './stacks/cloudfront-waf-stack';

import { parseAllowedCidrs, validateNetworkOptions } from './config-validation';

const app = new App();

// --- Context parameters -----------------------------------------------------
// useIdc       (optional): "false" to deploy Cognito + the API Gateway proxy
//              only, skipping all IAM Identity Center automation (default true).
// idcGroupName (required when useIdc=true): name of the IAM Identity Center
//              group to watch.
// allowedCidrs (optional): JSON array of client CIDRs allowed to reach the
//              proxy — enforced on the CloudFront web ACL, or on the API
//              Gateway resource policy when enableCloudFront=false.
// mfaRequired  (optional): "true" to enforce TOTP MFA on the Cognito pool.
// retain       (optional): "true" to retain the User Pool on stack delete.
// createTrail  (optional): "false" to skip creating a CloudTrail trail (default
//              true) — set false if the account already has a management-event
//              trail in this region. Ignored when useIdc=false.
// enableCloudFront (optional): "false" to publish the API Gateway URL directly
//              instead of fronting it with CloudFront (default true).
// enableWaf    (optional): "false" to skip the CloudFront web ACL (default
//              true). Ignored when enableCloudFront=false.
// wafRateLimit (optional): requests per 5 minutes per client IP before the WAF
//              rate rule blocks (default 100).
// originVerifySecret (optional): override the secret CloudFront sends as
//              `x-origin-verify` (default: a generated Secrets Manager value).
// accessLogging (optional): "false" to skip API Gateway access logs (default
//              true).
// apiGatewayAccountRole (optional): "false" to leave the account- and
//              region-wide API Gateway CloudWatch Logs role alone (default
//              true). Set false in a shared account that already has one.
// apiRateLimit / apiBurstLimit (optional): API Gateway stage throttle, in
//              requests/second and burst (defaults 10 and 20).
// `-c` values always arrive as strings, so accept the usual spellings of
// true/false in any case ("True", "FALSE", "no", "off", …) rather than silently
// treating an unrecognised value as the default. cdk.json entries arrive as real
// booleans and pass through unchanged.
const FALSEY = ['false', '0', 'no', 'off'];
const TRUTHY = ['true', '1', 'yes', 'on'];
const boolContext = (name: string, fallback: boolean): boolean => {
  const raw = app.node.tryGetContext(name);
  if (raw === undefined || raw === null || raw === '') return fallback;
  const value = String(raw).trim().toLowerCase();
  if (FALSEY.includes(value)) return false;
  if (TRUTHY.includes(value)) return true;
  throw new Error(
    `Context parameter '${name}' must be a boolean; got '${raw}'. ` +
      `Use one of: ${[...TRUTHY, ...FALSEY].join(', ')}`,
  );
};

// Same reasoning for numbers: reject a value that is not a usable integer at
// synth time, rather than letting it surface as a CREATE_FAILED mid-deploy.
const intContext = (name: string, min: number): number | undefined => {
  const raw = app.node.tryGetContext(name);
  if (raw === undefined || raw === null || raw === '') return undefined;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min) {
    throw new Error(
      `Context parameter '${name}' must be an integer of at least ${min}; got '${raw}'.`,
    );
  }
  return value;
};

const idcGroupName = app.node.tryGetContext('idcGroupName') as string | undefined;
if (idcGroupName !== undefined && (typeof idcGroupName !== 'string' || !idcGroupName.trim())) {
  throw new Error('idcGroupName must be a non-empty string.');
}
// IDC provisioning is the default; Cognito-only deployment is an explicit opt-out.
const useIdc = boolContext('useIdc', true);
const allowedCidrs = parseAllowedCidrs(app.node.tryGetContext('allowedCidrs'));
const mfaRequired = boolContext('mfaRequired', true);
const retain = boolContext('retain', true);
// Validate at synth time — an unknown value would otherwise only surface as a
// no-op in the sync Lambda, long after deploy.
const onRemoveRaw = app.node.tryGetContext('onRemove') as string | undefined;
const VALID_ON_REMOVE = ['delete', 'disable'];
if (onRemoveRaw && !VALID_ON_REMOVE.includes(onRemoveRaw)) {
  throw new Error(
    `Invalid onRemove value '${onRemoveRaw}'. ` +
      `Must be one of: ${VALID_ON_REMOVE.join(', ')}`,
  );
}
const onRemove = onRemoveRaw ?? 'disable';
const createTrail = boolContext('createTrail', true);
const enableCloudFront = boolContext('enableCloudFront', true);
const enableWaf = enableCloudFront && boolContext('enableWaf', true);
const originVerifySecret = app.node.tryGetContext('originVerifySecret') as
  | string
  | undefined;
if (boolContext('strictOriginPolicy', false)) {
  throw new Error('strictOriginPolicy is no longer supported. CloudFront origin access is enforced by the Lambda authorizer.');
}
validateNetworkOptions({ enableCloudFront, enableWaf, allowedCidrs });
const idcInstanceArn = app.node.tryGetContext('idcInstanceArn') as string | undefined;
if (idcInstanceArn !== undefined && (typeof idcInstanceArn !== 'string' || !/^arn:[a-z0-9-]+:sso:::instance\/(sso)?ins-[A-Za-z0-9-]+$/.test(idcInstanceArn))) {
  throw new Error('idcInstanceArn must be an IAM Identity Center instance ARN.');
}
const reconcileIntervalMinutes = intContext('reconcileIntervalMinutes', 1) ?? 5;
if (reconcileIntervalMinutes > 1440) throw new Error('reconcileIntervalMinutes must be at most 1440.');
const accessLogging = boolContext('accessLogging', true);
const apiGatewayAccountRole = boolContext('apiGatewayAccountRole', true);
// WAFv2 rejects a rate-based limit below 10.
const wafRateLimit = intContext('wafRateLimit', 10);
if (wafRateLimit !== undefined && wafRateLimit > 2000000000) throw new Error('wafRateLimit must not exceed 2000000000.');
const apiRateLimit = intContext('apiRateLimit', 1);
const apiBurstLimit = intContext('apiBurstLimit', 1);

// Only the IDC-backed deployment needs a group to watch.
if (useIdc && !idcGroupName) {
  throw new Error(
    "Context parameter 'idcGroupName' is required when useIdc=true. " +
      'Deploy with: cdk deploy -c idcGroupName=QuickDesktopUsers, ' +
      'or deploy Cognito only with: cdk deploy -c useIdc=false',
  );
}

// Deploy into the region IAM Identity Center emits its events in — which is the
// account/region resolved from the ambient CLI credentials/profile.
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

// A CLOUDFRONT-scoped web ACL only exists in us-east-1, so it needs its own
// stack there and the auth stack reads its ARN across regions. Cross-stack (and
// especially cross-region) references need a concrete account and region — an
// environment-agnostic synth cannot produce them.
if (enableWaf && (!env.account || !env.region)) {
  throw new Error(
    'Deploying the CloudFront web ACL requires a concrete account and region, ' +
      'because the web ACL must live in ' +
      `${CLOUDFRONT_WAF_REGION} while the rest of the stack does not. ` +
      'Configure AWS credentials (or set CDK_DEFAULT_ACCOUNT and ' +
      'CDK_DEFAULT_REGION) — or synth without the web ACL using ' +
      '-c enableWaf=false.',
  );
}

const wafStack = enableWaf
  ? new CloudFrontWafStack(app, 'QuickDesktopCloudFrontWafStack', {
      description:
        'AWS WAF web ACL for the Amazon Quick desktop OAuth proxy distribution ' +
        '(qs-quick-desktop-cognito-idc)',
      rateLimit: wafRateLimit,
      allowedCidrs,
      env,
    })
  : undefined;

const stack = new QuickDesktopAuthStack(app, 'QuickDesktopCognitoIdcStack', {
  description: useIdc
    ? 'Amazon Cognito OIDC provider for Amazon Quick on desktop, with automated ' +
      'user sync from IAM Identity Center (qs-quick-desktop-cognito-idc)'
    : 'Amazon Cognito OIDC provider for Amazon Quick on desktop ' +
      '(qs-quick-desktop-cognito-idc)',
  useIdc,
  idcGroupName,
  idcInstanceArn,
  reconcileIntervalMinutes,
  allowedCidrs,
  mfaRequired,
  retain,
  onRemove,
  createTrail,
  enableCloudFront,
  webAclArn: wafStack?.webAclArn,
  originVerifySecret,
  accessLogging,
  apiGatewayAccountRole,
  apiRateLimit,
  apiBurstLimit,
  env,
  // Lets this stack consume the us-east-1 web ACL ARN.
  crossRegionReferences: true,
});

// The distribution cannot be created before the web ACL it references.
if (wafStack) stack.addStackDependency(wafStack);

for (const target of [stack, wafStack]) {
  if (!target) continue;
  Tags.of(target).add('project', 'quick-desktop-cognito-idc');
  Tags.of(target).add('managed-by', 'cdk');
}

app.synth();
