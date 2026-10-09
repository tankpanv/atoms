import asyncio
import socket
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import uvicorn
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

from public_delivery import PlatformCors, published_document
from main import public_api


class PublicApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_cors_does_not_block_public_preflight_or_allow_null_control_plane(self):
        api = FastAPI()
        api.add_middleware(PlatformCors, allow_origins=['http://localhost:5173'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
        @api.options('/api/public/app/api/items')
        def preflight():
            from fastapi.responses import Response
            return Response(status_code=204, headers={'Access-Control-Allow-Origin':'*', 'Access-Control-Allow-Methods':'POST', 'Access-Control-Allow-Headers':'Authorization,Content-Type'})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://platform') as client:
            headers = {'Origin':'null', 'Access-Control-Request-Method':'POST', 'Access-Control-Request-Headers':'Content-Type'}
            response = await client.options('/api/public/app/api/items', headers=headers)
            self.assertEqual(response.status_code,204)
            self.assertEqual(response.headers['access-control-allow-origin'],'*')
            self.assertNotIn('access-control-allow-credentials',response.headers)
            self.assertEqual((await client.options('/api/projects', headers=headers)).status_code,400)

    def request(self, method='GET'):
        return Request({'type': 'http', 'method': method, 'scheme': 'http', 'server': ('localhost', 8000),
            'path': '/api/public/project/api/me', 'query_string': b'', 'headers': [
                (b'origin', b'null'), (b'cookie', b'atoms_refresh=private'),
                (b'authorization', b'Bearer app-token'), (b'access-control-request-headers', b'X-Session-Token')]})

    def database(self, row):
        db = MagicMock()
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
        return db

    async def test_private_and_unpublished_projects_never_start_or_forward(self):
        with patch('main.connection', self.database(None)), patch('main.agent_invoke', AsyncMock()) as invoke:
            for method in ('GET', 'POST', 'OPTIONS'):
                with self.assertRaises(HTTPException) as error:
                    await public_api(uuid.uuid4(), 'me', self.request(method))
                self.assertEqual(error.exception.status_code, 404)
            invoke.assert_not_called()

    async def test_public_preflight_does_not_start_worker_and_allows_app_headers(self):
        with patch('main.connection', self.database({'dev_command': ''})), patch('main.agent_invoke', AsyncMock()) as invoke:
            response = await public_api(uuid.uuid4(), 'me', self.request('OPTIONS'))
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.headers['access-control-allow-origin'], '*')
            self.assertIn('X-Session-Token', response.headers['access-control-allow-headers'])
            invoke.assert_not_called()

    async def test_paths_cannot_escape_project_api_to_control_plane(self):
        with patch('main.connection', self.database({'dev_command': ''})), patch('main.agent_invoke', AsyncMock()) as invoke:
            for path in ('../../invoke/run', '%2e%2e/../invoke/run', '%252e%252e/run', '..\\invoke'):
                with self.assertRaises(HTTPException) as error:
                    await public_api(uuid.uuid4(), path, self.request('POST'))
                self.assertEqual(error.exception.status_code,422)
            invoke.assert_not_called()

    async def test_existing_runtime_reused_and_cold_runtime_started_before_forwarding(self):
        token = 'x' * 43
        for warm in (True, False):
            status = {'status': {'running': True, 'url': f'/api/runtime/{token}/'}} if warm else {'status': None}
            invoke = AsyncMock(side_effect=[status, {'mode': 'live', 'url': f'/api/runtime/{token}/'}])
            forward = AsyncMock(return_value=JSONResponse({'authenticated': True}))
            project = uuid.uuid4()
            with patch('main.connection', self.database({'dev_command': 'npm run dev'})), patch('main.agent_invoke', invoke), patch('main.agent_endpoint', return_value='http://agent'), patch('main.forward_http', forward):
                response = await public_api(project, 'me', self.request('POST'))
            self.assertEqual(invoke.await_count, 1 if warm else 2)
            self.assertEqual(forward.call_args.kwargs['project_authorization'], 'Bearer app-token')
            self.assertEqual(forward.call_args.args[1], f'http://agent/projects/{project}/preview/{token}/api/me')
            self.assertEqual(response.headers['access-control-allow-origin'], '*')


class PublicBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project = str(uuid.uuid4())
        app = FastAPI()

        @app.get('/api/public/{project}')
        @app.get('/api/public/{project}/{path:path}')
        async def entry(project: str, request: Request, path: str = ''):
            prefix = '/api/public/' + project
            html = r'''<html><head><title>Storage app</title></head><body>
            <button id="login">Login</button><button id="logout">Logout</button><button id="navigate">Navigate</button><div id="state"></div>
            <script>
            const render=()=>document.querySelector('#state').textContent=(sessionStorage.getItem('token')||'Logged out')+' / '+(localStorage.getItem('settings')||'No settings');
            document.querySelector('#login').onclick=()=>{sessionStorage.setItem('token','Authenticated');localStorage.setItem('settings','Saved settings');render()};
            document.querySelector('#logout').onclick=()=>{sessionStorage.clear();localStorage.clear();render()};render();
            document.querySelector('#navigate').onclick=()=>history.pushState(null,'',location.pathname.replace(/\/$/,'')+'/records');
            </script></body></html>'''
            return published_document(html, prefix, request.url.path + '?__atoms_frame=1',
                framed=request.query_params.get('__atoms_frame') == '1')

        self.sock = socket.socket(); self.sock.bind(('127.0.0.1', 0)); self.sock.listen()
        self.origin = f'http://127.0.0.1:{self.sock.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(100):
            if self.server.started: break
            await asyncio.sleep(.02)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=['--no-sandbox'])

    async def asyncTearDown(self):
        await self.browser.close(); await self.playwright.stop()
        self.server.should_exit = True; await self.task; self.sock.close()

    async def test_real_published_frame_storage_reload_new_tabs_and_visitor_project_isolation(self):
        context = await self.browser.new_context()
        page = await context.new_page(); errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        url = self.origin + '/api/public/' + self.project
        await page.goto(url)
        frame = page.frame_locator('iframe')
        await frame.locator('#state').wait_for()
        self.assertEqual(await frame.locator('#state').inner_text(), 'Logged out / No settings')
        await frame.locator('#login').click()
        await page.wait_for_function("Object.keys(sessionStorage).some(k=>k.startsWith('atoms-published:'))")
        await page.reload()
        await frame.get_by_text('Authenticated / Saved settings', exact=True).wait_for()
        await frame.locator('#navigate').click()
        self.assertIn('__atoms_frame=1', page.frames[1].url)
        # Reloading just the app frame must preserve its state too.
        await page.frames[1].evaluate('location.reload()')
        await frame.get_by_text('Authenticated / Saved settings', exact=True).wait_for()
        tab = await context.new_page(); await tab.goto(url)
        await tab.frame_locator('iframe').get_by_text('Logged out / Saved settings', exact=True).wait_for()
        await page.goto(self.origin + '/api/public/' + str(uuid.uuid4()))
        await frame.get_by_text('Logged out / No settings', exact=True).wait_for()
        visitor = await self.browser.new_context(); guest = await visitor.new_page(); await guest.goto(url)
        await guest.frame_locator('iframe').get_by_text('Logged out / No settings', exact=True).wait_for()
        await page.goto(url); await frame.get_by_text('Authenticated / Saved settings', exact=True).wait_for()
        await frame.locator('#logout').click()
        await page.wait_for_function("Object.values(sessionStorage).includes('{}')")
        await page.reload(); await frame.get_by_text('Logged out / No settings', exact=True).wait_for()
        self.assertEqual(errors, [])
        await visitor.close(); await context.close()


if __name__ == '__main__': unittest.main()
