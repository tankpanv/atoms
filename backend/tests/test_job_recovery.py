"""Real queue persistence and user-stop behavior; no upstream calls."""
import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jobs
import test_billing as fixtures
from coding_runtime import ModelTemporaryError


class JobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with jobs.connection() as conn: jobs.init_jobs_db(conn)
        fixtures.BillingTests.setUp(self)
    def tearDown(self): fixtures.BillingTests.tearDown(self)

    async def test_running_record_without_executor_is_recovered_with_a_bound(self):
        with patch('jobs.process_project_queue',AsyncMock()) as executor:
            jobs.schedule_project(self.project)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(executor.await_count,1)
        with jobs.connection() as conn:
            row=conn.execute('SELECT status,recovery_attempts,retry_after FROM agent_jobs WHERE id=%s',(self.job,)).fetchone()
            self.assertEqual(row['status'],'queued');self.assertEqual(row['recovery_attempts'],1)
            self.assertIsNotNone(row['retry_after'])

    async def test_transient_error_requeues_same_job_and_waits_without_failure_message(self):
        with jobs.connection() as conn:
            conn.execute("UPDATE agent_jobs SET status='queued' WHERE id=%s",(self.job,))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('jobs.ensure_workspace',return_value=root),patch('jobs.make_plan',AsyncMock(side_effect=ModelTemporaryError('provider outage'))) as planner:
                await jobs.process_project_queue(self.project)
                await jobs.process_project_queue(self.project)
                self.assertEqual(planner.await_count,1)
        with jobs.connection() as conn:
            row=conn.execute('SELECT * FROM agent_jobs WHERE id=%s',(self.job,)).fetchone()
            self.assertEqual(row['status'],'queued')
            self.assertEqual(row['recovery_attempts'],1)
            self.assertIsNotNone(row['retry_after'])
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM messages WHERE project_id=%s AND role='assistant'",(self.project,)).fetchone()['n'],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM agent_steps WHERE job_id=%s AND kind='recovery'",(self.job,)).fetchone()['n'],1)
        jobs.stop_job(self.project,self.job)
        with jobs.connection() as conn:
            row=conn.execute('SELECT status,stop_requested FROM agent_jobs WHERE id=%s',(self.job,)).fetchone()
            self.assertEqual(row['status'],'stopped');self.assertTrue(row['stop_requested'])

    async def test_permanent_error_and_exhausted_recovery_do_not_loop(self):
        for attempts,error in [(0,RuntimeError('invalid configuration')),(jobs.RECOVERY_LIMIT,ModelTemporaryError('provider still unavailable'))]:
            with jobs.connection() as conn:
                conn.execute("UPDATE agent_jobs SET status='queued',recovery_attempts=%s,retry_after=NULL WHERE id=%s",(attempts,self.job))
            with tempfile.TemporaryDirectory() as directory,patch('jobs.ensure_workspace',return_value=Path(directory)),patch('jobs.make_plan',AsyncMock(side_effect=error)):
                await jobs.process_project_queue(self.project)
            with jobs.connection() as conn:
                row=conn.execute('SELECT status,recovery_attempts FROM agent_jobs WHERE id=%s',(self.job,)).fetchone()
                self.assertEqual(row['status'],'error');self.assertEqual(row['recovery_attempts'],attempts)
