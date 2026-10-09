import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import os
import socket
import tempfile
import asyncio
from pathlib import Path
from starlette.requests import Request

from main import proxy_runtime
from preview_gateway import PreviewHeaders, project_cors_headers
from service_proxy import forward_http
from agent_checks import http_request, browser_preview_url, browser_check


def scope(method='GET', preview=False):
    return {'type': 'http', 'method': method, 'scheme': 'http', 'server': ('preview', 8002),
            'path': '/api/runtime/token/api/events', 'query_string': b'', 'http_version': '1.1',
            'headers': [(b'origin', b'null'), (b'authorization', b'Bearer project-token'), (b'cookie', b'platform_session=secret')],
            **({'atoms_project_preview': True} if preview else {})}


async def receive():
    return {'type': 'http.request', 'body': b'', 'more_body': False}


class PreviewFullstackTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_preparation_rejects_foreign_credentials_and_stale_runtime(self):
        from fastapi import HTTPException
        with patch.dict(os.environ, {'DATABASE_URL':os.getenv('DATABASE_URL','postgresql://unused')}):
            import agent_service
        project = uuid.uuid4()
        token = 'r'*43
        with patch.object(agent_service,'worker_token',return_value='scoped-secret'), patch.object(agent_service,'project_record') as record, patch.object(agent_service,'connection') as connection, patch.object(agent_service,'ensure_network',AsyncMock()), patch.object(agent_service.httpx,'AsyncClient') as client:
            with self.assertRaises(HTTPException) as error:
                await agent_service.prepare_browser_preview(project,token,'foreign-secret')
            self.assertEqual(error.exception.status_code,403)
            record.assert_not_called(); connection.assert_not_called()
            response = httpx.Response(200,json={'status':{'running':True,'url':'/api/runtime/other/'}},request=httpx.Request('POST','http://worker'))
            client.return_value.__aenter__.return_value.post=AsyncMock(return_value=response)
            with self.assertRaises(HTTPException) as error:
                await agent_service.prepare_browser_preview(project,token,'scoped-secret')
            self.assertEqual(error.exception.status_code,409)
            connection.assert_not_called()
            response = httpx.Response(200,json={'status':{'running':True,'url':f'/api/runtime/{token}/'}},request=httpx.Request('POST','http://worker'))
            client.return_value.__aenter__.return_value.post.return_value=response
            result = await agent_service.prepare_browser_preview(project,token,'scoped-secret')
            self.assertEqual(result['url'],f'http://preview:8002/api/runtime/{token}/')
            statements=connection.return_value.__enter__.return_value.execute.call_args_list
            self.assertEqual(statements[-1].args[1][1],project)
            self.assertNotIn(token,statements[-1].args[1][0])

    async def test_project_custom_auth_preflight_preserves_isolation(self):
        request = scope('OPTIONS')
        request['headers'].append((b'access-control-request-headers',
            b'x-session-token, X-App-Version, authorization, X-Worker-Token, cookie, bad\r\nname'))
        messages = []
        async def send(message): messages.append(message)
        await PreviewHeaders(AsyncMock())(request, receive, send)
        headers = dict(messages[0]['headers'])
        allowed = headers[b'access-control-allow-headers'].decode().lower().split(', ')
        self.assertIn('x-session-token', allowed)
        self.assertIn('x-app-version', allowed)
        self.assertEqual(allowed.count('authorization'), 1)
        self.assertNotIn('x-worker-token', allowed)
        self.assertNotIn('cookie', allowed)
        self.assertNotIn('bad\r\nname', allowed)
        self.assertNotIn(b'access-control-allow-credentials', headers)
        self.assertEqual(headers[b'vary'], b'Access-Control-Request-Headers')

    async def test_browser_gateway_registration_is_scoped_and_no_local_fallback(self):
        project = uuid.uuid4()
        runtime = SimpleNamespace(base_aware=True, port=12345, prefix='/api/runtime/'+'x'*43, token='x'*43)
        post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request('POST', 'http://agent-service')))
        with patch.dict(os.environ, {'WORKER_TOKEN':'private-worker-secret','PROJECT_ID':str(project),'PREVIEW_GATEWAY_URL':'http://preview:8002'}), patch('agent_checks.httpx.AsyncClient') as client:
            client.return_value.__aenter__.return_value.post = post
            url = await browser_preview_url(project, runtime, runtime.prefix+'/settings')
            self.assertEqual(url, 'http://preview:8002'+runtime.prefix+'/settings')
            self.assertNotIn('secret', url)
            self.assertEqual(post.call_args.kwargs['headers'], {'X-Worker-Token':'private-worker-secret'})
            with self.assertRaisesRegex(ValueError, '跨项目'):
                await browser_preview_url(uuid.uuid4(), runtime, '/')
            post.return_value = httpx.Response(409, request=httpx.Request('POST', 'http://agent-service'))
            with self.assertRaises(httpx.HTTPStatusError):
                await browser_preview_url(project, runtime, '/')

    async def test_real_chromium_sandbox_custom_header_fetch_and_network_failure(self):
        import uvicorn
        from starlette.responses import HTMLResponse, JSONResponse
        observed = []
        fail_preflight = False
        async def application(request_scope, receive, send):
            if request_scope['path'].endswith('/api/me'):
                observed.append(dict(request_scope['headers']))
                response = JSONResponse({'authenticated': True})
            elif request_scope['path'].startswith('/api/storage/'):
                response = JSONResponse({'data': {}})
            else:
                response = HTMLResponse("""<html><head></head><body><div id="result">Loading</div><script>
                fetch('api/me', {headers:{'X-Session-Token':'project-token'}})
                .then(r=>r.json()).then(v=>document.getElementById('result').textContent=v.authenticated?'Authenticated':'Rejected')
                .catch(()=>document.getElementById('result').textContent='Network blocked');</script></body></html>""",
                    headers={'Content-Security-Policy':'sandbox allow-scripts allow-forms'})
            await response(request_scope, receive, send)
        secured = PreviewHeaders(application)
        async def gateway(request_scope, receive, send):
            if fail_preflight and request_scope['method'] == 'OPTIONS':
                from starlette.responses import Response
                await Response(status_code=204, headers={'Access-Control-Allow-Origin':'*',
                    'Access-Control-Allow-Methods':'GET','Access-Control-Allow-Headers':'Content-Type'})(request_scope,receive,send)
            else:
                await secured(request_scope,receive,send)
        sock = socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen()
        server = uvicorn.Server(uvicorn.Config(gateway, log_level='error', lifespan='off'))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started: break
                await asyncio.sleep(.02)
            runtime = SimpleNamespace(base_aware=True,port=1,prefix='/api/runtime/'+'x'*43,token='x'*43)
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'PREVIEW_GATEWAY_URL':f'http://127.0.0.1:{sock.getsockname()[1]}','WORKER_TOKEN':''}), patch('agent_checks.start_runtime',AsyncMock(return_value=runtime)):
                code, output = await browser_check(uuid.uuid4(),Path(directory),{'actions':[{'action':'assert_text','selector':'#result','value':'Authenticated'}]})
                self.assertEqual(code,0,output)
                self.assertEqual(observed[-1][b'origin'],b'null')
                self.assertEqual(observed[-1][b'x-session-token'],b'project-token')
                fail_preflight = True
                code, output = await browser_check(uuid.uuid4(),Path(directory),{'actions':[{'action':'assert_text','selector':'#result','value':'Network blocked'}]})
                self.assertEqual(code,1,output)
                self.assertIn('ERR_FAILED',output)
        finally:
            server.should_exit = True
            await task
            sock.close()

    async def test_api_acceptance_can_verify_project_bearer_authentication(self):
        def handler(request):
            status = 200 if request.headers.get('authorization') == 'Bearer project-token' else 401
            return httpx.Response(status, json={'authenticated': status == 200})
        original_client = httpx.AsyncClient
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler), **kwargs)
        service = SimpleNamespace(name='api', port=12345, process=SimpleNamespace(returncode=None))
        with patch('agent_checks.start_runtime', AsyncMock(return_value=SimpleNamespace(services=[service]))), patch('agent_checks.httpx.AsyncClient', side_effect=client):
            code, output = await http_request(uuid.uuid4(), {'service': 'api', 'path': '/api/me',
                'headers': {'Authorization': 'Bearer project-token'}, 'expect_status': 200,
                'expect_json': {'authenticated': True}})
            self.assertEqual(code, 0, output)
            code, output = await http_request(uuid.uuid4(), {'service': 'api', 'path': '/api/me',
                'expect_status': 401, 'expect_json': {'authenticated': False}})
            self.assertEqual(code, 0, output)

    async def test_sandbox_api_preflight_does_not_require_main_site_auth(self):
        upstream = AsyncMock()
        messages = []
        async def send(message): messages.append(message)
        await PreviewHeaders(upstream)(scope('OPTIONS'), receive, send)
        upstream.assert_not_called()
        self.assertEqual(messages[0]['status'], 204)
        headers = dict(messages[0]['headers'])
        self.assertEqual(headers[b'access-control-allow-origin'], b'*')
        self.assertIn(b'Authorization', headers[b'access-control-allow-headers'])

    async def test_sandbox_api_response_has_cors_and_never_sets_platform_cookie(self):
        async def upstream(request_scope, receive, send):
            self.assertTrue(request_scope.get('atoms_project_preview'))
            await send({'type': 'http.response.start', 'status': 200, 'headers': [
                (b'content-type', b'application/json'), (b'set-cookie', b'leak=secret'),
                (b'access-control-allow-origin', b'http://localhost')]})
            await send({'type': 'http.response.body', 'body': b'[]', 'more_body': False})
        messages = []
        async def send(message): messages.append(message)
        await PreviewHeaders(upstream)(scope(), receive, send)
        headers = dict(messages[0]['headers'])
        self.assertNotIn(b'set-cookie', headers)
        self.assertEqual(headers[b'access-control-allow-origin'], b'*')
        self.assertEqual(sum(name == b'access-control-allow-origin' for name, value in messages[0]['headers']), 1)

    async def test_only_preview_origin_forwards_project_bearer(self):
        for preview in (False, True):
            forwarded = AsyncMock(return_value='proxied')
            with patch('main.runtime_project', return_value=uuid.uuid4()), patch('main.agent_endpoint', return_value='http://agent'), patch('main.forward_http', forwarded), patch.dict('os.environ', {'PROJECT_ID': '', 'USER_ID': ''}):
                await proxy_runtime('token', Request(scope(preview=preview), receive), 'api/events')
            self.assertEqual(forwarded.call_args.kwargs['project_authorization'], 'Bearer project-token' if preview else '')

    async def test_interservice_forwarding_strips_cookie_and_preserves_explicit_app_auth(self):
        observed = []
        def handler(request):
            observed.append(request.headers)
            return httpx.Response(200, content=b'{}')
        original_client = httpx.AsyncClient
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler))
        with patch('service_proxy.httpx.AsyncClient', side_effect=client):
            for token in ('', 'Bearer project-token'):
                response = await forward_http(Request(scope(), receive), 'http://worker/api', {}, project_authorization=token)
                async for chunk in response.body_iterator: pass
        self.assertNotIn('authorization', observed[0])
        self.assertEqual(observed[1]['authorization'], 'Bearer project-token')
        self.assertTrue(all('cookie' not in headers for headers in observed))


if __name__ == '__main__':
    unittest.main()
