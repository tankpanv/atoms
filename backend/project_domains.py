"""Project-owned domains with actual DNS ownership validation; no simulated connections."""
import os
import re
import secrets
import uuid
from urllib.parse import urlsplit
import httpx
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from auth import connection,current_user
router=APIRouter(prefix='/api/projects',tags=['domains'])

def init_domains(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS project_domains(domain TEXT PRIMARY KEY,project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        verification_token TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',verified_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')

def owned(conn,project_id,user):
    if not conn.execute('SELECT 1 FROM projects WHERE id=%s AND owner_id=%s',(project_id,user['id'])).fetchone():
        raise HTTPException(404,'Project not found')

def domain_name(value):
    try:name=value.strip().rstrip('.').encode('idna').decode().lower()
    except UnicodeError:raise HTTPException(422,'域名格式无效')
    if len(name)>253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',name):
        raise HTTPException(422,'请输入域名，不包含协议、端口或路径')
    return name

class DomainInput(BaseModel):
    domain:str=Field(min_length=3,max_length=253)

@router.get('/{project_id}/domains')
def domains(project_id:uuid.UUID,user=Depends(current_user)):
    with connection() as conn:
        owned(conn,project_id,user)
        rows=conn.execute('SELECT domain,status,verification_token FROM project_domains WHERE project_id=%s ORDER BY created_at',(project_id,)).fetchall()
    return {'target':os.getenv('PUBLISH_PUBLIC_HOST',''),'domains':rows,'gateway_configured':bool(os.getenv('PUBLISH_PUBLIC_HOST',''))}

@router.post('/{project_id}/domains',status_code=201)
def add_domain(project_id:uuid.UUID,data:DomainInput,user=Depends(current_user)):
    name=domain_name(data.domain)
    with connection() as conn:
        owned(conn,project_id,user)
        conn.execute('INSERT INTO project_domains(domain,project_id,verification_token) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(name,project_id,'atoms-verify='+secrets.token_hex(24)))
        row=conn.execute('SELECT domain,project_id,status,verification_token FROM project_domains WHERE domain=%s',(name,)).fetchone()
        if row['project_id']!=project_id:raise HTTPException(409,'该域名已被其他项目绑定')
        return {key:value for key,value in row.items() if key!='project_id'}

async def dns_records(name,kind):
    async with httpx.AsyncClient(timeout=10) as client:
        response=await client.get('https://dns.google/resolve',params={'name':name,'type':kind,'edns_client_subnet':'0.0.0.0/0'})
        response.raise_for_status();body=response.json()
        if body.get('Status')!=0 or body.get('TC'):return []
        return [item['data'] for item in body.get('Answer',[]) if item.get('type')==kind]

@router.post('/{project_id}/domains/{domain}/verify')
async def verify_domain(project_id:uuid.UUID,domain:str,user=Depends(current_user)):
    name=domain_name(domain)
    with connection() as conn:
        owned(conn,project_id,user)
        row=conn.execute('SELECT verification_token FROM project_domains WHERE project_id=%s AND domain=%s',(project_id,name)).fetchone()
        if not row:raise HTTPException(404,'域名不存在')
    try:
        txt=await dns_records('_atoms-verification.'+name,16)
        ownership=row['verification_token'] in [text.replace('"','') for text in txt]
        target=os.getenv('PUBLISH_PUBLIC_HOST','').strip().rstrip('.').lower()
        matched=False
        if target and ownership:
            cname=await dns_records(name,5)
            matched=target in [record.rstrip('.').lower() for record in cname]
            if not matched:
                address=await dns_records(name,1)
                expected=await dns_records(target,1) if not re.fullmatch(r'[0-9.]+',target) else [target]
                matched=bool(set(address)&set(expected))
    except (httpx.HTTPError,ValueError):raise HTTPException(503,'DNS 查询失败，请稍后重试')
    status='connected' if ownership and matched else 'verified' if ownership else 'pending'
    with connection() as conn:
        # Compare the verification token to protect removal/recreation during DNS lookup.
        conn.execute('UPDATE project_domains SET status=%s,verified_at=CASE WHEN %s THEN NOW() ELSE NULL END WHERE project_id=%s AND domain=%s AND verification_token=%s',(status,ownership,project_id,name,row['verification_token']))
    return {'status':status,'ownership_verified':ownership,'routing_verified':matched}

@router.delete('/{project_id}/domains/{domain}',status_code=204)
def remove_domain(project_id:uuid.UUID,domain:str,user=Depends(current_user)):
    with connection() as conn:
        owned(conn,project_id,user)
        conn.execute('DELETE FROM project_domains WHERE project_id=%s AND domain=%s',(project_id,domain_name(domain)))
