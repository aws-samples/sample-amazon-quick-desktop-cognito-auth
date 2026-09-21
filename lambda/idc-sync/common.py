"""Shared event parsing for group-based provisioning. No SDK calls or credentials."""
import json
import re

IDC_USER_ID_ATTR = "custom:idc_user_id"
USER_ID = re.compile(r"(?:[0-9a-f]{10}-)?[A-Fa-f0-9]{8}-(?:[A-Fa-f0-9]{4}-){3}[A-Fa-f0-9]{12}\Z")


class SyncError(Exception):
    pass


def user_id(value):
    if isinstance(value, dict):
        value = value.get("userId") or value.get("UserId") or value.get("memberId")
    if not isinstance(value, str) or not USER_ID.fullmatch(value):
        raise SyncError("missing_or_invalid_user_id")
    return value


def log(action, **fields):
    # IDs/status only; never log emails, raw events, SDK exception messages or tokens.
    print(json.dumps({"event": "idc_sync", "action": action, **fields}, separators=(",", ":")))


def parse_directory_event(event, store_id, group_id):
    detail = event.get("detail") or {}
    if detail.get("errorCode") or detail.get("errorMessage"):
        return None
    source = detail.get("eventSource")
    if source not in {"sso-directory.amazonaws.com", "identitystore.amazonaws.com"}:
        return None
    params = detail.get("requestParameters") or {}
    supplied_store = params.get("identityStoreId")
    if supplied_store and supplied_store != store_id:
        return None
    if source == "identitystore.amazonaws.com" and supplied_store != store_id:
        return None
    name = detail.get("eventName")
    if name == "DeleteUser":
        return {"userId": user_id(params.get("userId") or params.get("userIdentifier")), "mode": "delete"}
    if name not in {"AddMemberToGroup", "RemoveMemberFromGroup", "CreateGroupMembership"}:
        return None
    if not params.get("groupId"):
        raise SyncError("membership_event_missing_group_id")
    if params["groupId"] != group_id:
        return None
    member = params.get("member") or params.get("memberId")
    return {"userId": user_id(member), "mode": "remove" if name == "RemoveMemberFromGroup" else "reconcile"}
