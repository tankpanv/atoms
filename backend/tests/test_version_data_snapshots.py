"""Real PostgreSQL export/restore and browser data isolation regression."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from psycopg import sql
from unittest.mock import patch
import httpx
from psycopg.types.json import Jsonb
import agent_service as service
from project_database import provision, remove, names
from project_state_snapshots import capture_state, restore_state, verify_state, remove_state_snapshots
from private_data_snapshots import capture_private_data, apply_private_data
from restoration_messages import create_messages, update_message


class PrivateDataTests(unittest.TestCase):
    def test_sqlite_wal_files_new_files_and_modes_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            private=root/'.atoms-data'; private.mkdir()
            db=sqlite3.connect(private/'app.sqlite3')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE items(id integer primary key, label text)')
            db.execute("INSERT INTO items(label) VALUES('before')"); db.commit()
            (private/'image.bin').write_bytes(b'original\x00asset')
            (private/'image.bin').chmod(0o640)
            state=capture_private_data(root)
            self.assertNotIn('app.sqlite3-wal',state)
            db.execute("UPDATE items SET label='later'"); db.commit(); db.close()
            (private/'extra').write_text('must disappear')
            (private/'image.bin').write_bytes(b'newer')
            apply_private_data(root,state)
            with sqlite3.connect(private/'app.sqlite3') as restored:
                self.assertEqual(restored.execute('SELECT label FROM items').fetchone()[0],'before')
            self.assertEqual((private/'image.bin').read_bytes(),b'original\x00asset')
            self.assertEqual((private/'image.bin').stat().st_mode & 0o777,0o640)
            self.assertFalse((private/'extra').exists())


class CheckpointReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_reconnects_after_restart_without_changing_snapshot(self):
        from project_data_client import checkpoint
        from unittest.mock import AsyncMock
        project,identity=uuid.uuid4(),str(uuid.uuid4())
        with patch('project_data_client.settings',return_value=('http://checkpoint',{})), \
             patch('project_data_client.httpx.AsyncClient') as client, \
             patch('project_data_client.asyncio.sleep',new_callable=AsyncMock):
            post=client.return_value.__aenter__.return_value.post
            post.side_effect=[httpx.ConnectError('service restarting'),httpx.Response(503),httpx.Response(200,json={'verified':True})]
            result=await checkpoint(project,'restore',identity)
            self.assertTrue(result['verified'])
            self.assertEqual(post.await_count,3)
            self.assertTrue(all(call.kwargs['json']=={'operation':'restore','id':identity} for call in post.await_args_list))

    async def test_missing_snapshot_is_not_retried(self):
        from project_data_client import checkpoint
        from unittest.mock import AsyncMock
        with patch('project_data_client.settings',return_value=('http://checkpoint',{})), \
             patch('project_data_client.httpx.AsyncClient') as client, \
             patch('project_data_client.asyncio.sleep',new_callable=AsyncMock) as sleep:
            post=client.return_value.__aenter__.return_value.post
            post.return_value=httpx.Response(422,json={'detail':'快照缺失'})
            with self.assertRaisesRegex(ValueError,'快照缺失'):
                await checkpoint(uuid.uuid4(),'restore',str(uuid.uuid4()))
            post.assert_awaited_once()
            sleep.assert_not_awaited()


class ManagedVersionDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project=uuid.uuid4(); self.user=uuid.uuid4()
        self.schema,self.role=names(self.project)
        with service.connection() as conn:
            conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'fixture')",(self.user,str(self.user)+'@example.test'))
            conn.execute("INSERT INTO projects(id,owner_id,title,prompt,status) VALUES(%s,%s,'State restore test','test','restoring')",(self.project,self.user))
            provision(conn,self.project,service.DATABASE_URL,service.SECRET)
            conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
            conn.execute(sql.SQL('CREATE TABLE {}.accounts(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,name text UNIQUE NOT NULL)').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("INSERT INTO {}.accounts(name) VALUES('version-one')").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE TABLE {}.children(id serial PRIMARY KEY,parent_id bigint REFERENCES {}.accounts(id), label text GENERATED ALWAYS AS (\'child\') STORED)').format(sql.Identifier(self.schema),sql.Identifier(self.schema)))
            conn.execute(sql.SQL('INSERT INTO {}.children(parent_id) VALUES(1)').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE VIEW {}.names AS SELECT name FROM {}.accounts').format(sql.Identifier(self.schema),sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE TABLE {}.events(id int, label text) PARTITION BY RANGE(id)').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE TABLE {}.events_first PARTITION OF {}.events FOR VALUES FROM (0) TO (100)').format(sql.Identifier(self.schema),sql.Identifier(self.schema)))
            conn.execute(sql.SQL("INSERT INTO {}.events VALUES(1,'partition row')").format(sql.Identifier(self.schema)))
            conn.execute('RESET ROLE')
            conn.execute('INSERT INTO preview_storage(project_id,data,sessions) VALUES(%s,%s,%s)',(self.project,Jsonb({'draft':'one'}),Jsonb({'tab-first':{'auth':'old-session'}})))

    async def asyncTearDown(self):
        with service.connection() as conn:
            remove_state_snapshots(conn,self.project)
            remove(conn,self.project)
            conn.execute('DELETE FROM projects WHERE id=%s',(self.project,))
            conn.execute('DELETE FROM users WHERE id=%s',(self.user,))

    async def test_old_new_old_data_schema_sequences_and_browser_roundtrip(self):
        old=await capture_state(self.project,service)
        with service.connection() as conn:
            conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
            conn.execute(sql.SQL('ALTER TABLE {}.accounts ADD COLUMN email text').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("INSERT INTO {}.accounts(name,email) VALUES('version-two','new@example.test')").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('CREATE TABLE {}.new_only(id int)').format(sql.Identifier(self.schema)))
            conn.execute('RESET ROLE')
            conn.execute('UPDATE preview_storage SET data=%s,sessions=%s WHERE project_id=%s',(Jsonb({'draft':'two'}),Jsonb({'tab-first':{'auth':'new-session'}}),self.project))
        new=await capture_state(self.project,service)
        for target,expected,has_new in [(old,'one',False),(new,'two',True),(old,'one',False)]:
            await restore_state(self.project,target['id'],service)
            self.assertTrue(verify_state(self.project,target['id'],service)['verified'])
            with service.connection() as conn:
                browser=conn.execute('SELECT data,sessions FROM preview_storage WHERE project_id=%s',(self.project,)).fetchone()
                self.assertEqual(browser['data'],{'draft':expected})
                self.assertEqual(browser['sessions']['tab-first']['auth'], 'old-session' if expected=='one' else 'new-session')
                self.assertEqual(bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name='accounts' AND column_name='email'",(self.schema,)).fetchone()),has_new)
                conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
                identifier=conn.execute(sql.SQL("INSERT INTO {}.accounts(name) VALUES('post-restore') RETURNING id").format(sql.Identifier(self.schema))).fetchone()['id']
                self.assertEqual(identifier,3 if has_new else 2)
                conn.rollback()
        with service.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM project_state_snapshots WHERE project_id=%s',(self.project,)).fetchone()['n'],2)
            conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
            with self.assertRaises(Exception):
                conn.execute(sql.SQL('SELECT * FROM {}.accounts').format(sql.Identifier('atoms_version_'+uuid.UUID(old['id']).hex)))

    async def test_missing_snapshot_never_changes_current_data(self):
        with self.assertRaisesRegex(ValueError,'快照缺失'):
            await restore_state(self.project,str(uuid.uuid4()),service)
        with service.connection() as conn:
            self.assertEqual(conn.execute(sql.SQL('SELECT name FROM {}.accounts').format(sql.Identifier(self.schema))).fetchone()['name'],'version-one')

    async def test_progress_message_updates_one_card_and_keeps_terminal_result(self):
        identity=uuid.uuid4()
        with service.connection() as conn:
            conn.execute("INSERT INTO project_restores(project_id,id,version,status,phase) VALUES(%s,%s,2,'running','校验版本')",(self.project,identity))
            create_messages(conn,self.project,identity,2)
            for phase in ['正在恢复数据库和浏览器数据','正在恢复数据库和浏览器数据','正在启动服务并验证预览']:
                update_message(conn,self.project,phase=phase)
            update_message(conn,self.project,phase='还原完成',status='succeeded',result={'restored_version':3})
        with service.connection() as conn:
            rows=conn.execute('SELECT role,content,restoration FROM messages WHERE project_id=%s ORDER BY created_at',(self.project,)).fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual([row['role'] for row in rows],['user','assistant'])
        assistant=next(row for row in rows if row['role']=='assistant')
        self.assertEqual(assistant['restoration']['status'],'succeeded')
        self.assertEqual(len(assistant['restoration']['steps']),3)
        self.assertIn('数据库和浏览器数据已同步',assistant['content'])

    async def test_failed_database_commit_restores_original_schema_and_browser(self):
        snapshot=await capture_state(self.project,service)
        with service.connection() as conn:
            conn.execute(sql.SQL("UPDATE {}.accounts SET name='current-data'").format(sql.Identifier(self.schema)))
            conn.execute('UPDATE preview_storage SET data=%s WHERE project_id=%s',(Jsonb({'draft':'current'}),self.project))
        from project_state_snapshots import clone_database as actual_clone
        async def fail_after_copy(*args,**kwargs):
            real_finalize=kwargs['finalize']
            def fail(conn):
                real_finalize(conn)
                raise ValueError('simulated validation failure')
            kwargs['finalize']=fail
            return await actual_clone(*args,**kwargs)
        with patch('project_state_snapshots.clone_database',side_effect=fail_after_copy):
            with self.assertRaisesRegex(ValueError,'simulated'):
                await restore_state(self.project,snapshot['id'],service)
        with service.connection() as conn:
            self.assertEqual(conn.execute(sql.SQL('SELECT name FROM {}.accounts').format(sql.Identifier(self.schema))).fetchone()['name'],'current-data')
            self.assertEqual(conn.execute('SELECT data FROM preview_storage WHERE project_id=%s',(self.project,)).fetchone()['data'],{'draft':'current'})

    async def test_browser_session_storage_rejects_old_epoch_and_restore_writes(self):
        import hashlib
        from preview_gateway import app
        token='test-preview-'+uuid.uuid4().hex
        with service.connection() as conn:
            conn.execute('INSERT INTO runtime_routes(token_hash,project_id) VALUES(%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),self.project))
            conn.execute("UPDATE projects SET status='ready' WHERE id=%s",(self.project,))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://preview') as client:
            url='/api/storage/'+token+'?session=tab-first'
            first=await client.get(url)
            self.assertEqual(first.status_code,200)
            epoch=first.json()['epoch']
            self.assertEqual(first.json()['session'],{'auth':'old-session'})
            update={'operation':'set','area':'session','session':'tab-first','key':'auth','value':'tab-value','epoch':epoch}
            self.assertEqual((await client.post(url,json=update)).status_code,200)
            self.assertEqual((await client.get(url)).json()['session']['auth'],'tab-value')
            with service.connection() as conn:
                conn.execute('UPDATE preview_storage SET epoch=gen_random_uuid() WHERE project_id=%s',(self.project,))
            self.assertEqual((await client.post(url,json=update)).status_code,409)
            update['epoch']=(await client.get(url)).json()['epoch']
            with service.connection() as conn:
                conn.execute("UPDATE projects SET status='restoring' WHERE id=%s",(self.project,))
            self.assertEqual((await client.post(url,json=update)).status_code,409)

    async def test_failed_build_rolls_back_code_postgres_and_private_browser_data_together(self):
        from project_restoration import capture_runtime_state, restore_version
        from agent import snapshot_files
        import agent, runtime
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/self.project.hex; root.mkdir()
            (root/'index.html').write_text('<h1>old target</h1>')
            (root/'.atoms-workspace.json').write_text(json.dumps({'build':'exit 7','dev':'python -m http.server $PORT --bind 127.0.0.1'}))
            private=root/'.atoms-data'; private.mkdir(); (private/'state').write_text('old private')
            state=capture_runtime_state(root)
            state['data']=await capture_state(self.project,service)
            target=snapshot_files(root)
            with service.connection() as conn:
                conn.execute("INSERT INTO project_versions(project_id,version,summary,files,preview_html,runtime_state) VALUES(%s,1,'fixture',%s,'',%s)",(self.project,Jsonb(target),Jsonb(state)))
                conn.execute("INSERT INTO project_restores(project_id,id,version,status,result) VALUES(%s,%s,1,'running',%s)",(self.project,uuid.uuid4(),Jsonb({'previous_status':'error'})))
                conn.execute(sql.SQL("UPDATE {}.accounts SET name='before-operation'").format(sql.Identifier(self.schema)))
                conn.execute('UPDATE preview_storage SET data=%s,sessions=%s WHERE project_id=%s',(Jsonb({'draft':'before-operation'}),Jsonb({'tab-first':{'auth':'before-operation'}}),self.project))
            (root/'index.html').write_text('<h1>before operation</h1>')
            (private/'state').write_text('before operation')
            before=snapshot_files(root)
            async def checkpoint(project,operation,identity=None):
                from project_state_snapshots import checked_snapshot
                if operation=='capture': return await capture_state(project,service)
                if operation=='restore': return await restore_state(project,identity,service)
                if operation=='verify': return verify_state(project,identity,service)
                with service.connection() as conn: checked_snapshot(conn,project,identity)
                return {'valid':True}
            with patch('agent.ensure_workspace',return_value=root), patch('runtime.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('runtime.project_uid',return_value=os.getuid()), patch('jobs.connection',service.connection), patch('project_data_client.checkpoint',side_effect=checkpoint):
                with self.assertRaisesRegex(ValueError,'构建失败'):
                    await restore_version(self.project,1)
            self.assertEqual(snapshot_files(root),before)
            self.assertEqual((private/'state').read_text(),'before operation')
            self.assertFalse((root/'.atoms-snapshots/active-restore').exists())
            with service.connection() as conn:
                self.assertEqual(conn.execute(sql.SQL('SELECT name FROM {}.accounts').format(sql.Identifier(self.schema))).fetchone()['name'],'before-operation')
                self.assertEqual(conn.execute('SELECT data,sessions FROM preview_storage WHERE project_id=%s',(self.project,)).fetchone()['sessions']['tab-first']['auth'],'before-operation')
