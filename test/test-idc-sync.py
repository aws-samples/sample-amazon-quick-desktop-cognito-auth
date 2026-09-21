#!/usr/bin/env python3
"""IDC behavior tests using real AWS SDK models with stubbed service responses."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.update(AWS_DEFAULT_REGION='us-east-1', AWS_REGION='us-east-1', AWS_ACCESS_KEY_ID='testing',
                  AWS_SECRET_ACCESS_KEY='testing', AWS_SESSION_TOKEN='testing', AWS_EC2_METADATA_DISABLED='true',
                  USER_POOL_ID='us-east-1_example', IDENTITY_STORE_ID='d-1234567890',
                  WATCHED_GROUP_ID='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', ON_REMOVE='delete',
                  WORK_QUEUE_URL='https://sqs.us-east-1.amazonaws.com/123456789012/work.fifo')
from botocore.stub import Stubber

ROOT=Path(__file__).resolve().parent.parent/'lambda/idc-sync'
sys.path.insert(0,str(ROOT))
from common import IDC_USER_ID_ATTR, SyncError, parse_directory_event
UID='11111111-1111-4111-8111-111111111111'
SECOND='22222222-2222-4222-8222-222222222222'
THIRD='33333333-3333-4333-8333-333333333333'
STORE=os.environ['IDENTITY_STORE_ID']; GROUP=os.environ['WATCHED_GROUP_ID']; POOL=os.environ['USER_POOL_ID']
CTX=types.SimpleNamespace(aws_request_id='request-id',get_remaining_time_in_millis=lambda:300000)


def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file);module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module);return module


def record(uid=UID,email='old@example.com',enabled=True,owned=True,username=None):
    attrs=[{'Name':'email','Value':email},{'Name':'email_verified','Value':'true'}]
    if owned:attrs.append({'Name':IDC_USER_ID_ATTR,'Value':uid})
    return {'Username':username or uid,'UserAttributes':attrs,'Enabled':enabled,'UserStatus':'CONFIRMED'}


def job(mode='reconcile',uid=UID,**extra):return {'userId':uid,'mode':mode,'identityStoreId':STORE,'groupId':GROUP,**extra}


def member(stubber,exists=True,uid=UID):
    stubber.add_response('is_member_in_groups',{'Results':[{'GroupId':GROUP,'MemberId':{'UserId':uid},'MembershipExists':exists}]},
                        {'IdentityStoreId':STORE,'GroupIds':[GROUP],'MemberId':{'UserId':uid}})


def describe(stubber,email='old@example.com',uid=UID):
    stubber.add_response('describe_user',{'UserId':uid,'IdentityStoreId':STORE,'UserName':'person','Emails':[{'Value':email,'Primary':True}]},
                        {'IdentityStoreId':STORE,'UserId':uid})


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.m=load('test_worker','handler.py');self.a=Stubber(self.m._identitystore);self.c=Stubber(self.m._cognito)
        self.a.activate();self.c.activate();self.output=io.StringIO();self.redirect=contextlib.redirect_stdout(self.output);self.redirect.__enter__()
    def tearDown(self):
        self.redirect.__exit__(None,None,None);self.a.assert_no_pending_responses();self.c.assert_no_pending_responses();self.a.deactivate();self.c.deactivate()

    def test_explicit_removal_uses_uid_without_describing_or_matching_email(self):
        member(self.a,False)
        self.c.add_response('admin_get_user',record(email='old-address@example.com'),{'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_delete_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job('remove'))
        self.assertNotIn('old-address',self.output.getvalue())

    def test_deleted_directory_user_can_be_disabled_without_describe_user(self):
        self.a.add_client_error('is_member_in_groups','ResourceNotFoundException',expected_params={'IdentityStoreId':STORE,'GroupIds':[GROUP],'MemberId':{'UserId':UID}})
        self.c.add_response('admin_get_user',record(),{'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_disable_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job())

    def test_scan_never_deletes_even_when_delete_is_configured(self):
        member(self.a,False)
        self.c.add_response('admin_get_user',record(),{'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_disable_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job())

    def test_delayed_add_does_not_enable_a_non_member(self):
        member(self.a,False)
        self.c.add_response('admin_get_user',record(enabled=False),{'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job())

    def test_delayed_removal_does_not_remove_a_current_member(self):
        member(self.a,True);describe(self.a)
        self.c.add_response('admin_get_user',record(),{'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job('remove'))

    def test_email_change_suspends_then_updates_then_enables(self):
        member(self.a);describe(self.a,'new@example.com')
        self.c.add_response('admin_get_user',record(),{'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_disable_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_update_user_attributes',{}, {'UserPoolId':POOL,'Username':UID,'UserAttributes':[{'Name':'email','Value':'new@example.com'}]})
        self.c.add_response('admin_enable_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.m.reconcile(job())
        self.assertNotIn('new@example.com',self.output.getvalue());self.assertNotIn('old@example.com',self.output.getvalue())

    def test_failed_email_change_remains_disabled_and_retries_item(self):
        member(self.a);describe(self.a,'new@example.com')
        self.c.add_response('admin_get_user',record(),{'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_disable_user',{}, {'UserPoolId':POOL,'Username':UID})
        self.c.add_client_error('admin_update_user_attributes','AliasExistsException','PRIVATE_ERROR_SENTINEL',expected_params={'UserPoolId':POOL,'Username':UID,'UserAttributes':[{'Name':'email','Value':'new@example.com'}]})
        result=self.m.handler({'Records':[{'eventSource':'aws:sqs','messageId':'one','attributes':{'MessageGroupId':'user'},'body':json.dumps(job())}]},CTX)
        self.assertEqual(result,{'batchItemFailures':[{'itemIdentifier':'one'}]});self.assertNotIn('PRIVATE_ERROR_SENTINEL',self.output.getvalue())

    def test_new_member_receives_invitation_and_managed_identity_marker(self):
        member(self.a);describe(self.a)
        self.c.add_client_error('admin_get_user','UserNotFoundException',expected_params={'UserPoolId':POOL,'Username':UID})
        self.c.add_response('admin_create_user',{}, {'UserPoolId':POOL,'Username':UID,'UserAttributes':[
            {'Name':'email','Value':'old@example.com'},{'Name':'email_verified','Value':'true'},{'Name':IDC_USER_ID_ATTR,'Value':UID}], 'DesiredDeliveryMediums':['EMAIL']})
        self.m.reconcile(job())

    def test_username_collision_cannot_adopt_an_unmanaged_account(self):
        member(self.a);describe(self.a)
        self.c.add_response('admin_get_user',record(owned=False),{'UserPoolId':POOL,'Username':UID})
        with self.assertRaisesRegex(SyncError,'not_owned'):self.m.reconcile(job())

    def test_stale_job_for_previous_group_is_ignored(self):
        self.m.reconcile(job(groupId=SECOND))
        self.assertIn('ignored_stale_configuration',self.output.getvalue())

    def test_fifo_failure_does_not_process_later_item_for_same_user(self):
        records=[{'eventSource':'aws:sqs','messageId':str(i),'attributes':{'MessageGroupId':'same'},'body':'PRIVATE_BAD_JSON'} for i in range(2)]
        result=self.m.handler({'Records':records},CTX)
        self.assertEqual(len(result['batchItemFailures']),2);self.assertNotIn('PRIVATE_BAD_JSON',self.output.getvalue())


class EventTests(unittest.TestCase):
    def event(self,name='AddMemberToGroup',source='sso-directory.amazonaws.com',**params):
        return {'detail':{'eventName':name,'eventSource':source,'requestParameters':{'groupId':GROUP,'member':{'memberId':UID},**params}}}
    def test_legacy_member_shape(self):self.assertEqual(parse_directory_event(self.event(),STORE,GROUP)['userId'],UID)
    def test_modern_member_shape(self):
        e=self.event('CreateGroupMembership','identitystore.amazonaws.com',identityStoreId=STORE,member=None,memberId={'userId':UID})
        self.assertEqual(parse_directory_event(e,STORE,GROUP)['userId'],UID)
    def test_failed_operation_does_not_provision(self):
        e=self.event();e['detail']['errorCode']='AccessDenied';self.assertIsNone(parse_directory_event(e,STORE,GROUP))
    def test_wrong_group_and_store_do_not_provision(self):
        self.assertIsNone(parse_directory_event(self.event(groupId=SECOND),STORE,GROUP))
        self.assertIsNone(parse_directory_event(self.event(identityStoreId='d-0000000000'),STORE,GROUP))
    def test_missing_group_is_not_treated_as_authorized(self):
        with self.assertRaisesRegex(SyncError,'missing_group'):parse_directory_event(self.event(groupId=None),STORE,GROUP)
    def test_api_delete_user_requires_expected_store(self):
        e=self.event('DeleteUser','identitystore.amazonaws.com',identityStoreId=STORE,userId=UID)
        self.assertEqual(parse_directory_event(e,STORE,GROUP)['mode'],'delete')
        del e['detail']['requestParameters']['identityStoreId'];self.assertIsNone(parse_directory_event(e,STORE,GROUP))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.m=load('test_coordinator','reconcile.py');self.c=Stubber(self.m._idp);self.a=Stubber(self.m._store)
        self.c.activate();self.a.activate();self.output=io.StringIO();self.redirect=contextlib.redirect_stdout(self.output);self.redirect.__enter__()
    def tearDown(self):
        self.redirect.__exit__(None,None,None);self.c.assert_no_pending_responses();self.a.assert_no_pending_responses();self.c.deactivate();self.a.deactivate()
    def cognito(self,users):
        self.c.add_response('list_users',{'Users':users},{'UserPoolId':POOL})
    def managed(self,uid,username=None):return {'Username':username or uid,'Attributes':[{'Name':IDC_USER_ID_ATTR,'Value':uid}]}
    def test_existing_members_and_stale_managed_users_are_queued_but_manual_users_are_not(self):
        self.cognito([self.managed(UID,'legacy@example.com'),self.managed(THIRD),{'Username':'manual-user','Attributes':[]}])
        self.a.add_response('list_group_memberships',{'GroupMemberships':[{'IdentityStoreId':STORE,'GroupId':GROUP,'MembershipId':SECOND,'MemberId':{'UserId':uid}} for uid in [UID,SECOND]]}, {'IdentityStoreId':STORE,'GroupId':GROUP})
        with patch.object(self.m._sqs,'send_message_batch',return_value={'Successful':[]}) as send:
            self.assertEqual(self.m.scan(CTX,'run'),{'queued':3})
        jobs=[json.loads(v['MessageBody']) for v in send.call_args.kwargs['Entries']]
        self.assertEqual({v['userId'] for v in jobs},{UID,SECOND,THIRD})
        self.assertEqual(next(v for v in jobs if v['userId']==UID)['cognitoUsername'],'legacy@example.com')
        self.assertTrue(all(v['mode']=='reconcile' for v in jobs));self.assertNotIn('legacy@example.com',self.output.getvalue())
    def test_deleted_group_still_queues_managed_users_for_safe_disable(self):
        self.cognito([self.managed(UID)])
        self.a.add_client_error('list_group_memberships','ResourceNotFoundException',expected_params={'IdentityStoreId':STORE,'GroupId':GROUP})
        with patch.object(self.m._sqs,'send_message_batch',return_value={'Successful':[]}) as send:self.m.scan(CTX,'run')
        self.assertEqual(json.loads(send.call_args.kwargs['Entries'][0]['MessageBody'])['mode'],'reconcile')
    def test_partial_sqs_failure_is_not_reported_as_success(self):
        with patch.object(self.m._sqs,'send_message_batch',return_value={'Failed':[{'Id':'0'}]}):
            with self.assertRaisesRegex(SyncError,'partially_failed'):self.m._send([job()],'run')
    def test_different_events_for_same_user_are_not_deduplicated(self):
        with patch.object(self.m._sqs,'send_message_batch',return_value={}) as send:
            self.m._send([job()],'event-one');self.m._send([job()],'event-two')
        first,second=[call.kwargs['Entries'][0] for call in send.call_args_list]
        self.assertEqual(first['MessageGroupId'],second['MessageGroupId']);self.assertNotEqual(first['MessageDeduplicationId'],second['MessageDeduplicationId'])


class ResolverTests(unittest.TestCase):
    def setUp(self):self.m=load('test_resolver','custom_resource.py');self.s=Stubber(self.m._sso_admin);self.s.activate()
    def tearDown(self):self.s.assert_no_pending_responses();self.s.deactivate()
    def instance(self,suffix,store):return {'InstanceArn':'arn:aws:sso:::instance/ssoins-'+suffix,'IdentityStoreId':store,'PrimaryRegion':'us-east-1'}
    def test_multiple_instances_require_explicit_selection(self):
        self.s.add_response('list_instances',{'Instances':[self.instance('1111111111111111',STORE),self.instance('2222222222222222','d-0000000000')]},{})
        with self.assertRaisesRegex(self.m.ResolveError,'Multiple'):self.m._resolve_instance()
    def test_pagination_and_explicit_selection(self):
        first=self.instance('1111111111111111',STORE);second=self.instance('2222222222222222','d-0000000000')
        self.s.add_response('list_instances',{'Instances':[first],'NextToken':'next'},{})
        self.s.add_response('list_instances',{'Instances':[second]},{'NextToken':'next'})
        self.assertEqual(self.m._resolve_instance(second['InstanceArn']),(second['InstanceArn'],second['IdentityStoreId']))
    def test_wrong_region_fails_before_creating_group(self):
        instance=self.instance('1111111111111111',STORE);instance['PrimaryRegion']='us-west-2'
        self.s.add_response('list_instances',{'Instances':[instance]},{})
        with self.assertRaisesRegex(self.m.ResolveError,'primary region'):self.m._resolve_instance()


if __name__=='__main__':unittest.main(verbosity=2)
