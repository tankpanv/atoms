"""Production application data is separate from the development connector."""
import hashlib
import hmac
from urllib.parse import quote, urlsplit, urlunsplit
from psycopg import sql
import uuid


def names(project_id):
    # Keep the application connector's identifier contract while using a
    # different identity, role, password and schema from development.
    identity = uuid.uuid5(project_id, 'atoms-production-database-v1')
    return f'project_{identity.hex}', f'atoms_app_{identity.hex}'


def credentials(project_id, database_url, secret):
    schema, role = names(project_id)
    password = hmac.new(secret.encode(), b'published-database:' + project_id.bytes, hashlib.sha256).hexdigest()
    parsed = urlsplit(database_url)
    host = parsed.hostname or 'db'
    host = f'[{host}]' if ':' in host else host
    address = f'{host}:{parsed.port}' if parsed.port else host
    return schema, role, password, urlunsplit((parsed.scheme, f'{quote(role)}:{password}@{address}', parsed.path, parsed.query, ''))


def provision(conn, project_id, database_url, secret):
    schema, role, password, url = credentials(project_id, database_url, secret)
    if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
    owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s', (schema,)).fetchone()
    if owner and owner['rolname'] != role:
        raise ValueError('发布数据库 Schema 所有者不匹配')
    conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}').format(sql.Identifier(schema), sql.Identifier(role)))
    conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('ALTER ROLE {} SET search_path TO {}, pg_catalog').format(sql.Identifier(role), sql.Identifier(schema)))
    conn.execute(sql.SQL('ALTER ROLE {} SET statement_timeout TO {}').format(sql.Identifier(role), sql.Literal('30s')))
    return schema, role, url


def remove(conn, project_id):
    schema, role = names(project_id)
    owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s', (schema,)).fetchone()
    if owner and owner['rolname'] != role:
        raise ValueError('发布数据库 Schema 所有者不匹配')
    conn.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s AND pid<>pg_backend_pid()', (role,))
    conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    if conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
        conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
