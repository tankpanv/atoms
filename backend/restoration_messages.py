"""One durable progress message per restoration, updated by the worker."""
import uuid
from datetime import datetime, timezone
from psycopg.types.json import Jsonb


def create_messages(conn, project_id, identity, version):
    metadata={'kind':'restore','id':str(identity),'version':version,'status':'running','phase':'正在校验版本文件','steps':[]}
    conn.execute("INSERT INTO messages(id,project_id,role,agent,content,restoration,created_at) VALUES(%s,%s,'user',NULL,%s,%s,clock_timestamp()) ON CONFLICT(id) DO NOTHING",
                 (uuid.uuid5(identity,'request'),project_id,f'从版本 v{version} 恢复',Jsonb(metadata)))
    conn.execute("INSERT INTO messages(id,project_id,role,agent,content,restoration,created_at) VALUES(%s,%s,'assistant','Mike',%s,%s,clock_timestamp()) ON CONFLICT(id) DO NOTHING",
                 (identity,project_id,f'正在还原版本 {version}，需要些时间，请等待…',Jsonb(metadata)))


def update_message(conn, project_id, *, phase=None, status='running', error='', result=None, identity=None):
    operation=conn.execute('SELECT id,version,phase FROM project_restores WHERE project_id=%s',(project_id,)).fetchone()
    if identity:
        pending=conn.execute('SELECT restoration FROM messages WHERE project_id=%s AND id=%s',(project_id,identity)).fetchone()
        if not pending:
            return
        operation={'id':identity,'version':pending['restoration']['version'],'phase':pending['restoration']['phase']}
    if not operation:
        return
    message=conn.execute('SELECT restoration FROM messages WHERE project_id=%s AND id=%s FOR UPDATE',(project_id,operation['id'])).fetchone()
    if not message:  # Older jobs did not create a chat card.
        return
    metadata=dict(message['restoration'])
    phase=phase or operation['phase']
    steps=list(metadata.get('steps',[]))
    if not steps or steps[-1]['phase']!=phase:
        steps.append({'phase':phase,'at':datetime.now(timezone.utc).isoformat()})
    metadata.update(status=status,phase=phase,steps=steps,error=error,result=result or {})
    version=operation['version']
    content=(f'已从版本 v{version} 恢复，代码、数据库和浏览器数据已同步，预览验证通过。' if status=='succeeded' else
             f'版本 {version} 还原失败：{error}' if status=='failed' else
             f'正在还原版本 {version}，需要些时间，请等待…\n{phase}')
    conn.execute('UPDATE messages SET content=%s,restoration=%s WHERE project_id=%s AND id=%s',(content,Jsonb(metadata),project_id,operation['id']))
