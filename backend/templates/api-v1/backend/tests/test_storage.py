import os
import unittest
import uuid
from fastapi.testclient import TestClient
from psycopg import sql
from backend.app.main import app
from backend.app.db import connection

@unittest.skipUnless(os.getenv('APP_DATABASE_URL') and os.getenv('APP_DATABASE_SCHEMA'), 'requires the real managed PostgreSQL connector')
class StorageTests(unittest.TestCase):
    def test_health_persistence_and_rollback(self):
        table = sql.Identifier('test_storage_' + uuid.uuid4().hex)
        try:
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/health').json(), {'status':'ok'})
                self.assertEqual(client.get('/api/missing').status_code, 404)
                with connection() as db:
                    db.execute(sql.SQL('CREATE TABLE {} (value TEXT)').format(table))
                    db.execute(sql.SQL('INSERT INTO {} VALUES (%s)').format(table), ('persisted',))
            with TestClient(app), connection() as db:
                self.assertEqual(db.execute(sql.SQL('SELECT value FROM {}').format(table)).fetchone()['value'], 'persisted')
            with self.assertRaises(ValueError):
                with connection() as db:
                    db.execute(sql.SQL('INSERT INTO {} VALUES (%s)').format(table), ('rollback',))
                    raise ValueError('abort')
            with connection() as db:
                self.assertEqual(db.execute(sql.SQL('SELECT COUNT(*) AS count FROM {}').format(table)).fetchone()['count'], 1)
        finally:
            with connection() as db:
                db.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(table))
