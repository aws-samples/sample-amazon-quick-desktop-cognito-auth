import { CfnIPSet, CfnWebACL } from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

/** Requests per evaluation window, per client IP, before the rate rule blocks. */
const DEFAULT_RATE_LIMIT = 100;
/** Rate-limit evaluation window. WAFv2 accepts 60, 120, 300 or 600 seconds. */
const RATE_WINDOW_SECONDS = 300;

/**
 * Managed-rule matches that are switched to Count rather than Block: each fires
 * on legitimate OAuth traffic often enough that blocking it would break
 * sign-in, and none protects against anything the rest of the group misses.
 *
 * - SizeRestrictions_QUERYSTRING: an `/oauth2/authorize` URL carries client_id,
 *   redirect_uri, scope, state and nonce and can exceed the 2 KB default.
 * - NoUserAgent_HEADER: not every OIDC client sends a User-Agent.
 * - GenericRFI_QUERYARGUMENTS: the remote-file-inclusion signature matches the
 *   `://` in `redirect_uri=http://127.0.0.1:18080`, so the loopback callback
 *   every RFC 8252 native app uses is blocked with a 403 before it reaches the
 *   proxy.
 * - GenericRFI_BODY: same signature over the POST body, which carries the same
 *   redirect_uri on the token exchange.
 * - EC2MetaDataSSRF_QUERYARGUMENTS / BODY: the managed rules also match the
 *   localhost loopback callback used by native OAuth clients. The proxy sends
 *   requests only to its configured Cognito origin and never fetches a supplied
 *   redirect_uri. Cognito enforces the app client's exact callback allowlist.
 */
const COUNTED_COMMON_RULES = [
  'SizeRestrictions_QUERYSTRING',
  'NoUserAgent_HEADER',
  'GenericRFI_QUERYARGUMENTS',
  'GenericRFI_BODY',
  'EC2MetaDataSSRF_QUERYARGUMENTS',
  'EC2MetaDataSSRF_BODY',
];

export interface CloudFrontWafProps {
  /**
   * Requests per 5 minutes per client IP before the rate-based rule blocks
   * (default 100). Raise it when clients share a NAT egress IP — every user
   * behind that IP counts against the same bucket.
   */
  readonly rateLimit?: number;
  /**
   * Optional IP allowlist. When set, requests from any other address are
   * blocked at the edge. Both IPv4 and IPv6 CIDRs are accepted.
   */
  readonly allowedCidrs?: string[];
  /** Attach the AWSManagedRulesCommonRuleSet group (default true). */
  readonly managedRules?: boolean;
}

/**
 * AWS WAF web ACL for the CloudFront distribution in front of the OAuth proxy.
 *
 * Must be created in **us-east-1** — that is the only region in which a
 * `CLOUDFRONT`-scoped web ACL can exist, whatever region the rest of the stack
 * lives in. See `CloudFrontWafStack`, which pins the region for exactly this
 * reason.
 */
export class CloudFrontWaf extends Construct {
  public readonly webAcl: CfnWebACL;
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: CloudFrontWafProps = {}) {
    super(scope, id);

    const { allowedCidrs } = props;
    const rateLimit = props.rateLimit ?? DEFAULT_RATE_LIMIT;
    const managedRules = props.managedRules ?? true;

    const rules: CfnWebACL.RuleProperty[] = [];

    // --- IP allowlist (priority 0: blocks before anything else is evaluated) --
    if (allowedCidrs?.length) {
      const ipSetStatements = this.createIpSets(allowedCidrs).map((ipSet) => ({
        ipSetReferenceStatement: { arn: ipSet.attrArn },
      }));

      rules.push({
        name: 'IpAllowlist',
        priority: 0,
        action: { block: {} },
        statement: {
          // Block everything that is NOT in the allowlist. An orStatement needs
          // at least two operands, so use the single set directly when there is
          // only one address family in play.
          notStatement: {
            statement:
              ipSetStatements.length === 1
                ? ipSetStatements[0]
                : { orStatement: { statements: ipSetStatements } },
          },
        },
        visibilityConfig: this.visibility('IpAllowlist'),
      });
    }

    // --- Rate limit ----------------------------------------------------------
    rules.push({
      name: 'RateLimitPerIp',
      priority: 10,
      action: { block: {} },
      statement: {
        rateBasedStatement: {
          limit: rateLimit,
          evaluationWindowSec: RATE_WINDOW_SECONDS,
          aggregateKeyType: 'IP',
        },
      },
      visibilityConfig: this.visibility('RateLimitPerIp'),
    });

    // --- AWS managed common rule set -----------------------------------------
    if (managedRules) {
      rules.push({
        name: 'AWSManagedRulesCommonRuleSet',
        priority: 20,
        // Managed rule groups take an overrideAction, never an action: the
        // group's own rules decide block/allow unless overridden per rule.
        overrideAction: { none: {} },
        statement: {
          managedRuleGroupStatement: {
            vendorName: 'AWS',
            name: 'AWSManagedRulesCommonRuleSet',
            ruleActionOverrides: COUNTED_COMMON_RULES.map((name) => ({
              name,
              actionToUse: { count: {} },
            })),
          },
        },
        visibilityConfig: this.visibility('AWSManagedRulesCommonRuleSet'),
      });
    }

    this.webAcl = new CfnWebACL(this, 'WebAcl', {
      name: 'QuickDesktopAuthProxyEdge',
      description: 'Rate limiting and managed rules for the Quick Desktop OAuth proxy',
      scope: 'CLOUDFRONT',
      // Allow by default: the rules above are the deny list. An OAuth endpoint
      // has to stay reachable by unauthenticated clients.
      defaultAction: { allow: {} },
      rules,
      visibilityConfig: this.visibility('QuickDesktopAuthProxyEdge'),
    });

    this.webAclArn = this.webAcl.attrArn;
  }

  /**
   * One IP set per address family — WAFv2 IP sets hold a single family, so a
   * mixed `allowedCidrs` list needs two of them.
   */
  private createIpSets(allowedCidrs: string[]): CfnIPSet[] {
    const v6 = allowedCidrs.filter((cidr) => cidr.includes(':'));
    const v4 = allowedCidrs.filter((cidr) => !cidr.includes(':'));

    const sets: CfnIPSet[] = [];
    if (v4.length) {
      sets.push(
        new CfnIPSet(this, 'AllowedIpV4', {
          name: 'QuickDesktopAllowedIpV4',
          description: 'Client IPv4 CIDRs allowed to reach the OAuth proxy',
          scope: 'CLOUDFRONT',
          ipAddressVersion: 'IPV4',
          addresses: v4,
        }),
      );
    }
    if (v6.length) {
      sets.push(
        new CfnIPSet(this, 'AllowedIpV6', {
          name: 'QuickDesktopAllowedIpV6',
          description: 'Client IPv6 CIDRs allowed to reach the OAuth proxy',
          scope: 'CLOUDFRONT',
          ipAddressVersion: 'IPV6',
          addresses: v6,
        }),
      );
    }
    return sets;
  }

  private visibility(metricName: string): CfnWebACL.VisibilityConfigProperty {
    return {
      cloudWatchMetricsEnabled: true,
      metricName,
      sampledRequestsEnabled: true,
    };
  }
}
