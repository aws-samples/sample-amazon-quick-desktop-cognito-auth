"""Deploy-time custom resource: resolves the IAM Identity Center instance and group.

Backed by the CDK ``custom_resources.Provider`` framework, so the handler only
returns ``Data`` / ``PhysicalResourceId`` — the framework sends the
CloudFormation response.

On Create/Update it resolves, from the group name passed in:
  - the IAM Identity Center instance ARN and Identity Store ID (``ListInstances``)
  - the group ID for the named group (``ListGroups`` filtered by DisplayName);
    if no such group exists it is created (``CreateGroup``) and the new ID is
    returned. On Update with a changed name, the new group is resolved/created
    and the old one is left untouched.

These values are read back by the stack and injected as environment variables on
the sync Lambda and as the group-ID filter on the EventBridge rule.

On Delete it is a no-op: the group is intentionally NOT deleted, since users may
have been assigned to it and deleting it would orphan those assignments.
"""

import os

import boto3
from botocore.exceptions import ClientError

_sso_admin = boto3.client("sso-admin")
_identitystore = boto3.client("identitystore")


class ResolveError(Exception):
    """Raised when the IDC instance or group cannot be resolved."""


def _resolve_instance(requested_arn=None) -> tuple[str, str]:
    """Do not silently choose between an account and organization instance."""
    instances = []
    token = None
    while True:
        try:
            resp = _sso_admin.list_instances(**({"NextToken": token} if token else {}))
        except ClientError as error:
            raise ResolveError("ListInstances failed: " + error.response["Error"]["Code"]) from None
        instances.extend(resp.get("Instances", []))
        token = resp.get("NextToken")
        if not token:
            break
    if requested_arn:
        instances = [instance for instance in instances if instance["InstanceArn"] == requested_arn]
    if not instances:
        raise ResolveError("No matching IAM Identity Center instance is visible in this account/region.")
    if len(instances) != 1:
        raise ResolveError("Multiple IAM Identity Center instances are visible. Set -c idcInstanceArn=<intended-instance-arn>.")
    instance = instances[0]
    primary = instance.get("PrimaryRegion")
    if primary and os.environ.get("AWS_REGION") and primary != os.environ["AWS_REGION"]:
        raise ResolveError("Deploy in the selected Identity Center instance's primary region: " + primary)
    return instance["InstanceArn"], instance["IdentityStoreId"]


def _lookup_groups(identity_store_id, group_name):
    groups, token = [], None
    while True:
        response = _identitystore.list_groups(
            IdentityStoreId=identity_store_id,
            Filters=[{"AttributePath": "DisplayName", "AttributeValue": group_name}],
            **({"NextToken": token} if token else {}),
        )
        groups.extend(response.get("Groups", []))
        token = response.get("NextToken")
        if not token:
            return groups


def _resolve_group_id(identity_store_id: str, group_name: str) -> str:
    """Returns the group ID for the named group, creating the group if absent."""
    try:
        groups = _lookup_groups(identity_store_id, group_name)
    except ClientError as error:
        raise ResolveError("ListGroups failed: " + error.response["Error"]["Code"]) from None
    if len(groups) > 1:
        raise ResolveError("GroupName must resolve to exactly one Identity Center group.")
    if groups:
        return groups[0]["GroupId"]

    return _create_group(identity_store_id, group_name)


def _create_group(identity_store_id: str, group_name: str) -> str:
    """Creates the named group and returns its ID.

    Called when ``ListGroups`` finds no match. Idempotency: IDC group
    DisplayNames must be unique within an identity store, so a concurrent create
    surfaces ``ConflictException`` — we treat that as "someone else just created
    it" and re-resolve by name rather than failing the deploy.
    """
    try:
        resp = _identitystore.create_group(
            IdentityStoreId=identity_store_id, DisplayName=group_name
        )
        group_id = resp["GroupId"]
        print(f"CREATED: IDC group '{group_name}' -> {group_id}")
        return group_id
    except _identitystore.exceptions.ConflictException:
        # Lost a race: the group now exists. Look it up again.
        print(f"CONFLICT: IDC group '{group_name}' already exists — re-resolving")
        groups = _lookup_groups(identity_store_id, group_name)
        if len(groups) != 1:
            raise ResolveError(
                f"CreateGroup for '{group_name}' hit a conflict but the group "
                f"could not be re-resolved in identity store {identity_store_id}."
            )
        return groups[0]["GroupId"]
    except ClientError as e:
        raise ResolveError("CreateGroup failed: " + e.response["Error"]["Code"]) from None


def handler(event: dict, _context: object) -> dict:
    request_type = event.get("RequestType")
    print(f"REQUEST: {request_type}")

    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "idc-resolver")}

    props = event.get("ResourceProperties") or {}
    group_name = props.get("GroupName")
    if not group_name:
        raise ResolveError("GroupName resource property is required")

    instance_arn, identity_store_id = _resolve_instance(props.get("InstanceArn"))
    group_id = _resolve_group_id(identity_store_id, group_name)

    print(
        f"RESOLVED: instance={instance_arn} "
        f"identityStore={identity_store_id} group={group_name} -> {group_id}"
    )

    # PhysicalResourceId is stable per (group name) so updates don't replace the
    # resource unnecessarily.
    return {
        "PhysicalResourceId": f"idc-resolver-{group_name}",
        "Data": {
            "InstanceArn": instance_arn,
            "IdentityStoreId": identity_store_id,
            "GroupId": group_id,
        },
    }
