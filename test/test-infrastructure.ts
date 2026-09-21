import assert from 'assert';
import { App, Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { QuickDesktopAuthStack } from '../lib/stacks/quick-desktop-auth-stack';
import { parseAllowedCidrs, validateNetworkOptions } from '../lib/config-validation';
import { CloudFrontWaf } from '../lib/constructs/cloudfront-waf';

assert.deepEqual(parseAllowedCidrs('["192.0.2.0/24", "2001:db8::/32"]'), ['192.0.2.0/24', '2001:db8::/32']);
for (const value of ['["nope"]', '["192.0.2.1/33"]', '["2001:db8::/129"]', '["fe80::1%en0/128"]', '[12]', '{}']) {
  assert.throws(() => parseAllowedCidrs(value));
}
assert.throws(() => validateNetworkOptions({ enableCloudFront: true, enableWaf: false, allowedCidrs: ['192.0.2.0/24'] }), /not be enforced/);
assert.throws(() => validateNetworkOptions({ enableCloudFront: true, enableWaf: true, allowedCidrs: ['0.0.0.0/0'] }), /\/0/);
validateNetworkOptions({ enableCloudFront: false, enableWaf: false, allowedCidrs: ['192.0.2.0/24'] });

const env = { account: '123456789012', region: 'us-east-1' };
function synth(useIdc: boolean) {
  const app = new App();
  return Template.fromStack(new QuickDesktopAuthStack(app, useIdc ? 'WithIdc' : 'WithoutIdc', {
    env, useIdc, idcGroupName: useIdc ? 'QuickDesktopUsers' : undefined,
    enableCloudFront: false, createTrail: false,
  })).toJSON();
}
const noIdc = synth(false);
assert(!Object.values<any>(noIdc.Resources).some(r => ['AWS::SQS::Queue', 'AWS::Events::Rule', 'AWS::CloudTrail::Trail'].includes(r.Type)));
const pool = Object.values<any>(noIdc.Resources).find(r => r.Type === 'AWS::Cognito::UserPool')!;
assert.equal(pool.DeletionPolicy, 'Retain');
assert.equal(pool.Properties.MfaConfiguration, 'ON');
assert.deepEqual(pool.Properties.UserAttributeUpdateSettings.AttributesRequireVerificationBeforeUpdate, ['email']);
const client = Object.values<any>(noIdc.Resources).find(r => r.Type === 'AWS::Cognito::UserPoolClient')!;
assert.deepEqual(client.Properties.WriteAttributes, ['email']);
assert(!client.Properties.ReadAttributes.includes('custom:idc_user_id'));
assert(Object.values<any>(noIdc.Resources).some(r => r.Type === 'Custom::CognitoOidcReadiness'));
const withIdc = synth(true);
const resources = Object.values<any>(withIdc.Resources);
const worker = resources.find(r => r.Type === 'AWS::Lambda::Function' && r.Properties.FunctionName === 'QuickDesktopIdcSync')!;
assert.equal(worker.Properties.Environment.Variables.ON_REMOVE, 'disable');
const mapping = resources.find(r => r.Type === 'AWS::Lambda::EventSourceMapping')!;
assert.equal(mapping.Properties.BatchSize, 1);
assert.deepEqual(mapping.Properties.FunctionResponseTypes, ['ReportBatchItemFailures']);
assert.equal(mapping.Properties.ScalingConfig.MaximumConcurrency, 2);
const queues = resources.filter(r => r.Type === 'AWS::SQS::Queue');
assert.equal(queues.length, 3);
assert(queues.every(r => r.Properties.SqsManagedSseEnabled === true));
assert(queues.some(r => r.Properties.FifoQueue && r.Properties.VisibilityTimeout >= worker.Properties.Timeout * 6));
assert.equal(resources.filter(r => r.Type === 'AWS::Lambda::EventInvokeConfig').length, 2);
assert(resources.some(r => r.Type === 'AWS::Events::Rule' && r.Properties.ScheduleExpression === 'rate(5 minutes)'));
assert(withIdc.Outputs.IdcWorkDlqUrl && withIdc.Outputs.IdcDeliveryDlqUrl);
const initialPolicy = resources.find(r => r.Type === 'AWS::IAM::Policy' &&
  r.Properties.PolicyName.startsWith('IdcSyncInitialReconciliationCustomResourcePolicy'))!;
assert(initialPolicy.Properties.PolicyDocument.Statement.some((s: any) => s.Action === 'lambda:InvokeFunction'));
assert(!JSON.stringify(withIdc).includes('"lambda:Invoke"'));
const defaultApp = new App();
const withTrail = Template.fromStack(new QuickDesktopAuthStack(defaultApp, 'DefaultIdc', {
  env, idcGroupName: 'QuickDesktopUsers', enableCloudFront: false,
})).toJSON();
assert(Object.values<any>(withTrail.Resources).some(r => r.Type === 'AWS::CloudTrail::Trail'));
assert(!Object.values<any>(withTrail.Resources).some(r => r.Type === 'Custom::S3AutoDeleteObjects'));

// The CloudFront origin credential must be generated rather than derived from
// a public stack identifier, and both consumers must receive the same reference.
const originApp = new App({ context: { '@aws-cdk/core:checkSecretUsage': true } });
const originTemplate = Template.fromStack(new QuickDesktopAuthStack(originApp, 'WithEdge', {
  env, useIdc: false, createTrail: false, enableCloudFront: true,
})).toJSON();
const originResources = Object.values<any>(originTemplate.Resources);
const originSecret = originResources.find(r => r.Type === 'AWS::SecretsManager::Secret')!;
assert(originSecret && originSecret.Properties.GenerateSecretString.PasswordLength >= 32);
const originAuthorizer = originResources.find(r => r.Type === 'AWS::Lambda::Function' &&
  r.Properties.FunctionName === 'QuickDesktopOriginVerifyAuthorizer')!;
const edgeDistribution = originResources.find(r => r.Type === 'AWS::CloudFront::Distribution')!;
const originHeader = edgeDistribution.Properties.DistributionConfig.Origins[0].OriginCustomHeaders
  .find((h: any) => h.HeaderName === 'x-origin-verify');
assert.deepEqual(originHeader.HeaderValue, originAuthorizer.Properties.Environment.Variables.ORIGIN_VERIFY_SECRET);
assert(JSON.stringify(originHeader.HeaderValue).includes('resolve:secretsmanager:'));
assert(!JSON.stringify(originTemplate).includes('aws:Referer'));
assert(!Object.values<any>(noIdc.Resources).some(r => r.Type === 'AWS::SecretsManager::Secret'));

// Native OAuth loopback callbacks must pass the managed rules while the
// remaining managed rules and rate limit continue enforcing their actions.
const wafApp = new App();
const wafStack = new Stack(wafApp, 'EdgeValidation', { env });
new CloudFrontWaf(wafStack, 'Waf');
const wafTemplate = Template.fromStack(wafStack).toJSON();
const acl = Object.values<any>(wafTemplate.Resources).find(r => r.Type === 'AWS::WAFv2::WebACL')!;
const commonRules = acl.Properties.Rules.find((r: any) => r.Name === 'AWSManagedRulesCommonRuleSet');
assert.deepEqual(commonRules.OverrideAction, { None: {} });
const overrides = commonRules.Statement.ManagedRuleGroupStatement.RuleActionOverrides;
assert.deepEqual(overrides.map((r: any) => r.Name).sort(), [
  'SizeRestrictions_QUERYSTRING', 'NoUserAgent_HEADER',
  'GenericRFI_QUERYARGUMENTS', 'GenericRFI_BODY',
  'EC2MetaDataSSRF_QUERYARGUMENTS', 'EC2MetaDataSSRF_BODY',
].sort());
assert(overrides.every((r: any) => JSON.stringify(r.ActionToUse) === JSON.stringify({ Count: {} })));
assert(acl.Properties.Rules.some((r: any) => r.Statement.RateBasedStatement && r.Action.Block));
console.log('Infrastructure and configuration checks passed for both IDC modes.');
