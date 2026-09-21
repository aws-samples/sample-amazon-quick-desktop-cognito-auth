#!/usr/bin/env python3
"""Credential-free behavior/privacy tests; optionally exercise the CFN inline copies.

python3.12 test/test-oidc-safeguards.py --workshop-template /path/to/template.yaml
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
ISSUER = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example'
DOMAIN = 'https://example.auth.us-east-1.amazoncognito.com'
CLIENT = 'example-client'
CTX = types.SimpleNamespace(aws_request_id='lambda-request-id', get_remaining_time_in_millis=lambda: 180000)


def inline(template, name):
    lines = template.read_text().splitlines()
    start = lines.index('  ' + name + ':')
    start = next(i for i in range(start, len(lines)) if lines[i] == '        ZipFile: |') + 1
    block = []
    for line in lines[start:]:
        if line and not line.startswith('          '):
            break
        block.append(line[10:] if line else '')
    return '\n'.join(block).rstrip() + '\n'


def load(name, source):
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, '<' + name + '>', 'exec'), module.__dict__)
    return module


def jwt(payload):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b'=').decode()
    # Deliberately unsigned: logging must label its checks as decoding, not verification.
    return enc({'alg': 'RS256', 'kid': 'public-key-id'}) + '.' + enc(payload) + '.fake-signature'


def tokens(**identity_overrides):
    identity = {'iss': ISSUER, 'aud': CLIENT, 'token_use': 'id', 'exp': int(time.time()) + 3600,
                'email': 'PRIVATE_EMAIL_SENTINEL', 'sub': 'PRIVATE_SUBJECT_SENTINEL'}
    identity.update(identity_overrides)
    access = {'iss': ISSUER, 'client_id': CLIENT, 'token_use': 'access', 'scope': 'openid email profile'}
    return {'id_token': jwt(identity), 'access_token': jwt(access), 'refresh_token': 'PRIVATE_REFRESH_SENTINEL'}


class Response:
    status = 200
    headers = {'x-amz-cognito-request-id': 'cognito-request-id'}

    def __init__(self, body): self.body = body
    def read(self, *args): return self.body
    def __enter__(self): return self
    def __exit__(self, *args): return False


def event(body='', path='/oauth2/token', method='POST'):
    return {'path': path, 'httpMethod': method, 'body': body,
            'headers': {'Authorization': 'Basic PRIVATE_AUTHORIZATION_SENTINEL'},
            'requestContext': {'requestId': 'api-request-id'}}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'COGNITO_DOMAIN': DOMAIN, 'COGNITO_ISSUER': ISSUER, 'COGNITO_CLIENT_ID': CLIENT})
        self.env.start()
    def tearDown(self): self.env.stop()

    def test_token_bytes_and_secrets_preserved_on_wire_but_not_logged(self):
        values = tokens()
        body = json.dumps(values, ensure_ascii=False, indent=2).encode()
        for name, source in PROXIES:
            with self.subTest(implementation=name):
                proxy = load(name, source);output = io.StringIO();seen = []
                def upstream(request, timeout):
                    seen.append(request)
                    return Response(body)
                request = event('grant_type=authorization_code&code=PRIVATE_CODE_SENTINEL&code_verifier=PRIVATE_VERIFIER_SENTINEL&scope=openid+email+profile+offline_access')
                with patch.object(proxy.urllib.request, 'urlopen', upstream), contextlib.redirect_stdout(output):
                    response = proxy.handler(request, CTX)
                self.assertEqual(response['body'].encode(), body)
                self.assertNotIn('offline_access', seen[0].data.decode())
                self.assertIn('PRIVATE_CODE_SENTINEL', seen[0].data.decode())
                self.assertEqual(response['headers']['Cache-Control'], 'no-store')
                self.assertEqual(response['headers']['Pragma'], 'no-cache')
                self.assertEqual(response['headers']['X-Quick-Auth-Request-Id'], 'api-request-id')
                record = json.loads(output.getvalue())
                self.assertEqual(record['cognito_request_id'], 'cognito-request-id')
                self.assertEqual(record['api_request_id'], 'api-request-id')
                self.assertTrue(record['contract_ok'])
                self.assertTrue(record['claims_decoded_only'])
                self.assertEqual(record['id_token_fp'], hashlib.sha256(values['id_token'].encode()).hexdigest()[-8:])
                for secret in [*values.values(), 'PRIVATE_EMAIL_SENTINEL', 'PRIVATE_SUBJECT_SENTINEL', 'PRIVATE_CODE_SENTINEL', 'PRIVATE_VERIFIER_SENTINEL', 'PRIVATE_AUTHORIZATION_SENTINEL']:
                    self.assertNotIn(secret, output.getvalue())

    def test_bad_claims_are_diagnostic_not_token_rewriting(self):
        for name, source in PROXIES:
            for override, flag in [({'iss': 'https://wrong.example'}, 'id_issuer_matches'), ({'aud': 'wrong'}, 'id_audience_matches'), ({'exp': 1}, 'id_unexpired'), ({'email': ''}, 'id_email_present')]:
                with self.subTest(implementation=name, flag=flag):
                    proxy = load(name, source);body = json.dumps(tokens(**override)).encode();output = io.StringIO()
                    with patch.object(proxy.urllib.request, 'urlopen', return_value=Response(body)), contextlib.redirect_stdout(output):
                        result = proxy.handler(event(), CTX)
                    self.assertEqual(result['statusCode'], 200)
                    self.assertEqual(result['body'].encode(), body)
                    record = json.loads(output.getvalue());self.assertFalse(record[flag]);self.assertFalse(record['contract_ok'])

    def test_malformed_token_response_never_masks_upstream_response(self):
        for name, source in PROXIES:
            for body in [b'not-json', b'[]', b'{"id_token":"malformed", "access_token":null}']:
                with self.subTest(implementation=name, body=body):
                    proxy=load(name, source);output=io.StringIO()
                    with patch.object(proxy.urllib.request, 'urlopen', return_value=Response(body)), contextlib.redirect_stdout(output):
                        result=proxy.handler(event(), CTX)
                    self.assertEqual(result['body'].encode(),body);self.assertEqual(result['statusCode'],200)
                    self.assertFalse(json.loads(output.getvalue())['contract_ok'])

    def test_errors_keep_body_and_no_store_without_logging_error_description(self):
        body=b'{"error":"invalid_grant","error_description":"PRIVATE_ERROR_SENTINEL"}'
        for name, source in PROXIES:
            with self.subTest(implementation=name):
                proxy=load(name,source);output=io.StringIO()
                error=urllib.error.HTTPError(DOMAIN+'/oauth2/token',400,'error',{'x-amz-cognito-request-id':'cognito-error-id'},io.BytesIO(body))
                with patch.object(proxy.urllib.request,'urlopen',side_effect=error),contextlib.redirect_stdout(output):
                    result=proxy.handler(event(),CTX)
                self.assertEqual(result['body'].encode(),body);self.assertEqual(result['statusCode'],400)
                self.assertEqual(result['headers']['Cache-Control'],'no-store')
                self.assertEqual(json.loads(output.getvalue())['oauth_error'],'invalid_grant')
                self.assertNotIn('PRIVATE_ERROR_SENTINEL',output.getvalue())

    def test_network_and_internal_errors_do_not_log_exception_messages(self):
        for name,source in PROXIES:
            for error,status in [(urllib.error.URLError('PRIVATE_NETWORK_SENTINEL'),502),(ValueError('PRIVATE_INTERNAL_SENTINEL'),500),(TimeoutError('PRIVATE_TIMEOUT_SENTINEL'),502)]:
                with self.subTest(implementation=name,error=type(error).__name__):
                    proxy=load(name,source);output=io.StringIO()
                    with patch.object(proxy.urllib.request,'urlopen',side_effect=error),contextlib.redirect_stdout(output):
                        result=proxy.handler(event(),CTX)
                    self.assertEqual(result['statusCode'],status)
                    self.assertNotIn('PRIVATE_',output.getvalue())

    def test_authorize_logs_no_state_nonce_or_redirect_url(self):
        for name,source in PROXIES:
            with self.subTest(implementation=name):
                proxy=load(name,source);request=event(path='/oauth2/authorize',method='GET');output=io.StringIO()
                request['queryStringParameters']={'scope':'openid profile email offline_access','state':'PRIVATE_STATE_SENTINEL','nonce':'PRIVATE_NONCE_SENTINEL','redirect_uri':'http://localhost:18080'}
                with contextlib.redirect_stdout(output):result=proxy.handler(request,CTX)
                self.assertEqual(result['statusCode'],302)
                self.assertIn('PRIVATE_STATE_SENTINEL',result['headers']['Location'])
                self.assertNotIn('PRIVATE_',output.getvalue());self.assertNotIn('localhost',output.getvalue())
                self.assertTrue(json.loads(output.getvalue())['openid_requested'])
                self.assertEqual(result['headers']['Cache-Control'],'no-store')


class Clock:
    now=0
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds


def discovery(domain=DOMAIN):
    return {'issuer':ISSUER,'jwks_uri':ISSUER+'/.well-known/jwks.json','authorization_endpoint':domain+'/oauth2/authorize',
            'token_endpoint':domain+'/oauth2/token','userinfo_endpoint':domain+'/oauth2/userInfo','scopes_supported':['openid','email','profile']}


class ReadinessTests(unittest.TestCase):
    def test_transient_metadata_unavailability_recovers_with_bounded_retry(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);clock=Clock()
                documents=[check.NotReady('metadata_unavailable'),discovery(),{'keys':[{'kid':'key','kty':'RSA','n':'modulus','e':'AQAB'}]}]
                with patch.object(check,'_json',side_effect=documents),patch.object(check.time,'monotonic',clock.monotonic),patch.object(check.time,'sleep',clock.sleep):
                    attempts=check.wait_until_ready(ISSUER,DOMAIN,12)
                self.assertEqual(attempts,2);self.assertEqual(clock.now,5)

    def test_empty_jwks_fails_within_budget(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);clock=Clock()
                def document(url,timeout):return {'keys':[]} if url.endswith('jwks.json') else discovery()
                with patch.object(check,'_json',side_effect=document),patch.object(check.time,'monotonic',clock.monotonic),patch.object(check.time,'sleep',clock.sleep):
                    with self.assertRaisesRegex(check.NotReady,'signing_keys_missing'):check.wait_until_ready(ISSUER,DOMAIN,12)
                self.assertEqual(clock.now,12)

    def test_wrong_issuer_or_jwks_is_rejected_before_following_untrusted_url(self):
        for name,source in READINESS:
            for field in ['issuer','jwks_uri']:
                with self.subTest(implementation=name,field=field):
                    check=load(name,source);doc=discovery();doc[field]='https://untrusted.example'
                    with patch.object(check,'_json',return_value=doc) as fetch:
                        with self.assertRaisesRegex(check.NotReady,field+'_mismatch'):check.check_once(ISSUER,DOMAIN,time.monotonic()+5)
                    self.assertEqual(fetch.call_count,1)

    def test_custom_domain_metadata_does_not_require_proxy_same_origin(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source)
                with patch.object(check,'_json',side_effect=[discovery('https://login.example.com'),{'keys':[{'kid':'key','kty':'RSA','n':'modulus','e':'AQAB'}]}]):
                    check.check_once(ISSUER,DOMAIN,time.monotonic()+5)

    def test_userinfo_metadata_is_not_a_desktop_readiness_dependency(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);document=discovery();del document['userinfo_endpoint']
                with patch.object(check,'_json',side_effect=[document,{'keys':[{'kid':'key','kty':'RSA','n':'modulus','e':'AQAB'}]}]):
                    check.check_once(ISSUER,DOMAIN,time.monotonic()+5)

    def test_delete_never_checks_endpoints(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);output=io.StringIO()
                with patch.object(check,'wait_until_ready') as wait,patch.object(check,'_send_response') as send,contextlib.redirect_stdout(output):
                    check.handler({'RequestType':'Delete','ResourceProperties':{}},CTX)
                wait.assert_not_called();self.assertEqual(send.call_args.args[2],'SUCCESS')

    def test_failed_readiness_sends_cfn_failure_without_sensitive_details(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);output=io.StringIO()
                with patch.object(check,'wait_until_ready',side_effect=ValueError('PRIVATE_CONFIG_SENTINEL')),patch.object(check,'_send_response') as send,contextlib.redirect_stdout(output):
                    check.handler({'RequestType':'Create','ResourceProperties':{'IssuerUrl':ISSUER,'CognitoDomain':DOMAIN}},CTX)
                self.assertEqual(send.call_args.args[2],'FAILED');self.assertNotIn('PRIVATE_',output.getvalue())
                self.assertNotIn('PRIVATE_',send.call_args.args[3])

    def test_success_reports_ready_and_uses_bounded_budget(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);output=io.StringIO()
                request={'RequestType':'Create','ResourceProperties':{'IssuerUrl':ISSUER,'CognitoDomain':DOMAIN}}
                with patch.object(check,'wait_until_ready',return_value=2) as wait,patch.object(check,'_send_response') as send,contextlib.redirect_stdout(output):
                    check.handler(request,CTX)
                self.assertEqual(send.call_args.args[2],'SUCCESS')
                self.assertEqual(send.call_args.args[4],{'Status':'READY','Attempts':2})
                self.assertLess(wait.call_args.args[2],180)

    def test_response_delivery_retries_without_logging_presigned_url(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);output=io.StringIO()
                request={'RequestType':'Delete','StackId':'stack','RequestId':'request','LogicalResourceId':'Readiness','ResponseURL':'https://s3.example/PRIVATE_CALLBACK_SIGNATURE'}
                with patch.object(check.urllib.request,'urlopen',side_effect=[urllib.error.URLError('PRIVATE_NETWORK_SENTINEL'),Response(b'')]) as send,patch.object(check.time,'sleep'),contextlib.redirect_stdout(output):
                    check.handler(request,CTX)
                self.assertEqual(send.call_count,2)
                self.assertEqual(send.call_args_list[0].args[0].data,send.call_args_list[1].args[0].data)
                self.assertNotIn('PRIVATE_',output.getvalue())

    def test_cfn_callback_is_sent_on_delete_without_properties(self):
        for name,source in READINESS:
            with self.subTest(implementation=name):
                check=load(name,source);calls=[];output=io.StringIO()
                def reply(request,timeout):calls.append(request);return Response(b'')
                request={'RequestType':'Delete','StackId':'stack','RequestId':'request','LogicalResourceId':'Readiness','ResponseURL':'https://s3.example/PRIVATE_CALLBACK_SIGNATURE'}
                with patch.object(check.urllib.request,'urlopen',side_effect=reply),contextlib.redirect_stdout(output):check.handler(request,CTX)
                self.assertEqual(len(calls),1);self.assertEqual(calls[0].method,'PUT')
                self.assertEqual(json.loads(calls[0].data)['Status'],'SUCCESS');self.assertNotIn('PRIVATE_',output.getvalue())


parser=argparse.ArgumentParser();parser.add_argument('--workshop-template',type=Path);args=parser.parse_args()
PROXIES=[('cdk_proxy',(ROOT/'lambda/auth-proxy/index.py').read_text())]
READINESS=[('cdk_readiness',(ROOT/'lambda/oidc-readiness/index.py').read_text())]
if args.workshop_template:
    assert len(args.workshop_template.read_bytes()) <= 51200, 'Workshop template exceeds the inline deployment limit'
    PROXIES.append(('cfn_proxy',inline(args.workshop_template,'AuthProxyFunction')))
    READINESS.append(('cfn_readiness',inline(args.workshop_template,'OidcReadinessFunction')))
    assert READINESS[0][1].strip()==READINESS[1][1].strip(), 'Readiness implementations drifted'
    helpers=lambda source:source.split('# BEGIN SAFE_DIAGNOSTICS')[1].split('# END SAFE_DIAGNOSTICS')[0]
    assert helpers(PROXIES[0][1])==helpers(PROXIES[1][1]), 'Diagnostic implementations drifted'
unittest.main(argv=[sys.argv[0]],verbosity=2)
