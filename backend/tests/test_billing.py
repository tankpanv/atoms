"""Exercise the real PostgreSQL billing transactions, including concurrency."""
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from account import connection, ensure_profile
from billing import create_order, pay_order, reserve_request, settle_request, reject_request, grant_due


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': uuid.uuid4(), 'email': 'billing-' + uuid.uuid4().hex + '@example.test'}
        self.project = uuid.uuid4()
        self.job = uuid.uuid4()
        with connection() as conn:
            conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'test-only')", (self.user['id'], self.user['email']))
            ensure_profile(conn, self.user)
            conn.execute("INSERT INTO projects(id,owner_id,title,prompt) VALUES(%s,%s,'Billing test','test')", (self.project, self.user['id']))
            conn.execute("INSERT INTO agent_jobs(id,project_id,prompt,model,status) VALUES(%s,%s,'test','openai/gpt-6-luna','running')", (self.job, self.project))

    def tearDown(self):
        with connection() as conn:
            conn.execute('DELETE FROM projects WHERE id=%s', (self.project,))
            conn.execute('DELETE FROM users WHERE id=%s', (self.user['id'],))

    def profile(self):
        with connection() as conn:
            return conn.execute('SELECT * FROM account_profiles WHERE user_id=%s', (self.user['id'],)).fetchone()

    def test_deleted_project_settles_usage_without_retaining_model_response(self):
        request = uuid.uuid4()
        with connection() as conn:
            reserve_request(conn, request, self.project, self.job, 'openai/gpt-6-luna', 'IMPLEMENT', 4096)
            conn.execute('DELETE FROM projects WHERE id=%s', (self.project,))
        with connection() as conn:
            row = settle_request(conn, request, {'prompt_tokens': 10, 'completion_tokens': 20}, {'choices': [{'content': 'project source'}]})
        self.assertIsNone(row['project_id'])
        self.assertIsNone(row['job_id'])
        self.assertIsNone(row['response'])
        self.assertEqual(self.profile()['reserved_credits'], 0)
        self.assertEqual(self.profile()['credits'], Decimal('14.99994500'))

    def test_deleting_project_cannot_reserve_another_model_call(self):
        with connection() as conn:
            conn.execute("UPDATE projects SET status='deleting' WHERE id=%s", (self.project,))
        with connection() as conn:
            with self.assertRaises(HTTPException) as rejected:
                reserve_request(conn, uuid.uuid4(), self.project, self.job, 'openai/gpt-6-luna', 'IMPLEMENT', 4096)
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(self.profile()['reserved_credits'], 0)

    def test_concurrent_payment_only_credits_once_and_server_prices(self):
        key = uuid.uuid4()
        with connection() as conn:
            order = create_order(conn, self.user['id'], key, 'topup', Decimal('2.50'))
            same = create_order(conn, self.user['id'], key, 'topup', Decimal('2.50'))
        self.assertEqual(order['id'], same['id'])
        def pay(_):
            with connection() as conn:
                return pay_order(conn, self.user['id'], order['id'])['status']
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(list(pool.map(pay, range(6))), ['paid'] * 6)
        self.assertEqual(self.profile()['credits'], Decimal('27.5'))
        with connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM account_transactions WHERE order_id=%s', (order['id'],)).fetchone()['n'], 1)
            with self.assertRaises(HTTPException):
                create_order(conn, self.user['id'], key, 'topup', Decimal('100'))

    def test_failed_cancelled_and_expired_orders_do_not_credit(self):
        for outcome in ('fail', 'cancel'):
            with connection() as conn:
                order = create_order(conn, self.user['id'], uuid.uuid4(), 'topup', Decimal(1))
                pay_order(conn, self.user['id'], order['id'], outcome)
            with connection() as conn:
                with self.assertRaises(HTTPException):
                    pay_order(conn, self.user['id'], order['id'])
        with connection() as conn:
            order = create_order(conn, self.user['id'], uuid.uuid4(), 'topup', Decimal(1))
            conn.execute("UPDATE billing_orders SET expires_at=NOW()-INTERVAL '1 minute' WHERE id=%s", (order['id'],))
        with connection() as conn:
            with self.assertRaises(HTTPException):
                pay_order(conn, self.user['id'], order['id'])
        self.assertEqual(self.profile()['credits'], 15)

    def test_exact_decimal_charge_duplicate_settlement_and_release(self):
        request = uuid.uuid4()
        with connection() as conn:
            row, created = reserve_request(conn, request, self.project, self.job, 'openai/gpt-6-luna', 'IMPLEMENT', 4096)
        self.assertTrue(created)
        self.assertGreater(self.profile()['reserved_credits'], 0)
        def settle(_):
            with connection() as conn:
                return settle_request(conn, request, {'prompt_tokens': 10, 'completion_tokens': 20}, {'choices': []})['charged_credits']
        with ThreadPoolExecutor(max_workers=4) as pool:
            charges = list(pool.map(settle, range(4)))
        self.assertEqual(charges, [Decimal('0.00005500')] * 4)
        self.assertEqual(self.profile()['credits'], Decimal('14.99994500'))
        self.assertEqual(self.profile()['reserved_credits'], 0)
        with connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM account_transactions WHERE request_id=%s', (request,)).fetchone()['n'], 1)

    def test_parallel_reservations_cannot_spend_the_same_available_balance(self):
        with connection() as conn:
            conn.execute('UPDATE account_profiles SET credits=.54 WHERE user_id=%s', (self.user['id'],))
        def reserve(_):
            try:
                with connection() as conn:
                    reserve_request(conn, uuid.uuid4(), self.project, self.job, 'openai/gpt-6-luna', 'IMPLEMENT', 4096)
                return 200
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(reserve, range(4))), [200, 402, 402, 402])
        self.assertLessEqual(self.profile()['reserved_credits'], self.profile()['credits'])

    def test_unknown_usage_is_not_guessed_or_refunded_and_rejected_is_released(self):
        request = uuid.uuid4()
        with connection() as conn:
            reserve_request(conn, request, self.project, self.job, 'openai/gpt-6-luna', 'PLAN', 100)
        with connection() as conn:
            with self.assertRaises(ValueError):
                settle_request(conn, request, {})
        self.assertGreater(self.profile()['reserved_credits'], 0)
        with connection() as conn:
            reject_request(conn, request, 'upstream HTTP 400')
            reject_request(conn, request, 'same failure')
        self.assertEqual(self.profile()['reserved_credits'], 0)
        self.assertEqual(self.profile()['credits'], 15)

    def test_annual_subscription_grants_monthly_only_once(self):
        with connection() as conn:
            order = create_order(conn, self.user['id'], uuid.uuid4(), 'subscription', plan='Pro', credits=350, annual=True)
            self.assertEqual(order['amount_usd'], Decimal('688.80'))
            pay_order(conn, self.user['id'], order['id'])
            conn.execute("UPDATE account_profiles SET next_grant_at=NOW()-INTERVAL '1 day' WHERE user_id=%s", (self.user['id'],))
            grant_due(conn, self.user['id'])
            grant_due(conn, self.user['id'])
        self.assertEqual(self.profile()['credits'], 715)
        self.assertEqual(self.profile()['plan'], 'Pro')

    def test_daily_free_limit_is_real_and_resets_next_month(self):
        now = datetime.now(timezone.utc)
        with connection() as conn:
            grant_due(conn, self.user['id'], now + timedelta(days=1))
            grant_due(conn, self.user['id'], now + timedelta(days=2))
        self.assertEqual(self.profile()['credits'], 25)


    def test_request_identity_and_running_job_are_validated(self):
        request = uuid.uuid4()
        with connection() as conn:
            reserve_request(conn, request, self.project, self.job, 'openai/gpt-6-luna', 'PLAN', 100, 'original')
        for stage, digest, job in [('REVIEW', 'original', self.job), ('PLAN', 'changed', self.job), ('PLAN', 'original', uuid.uuid4())]:
            with connection() as conn:
                with self.assertRaises(HTTPException):
                    reserve_request(conn, request, self.project, job, 'openai/gpt-6-luna', stage, 100, digest)
        with connection() as conn:
            conn.execute("UPDATE agent_jobs SET status='done' WHERE id=%s", (self.job,))
        with connection() as conn:
            with self.assertRaises(HTTPException):
                reserve_request(conn, uuid.uuid4(), self.project, self.job, 'openai/gpt-6-luna', 'PLAN', 100)

    def test_monthly_plan_and_downgrade_preserve_paid_credits(self):
        with connection() as conn:
            order = create_order(conn, self.user['id'], uuid.uuid4(), 'subscription', plan='Pro', credits=100)
            self.assertEqual(order['amount_usd'], 20)
            pay_order(conn, self.user['id'], order['id'])
            downgrade = create_order(conn, self.user['id'], uuid.uuid4(), 'subscription', plan='Free', credits=0)
            pay_order(conn, self.user['id'], downgrade['id'])
        self.assertEqual(self.profile()['credits'], 115)
        self.assertEqual(self.profile()['plan'], 'Free')
        self.assertIsNone(self.profile()['next_grant_at'])


if __name__ == '__main__':
    unittest.main()
