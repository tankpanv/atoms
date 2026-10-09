"""Managed PostgreSQL connector. Credentials are injected server-side, never bundled in UI."""
from contextlib import contextmanager
import os
import re
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def database_schema():
    schema = os.environ.get('APP_DATABASE_SCHEMA', '')
    if not re.fullmatch(r'project_[0-9a-f]{32}', schema):
        raise RuntimeError('缺少有效项目数据库 Schema；请使用平台数据库连接器配置')
    return schema


@contextmanager
def connection():
    url = os.environ.get('APP_DATABASE_URL', '')
    if not url:
        raise RuntimeError('缺少 APP_DATABASE_URL；请使用平台数据库连接器配置')
    schema = database_schema()
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as db:
        # Each request has its own connection/transaction. Exclude the platform public
        # schema; schema names are identifiers, user values use %s parameters.
        db.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
        role = db.execute('SELECT current_user AS role, current_schema() AS schema').fetchone()
        if role['role'] != 'atoms_app_' + schema.removeprefix('project_') or role['schema'] != schema:
            raise RuntimeError('数据库角色或 Schema 与项目不匹配')
        yield db  # context commits on success; rolls back and closes on exception


def initialize():
    with connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(current_schema(), 37))')
        # Add genuine domain tables/migrations here, not fabricated product data.
        db.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO schema_version VALUES (1) ON CONFLICT DO NOTHING')
