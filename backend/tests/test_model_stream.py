import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from coding_runtime import AgentState, ModelGateway, ModelContextOverflow, ModelTemporaryError


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data): self.data = data
    async def __aiter__(self):
        for offset in range(0, len(self.data), 17):
            yield self.data[offset:offset + 17]


def stream(*chunks):
    data = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Bytes(data.encode()))


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        endpoint = patch.dict('os.environ', {'AI_BASE_URL': 'https://openrouter.ai/api/v1',
                                            'OPENAI_BASE_URL': '', 'OPENAI_API_KEY': ''})
        endpoint.start()
        self.addCleanup(endpoint.stop)

    async def test_stage_change_reconciles_original_identity_without_executing_old_tools(self):
        import tempfile, uuid
        from pathlib import Path
        from billing_context import active_job
        from agent_session import AgentSession
        requests = []
        old_id = str(uuid.uuid4())
        old = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Old stabilization'}],
               'stream': True, 'max_tokens': 1000, '_billing_stage': 'STABILIZE', '_billing_request': old_id}
        def handler(request):
            payload = json.loads(request.content); requests.append(payload)
            message = ({'content': '', 'tool_calls': [{'id': 'old', 'type': 'function', 'function': {
                'name': 'write_file', 'arguments': '{"path":"must-not-exist.txt","content":"old proposal"}'}}]}
                if payload['_billing_request'] == old_id else {'content': 'New plan'})
            return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}], 'usage': {}})
        token = active_job.set(str(uuid.uuid4()))
        old['_billing_job'] = active_job.get()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'WORKER_TOKEN': 'test', 'PROJECT_ID': uuid.uuid4().hex}):
                root = Path(directory); session = AgentSession(root)
                session.state['pending_model'] = {'job_id': active_job.get(), 'payload': old}; session.save('pending')
                gateway = ModelGateway('test'); gateway.session = AgentSession(root)
                from agent_delivery import DeliveryBudget, budgeted_chat
                import time
                budget = DeliveryBudget(1000000, 120, time.monotonic() + 60)
                gateway.chat = budgeted_chat(gateway, budget, lambda **kw: None)
                messages = [{'role': 'user', 'content': 'Current planning goal'}]
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await gateway.chat(client, AgentState.PLAN, messages)
                self.assertEqual(requests[0], old)
                self.assertEqual(len(requests), 2)
                self.assertNotEqual(requests[1]['_billing_request'], old_id)
                self.assertEqual(requests[1]['_billing_stage'], 'PLAN')
                self.assertEqual(messages[0]['content'], 'Current planning goal')
                self.assertEqual(result['content'], 'New plan')
                self.assertNotIn('pending_model', AgentSession(root).state)
                self.assertFalse((root/'must-not-exist.txt').exists())
                with session.connect() as c:
                    event = c.execute("SELECT data FROM events WHERE kind='recovered_model_response'").fetchone()
                self.assertFalse(json.loads(event[0])['proposed_tools_executed'])
        finally:
            active_job.reset(token)

    async def test_unresolved_old_stage_never_starts_new_generation(self):
        import tempfile, uuid
        from pathlib import Path
        from billing_context import active_job
        from agent_session import AgentSession
        requests = []
        old = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Old goal'}],
               'stream': True, 'max_tokens': 1000, '_billing_stage': 'IMPLEMENT', '_billing_request': str(uuid.uuid4())}
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(503, json={'detail': {'code': 'MODEL_REQUEST_PENDING', 'retry_safe': False}})
        token = active_job.set(str(uuid.uuid4()))
        old['_billing_job'] = active_job.get()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'WORKER_TOKEN':'test','PROJECT_ID':uuid.uuid4().hex}), patch('coding_runtime.asyncio.sleep', AsyncMock()):
                session = AgentSession(Path(directory)); session.state['pending_model'] = {'job_id':active_job.get(),'payload':old}; session.save('pending')
                gateway = ModelGateway('test'); gateway.session = session
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ModelTemporaryError):
                        await gateway.chat(client, AgentState.PLAN, [{'role':'user','content':'New plan'}])
                self.assertEqual(requests, [old, old, old])
                self.assertEqual(AgentSession(Path(directory)).state['pending_model']['payload'], old)
        finally:
            active_job.reset(token)

    async def test_pending_replay_uses_original_context_even_when_resume_messages_overflow(self):
        import tempfile, uuid
        from pathlib import Path
        from billing_context import active_job
        from agent_session import AgentSession
        requests = []
        old = {'model': 'test', 'messages': [{'role': 'user', 'content': 'Original bounded request'}],
               'stream': True, 'max_tokens': 1000, '_billing_stage': 'IMPLEMENT', '_billing_request': str(uuid.uuid4())}
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Recovered original'}, 'finish_reason': 'stop'}], 'usage': {}})
        token = active_job.set(str(uuid.uuid4()))
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'WORKER_TOKEN': 'test', 'PROJECT_ID': uuid.uuid4().hex}):
                session = AgentSession(Path(directory))
                old['_billing_job'] = active_job.get()
                session.state['pending_model'] = {'job_id': active_job.get(), 'payload': old}
                session.save('pending')
                gateway = ModelGateway('test'); gateway.session = session
                messages = [{'role': 'user', 'content': '恢复说明' * 100000}]
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    result = await gateway.chat(client, AgentState.IMPLEMENT, messages)
                self.assertEqual(requests, [old])
                self.assertEqual(messages, old['messages'])
                self.assertEqual(result['_response_meta']['max_tokens'], old['max_tokens'])
                self.assertNotIn('pending_model', AgentSession(Path(directory)).state)
        finally:
            active_job.reset(token)

    async def test_fresh_request_clamps_output_to_actual_remaining_context(self):
        from agent_session import estimate_tokens
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return stream({'choices': [{'delta': {'content': 'Done'}, 'finish_reason': 'stop'}]})
        messages = [{'role': 'user', 'content': '当前实现' * 6000}]
        with patch.dict('os.environ', {'AI_API_KEY': 'test', 'WORKER_TOKEN': ''}), \
             patch('model_catalog.catalog', return_value=[{'id': 'test', 'context': 64000}]):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ModelGateway('test').chat(client, AgentState.IMPLEMENT, messages, max_tokens=60000)
        self.assertEqual(requests[0]['max_tokens'], 64000 - estimate_tokens(messages) - 4000)
        self.assertEqual(result['_response_meta']['max_tokens'], requests[0]['max_tokens'])

    async def test_pending_request_survives_job_resume_without_new_generation(self):
        import tempfile, uuid
        from pathlib import Path
        from billing_context import active_job
        from agent_session import AgentSession
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests)<=3:
                return httpx.Response(503,json={'detail':{'code':'MODEL_REQUEST_PENDING','retry_safe':False}})
            return httpx.Response(200,json={'choices':[{'message':{'content':'original answer'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2}})
        token=active_job.set(str(uuid.uuid4()))
        try:
            with tempfile.TemporaryDirectory() as directory,patch.dict('os.environ',{'WORKER_TOKEN':'test','PROJECT_ID':uuid.uuid4().hex}),patch('coding_runtime.asyncio.sleep',AsyncMock()):
                root=Path(directory)
                gateway=ModelGateway('test');gateway.session=AgentSession(root)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ModelTemporaryError):
                        await gateway.chat(client,AgentState.IMPLEMENT,[{'role':'user','content':'Original goal'}])
                    resumed=ModelGateway('test');resumed.session=AgentSession(root)
                    result=await resumed.chat(client,AgentState.IMPLEMENT,[{'role':'user','content':'Extra resume checkpoint changes context'}])
                    self.assertEqual(result['content'],'original answer')
                    self.assertNotIn('pending_model',AgentSession(root).state)
                self.assertTrue(all(item==requests[0] for item in requests))
        finally:active_job.reset(token)

    async def test_terminal_provider_failure_retries_but_does_not_execute_partial_tools(self):
        attempts=[]
        def handler(request):
            attempts.append(json.loads(request.content))
            if len(attempts)==1:
                return stream({'choices':[{'delta':{'tool_calls':[{'index':0,'id':'partial','function':{'name':'write_file','arguments':'{"path":'}}]}}]},
                    {'error':{'code':502,'message':'provider unavailable'}})
            return stream({'choices':[{'delta':{'content':'Recovered'},'finish_reason':'stop'}]},
                {'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
        with patch.dict('os.environ',{'AI_API_KEY':'test','WORKER_TOKEN':''}),patch('coding_runtime.asyncio.sleep',AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result=await ModelGateway('test').chat(client,AgentState.IMPLEMENT,[{'role':'user','content':'Build'}])
        self.assertEqual(result['content'],'Recovered')
        self.assertNotIn('tool_calls',result)
        self.assertEqual(len(attempts),2)

    async def test_worker_renews_id_only_for_confirmed_terminal_failure(self):
        import uuid
        from billing_context import active_job
        for code,safe in [('MODEL_ATTEMPT_FAILED',True),('MODEL_REQUEST_PENDING',False)]:
            attempts=[]
            def handler(request):
                attempts.append(json.loads(request.content))
                if len(attempts)<3:
                    return httpx.Response(503,json={'detail':{'code':code,'retry_safe':safe}})
                return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'Recovered'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2}})
            token=active_job.set(str(uuid.uuid4()))
            try:
                with patch.dict('os.environ',{'WORKER_TOKEN':'test','PROJECT_ID':uuid.uuid4().hex}),patch('coding_runtime.asyncio.sleep',AsyncMock()):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        await ModelGateway('test').chat(client,AgentState.IMPLEMENT,[{'role':'user','content':'Build'}])
            finally:active_job.reset(token)
            identifiers={a['_billing_request'] for a in attempts}
            self.assertEqual(len(identifiers),3 if safe else 1)

    async def test_scene_reasoning_policy_and_starvation_usage_are_preserved(self):
        requests, usage = [], []
        def handler(request):
            requests.append(json.loads(request.content))
            return stream({'choices':[{'delta':{'content':''},'finish_reason':'length'}]},
                          {'usage':{'prompt_tokens':100,'completion_tokens':2000,'total_tokens':2100,
                            'completion_tokens_details':{'reasoning_tokens':2000}}})
        with patch.dict('os.environ', {'AI_API_KEY':'test-key','WORKER_TOKEN':''}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ModelGateway('test-model',lambda stage,metrics:usage.append(metrics)).chat(
                    client,AgentState.TEST,[],max_tokens=8192,reasoning={'effort':'low'})
        self.assertEqual(requests[0]['reasoning'],{'effort':'low'})
        self.assertEqual(result['_response_meta']['content_chars'],0)
        self.assertEqual(result['_response_meta']['reasoning_tokens'],2000)
        self.assertEqual(usage[0]['reasoning_tokens'],2000)

    async def test_encrypted_region_failure_recovers_once_without_replaying_tool_results(self):
        requests = []
        def handler(request):
            payload = json.loads(request.content); requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(403, json={'error':{'message':'This model is not available in your region.',
                    'metadata':{'failed_routing_step':'Filter by Encrypted Payload Endpoint'}}})
            return stream({'choices':[{'delta':{'content':'Continue from verified code'},'finish_reason':'stop'}]},
                          {'usage':{'prompt_tokens':50,'completion_tokens':5,'total_tokens':55}})
        messages = [{'role':'system','content':'Engineer'}, {'role':'user','content':'Goal'},
                    {'role':'assistant','content':'','reasoning_details':[{'type':'reasoning.encrypted','data':'opaque'}],
                     'tool_calls':[{'id':'done','type':'function','function':{'name':'write_file','arguments':'{"path":"x","content":"done"}'}}]},
                    {'role':'tool','tool_call_id':'done','content':'File written; do not replay'}]
        usage = []
        with patch.dict('os.environ', {'AI_API_KEY':'test-key','WORKER_TOKEN':''}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                gateway = ModelGateway('test-model',lambda state,metrics:usage.append(metrics)); gateway.cache_scope='project'
                result = await gateway.chat(client,AgentState.IMPLEMENT,messages)
        self.assertEqual(len(requests),2)
        self.assertNotEqual(requests[0]['session_id'],requests[1]['session_id'])
        self.assertNotIn('reasoning_details',requests[1]['messages'][2])
        self.assertEqual(requests[1]['messages'][2]['tool_calls'],requests[0]['messages'][2]['tool_calls'])
        self.assertEqual(requests[1]['messages'][3],requests[0]['messages'][3])
        self.assertEqual(result['content'],'Continue from verified code')
        self.assertEqual(len(usage),1); self.assertEqual(usage[0]['total_tokens'],55)

    async def test_region_without_encrypted_history_is_not_blindly_retried(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(403,json={'error':{'message':'This model is not available in your region.'}})
        with patch.dict('os.environ', {'AI_API_KEY':'test-key','WORKER_TOKEN':''}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(RuntimeError):
                    await ModelGateway('test-model').chat(client,AgentState.IMPLEMENT,[{'role':'user','content':'Goal'}])
        self.assertEqual(len(requests),1)

    async def test_prompt_cache_hints_and_real_cache_metrics(self):
        requests = []
        usage = []
        def handler(request):
            requests.append(json.loads(request.content))
            return stream({'choices': [{'delta': {'content': 'OK'}, 'finish_reason': 'stop'}]},
                {'usage': {'prompt_tokens': 1500, 'completion_tokens': 2, 'total_tokens': 1502,
                           'prompt_tokens_details': {'cached_tokens': 1400, 'cache_write_tokens': 0}}})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key', 'AI_BASE_URL': 'https://openrouter.ai/api/v1', 'WORKER_TOKEN': ''}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                gateway = ModelGateway('anthropic/claude-test', lambda stage, metric: usage.append(metric))
                gateway.cache_scope = 'project'
                messages = [{'role': 'system', 'content': 'stable'}, {'role': 'user', 'content': 'goal'},
                            {'role': 'assistant', 'content': 'prior'}, {'role': 'user', 'content': 'next'}]
                await gateway.chat(client, AgentState.PLAN, messages)
                self.assertIsInstance(messages[-1]['content'], str)
        self.assertEqual(requests[0]['session_id'], 'atoms:project:planning')
        self.assertEqual(requests[0]['messages'][0]['content'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(requests[0]['messages'][-1]['content'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(usage[0]['cached_tokens'], 1400)
        self.assertEqual(usage[0]['total_tokens'], 1502)

    async def test_context_overflow_is_a_distinct_error_not_a_blind_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(400, json={'error': {'message': 'maximum context length exceeded'}})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ModelContextOverflow):
                    await ModelGateway('test-model').chat(client, AgentState.IMPLEMENT, [])
        self.assertEqual(len(requests), 1)

    async def test_reasoning_deltas_merge_without_duplicate_plaintext(self):
        def handler(request):
            return stream(
                {'choices': [{'delta': {'reasoning': 'Think ', 'reasoning_details': [{'type': 'reasoning.text', 'text': 'Think ', 'index': 0}]}}]},
                {'choices': [{'delta': {'reasoning': 'carefully', 'reasoning_details': [{'type': 'reasoning.text', 'text': 'carefully', 'index': 0}]}}]},
                {'choices': [{'delta': {'reasoning_details': [{'type': 'reasoning.text', 'signature': 'signed', 'index': 0}, {'type': 'reasoning.encrypted', 'data': 'opaque', 'id': 'sealed'}]}}]},
                {'choices': [{'delta': {'reasoning_details': [{'type': 'reasoning.summary', 'summary': 'First '}, {'type': 'reasoning.summary', 'summary': 'second'}]}}]},
                {'choices': [{'delta': {'content': 'OK'}, 'finish_reason': 'stop'}]})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ModelGateway('test-model').chat(client, AgentState.IMPLEMENT, [])
        self.assertNotIn('reasoning', result)
        self.assertEqual(len(result['reasoning_details']), 3)
        self.assertEqual(result['reasoning_details'][0]['text'], 'Think carefully')
        self.assertEqual(result['reasoning_details'][0]['signature'], 'signed')
        self.assertEqual(result['reasoning_details'][1]['data'], 'opaque')
        self.assertEqual(result['reasoning_details'][2]['summary'], 'First second')

    async def test_empty_reasoning_details_are_preserved_for_roundtrip(self):
        def handler(request):
            return stream({'choices': [{'delta': {'content': 'OK', 'reasoning_details': []}, 'finish_reason': 'stop'}]})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ModelGateway('test-model').chat(client, AgentState.IMPLEMENT, [])
        self.assertEqual(result['reasoning_details'], [])

    async def test_fragmented_tool_calls_and_empty_choices_usage(self):
        usage = []
        def handler(request):
            self.assertTrue(json.loads(request.content)['stream'])
            return stream(
                {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call1', 'function': {'name': 'write_file', 'arguments': '{"path":"hello'}}]}}]},
                {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '.py","content":"print(1)"}'}}]}}]},
                {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
                {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}):
            gateway = ModelGateway('test-model', lambda state, metrics: usage.append(metrics))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await gateway.chat(client, AgentState.IMPLEMENT, [])
        call = result['tool_calls'][0]
        self.assertEqual(call['id'], 'call1')
        self.assertEqual(call['function']['name'], 'write_file')
        self.assertEqual(json.loads(call['function']['arguments']), {'path': 'hello.py', 'content': 'print(1)'})
        self.assertEqual(usage[0]['total_tokens'], 30)
        self.assertEqual(result['_response_meta']['prompt_tokens'], 10)
        self.assertEqual(result['_response_meta']['total_tokens'], 30)

    async def test_incomplete_stream_never_returns_executable_tool_calls(self):
        requests = []
        def handler(request):
            requests.append(request)
            return stream({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'partial', 'function': {'name': 'write_file', 'arguments': '{"path":'}}]}}]})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}), patch('coding_runtime.asyncio.sleep', AsyncMock()):
            gateway = ModelGateway('test-model')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ModelTemporaryError) as error:
                    await gateway.chat(client, AgentState.IMPLEMENT, [])
        self.assertIsInstance(error.exception.__cause__, httpx.RemoteProtocolError)
        self.assertTrue(error.exception.pending)
        self.assertEqual(len(requests), 3)

    async def test_response_preserves_actual_output_cap_for_tool_validation(self):
        def handler(request):
            return stream({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'cut', 'function': {
                'name': 'write_files', 'arguments': '{"files":['}}]}, 'finish_reason': 'tool_calls'}]},
                {'usage': {'completion_tokens': 12000}})
        with patch.dict('os.environ', {'AI_API_KEY': 'test-key'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await ModelGateway('test-model').chat(client, AgentState.IMPLEMENT, [], max_tokens=12000)
        self.assertEqual(result['_response_meta'], {'finish_reason': 'tool_calls', 'completion_tokens': 12000,
            'reasoning_tokens': 0, 'content_chars': len(result.get('content') or ''), 'max_tokens': 12000})


if __name__ == '__main__':
    unittest.main()
