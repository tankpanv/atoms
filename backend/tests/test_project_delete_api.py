"""Real database and coordinator deletion tests; disposable project for every test."""
import uuid
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app, connection, current_user, grant_project_role
from agent import ensure_workspace


class ProjectDeleteApiTests(unittest.TestCase):
    def setUp(self):
        self.owner=uuid.uuid4(); self.project=uuid.uuid4()
        with connection() as c:
            c.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'test-only')", (self.owner, f'delete-{self.owner}@example.test'))
            c.execute("INSERT INTO projects(id,owner_id,title,prompt,workspace_path) VALUES(%s,%s,'delete fixture','fixture',%s)", (self.project,self.owner,f'projects/{self.project.hex}'))
            grant_project_role(c,self.project)
        self.root=ensure_workspace(self.project,self.owner)
        (self.root/'fixture.txt').write_text('fixture')
        self.url=f'/api/projects/{self.project}'
        app.dependency_overrides[current_user]=lambda:{'id':self.owner}
        self.client=TestClient(app)

    def tearDown(self):
        app.dependency_overrides[current_user]=lambda:{'id':self.owner}
        response=self.client.delete(self.url)
        self.assertIn(response.status_code,(204,404),response.text)
        app.dependency_overrides.clear()
        self.client.close()
        with connection() as c: c.execute('DELETE FROM users WHERE id=%s',(self.owner,))

    def test_storage_failure_preserves_tombstone_blocks_work_then_retries(self):
        with patch('main.delete_project_storage',side_effect=RuntimeError('injected storage outage')):
            response=self.client.delete(self.url)
        self.assertEqual(response.status_code,503,response.text)
        with connection() as c:
            self.assertEqual(c.execute('SELECT status FROM projects WHERE id=%s',(self.project,)).fetchone()['status'],'deleting')
        self.assertTrue((self.root/'fixture.txt').is_file())
        self.assertEqual(self.client.post(self.url+'/open',json={}).status_code,409)
        self.assertEqual(self.client.post(self.url+'/messages',json={'content':'continue'}).status_code,409)
        self.assertEqual(self.client.patch(self.url,json={'title':'rename'}).status_code,409)
        response=self.client.delete(self.url)
        self.assertEqual(response.status_code,204,response.text)
        self.assertFalse(self.root.exists())
        with connection() as c:
            self.assertFalse(c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(f'atoms_project_{self.project.hex}',)).fetchone())

    def test_foreign_owner_cannot_delete_project(self):
        app.dependency_overrides[current_user]=lambda:{'id':uuid.uuid4()}
        self.assertEqual(self.client.delete(self.url).status_code,404)
        self.assertTrue((self.root/'fixture.txt').exists())

    def test_concurrent_mutation_excludes_delete_and_releases_lock(self):
        with connection() as c:
            c.execute('SELECT pg_advisory_lock_shared(hashtextextended(%s,17))',(str(self.project),))
            self.assertEqual(self.client.delete(self.url).status_code,409)
            self.assertTrue(self.root.exists())
            c.execute('SELECT pg_advisory_unlock_shared(hashtextextended(%s,17))',(str(self.project),))
        self.assertEqual(self.client.delete(self.url).status_code,204)

    def test_running_job_cascades_steps_and_project_configuration(self):
        job=uuid.uuid4()
        with connection() as c:
            c.execute("INSERT INTO agent_jobs(id,project_id,prompt,model,status) VALUES(%s,%s,'fixture','openai/gpt-6-luna','running')",(job,self.project))
            c.execute("INSERT INTO agent_steps(job_id,kind,label,detail) VALUES(%s,'result','fixture','fixture')",(job,))
            c.execute("UPDATE projects SET status='running' WHERE id=%s",(self.project,))
        response=self.client.delete(self.url)
        self.assertEqual(response.status_code,204,response.text)
        with connection() as c:
            self.assertFalse(c.execute('SELECT id FROM agent_jobs WHERE id=%s',(job,)).fetchone())
            self.assertFalse(c.execute('SELECT id FROM agent_steps WHERE job_id=%s',(job,)).fetchone())
