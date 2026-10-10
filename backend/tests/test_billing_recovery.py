"""Fault injection at provider boundary; reservations and settlement use real PG."""
import asyncio
import copy
import unittest
import uuid
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import HTTPException
from coding_runtime import ModelProviderError
import billing_proxy as proxy
from billing import reserve_request
import test_billing as fixtures

class BillingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.BillingTests.setUp(self)
        env = patch.dict('os.environ', {'OPENAI_BASE_URL': '', 'OPENAI_API_KEY': '',
                                      'AI_BASE_URL': 'https://openrouter.ai/api/v1'})
        env.start()
        self.addCleanup(env.stop)
    def tearDown(self): fixtures.BillingTests.tearDown(self)
    def payload(self, request):
        return {'_billing_request':str(request),'_billing_job':str(self.job),'_billing_stage':'IMPLEMENT',
                'model':'openai/gpt-6-luna','messages':[{'role':'user','content':'test'}],'max_tokens':32}
    def row(self, request):
        with proxy.connection() as db:
            return db.execute('SELECT * FROM billing_requests WHERE id=%s',(request,)).fetchone()

    async def test_preconnect_failures_release_hold_and_authorize_safe_retry(self):
        for failure in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            with self.subTest(failure=failure.__name__):
                request=uuid.uuid4()
                with patch.object(proxy.ModelGateway,'_stream',AsyncMock(side_effect=failure('All connection attempts failed'))):
                    with self.assertRaises(HTTPException) as error:
                        await proxy.proxy_model(self.project,self.payload(request))
                self.assertEqual(error.exception.detail['code'],'MODEL_ATTEMPT_FAILED')
                row=self.row(request)
                self.assertEqual(row['status'],'rejected')
                self.assertTrue(row['attempt_finished'] and row['retry_safe'])
                with proxy.connection() as db:
                    account=db.execute('SELECT credits,reserved_credits FROM account_profiles WHERE user_id=%s',(self.user['id'],)).fetchone()
                    self.assertEqual(account['reserved_credits'],0)
                    self.assertEqual(account['credits'],15)
                    self.assertEqual(db.execute('SELECT COUNT(*) AS n FROM account_transactions WHERE request_id=%s',(request,)).fetchone()['n'],0)

    async def test_legacy_preconnect_failure_is_repaired_but_unknown_response_is_not(self):
        safe,ambiguous=uuid.uuid4(),uuid.uuid4()
        with proxy.connection() as db:
            for request,error in [(safe,'All connection attempts failed'),(ambiguous,'connection lost')]:
                reserve_request(db,request,self.project,self.job,'openai/gpt-6-luna','IMPLEMENT',32)
                db.execute("UPDATE billing_requests SET status='unknown',error=%s,updated_at=NOW()-INTERVAL '1 minute' WHERE id=%s",(error,request))
        await proxy.reconcile_once([safe,ambiguous])
        await proxy.reconcile_once([safe,ambiguous])
        self.assertEqual(self.row(safe)['status'],'rejected')
        self.assertTrue(self.row(safe)['retry_safe'])
        self.assertEqual(self.row(ambiguous)['status'],'unknown')
        self.assertFalse(self.row(ambiguous)['retry_safe'])

    async def test_failed_attempt_can_retry_under_new_identity_with_separate_accounting(self):
        first,second=uuid.uuid4(),uuid.uuid4()
        async def fail(gateway,client,payload):
            gateway.on_metadata({'id':'gen-test-'+first.hex})
            raise ModelProviderError({'code':502,'message':'provider unavailable'})
        with patch.object(proxy.ModelGateway,'_stream',fail):
            with self.assertRaises(HTTPException) as error:
                await proxy.proxy_model(self.project,self.payload(first))
        self.assertEqual(error.exception.detail['code'],'MODEL_ATTEMPT_FAILED')
        row=self.row(first)
        self.assertEqual(row['status'],'unknown')
        self.assertTrue(row['attempt_finished'] and row['retry_safe'])
        with patch.object(proxy.ModelGateway,'_stream',AsyncMock()) as upstream:
            with self.assertRaises(HTTPException) as repeat:
                await proxy.proxy_model(self.project,self.payload(first))
            self.assertTrue(repeat.exception.detail['retry_safe'])
            upstream.assert_not_awaited()
        answer={'choices':[{'message':{'content':'done'}}],'usage':{'prompt_tokens':10,'completion_tokens':2}}
        with patch.object(proxy.ModelGateway,'_stream',AsyncMock(return_value=answer)) as upstream:
            self.assertEqual(await proxy.proxy_model(self.project,self.payload(second)),answer)
            self.assertEqual(await proxy.proxy_model(self.project,self.payload(second)),answer)
            self.assertEqual(upstream.await_count,1)
        self.assertEqual(self.row(second)['status'],'settled')
        # Terminal SSE failure has real usage but no finish_reason. This must settle.
        original=httpx.AsyncClient
        def handler(request):return httpx.Response(200,json={'data':{'native_tokens_prompt':10,'native_tokens_completion':1,'finish_reason':None,'cancelled':False,'total_cost':0.001}})
        with proxy.connection() as db:
            db.execute("UPDATE billing_requests SET updated_at=NOW()-INTERVAL '1 minute' WHERE id=%s",(first,))
        with patch('billing_proxy.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            await proxy.reconcile_once([first])
            await proxy.reconcile_once([first])
        self.assertEqual(self.row(first)['status'],'settled')
        with proxy.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM account_transactions WHERE request_id=%s',(first,)).fetchone()['count'],1)

    async def test_valid_response_without_usage_is_saved_and_replayed_without_generation(self):
        request=uuid.uuid4();answer={'choices':[{'message':{'content':'valid answer'}}]}
        with patch.object(proxy.ModelGateway,'_stream',AsyncMock(return_value=answer)) as upstream:
            self.assertEqual(await proxy.proxy_model(self.project,self.payload(request)),answer)
            await asyncio.sleep(0)
            self.assertEqual(await proxy.proxy_model(self.project,self.payload(request)),answer)
            self.assertEqual(upstream.await_count,1)
        self.assertEqual(self.row(request)['status'],'unknown')
        self.assertEqual(self.row(request)['response'],answer)

    async def test_ambiguous_transport_failure_never_authorizes_new_generation(self):
        request=uuid.uuid4()
        with patch.object(proxy.ModelGateway,'_stream',AsyncMock(side_effect=httpx.ReadTimeout('connection lost'))):
            with self.assertRaises(HTTPException):await proxy.proxy_model(self.project,self.payload(request))
        self.assertFalse(self.row(request)['retry_safe'])
        await asyncio.sleep(0)
        with patch.object(proxy.ModelGateway,'_stream',AsyncMock()) as upstream:
            with self.assertRaises(HTTPException) as error:await proxy.proxy_model(self.project,self.payload(request))
            self.assertEqual(error.exception.detail['code'],'MODEL_REQUEST_PENDING')
            self.assertFalse(error.exception.detail['retry_safe'])
            upstream.assert_not_awaited()

    async def test_lost_response_reconciles_then_authorizes_retry_only_after_terminal_confirmation(self):
        request=uuid.uuid4()
        async def lost(gateway,client,payload):
            gateway.on_metadata({'id':'gen-test-'+request.hex})
            raise httpx.ReadTimeout('lost response')
        with patch.object(proxy.ModelGateway,'_stream',lost):
            with self.assertRaises(HTTPException):await proxy.proxy_model(self.project,self.payload(request))
        original=httpx.AsyncClient
        def handler(http_request):return httpx.Response(200,json={'data':{'native_tokens_prompt':10,'native_tokens_completion':2,'finish_reason':'stop','cancelled':False}})
        with proxy.connection() as db:
            db.execute("UPDATE billing_requests SET updated_at=NOW()-INTERVAL '1 minute' WHERE id=%s",(request,))
        with patch('billing_proxy.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            await proxy.reconcile_once([request])
        self.assertEqual(self.row(request)['status'],'settled')
        self.assertTrue(self.row(request)['retry_safe'])
        with self.assertRaises(HTTPException) as error:
            await proxy.proxy_model(self.project,self.payload(request))
        self.assertEqual(error.exception.detail['code'],'MODEL_ATTEMPT_FAILED')
