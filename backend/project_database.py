"""Managed PostgreSQL connector: one restricted application role/schema per project."""
import hashlib
import hmac
import os
import uuid
from urllib.parse import urlsplit, urlunsplit, quote
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def names(project_id):
    project_id = uuid.UUID(str(project_id))
    return f'project_{project_id.hex}', f'atoms_app_{project_id.hex}'


def credentials(project_id, database_url=None, secret=None):
    project_id = uuid.UUID(str(project_id))
    database_url = database_url or os.environ['DATABASE_URL']
    secret = secret or os.getenv('AGENT_SECRET') or hashlib.sha256((database_url+':agent-control').encode()).hexdigest()
    schema, role = names(project_id)
    password = hmac.new(secret.encode(), b'application-database:'+project_id.bytes, hashlib.sha256).hexdigest()
    parsed = urlsplit(database_url)
    host = parsed.hostname or 'db'
    host = f'[{host}]' if ':' in host else host
    address = f'{host}:{parsed.port}' if parsed.port else host
    dsn = urlunsplit((parsed.scheme, f'{quote(role)}:{quote(password)}@{address}', parsed.path, parsed.query, ''))
    return schema, role, password, dsn


def init_connector(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS project_databases (
        project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        schema_name TEXT UNIQUE NOT NULL, role_name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    # Platform owner keeps its rights. Untrusted projects must not create objects
    # in the shared search path, including on databases upgraded from PG14.
    conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')


def provision(conn, project_id, database_url=None, secret=None):
    schema, role, password, _ = credentials(project_id, database_url, secret)
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 29))', (str(project_id),))
    if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
    else:
        conn.execute(sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
    owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s', (schema,)).fetchone()
    if owner and owner['rolname'] != role:
        raise ValueError('项目 Schema 所有者不匹配，拒绝覆盖')
    conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}').format(sql.Identifier(schema), sql.Identifier(role)))
    conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('ALTER ROLE {} SET search_path TO {}, pg_catalog').format(sql.Identifier(role), sql.Identifier(schema)))
    conn.execute(sql.SQL('ALTER ROLE {} SET statement_timeout TO {}').format(sql.Identifier(role), sql.Literal('30s')))
    conn.execute('''INSERT INTO project_databases(project_id,schema_name,role_name) VALUES(%s,%s,%s)
        ON CONFLICT(project_id) DO UPDATE SET schema_name=EXCLUDED.schema_name,role_name=EXCLUDED.role_name''', (project_id,schema,role))


def environment(project_id):
    project_id = uuid.UUID(str(project_id))
    worker_id = os.getenv('PROJECT_ID')
    if worker_id:
        if uuid.UUID(worker_id) != project_id:
            raise ValueError('数据库连接器不能跨项目使用')
        url = os.getenv('APP_DATABASE_URL', '')
        schema = os.getenv('APP_DATABASE_SCHEMA', '')
        if not url or not schema:
            return {}  # existing/custom projects do not implicitly change databases
        if schema != names(project_id)[0]:
            raise ValueError('数据库 Schema 与项目不匹配')
    elif os.getenv('DATABASE_URL'):
        schema, _, _, url = credentials(project_id)
    else:
        return {}  # isolated unit tests/no managed connector
    return {'APP_DATABASE_URL':url, 'APP_DATABASE_SCHEMA':schema}


def metadata(conn, project_id):
    row = conn.execute('SELECT schema_name,role_name,created_at FROM project_databases WHERE project_id=%s', (project_id,)).fetchone()
    return {'provider':'postgresql', 'managed':True, 'schema':row['schema_name'], 'role':row['role_name'], 'configured':True} if row else {'provider':'postgresql','managed':True,'configured':False}


def check(project_id):
    schema, role, _, url = credentials(project_id)
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as db:
        state = db.execute('SELECT current_schema() AS schema, current_user AS role').fetchone()
        if state['schema'] != schema or state['role'] != role:
            raise ValueError('数据库连接器隔离检查失败')
        tables = db.execute('SELECT COUNT(*) AS count FROM information_schema.tables WHERE table_schema=%s', (schema,)).fetchone()['count']
    return {'connected':True,'schema':schema,'role':role,'tables':tables}


def remove(conn, project_id):
    schema, role = names(project_id)
    owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s', (schema,)).fetchone()
    if owner and owner['rolname'] != role:
        raise ValueError('项目 Schema 所有者不匹配，拒绝删除')
    conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s AND pid<>pg_backend_pid()', (role,))
    conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
        conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    conn.execute('DELETE FROM project_databases WHERE project_id=%s', (project_id,))


def browse_tables(project_id, table=None, offset=0, limit=50):
    """Read only the owned project's schema using its restricted application role."""
    schema, role, _, url = credentials(project_id)
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as db:
        db.execute('SET TRANSACTION READ ONLY')
        db.execute("SET LOCAL statement_timeout = '5s'")
        state = db.execute('SELECT current_schema() AS schema, current_user AS role').fetchone()
        if state['schema'] != schema or state['role'] != role:
            raise ValueError('数据库连接器隔离检查失败')
        tables = db.execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (schema,)).fetchall()
        if table is None:
            return {'schema': schema, 'tables': tables}
        if table not in [item['name'] for item in tables]:
            raise KeyError(table)
        columns = db.execute('SELECT column_name AS name, data_type AS type, is_nullable AS nullable FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position', (schema,table)).fetchall()
        primary = db.execute("SELECT a.attname AS name FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attnum", (sql.Identifier(schema,table).as_string(db),)).fetchall()
        order = sql.SQL(', ').join(sql.Identifier(c['name']) for c in primary) if primary else sql.SQL('ctid')
        rows = db.execute(sql.SQL('SELECT * FROM {}.{} ORDER BY {} LIMIT %s OFFSET %s').format(sql.Identifier(schema),sql.Identifier(table),order),(limit+1,offset)).fetchall()
        def display(value):
            if value is None or isinstance(value,(str,int,float,bool)):
                return value[:10000] if isinstance(value,str) else value
            if isinstance(value,(dict,list)):
                import json
                return json.dumps(value,ensure_ascii=False,default=str)[:10000]
            return str(value)[:10000]
        return {'table':table,'columns':columns,'rows':[{key:display(value) for key,value in row.items()} for row in rows[:limit]],'offset':offset,'limit':limit,'has_more':len(rows)>limit}
