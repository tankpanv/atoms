import asyncio
import json
import os
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn
from starlette.responses import HTMLResponse, JSONResponse
from agent_checks import browser_check, page_controls


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_selectors_execute_with_multiline_options_and_duplicate_labels(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                await page.set_content('''<main><select><option value="weight">体重（kg）</option>
                <option value="height">身高（cm）</option></select><select><option value="all">全部</option>
                <option value="30">近30天</option></select><button>编辑</button><button>编辑</button></main>''')
                controls = await page_controls(page)
                for control in controls:
                    self.assertEqual(await page.locator(control['selector']).count(), 1, control)
                selects = [c for c in controls if c['tag'] == 'select']
                self.assertEqual(selects[0]['options'][1]['value'], 'height')
                await page.locator(selects[0]['selector']).select_option('height')
                await page.locator(selects[1]['selector']).select_option('30')
                self.assertEqual(await page.locator('select').nth(0).input_value(), 'height')
                self.assertEqual(await page.locator('select').nth(1).input_value(), '30')
            finally:
                await browser.close()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.value = None
        self.project_id = uuid.uuid4()
        self.prefix = '/api/runtime/'+'w'*43
        async def app(scope, receive, send):
            path=scope['path']
            if path.endswith('/api/slow'):
                await asyncio.sleep(1)
                response=JSONResponse({'detail':'database unavailable'},status_code=503)
            elif path.endswith('/api/reject'):
                response=JSONResponse({'detail':'invalid input'},status_code=422)
            elif path.endswith('/api/items'):
                if scope['method']=='POST':
                    body=await receive()
                    self.value=json.loads(body['body'])['title']
                response=JSONResponse({'title':self.value},status_code=201 if scope['method']=='POST' else 200)
            else:
                if path.endswith('/dialog'):
                    response=HTMLResponse('''<html><body><button id="delete" onclick="document.querySelector('#result').textContent=confirm('Delete QA item?')?'Deleted':'Kept'">Delete</button><div id="result">Ready</div></body></html>''',headers={'Content-Security-Policy':'sandbox allow-scripts allow-forms allow-modals'})
                    await response(scope,receive,send)
                    return
                if path.endswith('/session'):
                    from preview_gateway import STORAGE_SHIM
                    response = HTMLResponse('<html><head>'+STORAGE_SHIM+'''</head><body>
                    <button id="login">Login</button><button id="logout">Logout</button><div id="session"></div>
                    <script>const render=()=>document.querySelector('#session').textContent=sessionStorage.getItem('token')||'Logged out';
                    document.querySelector('#login').onclick=()=>{sessionStorage.setItem('token','QA authenticated');render()};
                    document.querySelector('#logout').onclick=()=>{sessionStorage.clear();render()};render();</script></body></html>''',
                    headers={'Content-Security-Policy':'sandbox allow-scripts allow-forms'})
                    await response(scope,receive,send)
                    return
                if path.startswith('/api/storage/'):
                    await JSONResponse({'data':{}}, headers={'Access-Control-Allow-Origin':'*'})(scope,receive,send)
                    return
                endpoint='api/slow' if path.endswith('/slow') else 'api/reject' if path.endswith('/reject') else 'api/items'
                response=HTMLResponse('''<html><body><input id="title"><button id="save">Save</button>
                <div id="result">Ready</div><script>
                const show=v=>document.getElementById('result').textContent=v.title||v.detail||'Ready';
                fetch('''+json.dumps(endpoint)+''').then(r=>r.json()).then(show);
                document.getElementById('save').onclick=()=>fetch('api/items',{method:'POST',
                  headers:{'Content-Type':'application/json'},body:JSON.stringify({title:document.getElementById('title').value})})
                  .then(r=>r.json()).then(show);
                </script></body></html>''')
            await response(scope,receive,send)
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen()
        self.server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
        self.task=asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(100):
            if self.server.started: break
            await asyncio.sleep(.02)
        self.runtime=SimpleNamespace(base_aware=True,port=self.sock.getsockname()[1],prefix=self.prefix,token='w'*43)

    async def asyncTearDown(self):
        self.server.should_exit=True
        await self.task
        self.sock.close()
        self.directory.cleanup()

    async def check(self, actions, path='/'):
        with patch.dict(os.environ,{'WORKER_TOKEN':'','PREVIEW_GATEWAY_URL':''}),patch('agent_checks.start_runtime',AsyncMock(return_value=self.runtime)):
            return await browser_check(self.project_id,self.root,{'path':path,'actions':actions})

    async def test_sandbox_login_survives_reload_and_multiple_checks_without_cross_project_leak(self):
        code, report = await self.check([
            {'action':'click','selector':'#login'},
            {'action':'assert_text','selector':'#session','value':'QA authenticated'},
            {'action':'reload'},
            {'action':'assert_text','selector':'#session','value':'QA authenticated'}], '/session')
        self.assertEqual(code,0,report)

        code, report = await self.check([{'action':'assert_text','selector':'#session','value':'QA authenticated'}], '/session')
        self.assertEqual(code,0,report)
        original = self.project_id
        self.project_id = uuid.uuid4()
        code, report = await self.check([{'action':'assert_text','selector':'#session','value':'Logged out'}], '/session')
        self.assertEqual(code,0,report)
        self.project_id = original
        # Log in again since the intentionally changed project cannot share state.
        code, report = await self.check([
            {'action':'click','selector':'#login'}, {'action':'click','selector':'#logout'},
            {'action':'reload'}, {'action':'assert_text','selector':'#session','value':'Logged out'}], '/session')
        self.assertEqual(code,0,report)
        code, report = await self.check([{'action':'assert_text','selector':'#session','value':'Logged out'}], '/session')
        self.assertEqual(code,0,report)

    async def test_native_confirmation_requires_explicit_accept_and_reports_actual_dialog(self):
        code,report=await self.check([{'action':'click','selector':'#delete'},{'action':'assert_text','selector':'#result','value':'Kept'}],'/dialog')
        self.assertEqual(code,0,report)
        self.assertEqual(json.loads(report)['dialogs'][0]['response'],'dismiss')
        code,report=await self.check([{'action':'click','selector':'#delete','dialog':'accept'},{'action':'assert_text','selector':'#result','value':'Deleted'}],'/dialog')
        self.assertEqual(code,0,report)
        self.assertEqual(json.loads(report)['dialogs'][0]['message'],'Delete QA item?')

    async def test_late_backend_failure_cannot_pass_optimistic_page(self):
        code,report=await self.check([{'action':'assert_text','selector':'#result','value':'Ready'}],'/slow')
        self.assertEqual(code,1,report)
        self.assertIn('503',report)

    async def test_expected_validation_rejection_does_not_hide_other_failures(self):
        code,report=await self.check([
            {'action':'assert_response','selector':'/api/reject','status':422,'expect_json':{'detail':'invalid input'}},
            {'action':'assert_text','selector':'#result','value':'invalid input'}],'/reject')
        self.assertEqual(code,0,report)
        self.assertTrue(json.loads(report)['network'][0]['asserted'])
        code,report=await self.check([{'action':'assert_text','selector':'#result','value':'invalid input'}],'/reject')
        self.assertEqual(code,1,report)  # same error without explicit acceptance fails

    async def test_write_then_reload_verifies_real_readback(self):
        code,report=await self.check([
            {'action':'fill','selector':'#title','value':'Unique workflow record'},
            {'action':'click','selector':'#save'},
            {'action':'assert_response','selector':'/api/items','method':'POST','status':201,'expect_json':{'title':'Unique workflow record'}},
            {'action':'assert_text','selector':'#result','value':'Unique workflow record'},
            {'action':'reload'},
            {'action':'assert_response','selector':'/api/items','expect_json':{'title':'Unique workflow record'}},
            {'action':'assert_text','selector':'#result','value':'Unique workflow record'}])
        self.assertEqual(code,0,report)
        self.assertEqual(self.value,'Unique workflow record')

    async def test_http_200_with_wrong_business_result_does_not_pass(self):
        code,report=await self.check([{'action':'assert_response','selector':'/api/items','expect_json':{'title':'Never saved'}}])
        self.assertEqual(code,1,report)
        self.assertIn('JSON result does not match',report)
        self.assertTrue(any(c['selector']=='#save' for c in json.loads(report)['controls']))


if __name__=='__main__': unittest.main()
