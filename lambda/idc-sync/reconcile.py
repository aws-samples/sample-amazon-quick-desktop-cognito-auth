"""Queue bounded per-user work from a periodic directory snapshot or fast event.

Membership/user changes made through the console, APIs or SCIM converge through
this snapshot path. The worker rechecks current membership before changing users;
the snapshot itself never deletes Cognito users or decides authorization.
"""
import hashlib
import json
import os
import uuid

import boto3
from botocore.config import Config
from common import IDC_USER_ID_ATTR, SyncError, log, parse_directory_event, user_id

STORE = os.environ["IDENTITY_STORE_ID"]
GROUP = os.environ["WATCHED_GROUP_ID"]
POOL = os.environ["USER_POOL_ID"]
QUEUE = os.environ["WORK_QUEUE_URL"]
CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=3, read_timeout=10)
_idp = boto3.client("cognito-idp", config=CONFIG)
_store = boto3.client("identitystore", config=CONFIG)
_sqs = boto3.client("sqs", config=CONFIG)


def _guard_time(context):
    if context.get_remaining_time_in_millis() < 20000:
        raise SyncError("reconciliation_scan_exceeded_budget")


def _send(jobs, run_id):
    entries = []
    for i, job in enumerate(jobs):
        uid = user_id(job["userId"])
        body = {**job, "identityStoreId": STORE, "groupId": GROUP}
        entries.append({
            "Id": str(i), "MessageBody": json.dumps(body, sort_keys=True),
            "MessageGroupId": hashlib.sha256(uid.encode()).hexdigest(),
            # Different source events must not be deduplicated merely because
            # they affect the same user (e.g. add followed immediately by remove).
            "MessageDeduplicationId": hashlib.sha256((run_id + json.dumps(body, sort_keys=True)).encode()).hexdigest(),
        })
    result = _sqs.send_message_batch(QueueUrl=QUEUE, Entries=entries)
    if result.get("Failed"):
        raise SyncError("enqueue_partially_failed")


def scan(context, run_id):
    managed, members = {}, set()
    for page in _idp.get_paginator("list_users").paginate(UserPoolId=POOL):
        _guard_time(context)
        for record in page.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in record.get("Attributes", [])}
            uid = attrs.get(IDC_USER_ID_ATTR)
            if not uid:
                continue  # Never adopt or revoke manually managed accounts.
            user_id(uid)
            if uid in managed and managed[uid] != record["Username"]:
                raise SyncError("duplicate_managed_identity")
            managed[uid] = record["Username"]
    try:
        for page in _store.get_paginator("list_group_memberships").paginate(IdentityStoreId=STORE, GroupId=GROUP):
            _guard_time(context)
            for membership in page.get("GroupMemberships", []):
                members.add(user_id(membership.get("MemberId")))
    except _store.exceptions.ResourceNotFoundException:
        # A deleted source group no longer grants access. Workers recheck state
        # individually and only disable on scan jobs; there is no bulk deletion.
        members.clear()
        log("source_group_absent")
    jobs = []
    count = 0
    for uid in sorted(members | managed.keys()):
        _guard_time(context)
        job = {"userId": uid, "mode": "reconcile"}
        if uid in managed:
            job["cognitoUsername"] = managed[uid]
        jobs.append(job)
        if len(jobs) == 10:
            _send(jobs, run_id); count += len(jobs); jobs = []
    if jobs:
        _send(jobs, run_id); count += len(jobs)
    log("scan_queued", users=count)
    return {"queued": count}


def handler(event, context):
    run_id = str(event.get("id") or (event.get("detail") or {}).get("eventID") or uuid.uuid4())
    try:
        if event.get("operation") == "reconcile":
            return scan(context, run_id)
        job = parse_directory_event(event, STORE, GROUP)
        if not job:
            return {"status": "ignored"}
        _send([job], run_id)
        return {"queued": 1}
    except Exception as error:
        log("coordinator_failed", error_type=type(error).__name__, request_id=getattr(context, "aws_request_id", None))
        reason = str(error) if isinstance(error, SyncError) else type(error).__name__
        raise SyncError(reason) from None
