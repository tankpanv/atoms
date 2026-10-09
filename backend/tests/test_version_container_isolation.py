import asyncio
import os
import socket
import unittest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from fastapi import HTTPException
import agent_service


class FreshVersionContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_mutation_rejects_before_worker_teardown(self):
        project=uuid.uuid4()
        selected,acquisition=MagicMock(),MagicMock()
        selected.fetchone.return_value={'runtime_state':{'data':{'id':str(uuid.uuid4())}}}
        acquisition.fetchone.return_value={'acquired':False}
        conn=MagicMock();conn.execute.side_effect=[selected,acquisition]
        with patch.object(agent_service,'connection') as connection, \
             patch.object(agent_service,'invoke_worker',AsyncMock()) as invoke:
            connection.return_value.__enter__.return_value=conn
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_service.app),base_url='http://coordinator') as client:
                response=await client.post(f'/projects/{project}/invoke/restore',json={'version':1},headers={'X-Agent-Secret':agent_service.SECRET})
            self.assertEqual(response.status_code,409)
            self.assertIn('工作区未修改',response.json()['detail'])
            invoke.assert_not_awaited()

    async def test_missing_data_snapshot_rejects_before_replacing_container(self):
        project=uuid.uuid4()
        conn=MagicMock()
        conn.execute.return_value.fetchone.return_value={'runtime_state':{}}
        with patch.object(agent_service,'connection') as connection, \
             patch.object(agent_service,'ensure_worker',AsyncMock()) as ensure:
            connection.return_value.__enter__.return_value=conn
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_service.app),base_url='http://coordinator') as client:
                response=await client.post(f'/projects/{project}/invoke/restore',json={'version':1},headers={'X-Agent-Secret':agent_service.SECRET})
            self.assertEqual(response.status_code,422)
            self.assertIn('当前代码和数据未修改',response.json()['detail'])
            ensure.assert_not_awaited()

    async def test_restore_recreates_even_an_up_to_date_idle_container(self):
        project, owner = uuid.uuid4(), uuid.uuid4()
        network = agent_service.worker_name(project)
        old = {'Image':'current-image','State':{'Running':True},'Config':{'Labels':{
            'atoms.project':project.hex,'atoms.compose':agent_service.COMPOSE_PROJECT}}}
        calls = []
        async def docker(method, path, data=None):
            calls.append((method,path,data))
            if path.startswith('/volumes/'):
                return httpx.Response(200,json={'Mountpoint':'/fixture/workspaces'})
            if path.startswith('/images/'):
                return httpx.Response(200,json={'Id':'current-image'})
            if method == 'GET' and path == '/containers/'+socket.gethostname()+'/json':
                return httpx.Response(200,json={'Mounts':[{'Destination':os.getenv('MODEL_LIST_PATH','/model_list'),'Source':'/fixture/models'}]})
            if method == 'DELETE' or path.endswith('/start'):
                return httpx.Response(204)
            if path.startswith('/containers/create'):
                return httpx.Response(201,json={'Id':'new-container'})
            raise AssertionError((method,path))
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        health = httpx.Response(200,json={'project_id':str(project)})
        with patch.object(agent_service,'_capacity_lock',asyncio.Lock()), patch.object(agent_service,'_project_locks',{}), \
             patch.object(agent_service,'project_record',return_value=({'owner_id':owner,'status':'ready'},'fixture/'+project.hex)), \
             patch.object(agent_service,'container_for',AsyncMock(return_value=old)), \
             patch.object(agent_service,'docker',side_effect=docker), \
             patch.object(agent_service,'ensure_network',AsyncMock(return_value=network)), \
             patch.object(agent_service,'remove_network',AsyncMock()) as remove_network, \
             patch.object(agent_service,'reserve_capacity',AsyncMock()), \
             patch.object(agent_service,'worker_database_url',return_value='postgresql://fixture'), \
             patch.object(agent_service,'worker_token',return_value='fixture'), \
             patch.object(agent_service,'connection') as connection, \
             patch('project_database.credentials',return_value=('app','user','password','postgresql://app')), \
             patch.object(agent_service.httpx,'AsyncClient') as client:
            connection.return_value.__enter__.return_value = conn
            client.return_value.__aenter__.return_value.get = AsyncMock(return_value=health)
            url, mode = await agent_service.ensure_worker(project,fresh_restore=True)
        self.assertEqual(mode,'restored')
        remove_network.assert_not_awaited()
        self.assertIn(network,url)
        removed = next(i for i,call in enumerate(calls) if call[0]=='DELETE')
        created = next(i for i,call in enumerate(calls) if call[1].startswith('/containers/create'))
        self.assertLess(removed,created)
        config = calls[created][2]
        self.assertEqual(config['Labels']['atoms.project'],project.hex)
        self.assertTrue(config['HostConfig']['ReadonlyRootfs'])
        self.assertNotIn('FOWNER',config['HostConfig']['CapAdd'])
        self.assertTrue(any(call.args[0].startswith('DELETE FROM runtime_routes') for call in conn.execute.call_args_list))

    async def test_busy_restore_never_removes_existing_container(self):
        project = uuid.uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'busy':True}
        docker = AsyncMock(return_value=httpx.Response(200,json={'Mountpoint':'/fixture'}))
        with patch.object(agent_service,'_capacity_lock',asyncio.Lock()), patch.object(agent_service,'_project_locks',{}), \
             patch.object(agent_service,'project_record',return_value=({'status':'restoring'},'fixture')), \
             patch.object(agent_service,'container_for',AsyncMock(return_value={})), \
             patch.object(agent_service,'docker',docker), \
             patch.object(agent_service,'ensure_network',AsyncMock()), \
             patch.object(agent_service,'remove_worker',AsyncMock()) as remove, \
             patch.object(agent_service,'connection') as connection:
            connection.return_value.__enter__.return_value=conn
            with self.assertRaises(HTTPException) as error:
                await agent_service.ensure_worker(project,fresh_restore=True)
        self.assertEqual(error.exception.status_code,409)
        remove.assert_not_called()


class DockerCleanupDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_removal_allows_subprocess_cleanup_to_complete(self):
        with patch.object(agent_service.httpx, 'AsyncClient') as client:
            client.return_value.__aenter__.return_value.request = AsyncMock(return_value=httpx.Response(204))
            await agent_service.docker('DELETE', '/containers/fixture?force=true&v=true')
            self.assertEqual(client.call_args.kwargs['timeout'].read, 120)
            await agent_service.docker('GET', '/containers/fixture/json')
            self.assertEqual(client.call_args.kwargs['timeout'].read, 30)

    async def test_engine_timeout_identifies_the_failed_operation(self):
        with patch.object(agent_service.httpx, 'AsyncClient') as client:
            client.return_value.__aenter__.return_value.request = AsyncMock(side_effect=httpx.ReadTimeout(''))
            with self.assertRaises(HTTPException) as error:
                await agent_service.docker('DELETE', '/containers/fixture?force=true&v=true')
        self.assertIn('DELETE /containers/fixture', error.exception.detail)
        self.assertIn('ReadTimeout', error.exception.detail)
