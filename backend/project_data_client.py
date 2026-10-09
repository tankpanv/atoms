"""Worker bridge to privileged project-scoped data checkpoints."""
import asyncio
import hashlib
import hmac
import os
import uuid
import time
import httpx


def settings(project_id):
    bound = os.getenv('PROJECT_ID')
    if bound and uuid.UUID(bound) != project_id:
        raise ValueError('数据快照不能跨项目使用')
    endpoint = os.getenv('STATE_CHECKPOINT_URL') or os.getenv('AGENT_SERVICES','http://agent-service:9001').split(',')[0]
    secret = os.getenv('AGENT_SECRET') or hashlib.sha256((os.environ['DATABASE_URL']+':agent-control').encode()).hexdigest()
    token = os.getenv('WORKER_TOKEN') if bound else hmac.new(secret.encode(),b'worker-project:'+project_id.bytes,hashlib.sha256).hexdigest()
    return endpoint.rstrip('/')+'/projects/'+str(project_id)+'/data-checkpoint', {'X-Worker-Token':token}


async def checkpoint(project_id, operation, identity=None):
    url, headers = settings(project_id)
    deadline=time.monotonic()+60
    async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=5)) as client:
        while True:
            try:
                result=await client.post(url,headers=headers,json={'operation':operation,'id':identity})
            except httpx.HTTPError as exc:
                if time.monotonic()>=deadline:
                    raise ValueError('数据检查点服务连接中断，等待重连后仍不可用') from exc
            else:
                if result.is_success:
                    return result.json()
                if result.status_code not in {502,503,504} or time.monotonic()>=deadline:
                    try:
                        detail=result.json().get('detail','数据库和浏览器数据检查点操作失败')
                    except ValueError:
                        detail='数据库和浏览器数据检查点操作失败'
                    raise ValueError(detail)
            # Snapshot IDs are immutable. Replaying a restore after a lost
            # reply restores the same state; its transaction and write lock
            # prevent a partially applied retry. Do not retry validation errors.
            await asyncio.sleep(min(2,max(0,deadline-time.monotonic())))
