"""Immutable managed database/browser checkpoints; coordinator privilege only.

Application roles can use their working schema, never a historical checkpoint.
Restoring replaces structure, rows, constraints, functions and sequence state in
one transaction, together with the managed browser storage. No platform tables
or published application's database are replaced.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from psycopg import sql
from psycopg.types.json import Jsonb
from clone_database import clone_database
from project_database import names


def init_state_snapshots(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS project_state_snapshots (
        id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        schema_name TEXT UNIQUE NOT NULL, browser JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    conn.execute("ALTER TABLE preview_storage ADD COLUMN IF NOT EXISTS sessions JSONB NOT NULL DEFAULT '{}'::jsonb")
    conn.execute("ALTER TABLE preview_storage ADD COLUMN IF NOT EXISTS epoch UUID NOT NULL DEFAULT gen_random_uuid()")


def digest_rows(conn, schema):
    """Compare all table values and sequence generators without buffering data."""
    checksum = hashlib.sha256()
    tables = conn.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p') AND NOT c.relispartition ORDER BY c.relname", (schema,)).fetchall()
    for table in tables:
        checksum.update(table['relname'].encode())
        query = sql.SQL('COPY (SELECT row_to_json(t)::text FROM {}.{} t ORDER BY row_to_json(t)::text COLLATE "C") TO STDOUT').format(sql.Identifier(schema), sql.Identifier(table['relname']))
        with conn.cursor().copy(query) as stream:
            for chunk in stream:
                checksum.update(chunk)
    for sequence in conn.execute('SELECT sequencename FROM pg_sequences WHERE schemaname=%s ORDER BY sequencename', (schema,)).fetchall():
        name = sequence['sequencename']
        state = conn.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(name))).fetchone()
        checksum.update(json.dumps([name,state],sort_keys=True).encode())
    return checksum.hexdigest()


def structure_digest(conn, schema):
    queries = [
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.relispopulated,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) AS default FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum WHERE n.nspname=%s ORDER BY c.relname,a.attnum",
        "SELECT c.relname,co.conname,pg_get_constraintdef(co.oid,true) AS definition FROM pg_constraint co JOIN pg_class c ON co.conrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=%s ORDER BY c.relname,co.conname",
        "SELECT c.relname,pg_get_indexdef(c.oid) AS definition FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relkind IN ('i','I') ORDER BY c.relname",
        "SELECT p.proname,pg_get_functiondef(p.oid) AS definition FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE n.nspname=%s AND p.prokind IN ('f','p') ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
        "SELECT c.relname,pg_get_viewdef(c.oid,true) AS definition FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=%s AND c.relkind IN ('v','m') ORDER BY c.relname",
        "SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid,true) AS definition FROM pg_trigger t JOIN pg_class c ON t.tgrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=%s AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT t.typname,e.enumlabel FROM pg_type t JOIN pg_namespace n ON t.typnamespace=n.oid JOIN pg_enum e ON e.enumtypid=t.oid WHERE n.nspname=%s ORDER BY t.typname,e.enumsortorder",
        "SELECT policyname,tablename,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname=%s ORDER BY tablename,policyname",
        "SELECT t.typname,t.typtype,t.typnotnull,t.typdefault,format_type(t.typbasetype,t.typtypmod) AS base FROM pg_type t JOIN pg_namespace n ON t.typnamespace=n.oid WHERE n.nspname=%s AND t.typtype='d' ORDER BY t.typname",
    ]
    objects = [conn.execute(query,(schema,)).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(objects,default=str,sort_keys=True).replace(schema,'__project__').encode()).hexdigest()


def verify_state(project_id, identity, service):
    schema,_ = names(project_id)
    with service.connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        snapshot = checked_snapshot(conn,project_id,identity)
        if structure_digest(conn,schema)!=structure_digest(conn,snapshot['schema_name']) or digest_rows(conn,schema)!=digest_rows(conn,snapshot['schema_name']):
            raise ValueError('数据库结构、数据或序列与目标快照不一致，未确认还原成功')
        browser=conn.execute('SELECT data,sessions FROM preview_storage WHERE project_id=%s',(project_id,)).fetchone()
        if dict(browser or {'data':{},'sessions':{}})!=snapshot['browser']:
            raise ValueError('浏览器数据与目标快照不一致，未确认还原成功')
    return {'verified':True}


async def capture_state(project_id, service):
    identity = uuid.uuid4()
    checkpoint_schema = 'atoms_version_' + identity.hex
    checkpoint_role = 'atoms_checkpoint_' + identity.hex
    browser = {}
    source_identity = {}
    with service.connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,31))', (str(project_id),))
        def prepare(destination):
            destination.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(sql.Identifier(checkpoint_role)))
            destination.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(checkpoint_schema),sql.Identifier(checkpoint_role)))
            destination.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(checkpoint_schema)))
        def read_source(source):
            state = source.execute('SELECT data,sessions FROM preview_storage WHERE project_id=%s',(project_id,)).fetchone()
            browser.update(state or {'data':{},'sessions':{}})
            schema = names(project_id)[0]
            source_identity.update(rows=digest_rows(source,schema),structure=structure_digest(source,schema))
        def finalize(destination):
            if digest_rows(destination,checkpoint_schema)!=source_identity['rows'] or structure_digest(destination,checkpoint_schema)!=source_identity['structure']:
                raise ValueError('数据库快照结构或数据验证失败，未保存不完整版本')
            destination.execute('INSERT INTO project_state_snapshots(id,project_id,schema_name,browser) VALUES(%s,%s,%s,%s)',(identity,project_id,checkpoint_schema,Jsonb(browser)))
        result = await clone_database(project_id,project_id,service.connection,service.docker,service.COMPOSE_PROJECT,service.DATABASE_URL,True,
                                      target_names=(checkpoint_schema,checkpoint_role),destination_connection=conn,prepare=prepare,read_source=read_source,finalize=finalize)
    return {'format':1,'id':str(identity),**result}


def checked_snapshot(conn, project_id, identity):
    identity = uuid.UUID(str(identity))
    snapshot = conn.execute('SELECT schema_name,browser FROM project_state_snapshots WHERE id=%s AND project_id=%s',(identity,project_id)).fetchone()
    if not snapshot or snapshot['schema_name'] != 'atoms_version_'+identity.hex:
        raise ValueError('数据快照缺失或不属于当前项目；当前数据未修改')
    return snapshot


async def restore_state(project_id, identity, service):
    schema, role = names(project_id)
    with service.connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,31))',(str(project_id),))
        snapshot = checked_snapshot(conn,project_id,identity)
        owner = conn.execute('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=%s',(schema,)).fetchone()
        if not owner or owner['rolname'] != role:
            raise ValueError('工作数据库所有者不匹配；当前数据未修改')
        incoming = conn.execute('''SELECT 1 FROM pg_constraint co JOIN pg_class target ON target.oid=co.confrelid
            JOIN pg_namespace tn ON tn.oid=target.relnamespace JOIN pg_class source ON source.oid=co.conrelid
            JOIN pg_namespace sn ON sn.oid=source.relnamespace WHERE tn.nspname=%s AND sn.nspname<>%s LIMIT 1''',(schema,schema)).fetchone()
        incoming_view = conn.execute('''SELECT 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid=d.objid
            JOIN pg_class v ON v.oid=r.ev_class JOIN pg_namespace vn ON vn.oid=v.relnamespace
            JOIN pg_class c ON c.oid=d.refobjid JOIN pg_namespace cn ON cn.oid=c.relnamespace
            WHERE d.classid='pg_rewrite'::regclass AND d.refclassid='pg_class'::regclass AND cn.nspname=%s AND vn.nspname<>%s LIMIT 1''',(schema,schema)).fetchone()
        if incoming or incoming_view:
            raise ValueError('其他 Schema 引用了项目数据库，不能安全覆盖；当前数据未修改')
        conn.execute("SET LOCAL lock_timeout='10s'")
        source_digest = digest_rows(conn,snapshot['schema_name'])
        source_structure = structure_digest(conn,snapshot['schema_name'])
        def prepare(destination):
            # The worker has stopped all app processes before this RPC. Locking
            # schema objects additionally drains previously accepted DB writes.
            destination.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            destination.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(schema),sql.Identifier(role)))
            destination.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
        def finalize(destination):
            if digest_rows(destination,schema) != source_digest or structure_digest(destination,schema)!=source_structure:
                raise ValueError('还原数据库数据或序列校验不一致，事务未提交')
            browser = snapshot['browser']
            destination.execute('''INSERT INTO preview_storage(project_id,data,sessions,epoch) VALUES(%s,%s,%s,gen_random_uuid())
                ON CONFLICT(project_id) DO UPDATE SET data=EXCLUDED.data,sessions=EXCLUDED.sessions,epoch=EXCLUDED.epoch,updated_at=NOW()''',
                (project_id,Jsonb(browser['data']),Jsonb(browser['sessions'])))
            destination.execute('DELETE FROM preview_tokens WHERE project_id=%s',(project_id,))
        result = await clone_database(project_id,project_id,service.connection,service.docker,service.COMPOSE_PROJECT,service.DATABASE_URL,True,
                                      source_schema=snapshot['schema_name'],destination_connection=conn,prepare=prepare,finalize=finalize)
    return {'restored':True,**result}


def remove_state_snapshots(conn, project_id):
    for snapshot in conn.execute('SELECT id,schema_name FROM project_state_snapshots WHERE project_id=%s',(project_id,)).fetchall():
        if snapshot['schema_name'] != 'atoms_version_'+snapshot['id'].hex:
            raise ValueError('历史数据库 Schema 标识无效，拒绝删除')
        conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(snapshot['schema_name'])))
        role = 'atoms_checkpoint_'+snapshot['id'].hex
        conn.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    conn.execute('DELETE FROM project_state_snapshots WHERE project_id=%s',(project_id,))
