"""Database-backed account isolation and atomic redemption regression checks."""
import hashlib
import unittest
import uuid
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from account import connection, init_account_db
from auth import current_user
from main import app


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.user = {"id": uuid.uuid4(), "email": "account-test-" + uuid.uuid4().hex + "@example.test"}
        self.other = {"id": uuid.uuid4(), "email": "account-test-" + uuid.uuid4().hex + "@example.test"}
        with connection() as conn:
            init_account_db(conn)
            for user in (self.user, self.other):
                conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,'test-only')", (user["id"], user["email"]))
        app.dependency_overrides[current_user] = lambda: self.user
        self.client = TestClient(app)
        self.code = "test-" + uuid.uuid4().hex
        self.code_hash = hashlib.sha256(self.code.encode()).hexdigest()

    def tearDown(self):
        app.dependency_overrides.clear()
        with connection() as conn:
            conn.execute("DELETE FROM account_credit_codes WHERE code_hash=%s", (self.code_hash,))
            conn.execute("DELETE FROM users WHERE id IN (%s,%s)", (self.user["id"], self.other["id"]))
        self.client.close()

    def test_preferences_and_account_isolation(self):
        response = self.client.patch('/api/account', json={"display_name": "New name", "preferences": {"theme": "system"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/account').json()['display_name'], 'New name')
        self.assertEqual(self.client.patch('/api/account', json={"preferences": {"default_model": "unconfigured/model"}}).status_code, 422)
        self.assertEqual(self.client.patch('/api/account', json={"display_name": "   "}).status_code, 422)
        self.assertEqual(self.client.patch('/api/account', json={"avatar": "data:image/svg+xml;base64,PHN2Zz4="}).status_code, 422)
        self.assertEqual(self.client.patch('/api/account', json={"credits": 99999, "plan": "Max"}).status_code, 422)
        app.dependency_overrides[current_user] = lambda: self.other
        self.assertNotEqual(self.client.get('/api/account').json()['display_name'], 'New name')
        self.assertEqual(self.client.get('/api/account').json()['credits'], 15)

    def test_code_is_redeemed_exactly_once_under_concurrency(self):
        with connection() as conn:
            conn.execute("INSERT INTO account_credit_codes(code_hash,credits) VALUES(%s,10)", (self.code_hash,))
        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(lambda _: self.client.post('/api/account/redeem', json={"code": self.code}).status_code, range(4)))
        self.assertEqual(sorted(statuses), [200, 400, 400, 400])
        self.assertEqual(self.client.get('/api/account').json()['credits'], 25)
        self.assertEqual(len(self.client.get('/api/account/transactions').json()), 2)

    def test_expired_and_invalid_codes_do_not_change_balance(self):
        with connection() as conn:
            conn.execute("INSERT INTO account_credit_codes(code_hash,credits,expires_at) VALUES(%s,10,NOW()-INTERVAL '1 day')", (self.code_hash,))
        self.assertEqual(self.client.post('/api/account/redeem', json={"code": self.code}).status_code, 400)
        self.assertEqual(self.client.get('/api/account').json()['credits'], 15)
        self.assertEqual([t['kind'] for t in self.client.get('/api/account/transactions').json()], ['daily_grant'])

    def test_storage_uses_recorded_relative_path_and_owner_scope(self):
        project_id = uuid.uuid4()
        root = Path(os.getenv('WORKSPACE_ROOT', '/workspaces'))
        directory = root / 'projects' / project_id.hex
        directory.mkdir(parents=True)
        try:
            (directory / 'file.txt').write_bytes(b'x' * 42)
            with connection() as conn:
                conn.execute("INSERT INTO projects(id,owner_id,title,prompt,workspace_path) VALUES(%s,%s,'Storage test','test',%s)", (project_id, self.user['id'], 'projects/' + project_id.hex))
            data = self.client.get('/api/account/storage').json()
            self.assertEqual(data['total_bytes'], 42)
            self.assertEqual(data['projects'][0]['files'], 1)
            app.dependency_overrides[current_user] = lambda: self.other
            self.assertEqual(self.client.get('/api/account/storage').json()['projects'], [])
        finally:
            with connection() as conn:
                conn.execute('DELETE FROM projects WHERE id=%s', (project_id,))
            (directory / 'file.txt').unlink()
            directory.rmdir()

    def test_account_requires_authentication(self):
        app.dependency_overrides.clear()
        self.assertEqual(self.client.get('/api/account').status_code, 401)

    def test_order_creation_does_not_grant_credits_until_paid(self):
        response = self.client.post('/api/account/billing/checkout', json={"plan": "Max", "credits": 500, "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'pending')
        self.assertEqual(self.client.get('/api/account').json()['plan'], 'Free')
        self.assertEqual(self.client.get('/api/account').json()['credits'], 15)


    def test_billing_owner_scope_and_price_tampering(self):
        response = self.client.post('/api/account/billing/topup', json={"amount_usd": "2", "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200)
        order = response.json()['id']
        self.assertEqual(self.client.post('/api/account/billing/topup', json={"amount_usd": "2", "idempotency_key": str(uuid.uuid4()), "grant": 99999}).status_code, 422)
        app.dependency_overrides[current_user] = lambda: self.other
        self.assertEqual(self.client.post(f'/api/account/orders/{order}/pay', json={"outcome": "success"}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/account/orders/{order}/receipt').status_code, 404)
        self.assertEqual(self.client.get('/api/account/orders').json(), [])
        app.dependency_overrides[current_user] = lambda: self.user
        self.assertEqual(self.client.post(f'/api/account/orders/{order}/pay', json={"outcome": "success"}).status_code, 200)
        receipt = self.client.get(f'/api/account/orders/{order}/receipt')
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()['receipt_type'], 'simulation_payment_receipt')
        self.assertEqual(self.client.get('/api/account').json()['credits'], 25)

    def test_usage_and_orders_require_authentication(self):
        app.dependency_overrides.clear()
        for path in ('/api/account/usage', '/api/account/orders', '/api/account/transactions'):
            self.assertEqual(self.client.get(path).status_code, 401)


if __name__ == '__main__':
    unittest.main()
