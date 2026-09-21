import { CustomResource, Duration } from 'aws-cdk-lib';
import { Function as LambdaFunction, Runtime, Code } from 'aws-cdk-lib/aws-lambda';
import { Provider, AwsCustomResource, AwsCustomResourcePolicy, PhysicalResourceId } from 'aws-cdk-lib/custom-resources';
import { Rule, RuleTargetInput, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction as LambdaTarget } from 'aws-cdk-lib/aws-events-targets';
import { SqsDestination } from 'aws-cdk-lib/aws-lambda-destinations';
import { SqsEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import { PolicyStatement, Effect } from 'aws-cdk-lib/aws-iam';
import { Queue, QueueEncryption } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { UserPool } from 'aws-cdk-lib/aws-cognito';
import { lambdaAsset, PYTHON_ASSET_OPTIONS } from '../lambda-assets';

export interface IdcSyncAutomationProps {
  readonly idcGroupName: string;
  readonly idcInstanceArn?: string;
  readonly userPool: UserPool;
  /** Explicit removal/deletion events only. Periodic scans always disable. */
  readonly onRemove?: string;
  readonly reconcileIntervalMinutes?: number;
}

/** Group-authoritative provisioning with event fast paths and a periodic safety net. */
export class IdcSyncAutomation extends Construct {
  public readonly syncFunction: LambdaFunction;
  public readonly coordinator: LambdaFunction;
  public readonly groupId: string;
  public readonly identityStoreId: string;
  public readonly deadLetterQueue: Queue;
  public readonly workDeadLetterQueue: Queue;
  public readonly workQueue: Queue;

  constructor(scope: Construct, id: string, props: IdcSyncAutomationProps) {
    super(scope, id);
    const { idcGroupName, userPool, onRemove = 'disable', idcInstanceArn } = props;
    const interval = props.reconcileIntervalMinutes ?? 5;
    if (!idcGroupName.trim()) throw new Error('idcGroupName must not be empty.');
    if (!['delete', 'disable'].includes(onRemove)) throw new Error('onRemove must be delete or disable.');
    if (!Number.isInteger(interval) || interval < 1 || interval > 1440) throw new Error('reconcileIntervalMinutes must be 1–1440.');

    const resolverFn = new LambdaFunction(this, 'IdcResolverFunction', {
      functionName: 'QuickDesktopIdcResolver', runtime: Runtime.PYTHON_3_12,
      handler: 'custom_resource.handler', code: Code.fromAsset(lambdaAsset('idc-sync'), PYTHON_ASSET_OPTIONS),
      timeout: Duration.seconds(60), description: 'Resolves one explicit or unambiguous IDC instance and group',
    });
    resolverFn.addToRolePolicy(new PolicyStatement({ effect: Effect.ALLOW,
      actions: ['sso:ListInstances', 'identitystore:ListGroups', 'identitystore:CreateGroup'], resources: ['*'],
    }));
    const provider = new Provider(this, 'IdcResolverProvider', { onEventHandler: resolverFn });
    const resolved = new CustomResource(this, 'IdcResolved', {
      serviceToken: provider.serviceToken,
      properties: { GroupName: idcGroupName, ...(idcInstanceArn && { InstanceArn: idcInstanceArn }) },
    });
    this.identityStoreId = resolved.getAttString('IdentityStoreId');
    this.groupId = resolved.getAttString('GroupId');

    // Retain the existing delivery-failure queue logical ID. Lambda asynchronous
    // processing failures must also have a destination: EventBridge's DLQ alone
    // cannot capture a handler failure after Lambda accepted delivery.
    this.deadLetterQueue = new Queue(this, 'IdcSyncDlq', {
      queueName: 'QuickDesktopIdcSyncDlq', retentionPeriod: Duration.days(14),
      encryption: QueueEncryption.SQS_MANAGED, enforceSSL: true,
    });
    this.workDeadLetterQueue = new Queue(this, 'IdcSyncWorkDlq', {
      queueName: 'QuickDesktopIdcSyncWorkDlq.fifo', fifo: true,
      retentionPeriod: Duration.days(14), encryption: QueueEncryption.SQS_MANAGED, enforceSSL: true,
    });
    this.workQueue = new Queue(this, 'IdcSyncWorkQueue', {
      queueName: 'QuickDesktopIdcSyncWork.fifo', fifo: true,
      visibilityTimeout: Duration.minutes(12), retentionPeriod: Duration.days(4),
      encryption: QueueEncryption.SQS_MANAGED, enforceSSL: true,
      deadLetterQueue: { queue: this.workDeadLetterQueue, maxReceiveCount: 3 },
    });
    const environment = {
      USER_POOL_ID: userPool.userPoolId, IDENTITY_STORE_ID: this.identityStoreId,
      WATCHED_GROUP_ID: this.groupId, ON_REMOVE: onRemove,
    };
    this.syncFunction = new LambdaFunction(this, 'IdcSyncFunction', {
      functionName: 'QuickDesktopIdcSync', runtime: Runtime.PYTHON_3_12,
      handler: 'handler.handler', code: Code.fromAsset(lambdaAsset('idc-sync'), PYTHON_ASSET_OPTIONS),
      timeout: Duration.seconds(120), environment,
      onFailure: new SqsDestination(this.deadLetterQueue), retryAttempts: 2,
      description: 'Reconciles managed Cognito users against current IDC group membership',
    });
    this.syncFunction.addToRolePolicy(new PolicyStatement({
      actions: ['identitystore:DescribeUser', 'identitystore:IsMemberInGroups'], resources: ['*'],
    }));
    this.syncFunction.addToRolePolicy(new PolicyStatement({
      actions: ['cognito-idp:AdminGetUser', 'cognito-idp:AdminCreateUser', 'cognito-idp:AdminUpdateUserAttributes',
        'cognito-idp:AdminEnableUser', 'cognito-idp:AdminDisableUser', 'cognito-idp:AdminDeleteUser', 'cognito-idp:ListUsers'],
      resources: [userPool.userPoolArn],
    }));
    this.syncFunction.addEventSource(new SqsEventSource(this.workQueue, {
      batchSize: 1, maxConcurrency: 2, reportBatchItemFailures: true,
    }));

    this.coordinator = new LambdaFunction(this, 'IdcReconciler', {
      functionName: 'QuickDesktopIdcReconciler', runtime: Runtime.PYTHON_3_12,
      handler: 'reconcile.handler', code: Code.fromAsset(lambdaAsset('idc-sync'), PYTHON_ASSET_OPTIONS),
      timeout: Duration.minutes(5), memorySize: 256,
      environment: { ...environment, WORK_QUEUE_URL: this.workQueue.queueUrl },
      onFailure: new SqsDestination(this.deadLetterQueue), retryAttempts: 2, maxEventAge: Duration.minutes(10),
      description: 'Queues current group members and managed users for safe reconciliation',
    });
    this.coordinator.addToRolePolicy(new PolicyStatement({ actions: ['identitystore:ListGroupMemberships'], resources: ['*'] }));
    this.coordinator.addToRolePolicy(new PolicyStatement({ actions: ['cognito-idp:ListUsers'], resources: [userPool.userPoolArn] }));
    this.workQueue.grantSendMessages(this.coordinator);

    const target = () => new LambdaTarget(this.coordinator, { retryAttempts: 2, deadLetterQueue: this.deadLetterQueue });
    new Rule(this, 'IdcMembershipRule', {
      ruleName: 'QuickDesktopIdcMembershipSync', description: 'Fast path for successful console membership changes',
      eventPattern: { source: ['aws.sso-directory'], detailType: ['AWS API Call via CloudTrail'],
        detail: { eventSource: ['sso-directory.amazonaws.com'], eventName: ['AddMemberToGroup', 'RemoveMemberFromGroup'],
          errorCode: [{ exists: false }], requestParameters: { groupId: [this.groupId] } } },
      targets: [target()],
    });
    new Rule(this, 'IdcUserDeleteRule', {
      ruleName: 'QuickDesktopIdcUserDeleteSync', description: 'Fast path for successful console user deletion',
      eventPattern: { source: ['aws.sso-directory'], detailType: ['AWS API Call via CloudTrail'],
        detail: { eventSource: ['sso-directory.amazonaws.com'], eventName: ['DeleteUser'], errorCode: [{ exists: false }] } },
      targets: [target()],
    });
    new Rule(this, 'IdentityStoreMembershipRule', {
      eventPattern: { source: ['aws.identitystore'], detailType: ['AWS API Call via CloudTrail'],
        detail: { eventSource: ['identitystore.amazonaws.com'], eventName: ['CreateGroupMembership'],
          errorCode: [{ exists: false }], requestParameters: { identityStoreId: [this.identityStoreId], groupId: [this.groupId] } } },
      targets: [target()],
    });
    new Rule(this, 'IdentityStoreUserDeleteRule', {
      eventPattern: { source: ['aws.identitystore'], detailType: ['AWS API Call via CloudTrail'],
        detail: { eventSource: ['identitystore.amazonaws.com'], eventName: ['DeleteUser'],
          errorCode: [{ exists: false }], requestParameters: { identityStoreId: [this.identityStoreId] } } },
      targets: [target()],
    });
    // DeleteGroupMembership carries a membership ID, not a user/group ID. SCIM
    // has another event schema. The snapshot path covers both without guessing
    // missing IDs or silently ignoring existing group members.
    new Rule(this, 'IdcReconciliationSchedule', {
      schedule: Schedule.rate(Duration.minutes(interval)),
      targets: [new LambdaTarget(this.coordinator, { retryAttempts: 2, deadLetterQueue: this.deadLetterQueue,
        event: RuleTargetInput.fromObject({ operation: 'reconcile' }) })],
    });
    const initialCall = {
      service: 'Lambda', action: 'invoke',
      parameters: { FunctionName: this.coordinator.functionArn, InvocationType: 'Event',
        Payload: JSON.stringify({ operation: 'reconcile', groupId: this.groupId, identityStoreId: this.identityStoreId }) },
      physicalResourceId: PhysicalResourceId.of('InitialIdcReconciliation'),
    };
    const initial = new AwsCustomResource(this, 'InitialReconciliation', {
      onCreate: initialCall, onUpdate: initialCall, installLatestAwsSdk: false,
      // SDK Invoke maps to IAM InvokeFunction, not lambda:Invoke.
      policy: AwsCustomResourcePolicy.fromStatements([new PolicyStatement({
        actions: ['lambda:InvokeFunction'], resources: [this.coordinator.functionArn],
      })]),
    });
    initial.node.addDependency(this.coordinator, this.syncFunction);
  }
}
