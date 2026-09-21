"""Reconcile queued Identity Center users against current group membership.

The queue serializes work per user. Failed items use SQS redrive; successful but
stale events never grant access without a current membership check. Scheduled
reconciliation only disables absent managed users; explicit removal/deletion
events may honor ON_REMOVE=delete. No passwords or MFA state are copied from IDC.
"""
import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from common import IDC_USER_ID_ATTR, SyncError, log, parse_directory_event, user_id

USER_POOL_ID = os.environ["USER_POOL_ID"]
IDENTITY_STORE_ID = os.environ["IDENTITY_STORE_ID"]
WATCHED_GROUP_ID = os.environ["WATCHED_GROUP_ID"]
ON_REMOVE = os.environ.get("ON_REMOVE", "disable")
if ON_REMOVE not in {"delete", "disable"} or not WATCHED_GROUP_ID:
    raise ValueError("Invalid IDC sync configuration")
SDK_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=3, read_timeout=10)
_identitystore = boto3.client("identitystore", config=SDK_CONFIG)
_cognito = boto3.client("cognito-idp", config=SDK_CONFIG)


def _attributes(user):
    return {a["Name"]: a["Value"] for a in user.get("UserAttributes", user.get("Attributes", []))}


def _get_owned(uid, username=None):
    try:
        record = _cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username or uid)
    except _cognito.exceptions.UserNotFoundException:
        return None
    if _attributes(record).get(IDC_USER_ID_ATTR) != uid:
        raise SyncError("cognito_username_not_owned_by_sync")
    return record


def _find_legacy(uid):
    matches = []
    for page in _cognito.get_paginator("list_users").paginate(UserPoolId=USER_POOL_ID):
        for record in page.get("Users", []):
            if _attributes(record).get(IDC_USER_ID_ATTR) == uid:
                matches.append(record["Username"])
    if len(matches) > 1:
        raise SyncError("ambiguous_legacy_identity")
    return _get_owned(uid, matches[0]) if matches else None


def _is_member(uid):
    try:
        result = _identitystore.is_member_in_groups(
            IdentityStoreId=IDENTITY_STORE_ID, GroupIds=[WATCHED_GROUP_ID], MemberId={"UserId": uid}
        )
    except _identitystore.exceptions.ResourceNotFoundException:
        # The user may have been deleted by the time a removal arrives.
        return False
    rows = [r for r in result.get("Results", []) if r.get("GroupId") == WATCHED_GROUP_ID]
    if len(rows) != 1 or not isinstance(rows[0].get("MembershipExists"), bool):
        raise SyncError("invalid_membership_response")
    return rows[0]["MembershipExists"]


def _current_email(uid):
    record = _identitystore.describe_user(IdentityStoreId=IDENTITY_STORE_ID, UserId=uid)
    emails = [r for r in (record.get("Emails") or []) if isinstance(r.get("Value"), str) and r["Value"]]
    preferred = next((r for r in emails if r.get("Primary")), emails[0] if emails else None)
    if not preferred:
        raise SyncError("identity_store_user_has_no_email")
    return preferred["Value"]


def _upsert(uid, username=None):
    email = _current_email(uid)
    desired = {"email": email, "email_verified": "true", IDC_USER_ID_ATTR: uid}
    record = _get_owned(uid, username)
    if record is None:
        try:
            _cognito.admin_create_user(
                UserPoolId=USER_POOL_ID, Username=uid,
                UserAttributes=[{"Name": k, "Value": v} for k, v in desired.items()],
                DesiredDeliveryMediums=["EMAIL"],
            )
            log("created", user_id=uid)
            return
        except _cognito.exceptions.UsernameExistsException:
            # A duplicate invocation may have created the same managed user.
            record = _get_owned(uid)
        except _cognito.exceptions.AliasExistsException:
            # Only adopt a legacy username proven to belong to this IDC user.
            record = _find_legacy(uid)
        if record is None:
            raise SyncError("cognito_identity_or_email_alias_collision")
    # A failed email migration must not leave a user authenticating as their
    # old Quick identity. Suspend first, then update and re-enable on success.
    if _attributes(record).get("email") != email and record.get("Enabled", False):
        _cognito.admin_disable_user(UserPoolId=USER_POOL_ID, Username=record["Username"])
        record = {**record, "Enabled": False}
    changed = [{"Name": k, "Value": v} for k, v in desired.items() if _attributes(record).get(k) != v]
    if changed:
        _cognito.admin_update_user_attributes(UserPoolId=USER_POOL_ID, Username=record["Username"], UserAttributes=changed)
    if not record.get("Enabled", False):
        _cognito.admin_enable_user(UserPoolId=USER_POOL_ID, Username=record["Username"])
    log("member_reconciled", user_id=uid, attributes_updated=bool(changed))


def _remove(uid, username=None, allow_delete=False):
    record = _get_owned(uid, username)
    # Legacy users are included with their username by the scheduled scan.
    if record is None:
        log("absent", user_id=uid)
        return
    try:
        if allow_delete and ON_REMOVE == "delete":
            _cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=record["Username"])
            log("deleted", user_id=uid)
        else:
            if record.get("Enabled", False):
                _cognito.admin_disable_user(UserPoolId=USER_POOL_ID, Username=record["Username"])
            log("disabled", user_id=uid)
    except _cognito.exceptions.UserNotFoundException:
        log("already_absent", user_id=uid)


def reconcile(job):
    if job.get("identityStoreId") != IDENTITY_STORE_ID or job.get("groupId") != WATCHED_GROUP_ID:
        log("ignored_stale_configuration")
        return
    uid = user_id(job.get("userId"))
    mode = job.get("mode", "reconcile")
    if mode not in {"reconcile", "remove", "delete"}:
        raise SyncError("invalid_job_mode")
    username = job.get("cognitoUsername")
    if username is not None and (not isinstance(username, str) or not username):
        raise SyncError("invalid_cognito_username")
    if _is_member(uid):
        _upsert(uid, username)
    else:
        # A scan may observe temporary directory inconsistency. Disable safely;
        # never destructively delete an account based solely on a scan result.
        _remove(uid, username, allow_delete=mode in {"remove", "delete"})


def handler(event, context):
    if isinstance(event.get("Records"), list):
        failed_groups, failures = set(), []
        for record in event["Records"]:
            group = (record.get("attributes") or {}).get("MessageGroupId")
            try:
                if group in failed_groups:
                    failures.append({"itemIdentifier": record["messageId"]})
                    continue
                if record.get("eventSource") != "aws:sqs":
                    raise SyncError("unexpected_event_source")
                reconcile(json.loads(record["body"]))
            except Exception as error:
                failed_groups.add(group)
                failures.append({"itemIdentifier": record["messageId"]})
                code = error.response.get("Error", {}).get("Code", "sdk_error") if isinstance(error, ClientError) else type(error).__name__
                log("failed", error_code=code, request_id=getattr(context, "aws_request_id", None))
        return {"batchItemFailures": failures}
    # Backward-compatible handling while an existing stack's EventBridge targets
    # transition to the coordinator. Failed/wrong-store/group events are ignored.
    parsed = parse_directory_event(event, IDENTITY_STORE_ID, WATCHED_GROUP_ID)
    if not parsed:
        return {"status": "ignored"}
    try:
        reconcile({**parsed, "identityStoreId": IDENTITY_STORE_ID, "groupId": WATCHED_GROUP_ID})
    except Exception as error:
        reason = str(error) if isinstance(error, SyncError) else type(error).__name__
        log("failed", error_code=reason, request_id=getattr(context, "aws_request_id", None))
        raise SyncError(reason) from None
    return {"status": "ok"}
