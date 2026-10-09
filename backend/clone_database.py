"""Transactional schema clone with PostgreSQL's own DDL export and binary COPY."""
import json
import re
import uuid
from contextlib import nullcontext
from urllib.parse import quote, urlsplit
from psycopg import sql
from project_database import names
from project_clone import unpack_docker_stream

async def clone_database(source, target, connection, docker, compose_project, database_url, include_data, *, target_names=None, source_schema=None, destination_connection=None, prepare=None, read_source=None, finalize=None):
    source, target = uuid.UUID(str(source)), uuid.UUID(str(target))
    source_schema = source_schema or names(source)[0]
    target_schema, target_role = target_names or names(target)
    filters = quote(json.dumps({'label': [f'com.docker.compose.project={compose_project}', 'com.docker.compose.service=db']}))
    containers = (await docker('GET', f'/containers/json?filters={filters}')).json()
    if len(containers) != 1:
        raise ValueError('无法定位托管 PostgreSQL 服务')
    parsed = urlsplit(database_url)
    with connection() as original, (nullcontext(destination_connection) if destination_connection else connection()) as destination:
        original.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if read_source:
            read_source(original)
        if prepare:
            prepare(destination)
        exists = original.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (source_schema,)).fetchone()
        if not exists:
            if finalize:
                finalize(destination)
            return {'tables': 0, 'rows': 0}
        # Foreign schema dependencies would make a clone share data with the original.
        dependencies = original.execute('''SELECT 1 FROM pg_depend d JOIN pg_class c ON d.objid=c.oid
            JOIN pg_namespace n ON c.relnamespace=n.oid JOIN pg_class r ON d.refobjid=r.oid
            JOIN pg_namespace rn ON r.relnamespace=rn.oid
            WHERE n.nspname=%s AND rn.nspname NOT IN (%s,'pg_catalog') AND d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass LIMIT 1''', (source_schema,source_schema)).fetchone()
        if dependencies:
            raise ValueError('数据库引用其他项目的 Schema，不能安全克隆')
        snapshot = original.execute('SELECT pg_export_snapshot() AS snapshot').fetchone()['snapshot']
        async def ddl(section):
            command = ['pg_dump', '-U', parsed.username, '-d', parsed.path.lstrip('/'), '--schema', source_schema,
                       '--schema-only', '--no-owner', '--no-privileges', '--section', section, '--snapshot', snapshot]
            execution = (await docker('POST', f"/containers/{containers[0]['Id']}/exec", {'Cmd': command, 'AttachStdout': True, 'AttachStderr': True})).json()['Id']
            response = await docker('POST', f'/exec/{execution}/start', {'Detach': False, 'Tty': False})
            status = (await docker('GET', f'/exec/{execution}/json')).json()
            output, _ = unpack_docker_stream(response.content)
            if status.get('ExitCode') != 0:
                raise ValueError('PostgreSQL 数据库结构导出失败')
            text = output.decode().replace(source_schema, target_schema)
            text = '\n'.join(line for line in text.splitlines() if not line.startswith('\\') and not line.startswith(f'CREATE SCHEMA {target_schema};'))
            # pg_dump may reset search_path; fully qualified object names stay inside target.
            destination.execute(text, prepare=False)
        destination.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(target_role)))
        await ddl('pre-data')
        tables = original.execute('''SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p') AND NOT c.relispartition ORDER BY c.relname''', (source_schema,)).fetchall()
        row_count = 0
        if include_data:
            for table in tables:
                name = table['relname']
                columns = original.execute('''SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped AND a.attgenerated='' ORDER BY a.attnum''', (source_schema,name)).fetchall()
                if not columns:
                    continue
                column_sql = sql.SQL(',').join(sql.Identifier(item['attname']) for item in columns)
                with original.cursor().copy(sql.SQL('COPY (SELECT {} FROM {}.{}) TO STDOUT (FORMAT BINARY)').format(column_sql,sql.Identifier(source_schema),sql.Identifier(name))) as read:
                    with destination.cursor().copy(sql.SQL('COPY {}.{} ({}) FROM STDIN (FORMAT BINARY)').format(sql.Identifier(target_schema),sql.Identifier(name),column_sql)) as write:
                        for chunk in read:
                            write.write(chunk)
                row_count += original.execute(sql.SQL('SELECT COUNT(*) AS count FROM {}.{}').format(sql.Identifier(source_schema),sql.Identifier(name))).fetchone()['count']
            sequences = original.execute('SELECT sequencename FROM pg_sequences WHERE schemaname=%s', (source_schema,)).fetchall()
            for sequence in sequences:
                name = sequence['sequencename']
                state = original.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(source_schema),sql.Identifier(name))).fetchone()
                destination.execute('SELECT setval(%s::regclass,%s,%s)', (f'{target_schema}.{name}',state['last_value'],state['is_called']))
        await ddl('post-data')
        if finalize:
            destination.execute('RESET ROLE')
            destination.execute('SET LOCAL search_path TO public, pg_catalog')
            finalize(destination)
        return {'tables': len(tables), 'rows': row_count}
