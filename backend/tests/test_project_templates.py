import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
import agent
import project_templates as templates
from test_agent_harness import example_plan


def default_plan(fullstack=False):
    plan=example_plan()
    plan['architecture']['frontend']={'stack':'Vite React TypeScript','directory':'frontend'}
    plan['architecture']['backend']={'required':fullstack,'reason':'Server persistence' if fullstack else 'Local game','stack':'FastAPI PostgreSQL' if fullstack else 'none','directory':'backend'}
    return plan

class TemplatePolicyTests(unittest.TestCase):
    def test_defaults_follow_architecture_and_explicit_user_stack(self):
        self.assertEqual(templates.select_template(default_plan(),'制作24点游戏'),'web-v1')
        self.assertEqual(templates.select_template(default_plan(True),'多人共享任务'),'fullstack-v1')
        for request in ['使用 Vue 写网站','Next.js + PostgreSQL','用 Django','用 React 18','使用 FastAPI MySQL','使用 Go 实现服务','微信小程序']:
            self.assertIsNone(templates.select_template(default_plan(True),request),request)
        self.assertIsNone(templates.select_template(default_plan(),'继续',[{'role':'user','content':'使用 Vue'}]))
        self.assertEqual(templates.select_template(default_plan(),'做个游戏',[{'role':'assistant','content':'could use Vue'}]),'web-v1')
        plan=default_plan();plan['application_type']='cli'
        self.assertIsNone(templates.select_template(plan,'开发命令行工具'))
        plan=default_plan(True);plan['architecture']['backend']['stack']='Node Express'
        self.assertIsNone(templates.select_template(plan,'开发网站'))

    def test_install_is_real_preflighted_and_keeps_user_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.atoms').mkdir();(root/'.atoms/PLAN.md').write_text('User goal')
            manifest=templates.install_template(root,'fullstack-v1')
            self.assertEqual(len(manifest['sha256']),64)
            config=json.loads((root/'.atoms-workspace.json').read_text())
            self.assertEqual(config['services'][0]['port_env'],'API_PORT')
            self.assertIn('base + \'api\'',(root/'frontend/vite.config.ts').read_text())
            self.assertTrue((root/'package-lock.json').is_file())
            self.assertTrue((root/'backend/app/main.py').is_file())
            self.assertEqual((root/'.atoms/PLAN.md').read_text(),'User goal')
            with self.assertRaises(ValueError):templates.install_template(root,'web-v1')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'frontend/src').mkdir(parents=True)
            existing=root/'frontend/src/App.tsx';existing.write_text('Original code')
            with self.assertRaises(ValueError):templates.install_template(root,'web-v1')
            self.assertEqual(existing.read_text(),'Original code')
            self.assertFalse((root/'package.json').exists())

    def test_dependency_cache_is_exact_and_project_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cache=root/'cache';cache.mkdir()
            templates.install_template(cache,'web-v1');(cache/'node_modules').mkdir()
            (cache/'node_modules/example').write_text('cached dependency')
            (cache/'frontend/node_modules').mkdir()
            (cache/'frontend/node_modules/nested-example').write_text('nested dependency')
            (cache/'fingerprint').write_text(templates.dependency_fingerprint(cache))
            with patch.object(templates,'CACHE_ROOT',cache):
                for name in ['a','b']:
                    project=root/name;project.mkdir();templates.install_template(project,'web-v1')
                    self.assertTrue(templates.restore_dependency_cache(project,os.getuid()))
                (root/'a/node_modules/example').write_text('changed only project A')
                (root/'a/frontend/node_modules/nested-example').write_text('changed workspace package')
                self.assertEqual((root/'b/frontend/node_modules/nested-example').read_text(),'nested dependency')
                self.assertEqual((cache/'frontend/node_modules/nested-example').read_text(),'nested dependency')
                self.assertEqual((root/'b/node_modules/example').read_text(),'cached dependency')
                self.assertEqual((cache/'node_modules/example').read_text(),'cached dependency')
                project=root/'c';project.mkdir();templates.install_template(project,'web-v1')
                (project/'frontend/package.json').write_text('{}')
                self.assertFalse(templates.restore_dependency_cache(project,os.getuid()))
                self.assertFalse((project/'node_modules').exists())
                project=root/'d';project.mkdir();templates.install_template(project,'web-v1')
                (project/'.npmrc').write_text('legacy-peer-deps=true')
                self.assertFalse(templates.restore_dependency_cache(project,os.getuid()))

class TemplateHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_enters_engineering_with_actual_source_and_no_generator(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/uuid.uuid4().hex;root.mkdir()
            captured=[]
            class Gateway:
                def __init__(self,*args):pass
                async def chat(self,client,state,messages,*args,**kwargs):
                    captured.append(messages)
                    raise RuntimeError('Reached product implementation')
            with patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}]), patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.ModelGateway',Gateway), patch('agent.ensure_npm_dependencies',AsyncMock(return_value=(0,'Real cache ready'))), patch('agent.scaffold_project',AsyncMock()) as scaffold:
                with self.assertRaisesRegex(RuntimeError,'Reached product implementation'):
                    await agent.run_agent(uuid.UUID(hex=root.name),'制作24点游戏','test',lambda *a,**kw:None,plan=json.dumps(default_plan()))
                scaffold.assert_not_awaited()
            evidence=json.loads((root/'.atoms/task-state.json').read_text())['evidence']
            self.assertEqual(evidence[0]['kind'],'install_default_template')
            handoff=json.dumps(captured[0],ensure_ascii=False)
            self.assertIn('默认模板编写规范',handoff)
            self.assertIn('export default function Index',handoff)
            self.assertIn('Required DESIGN.md structure',handoff)
            self.assertIn('Freely loadable fonts must have a usable local fallback',handoff)
            self.assertIn('Direction & Layout',handoff)
            self.assertIn('UI_COMPONENTS.md',handoff)
            self.assertNotIn('const ChartContext',handoff)
            self.assertNotIn('lockfileVersion',handoff)  # avoid wasting model context on lockfile

class CachedTemplateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.getuid()==0 and (templates.CACHE_ROOT/'fingerprint').exists(),'requires built worker dependency cache and root process isolation')
    async def test_real_cached_template_build_and_system_code_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder).chmod(0o711)
            root=Path(folder)/uuid.uuid4().hex;root.mkdir()
            project=uuid.UUID(hex=root.name)
            templates.install_template(root,'fullstack-v1')
            uid=agent.project_uid(project)
            agent.set_workspace_owner(root,uid)
            self.assertTrue(templates.restore_dependency_cache(root,uid))
            # Build immediately from the restored cache, before any install can hide a missing workspace package.
            with patch('agent.ensure_workspace',return_value=root):
                code,output=await agent.run_shell(project,'npm run build',90)
                self.assertEqual(code,0,output[-3000:])
            from verification_policy import inspect_sources
            code, report = inspect_sources(agent.snapshot_files(root))
            self.assertEqual(code, 0, report)
            with patch('agent.ensure_workspace',return_value=root):
                code, output = await agent.run_build(project)
                self.assertEqual(code, 0, output[-3000:])
                css='\n'.join(path.read_text() for path in (root/'dist/assets').glob('*.css'))
                self.assertIn('--primary',css)
                self.assertIn('.bg-primary',css)
                self.assertIn('.min-h-screen',css)

class ExplicitStackHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_version_is_not_initialized_using_latest_generator(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/uuid.uuid4().hex;root.mkdir()
            class Gateway:
                def __init__(self,*args):pass
                async def chat(self,*args,**kwargs):raise RuntimeError('Requested stack engineering')
            with patch('model_catalog.catalog',return_value=[{'id':'test','context':128000}]), patch('agent.ensure_workspace',return_value=root), patch('agent.project_uid',return_value=os.getuid()), patch('agent.ModelGateway',Gateway), patch('agent.scaffold_project',AsyncMock()) as scaffold:
                with self.assertRaisesRegex(RuntimeError,'Requested stack engineering'):
                    await agent.run_agent(uuid.UUID(hex=root.name),'使用 React 18 和 Vite 5 实现网站','test',lambda *a,**kw:None,plan=json.dumps(default_plan()))
                scaffold.assert_not_awaited()
            self.assertFalse((root/'.atoms/template.json').exists())
            self.assertFalse((root/'frontend').exists())
