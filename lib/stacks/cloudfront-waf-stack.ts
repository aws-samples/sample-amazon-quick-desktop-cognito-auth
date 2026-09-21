import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CloudFrontWaf } from '../constructs/cloudfront-waf';

/** The only region in which a CLOUDFRONT-scoped WAFv2 web ACL can be created. */
export const CLOUDFRONT_WAF_REGION = 'us-east-1';

export interface CloudFrontWafStackProps extends StackProps {
  /** Requests per 5 minutes per client IP before the rate rule blocks. */
  readonly rateLimit?: number;
  /** Optional client IP allowlist enforced at the edge. */
  readonly allowedCidrs?: string[];
  /** Attach AWSManagedRulesCommonRuleSet (default true). */
  readonly managedRules?: boolean;
}

/**
 * Holds nothing but the CloudFront web ACL.
 *
 * It exists as a separate stack purely because a `CLOUDFRONT`-scoped web ACL
 * must live in us-east-1, while the rest of the deployment goes wherever IAM
 * Identity Center emits its events. The auth stack consumes `webAclArn` through
 * a cross-region reference, so deploy both together (`cdk deploy --all`).
 */
export class CloudFrontWafStack extends Stack {
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: CloudFrontWafStackProps) {
    super(scope, id, {
      ...props,
      env: { ...props.env, region: CLOUDFRONT_WAF_REGION },
      // Publishes webAclArn as an SSM parameter the auth stack can read from
      // its own region.
      crossRegionReferences: true,
    });

    const waf = new CloudFrontWaf(this, 'Waf', {
      rateLimit: props.rateLimit,
      allowedCidrs: props.allowedCidrs,
      managedRules: props.managedRules,
    });

    this.webAclArn = waf.webAclArn;

    new CfnOutput(this, 'WebAclArn', {
      value: waf.webAclArn,
      description: 'CLOUDFRONT-scoped web ACL attached to the OAuth proxy distribution',
    });
  }
}
