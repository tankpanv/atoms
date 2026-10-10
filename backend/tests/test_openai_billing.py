import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

import billing_proxy as proxy
import coding_runtime as runtime
import test_billing as fixtures
from billing import init_billing_db, settle_request
from test_openai_routing import ENV, answer
from test_model_stream import stream


class OpenAIBillingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with proxy.connection() as conn:
            init_billing_db(conn)
        fixtures.BillingTests.setUp(self)
        runtime._openai_unavailable.clear()
        env = patch.dict('os.environ', ENV)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(runtime._openai_unavailable.clear)
        self.requests = []

    def tearDown(self):
        fixtures.BillingTests.tearDown(self)

    def payload(self, request_id):
        return {'_billing_request': str(request_id), '_billing_job': str(self.job),
                '_billing_stage': 'IMPLEMENT', 'model': 'openai/gpt-6-luna',
                'messages': [{'role': 'user', 'content': 'test'}], 'max_tokens': 32}

    def rows(self):
        with proxy.connection() as conn:
            return conn.execute('SELECT * FROM billing_requests WHERE job_id=%s ORDER BY created_at', (self.job,)).fetchall()

    async def call(self, handler, request_id=None):
        original = httpx.AsyncClient
        def capture(request):
            self.requests.append(request)
            return handler(request)
        with patch('billing_proxy.httpx.AsyncClient', side_effect=lambda **kw: original(
                transport=httpx.MockTransport(capture), **kw)), patch('coding_runtime.asyncio.sleep', AsyncMock()):
            return await proxy.proxy_model(self.project, self.payload(request_id or uuid.uuid4()))

    async def test_direct_usage_settles_once_with_canonical_model_and_source(self):
        request_id = uuid.uuid4()
        result = await self.call(lambda request: answer(), request_id)
        await asyncio.sleep(0)
        self.assertEqual(await proxy.proxy_model(self.project, self.payload(request_id)), result)
        self.assertEqual(len(self.requests), 1)
        row, = self.rows()
        self.assertEqual(row['model'], 'openai/gpt-6-luna')
        self.assertEqual(row['provider_source'], 'openai')
        self.assertEqual(row['provider_base_url'], ENV['OPENAI_BASE_URL'])
        self.assertEqual(row['status'], 'settled')
        self.assertEqual((row['prompt_tokens'], row['completion_tokens']), (11, 5))

    async def test_three_rejections_fallback_preserves_accounting_and_original_replay(self):
        def handler(request):
            return httpx.Response(401, json={'error': 'no access'}) if request.url.host == 'dedicated.test' else answer()
        request_id = uuid.uuid4()
        result = await self.call(handler, request_id)
        await asyncio.sleep(0)
        self.assertEqual(await proxy.proxy_model(self.project, self.payload(request_id)), result)
        rows = self.rows()
        self.assertEqual([r['status'] for r in rows], ['rejected'] * 3 + ['settled'])
        self.assertEqual([r['provider_source'] for r in rows], ['openai'] * 3 + ['aggregate'])
        self.assertEqual(rows[0]['routed_request_id'], rows[-1]['id'])
        self.assertTrue(all(r['parent_request_id'] == request_id for r in rows[1:]))
        self.assertEqual(len(self.requests), 4)
        with proxy.connection() as conn:
            account = conn.execute('SELECT reserved_credits FROM account_profiles WHERE user_id=%s', (self.user['id'],)).fetchone()
            self.assertEqual(account['reserved_credits'], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM account_transactions WHERE user_id=%s AND request_id IS NOT NULL', (self.user['id'],)).fetchone()['n'], 1)

    async def test_failed_stream_usage_and_final_usage_are_settled_separately(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return stream({'id': 'resp_failed_' + self.job.hex, 'model': 'gpt-6-luna',
                               'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 3}},
                              {'error': {'code': 502, 'message': 'failed'}})
            return answer()
        await self.call(handler)
        rows = self.rows()
        self.assertEqual([r['status'] for r in rows], ['settled', 'settled'])
        self.assertEqual([r['prompt_tokens'] for r in rows], [20, 11])
        self.assertNotEqual(rows[0]['generation_id'], rows[1]['generation_id'])

    async def test_interrupted_usage_is_not_overwritten_or_queried_at_openrouter(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return stream({'id': 'resp_lost_' + self.job.hex, 'choices': [{'delta': {'content': 'partial'}}]})
            return answer()
        request_id = uuid.uuid4()
        result = await self.call(handler, request_id)
        rows = self.rows()
        self.assertEqual(rows[0]['status'], 'unknown')
        self.assertEqual(rows[0]['usage'], {})
        self.assertEqual(rows[1]['status'], 'settled')
        with proxy.connection() as conn:
            conn.execute("UPDATE billing_requests SET updated_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (request_id,))
        with patch('billing_proxy.httpx.AsyncClient') as client:
            await proxy.reconcile_once([request_id])
            client.assert_not_called()
        # Later accounting of the first generation cannot erase the cached
        # final response, nor charge the second generation again.
        with proxy.connection() as conn:
            settle_request(conn, request_id, {'prompt_tokens': 9, 'completion_tokens': 1})
        await asyncio.sleep(0)
        self.assertEqual(await proxy.proxy_model(self.project, self.payload(request_id)), result)
        self.assertEqual(len(self.requests), 2)

    async def test_restart_uses_final_attempt_status_instead_of_failed_parent(self):
        def handler(request):
            if request.url.host == 'dedicated.test':
                return httpx.Response(401, json={'error': 'no access'})
            raise httpx.ReadTimeout('aggregate response lost')
        request_id = uuid.uuid4()
        with self.assertRaises(HTTPException):
            await self.call(handler, request_id)
        await asyncio.sleep(0)
        self.assertEqual(self.rows()[0]['status'], 'rejected')
        self.assertEqual(self.rows()[-1]['status'], 'unknown')
        with self.assertRaises(HTTPException) as error:
            await proxy.proxy_model(self.project, self.payload(request_id))
        self.assertEqual(error.exception.detail['code'], 'MODEL_REQUEST_PENDING')
        self.assertEqual(len(self.requests), 4)

    async def test_duplicate_pending_identity_shares_active_fallback(self):
        request_id = uuid.uuid4()
        entered, release = asyncio.Event(), asyncio.Event()
        async def upstream(gateway, client, payload, **kwargs):
            if kwargs['dedicated']:
                raise httpx.ConnectError('unavailable')
            entered.set()
            await release.wait()
            return {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 11, 'completion_tokens': 5}}
        with patch.object(runtime.ModelGateway, '_stream_once', upstream), patch('coding_runtime.asyncio.sleep', AsyncMock()):
            first = asyncio.create_task(proxy.proxy_model(self.project, self.payload(request_id)))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(proxy.proxy_model(self.project, self.payload(request_id)))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.rows()), 4)
