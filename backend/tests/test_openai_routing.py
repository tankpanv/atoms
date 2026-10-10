import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import coding_runtime as runtime
from coding_runtime import AgentState, ModelContextOverflow, ModelGateway
from test_model_stream import stream


ENV = {'AI_BASE_URL': 'https://openrouter.ai/api/v1', 'AI_API_KEY': 'aggregate-key',
       'OPENAI_BASE_URL': 'http://dedicated.test/v1', 'OPENAI_API_KEY': 'dedicated-key',
       'WORKER_TOKEN': ''}


def answer():
    return stream({'id': 'resp_test', 'model': 'gpt-6.1-sol',
                   'choices': [{'delta': {'content': 'OK'}, 'finish_reason': 'stop'}]},
                  {'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 5, 'total_tokens': 16}})


class OpenAIRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        runtime._openai_unavailable.clear()
        env = patch.dict('os.environ', ENV)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(runtime._openai_unavailable.clear)
        self.requests = []

    async def call(self, handler, model='openai/gpt-6.1-sol', **kwargs):
        def capture(request):
            self.requests.append(request)
            return handler(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            return await ModelGateway(model).chat(client, AgentState.IMPLEMENT,
                                                  [{'role': 'user', 'content': 'Build'}], **kwargs)

    async def test_direct_adapts_parameters_and_preserves_tool_receipts(self):
        original = {'model': 'openai/gpt-6.1-sol', 'max_tokens': 128, 'stream': True,
                    'reasoning': {'effort': 'low'}, 'session_id': 'atoms:test:cache',
                    'messages': [{'role': 'assistant', 'content': '',
                                  'reasoning_details': [{'type': 'reasoning.encrypted', 'data': 'opaque'}],
                                  'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
                                      'name': 'read_file', 'arguments': '{}'}}]},
                                 {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'Already read'}]}
        before = copy.deepcopy(original)
        def handler(request):
            self.requests.append(request)
            return answer()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await ModelGateway(original['model'])._stream(client, original)
        sent = json.loads(self.requests[0].content)
        self.assertEqual(self.requests[0].url.host, 'dedicated.test')
        self.assertEqual(self.requests[0].headers['Authorization'], 'Bearer dedicated-key')
        self.assertEqual(sent['model'], 'gpt-6.1-sol')
        self.assertEqual(sent['max_completion_tokens'], 128)
        self.assertEqual(sent['reasoning_effort'], 'low')
        self.assertEqual(sent['stream_options'], {'include_usage': True})
        for field in ('max_tokens', 'session_id', 'reasoning'):
            self.assertNotIn(field, sent)
        self.assertNotIn('reasoning_details', sent['messages'][0])
        self.assertEqual(sent['messages'][1], original['messages'][1])
        self.assertEqual(original, before)
        self.assertEqual(result['usage']['total_tokens'], 16)

    async def test_three_failures_fallback_and_cooldown_then_recovery(self):
        healthy = False
        def handler(request):
            if request.url.host == 'dedicated.test' and not healthy:
                return httpx.Response(401, json={'error': {'message': 'unavailable'}})
            return answer()
        with patch('coding_runtime.asyncio.sleep', AsyncMock()) as sleep:
            result = await self.call(handler)
            self.assertEqual(result['content'], 'OK')
            self.assertEqual([r.url.host for r in self.requests], ['dedicated.test'] * 3 + ['openrouter.ai'])
            self.assertEqual(sleep.await_count, 2)
            fallback = json.loads(self.requests[-1].content)
            self.assertEqual(fallback['model'], 'openai/gpt-6.1-sol')
            self.assertIn('max_tokens', fallback)
            self.assertNotIn('max_completion_tokens', fallback)
            self.assertEqual(self.requests[-1].headers['Authorization'], 'Bearer aggregate-key')
            await self.call(handler)
            self.assertEqual(self.requests[-1].url.host, 'openrouter.ai')
            await self.call(handler, model='openai/gpt-6-luna')
            self.assertEqual([r.url.host for r in self.requests[-4:]], ['dedicated.test'] * 3 + ['openrouter.ai'])
            healthy = True
            for key in runtime._openai_unavailable:
                runtime._openai_unavailable[key] = 0
            await self.call(handler)
            self.assertEqual(self.requests[-1].url.host, 'dedicated.test')

    async def test_transient_failure_recovers_before_fallback(self):
        def handler(request):
            if len(self.requests) < 3:
                raise httpx.ConnectTimeout('unavailable')
            return answer()
        with patch('coding_runtime.asyncio.sleep', AsyncMock()):
            result = await self.call(handler)
        self.assertEqual(result['content'], 'OK')
        self.assertEqual([r.url.host for r in self.requests], ['dedicated.test'] * 3)
        self.assertFalse(runtime._openai_unavailable)

    async def test_quickrouter_tool_reasoning_is_adapted_only_for_direct_source(self):
        tools = [{'type': 'function', 'function': {'name': 'status', 'parameters': {'type': 'object'}}}]
        with patch.dict('os.environ', {'AI_BASE_URL': 'https://api.quickrouter.ai/v1'}):
            await self.call(lambda request: answer(), tools=tools)
            def handler(request):
                if request.url.host == 'dedicated.test':
                    return httpx.Response(503, json={'error': 'unavailable'})
                return answer()
            with patch('coding_runtime.asyncio.sleep', AsyncMock()):
                await self.call(handler, tools=tools)
        self.assertEqual(json.loads(self.requests[0].content)['reasoning_effort'], 'low')
        self.assertEqual(json.loads(self.requests[-1].content)['reasoning_effort'], 'none')
        self.assertEqual(self.requests[-1].url.host, 'api.quickrouter.ai')

    async def test_incomplete_direct_tools_are_discarded_on_fallback(self):
        def handler(request):
            if request.url.host == 'dedicated.test':
                return stream({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'partial',
                    'function': {'name': 'write_file', 'arguments': '{'}}]}}]})
            return answer()
        with patch('coding_runtime.asyncio.sleep', AsyncMock()):
            result = await self.call(handler)
        self.assertEqual(result['content'], 'OK')
        self.assertNotIn('tool_calls', result)
        self.assertEqual(len(self.requests), 4)

    async def test_other_models_and_incomplete_configuration_keep_aggregate(self):
        await self.call(lambda request: answer(), model='anthropic/claude-test')
        for field in ('OPENAI_BASE_URL', 'OPENAI_API_KEY'):
            with patch.dict('os.environ', {field: '  '}):
                await self.call(lambda request: answer())
        self.assertEqual([r.url.host for r in self.requests], ['openrouter.ai'] * 3)

    async def test_full_endpoint_and_stage_override(self):
        with patch.dict('os.environ', {'OPENAI_BASE_URL': 'http://dedicated.test/v1/chat/completions/',
                                      'AI_IMPLEMENT_MODEL': 'openai/gpt-6-luna'}):
            await self.call(lambda request: answer(), model='anthropic/claude-test')
        self.assertEqual(self.requests[0].url.path, '/v1/chat/completions')
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'gpt-6-luna')

    async def test_context_overflow_keeps_existing_compaction_behavior(self):
        with self.assertRaises(ModelContextOverflow):
            await self.call(lambda request: httpx.Response(400, json={'error': {'message': 'maximum context length exceeded'}}))
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(runtime._openai_unavailable)

    async def test_workers_only_send_canonical_payload_to_billing_gateway(self):
        from billing_context import active_job
        token = active_job.set('job')
        try:
            with patch.dict('os.environ', {'WORKER_TOKEN': 'worker', 'PROJECT_ID': 'project'}):
                await self.call(lambda request: answer())
        finally:
            active_job.reset(token)
        self.assertEqual(self.requests[0].url.host, 'agent-service')
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'openai/gpt-6.1-sol')
        self.assertNotIn('Authorization', self.requests[0].headers)
