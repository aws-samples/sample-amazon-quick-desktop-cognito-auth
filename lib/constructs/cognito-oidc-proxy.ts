import { CustomResource, Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import {
  UserPool,
  UserPoolClient,
  UserPoolDomain,
  Mfa,
  OAuthScope,
  AccountRecovery,
  StringAttribute,
  ClientAttributes,
} from 'aws-cdk-lib/aws-cognito';
import {
  AccessLogField,
  AccessLogFormat,
  LogGroupLogDestination,
  RestApi,
  LambdaIntegration,
  EndpointType,
  MethodLoggingLevel,
  RequestAuthorizer,
} from 'aws-cdk-lib/aws-apigateway';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AllowedMethods,
  CacheCookieBehavior,
  CacheHeaderBehavior,
  CachePolicy,
  CacheQueryStringBehavior,
  CfnOriginRequestPolicy,
  Distribution,
  HttpVersion,
  OriginRequestCookieBehavior,
  OriginRequestHeaderBehavior,
  OriginRequestPolicy,
  OriginRequestQueryStringBehavior,
  PriceClass,
  ViewerProtocolPolicy,
} from 'aws-cdk-lib/aws-cloudfront';
import { RestApiOrigin } from 'aws-cdk-lib/aws-cloudfront-origins';
import { Function as LambdaFunction, Runtime, Code } from 'aws-cdk-lib/aws-lambda';
import {
  PolicyDocument,
  PolicyStatement,
  Effect,
  AnyPrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { lambdaAsset, PYTHON_ASSET_OPTIONS } from '../lambda-assets';

/** Header CloudFront injects on origin requests so the proxy can prove origin. */
const ORIGIN_VERIFY_HEADER = 'x-origin-verify';

/** Steady-state requests per second allowed on the API Gateway stage. */
const DEFAULT_API_RATE_LIMIT = 10;
/** Burst allowance on top of the steady-state rate. */
const DEFAULT_API_BURST_LIMIT = 20;

/**
 * JSON access log format, kept field-for-field in step with the workshop
 * CloudFormation port of this project so log queries work against either.
 *
 * `jsonWithStandardFields` is not used: it omits the integration and latency
 * fields, which are what separate "Cognito rejected it" from "the proxy Lambda
 * failed", and it offers caller/user fields that are always empty here — the
 * origin-verify authorizer returns a fixed `principalId`, not an end user.
 */
const ACCESS_LOG_FORMAT = AccessLogFormat.custom(
  JSON.stringify({
    requestId: AccessLogField.contextRequestId(),
    ip: AccessLogField.contextIdentitySourceIp(),
    requestTime: AccessLogField.contextRequestTime(),
    httpMethod: AccessLogField.contextHttpMethod(),
    resourcePath: AccessLogField.contextResourcePath(),
    status: AccessLogField.contextStatus(),
    protocol: AccessLogField.contextProtocol(),
    responseLength: AccessLogField.contextResponseLength(),
    integrationStatus: AccessLogField.contextIntegrationStatus(),
    integrationLatency: AccessLogField.contextIntegrationLatency(),
    responseLatency: AccessLogField.contextResponseLatency(),
    userAgent: AccessLogField.contextIdentityUserAgent(),
  }),
);

export interface CognitoOidcProxyProps {
  /** Enforce TOTP (authenticator-app) MFA on all users. */
  readonly mfaRequired?: boolean;
  /** Retain the User Pool when the stack is destroyed. */
  readonly removalPolicy: RemovalPolicy;
  /**
   * Put a CloudFront distribution in front of the API Gateway (default true).
   * The published auth/token endpoints then point at CloudFront.
   */
  readonly enableCloudFront?: boolean;
  /**
   * Optional IP allowlist (CIDRs). Applied as an API Gateway resource policy
   * **only when CloudFront is disabled** — with CloudFront in front, the API
   * sees a CloudFront edge IP rather than the client's, so the allowlist has to
   * be enforced at the edge by `CloudFrontWaf` instead.
   */
  readonly allowedCidrs?: string[];
  /** ARN of a CLOUDFRONT-scoped web ACL to attach to the distribution. */
  readonly webAclArn?: string;
  /**
   * Secret CloudFront sends as `x-origin-verify` on every origin request, and
   * that the API Gateway Lambda authorizer requires. By default Secrets Manager
   * generates a random value that CloudFormation resolves at deployment.
   */
  readonly originVerifySecret?: string;
  /**
   * Write API Gateway access logs to CloudWatch Logs as JSON (default true).
   *
   * Separate from the execution logging `loggingLevel` controls: access logs are
   * the per-request audit trail (caller IP, path, status, latency), which is what
   * you need when investigating abuse of an unauthenticated OAuth endpoint.
   */
  readonly accessLogging?: boolean;
  /** Retention for the access log group (default one month). */
  readonly accessLogRetention?: RetentionDays;
  /**
   * Create the API Gateway account-level CloudWatch Logs role (default true).
   *
   * **The API Gateway account setting is account- and region-wide, not
   * per-API.** Creating it overwrites whatever role the region already had,
   * which can redirect — or, with a narrower role, break — logging for every
   * other REST API in the account. Set false in a shared account that already
   * has one configured: access logging keeps working against the existing role.
   */
  readonly apiGatewayAccountRole?: boolean;
  /** Steady-state requests per second on the stage (default 10). */
  readonly apiRateLimit?: number;
  /** Burst allowance on the stage (default 20). */
  readonly apiBurstLimit?: number;
}

/**
 * Amazon Cognito configured as an OIDC provider for Amazon Quick on desktop,
 * fronted by an API Gateway + Lambda proxy that strips the `offline_access`
 * scope (which Quick sends but Cognito does not support) from OAuth requests,
 * and — by default — a CloudFront distribution that terminates TLS at the edge,
 * carries the AWS WAF web ACL, and proves origin with a secret header that an
 * API Gateway Lambda authorizer checks before the proxy is invoked.
 */
export class CognitoOidcProxy extends Construct {
  public readonly userPool: UserPool;
  public readonly userPoolClient: UserPoolClient;
  public readonly api: RestApi;
  /** Undefined when deployed with `enableCloudFront: false`. */
  public readonly distribution?: Distribution;
  public readonly issuerUrl: string;
  public readonly authEndpoint: string;
  public readonly tokenEndpoint: string;
  public readonly jwksUri: string;
  public readonly cognitoReadiness: CustomResource;
  public readonly proxyLogGroupName: string;
  /** Base URL the OAuth endpoints are published under (CloudFront or the API). */
  public readonly proxyBaseUrl: string;
  /** Undefined when deployed with `accessLogging: false`. */
  public readonly accessLogGroup?: LogGroup;

  constructor(scope: Construct, id: string, props: CognitoOidcProxyProps) {
    super(scope, id);

    const { account, region } = Stack.of(this);
    const { mfaRequired, removalPolicy, allowedCidrs, webAclArn } =
      props;
    const enableCloudFront = props.enableCloudFront ?? true;
    const accessLogging = props.accessLogging ?? true;
    const apiGatewayAccountRole = props.apiGatewayAccountRole ?? true;
    const apiRateLimit = props.apiRateLimit ?? DEFAULT_API_RATE_LIMIT;
    const apiBurstLimit = props.apiBurstLimit ?? DEFAULT_API_BURST_LIMIT;
    // A stack identifier is public metadata, not a suitable source of entropy
    // for an origin credential. Generate a secret and resolve it during deployment.
    const generatedOriginSecret = enableCloudFront && !props.originVerifySecret
      ? new Secret(this, 'OriginVerifySecret', {
          generateSecretString: { passwordLength: 40, excludePunctuation: true },
          removalPolicy: RemovalPolicy.DESTROY,
        })
      : undefined;
    const originVerifySecret = enableCloudFront
      // These AWS resource properties require the resolved string. The template
      // contains a Secrets Manager dynamic reference, never the generated value.
      ? (props.originVerifySecret ?? generatedOriginSecret!.secretValue.unsafeUnwrap())
      : undefined;

    // --- Cognito User Pool ---------------------------------------------------
    this.userPool = new UserPool(this, 'UserPool', {
      userPoolName: 'QuickDesktopUserPool',
      selfSignUpEnabled: false,
      signInAliases: { username: true, email: true },
      autoVerify: { email: true },
      keepOriginal: { email: true },
      userInvitation: {
        emailSubject: 'Your Amazon Quick Desktop invitation',
        emailBody: `You have been invited to Amazon Quick Desktop.\n\nYour username is {username} — but please log in with your email address instead.\nTemporary password: {####}\n\nYou will be asked to set a new password on first login.`,
      },
      standardAttributes: { email: { required: true, mutable: true } },
      // Correlates each Cognito user back to its IAM Identity Center user ID, so
      // the sync Lambda can find the right user on a `DeleteUser` event (which
      // carries only the IDC userId, not an email).
      customAttributes: {
        idc_user_id: new StringAttribute({ minLen: 1, maxLen: 256, mutable: true }),
      },
      mfa: (mfaRequired ?? true) ? Mfa.REQUIRED : Mfa.OPTIONAL,
      mfaSecondFactor: { sms: false, otp: true },
      accountRecovery: AccountRecovery.EMAIL_ONLY,
      removalPolicy,
    });

    // Hosted-UI domain. The prefix is account+region scoped to stay globally
    // unique without hardcoding any account or region.
    const domainPrefix = `quick-desktop-${account}-${region}`;
    const userPoolDomain = new UserPoolDomain(this, 'UserPoolDomain', {
      userPool: this.userPool,
      cognitoDomain: { domainPrefix },
    });

    // Public app client (no secret) — Quick desktop is a public OAuth client
    // using the authorization-code grant.
    this.userPoolClient = this.userPool.addClient('AppClient', {
      userPoolClientName: 'QuickDesktopAppClient',
      generateSecret: false,
      // The sync ownership marker is admin-managed. Do not grant this public
      // client Cognito's default write access to every custom attribute.
      // Email is required by the existing pool schema; keep updates verified.
      writeAttributes: new ClientAttributes().withStandardAttributes({ email: true }),
      readAttributes: new ClientAttributes().withStandardAttributes({
        email: true, emailVerified: true, fullname: true, givenName: true,
        familyName: true, preferredUsername: true,
      }),
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [OAuthScope.OPENID, OAuthScope.EMAIL, OAuthScope.PROFILE],
        // Both loopback spellings: RFC 8252 §7.3 has native apps redirect to
        // the IP literal, and Quick desktop may send either form.
        callbackUrls: ['http://localhost:18080', 'http://127.0.0.1:18080'],
      },
      // Quick uses only the OAuth authorization-code grant via the hosted UI,
      // so the SRP/USER_PASSWORD/CUSTOM flows are unnecessary and enabling them
      // only widens the attack surface. They have to be turned off explicitly:
      // omitting `authFlows` leaves `ExplicitAuthFlows` unset, and Cognito then
      // applies its own defaults rather than disabling anything. Passing all
      // three as false synthesizes `[ALLOW_REFRESH_TOKEN_AUTH]` — the one flow
      // the hosted-UI grant actually needs, to refresh an expired session.
      authFlows: { custom: false, userSrp: false, userPassword: false },
    });

    const cognitoDomain = `https://${domainPrefix}.auth.${region}.amazoncognito.com`;
    this.issuerUrl = `https://cognito-idp.${region}.amazonaws.com/${this.userPool.userPoolId}`;
    this.jwksUri = `${this.issuerUrl}/.well-known/jwks.json`;

    // Check public metadata before publishing the proxy. This makes no login
    // request and cannot certify Quick's separately created extension access.
    const readinessFn = new LambdaFunction(this, 'OidcReadinessFunction', {
      functionName: 'QuickDesktopOidcReadiness',
      runtime: Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: Code.fromAsset(lambdaAsset('oidc-readiness'), PYTHON_ASSET_OPTIONS),
      timeout: Duration.seconds(180),
      description: 'Checks Cognito discovery and signing keys before deployment completes',
      logGroup: new LogGroup(this, 'OidcReadinessLogs', {
        retention: RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    this.cognitoReadiness = new CustomResource(this, 'CognitoReadiness', {
      resourceType: 'Custom::CognitoOidcReadiness',
      serviceToken: readinessFn.functionArn,
      serviceTimeout: Duration.seconds(210),
      properties: {
        IssuerUrl: this.issuerUrl,
        CognitoDomain: cognitoDomain,
        // Bump when changing the probe to rerun it on existing deployments.
        CheckRevision: '1',
      },
    });
    this.cognitoReadiness.node.addDependency(userPoolDomain);

    // --- Auth proxy Lambda ---------------------------------------------------
    const proxyFn = new LambdaFunction(this, 'AuthProxyFunction', {
      functionName: 'QuickDesktopAuthProxy',
      runtime: Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: Code.fromAsset(lambdaAsset('auth-proxy'), PYTHON_ASSET_OPTIONS),
      timeout: Duration.seconds(10),
      environment: {
        COGNITO_DOMAIN: cognitoDomain,
        COGNITO_ISSUER: this.issuerUrl,
        COGNITO_CLIENT_ID: this.userPoolClient.userPoolClientId,
      },
      description: 'Strips offline_access scope from Quick desktop OAuth requests',
    });

    proxyFn.node.addDependency(this.cognitoReadiness);
    this.proxyLogGroupName = proxyFn.logGroup.logGroupName;
    const integration = new LambdaIntegration(proxyFn);

    // --- Origin-verify Lambda authorizer -------------------------------------
    // Undefined without CloudFront: nothing would then send the header, so
    // attaching the authorizer would deny every request. Both methods stay
    // `AuthorizationType: NONE` in that mode.
    const authorizer = originVerifySecret
      ? this.createOriginVerifyAuthorizer(originVerifySecret)
      : undefined;
    const policy = this.createResourcePolicy({
      // With CloudFront in front, the CIDR allowlist moves to the edge web ACL.
      allowedCidrs: enableCloudFront ? undefined : allowedCidrs,
    });

    // Per-request audit trail. Retained on stack delete would orphan the group,
    // so it follows the stack; the retention bounds its cost meanwhile.
    if (accessLogging) {
      this.accessLogGroup = new LogGroup(this, 'AccessLogs', {
        retention: props.accessLogRetention ?? RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      });
    }

    this.api = new RestApi(this, 'AuthProxyApi', {
      restApiName: 'QuickDesktopAuthProxy',
      description: 'OAuth proxy in front of the Cognito hosted UI',
      endpointTypes: [EndpointType.REGIONAL],
      // The account-level CloudWatch Logs role is a single, region-wide setting.
      // CDK creates it by default; `apiGatewayAccountRole: false` leaves the
      // account's existing configuration alone (see the prop docs).
      cloudWatchRole: apiGatewayAccountRole,
      deployOptions: {
        stageName: 'prod',
        loggingLevel: MethodLoggingLevel.ERROR,
        // Aggregate backstop *behind* the edge WAF, which is the primary rate
        // control (per client IP, at CloudFront). This one caps what reaches the
        // origin at all, which bounds Lambda cost if the origin secret ever
        // leaks — WAF cannot help there, because such traffic skips CloudFront.
        throttlingRateLimit: apiRateLimit,
        throttlingBurstLimit: apiBurstLimit,
        metricsEnabled: true,
        ...(this.accessLogGroup && {
          accessLogDestination: new LogGroupLogDestination(this.accessLogGroup),
          accessLogFormat: ACCESS_LOG_FORMAT,
        }),
      },
      ...(policy && { policy }),
    });

    const oauth2 = this.api.root.addResource('oauth2');
    // `authorizer` is undefined without CloudFront, which leaves both methods
    // unauthenticated — see createOriginVerifyAuthorizer.
    oauth2.addResource('authorize').addMethod('GET', integration, { authorizer });
    oauth2.addResource('token').addMethod('POST', integration, { authorizer });

    // --- CloudFront ----------------------------------------------------------
    if (enableCloudFront) {
      this.distribution = this.createDistribution({
        originVerifySecret: originVerifySecret!,
        webAclArn,
      });
      this.proxyBaseUrl = `https://${this.distribution.distributionDomainName}/`;
    } else {
      this.proxyBaseUrl = this.api.url;
    }

    this.authEndpoint = `${this.proxyBaseUrl}oauth2/authorize`;
    this.tokenEndpoint = `${this.proxyBaseUrl}oauth2/token`;
  }

  /**
   * API Gateway REQUEST authorizer that rejects anything not carrying the
   * CloudFront origin-verify secret, before the proxy Lambda is invoked.
   *
   * REQUEST rather than TOKEN: the check is on a custom header, and a TOKEN
   * authorizer only reads a bearer token from one named header and matches it
   * against a regex.
   *
   * Two properties are load-bearing and coupled:
   *
   * - `resultsCacheTtl: 0` — the authorizer returns a policy scoped to the
   *   invoked `methodArn`, and a cached policy applies to the whole API ("Ensure
   *   that your policy is applicable to all resources and methods across your
   *   API"). With caching on, the policy cached by the first call to
   *   `/oauth2/authorize` would deny the following `/oauth2/token` call. Caching
   *   buys nothing here anyway: a sign-in is two requests.
   * - `identitySources: []` — nothing to name, because identity sources exist to
   *   build the cache key and there is no cache. With caching off "API Gateway
   *   directly passes the request to the Lambda function", so this function is
   *   invoked on every request and does the header check itself; rejections stay
   *   a uniform `403`. The empty list is also the only value CDK accepts here —
   *   it requires at least one identity source once the TTL is above zero, and
   *   with caching on a request missing a named identity source is answered
   *   `401` without the authorizer being invoked at all.
   */
  private createOriginVerifyAuthorizer(originVerifySecret: string): RequestAuthorizer {
    const authorizerFn = new LambdaFunction(this, 'OriginVerifyAuthorizerFunction', {
      functionName: 'QuickDesktopOriginVerifyAuthorizer',
      runtime: Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: Code.fromAsset(
        lambdaAsset('origin-verify-authorizer'),
        PYTHON_ASSET_OPTIONS,
      ),
      // Header comparison only — no outbound calls, so the default 3s is ample.
      timeout: Duration.seconds(5),
      environment: { ORIGIN_VERIFY_SECRET: originVerifySecret },
      description: 'Allows only requests carrying the CloudFront origin-verify header',
    });

    return new RequestAuthorizer(this, 'OriginVerifyAuthorizer', {
      authorizerName: 'OriginVerifyAuthorizer',
      handler: authorizerFn,
      identitySources: [],
      resultsCacheTtl: Duration.seconds(0),
    });
  }

  /**
   * CloudFront distribution in front of the API Gateway stage.
   *
   * Nothing is cached: OAuth responses are single-use (authorization codes,
   * tokens) and caching any of them would be a security bug. The distribution
   * is here for the edge WAF, edge TLS termination, and a stable hostname that
   * hides the API Gateway endpoint.
   */
  private createDistribution(opts: {
    originVerifySecret: string;
    webAclArn?: string;
  }): Distribution {
    const { originVerifySecret, webAclArn } = opts;

    const origin = new RestApiOrigin(this.api, {
      customHeaders: {
        [ORIGIN_VERIFY_HEADER]: originVerifySecret,
      },
    });

    // Deliberately narrow rather than the managed ALL_VIEWER_EXCEPT_HOST_HEADER
    // policy: the origin sees only the three headers it actually reads, so no
    // viewer header reaches API Gateway or Cognito unexamined.
    //
    // This is defence in depth, not what protects `x-origin-verify`. A custom
    // origin header wins over a viewer header of the same name either way:
    // "If the header names and values that you specify are not already present
    // in the viewer request, CloudFront adds them to the origin request. If a
    // header is present, CloudFront overwrites the header value before
    // forwarding the request to the origin." So a client cannot smuggle its own
    // `x-origin-verify` past the custom origin header above,
    // even under an all-viewer-headers policy.
    const originRequestPolicy = new OriginRequestPolicy(this, 'OriginRequestPolicy', {
      comment: 'Quick Desktop OAuth proxy: query strings plus the OAuth headers only',
      // The OAuth parameters (client_id, redirect_uri, scope, state, …) all
      // travel in the query string on /oauth2/authorize.
      queryStringBehavior: OriginRequestQueryStringBehavior.all(),
      // `Authorization` belongs in this list too, but it can only be added via
      // the escape hatch below — see the comment there.
      headerBehavior: OriginRequestHeaderBehavior.allowList('Content-Type', 'Accept'),
      cookieBehavior: OriginRequestCookieBehavior.none(),
    });

    // `OriginRequestHeaderBehavior.allowList` throws on `Authorization` and
    // tells you to forward it through a cache policy instead. That advice no
    // longer holds: a cache policy with caching disabled (all TTLs zero, as
    // below) may not name any cache key component, and CloudFront rejects the
    // stack with "The parameter HeaderBehavior is invalid for policy with
    // caching disabled." CloudFront itself is happy to forward `Authorization`
    // from an origin request policy — the managed AllViewer policy does — so
    // the L1 property is set directly to get past the L2 validation.
    (originRequestPolicy.node.defaultChild as CfnOriginRequestPolicy).addPropertyOverride(
      'OriginRequestPolicyConfig.HeadersConfig.Headers',
      ['Authorization', 'Content-Type', 'Accept'],
    );

    // Equivalent to the managed CachingDisabled policy: all three TTLs zero and
    // no cache key components. Named separately only so it shows up in the
    // console under this stack's name. Everything the origin needs is forwarded
    // by the origin request policy above, not from here.
    const cachePolicy = new CachePolicy(this, 'CachePolicy', {
      comment: 'Quick Desktop OAuth proxy: caching off',
      minTtl: Duration.seconds(0),
      defaultTtl: Duration.seconds(0),
      maxTtl: Duration.seconds(0),
      headerBehavior: CacheHeaderBehavior.none(),
      queryStringBehavior: CacheQueryStringBehavior.none(),
      cookieBehavior: CacheCookieBehavior.none(),
      enableAcceptEncodingGzip: false,
      enableAcceptEncodingBrotli: false,
    });

    return new Distribution(this, 'Distribution', {
      comment: 'Amazon Quick desktop OAuth proxy',
      defaultBehavior: {
        origin,
        // POST is required for /oauth2/token.
        allowedMethods: AllowedMethods.ALLOW_ALL,
        cachePolicy,
        originRequestPolicy,
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // The cache policy above does not normalize Accept-Encoding, so
        // CloudFront could not compress anyway — and a token response is a few
        // hundred bytes. Off, rather than configured-but-inert.
        compress: false,
      },
      // Covers North America, Europe and Asia Pacific (including Seoul and
      // Tokyo). Clients outside those regions are still served, just from a
      // further edge — a per-sign-in request volume does not justify the
      // price-class-all premium.
      priceClass: PriceClass.PRICE_CLASS_200,
      httpVersion: HttpVersion.HTTP2_AND_3,
      // The default *.cloudfront.net certificate: no custom domain, so no ACM
      // certificate and no DNS to manage. Its minimum TLS version is fixed by
      // CloudFront, which is why minimumProtocolVersion is not set here.
      webAclId: webAclArn,
    });
  }

  /**
   * API Gateway resource policy for the direct-API mode. It denies source IPs
   * outside the configured CIDRs. CloudFront mode uses the edge WAF allowlist
   * and the required origin-verification Lambda authorizer instead.
   */
  private createResourcePolicy(opts: {
    allowedCidrs?: string[];
  }): PolicyDocument | undefined {
    const { allowedCidrs } = opts;
    if (!allowedCidrs?.length) return undefined;

    const invoke = {
      principals: [new AnyPrincipal()],
      actions: ['execute-api:Invoke'],
      resources: ['execute-api:/*/*/*'],
    };

    const statements = [new PolicyStatement({ effect: Effect.ALLOW, ...invoke })];

    if (allowedCidrs?.length) {
      statements.push(
        new PolicyStatement({
          effect: Effect.DENY,
          ...invoke,
          conditions: { NotIpAddress: { 'aws:SourceIp': allowedCidrs } },
        }),
      );
    }

    return new PolicyDocument({ statements });
  }
}
