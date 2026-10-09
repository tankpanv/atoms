"""GitHub OAuth account linking and private/public repository access."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import urllib.parse
import uuid
import zipfile
import io
import stat
import shutil
import tempfile
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from auth import connection, current_user

router = APIRouter(prefix="/api/account/connectors/github", tags=["github connector"])
API = 'https://api.github.com'
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Atoms-Connector'}


def init_github_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS github_connections (
        user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        github_user_id BIGINT NOT NULL UNIQUE, login TEXT NOT NULL, avatar_url TEXT NOT NULL DEFAULT '',
        token_ciphertext BYTEA NOT NULL, scope TEXT NOT NULL DEFAULT '', token_type TEXT NOT NULL DEFAULT 'bearer',
        expires_at TIMESTAMPTZ, refresh_ciphertext BYTEA, refresh_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.execute("""CREATE TABLE IF NOT EXISTS github_oauth_states (
        state_hash TEXT PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_verifier_ciphertext BYTEA NOT NULL, expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.execute("ALTER TABLE github_oauth_states ADD COLUMN IF NOT EXISTS code_verifier_ciphertext BYTEA")
    conn.execute("""CREATE TABLE IF NOT EXISTS github_project_repositories (
        project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        repository_id BIGINT NOT NULL, full_name TEXT NOT NULL, html_url TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main', imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def config():
    client_id = os.getenv('GITHUB_CLIENT_ID', '').strip()
    client_secret = os.getenv('GITHUB_CLIENT_SECRET', '').strip()
    callback = os.getenv('GITHUB_CALLBACK_URL', '').strip()
    encoded = os.getenv('GITHUB_TOKEN_ENCRYPTION_KEY', '').strip()
    if not all((client_id, client_secret, callback, encoded)):
        raise HTTPException(503, 'GitHub 连接器未配置。请设置 GITHUB_CLIENT_ID、GITHUB_CLIENT_SECRET、GITHUB_CALLBACK_URL 和 GITHUB_TOKEN_ENCRYPTION_KEY。')
    try:
        key = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
    except (ValueError, TypeError):
        key = b''
    parsed = urllib.parse.urlsplit(callback)
    if len(key) != 32 or parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(503, 'GitHub 配置无效：密钥必须是 32 字节 Base64，回调地址必须是 HTTPS。')
    return client_id, client_secret, callback, key


def encrypt(value: str, key: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, value.encode(), b'atoms-github-oauth-v1')


def decrypt(value: bytes, key: bytes) -> str:
    raw = bytes(value)
    return AESGCM(key).decrypt(raw[:12], raw[12:], b'atoms-github-oauth-v1').decode()


def redirect_result(result: str):
    public_url = os.getenv('APP_PUBLIC_URL', '').strip().rstrip('/')
    if not public_url:
        callback = os.getenv('GITHUB_CALLBACK_URL', '')
        parsed = urllib.parse.urlsplit(callback)
        public_url = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme and parsed.netloc else ''
    return RedirectResponse((public_url or '/') + '/zh/dashboard?settings=connectors&github=' + urllib.parse.quote(result), status_code=303)


@router.get('')
def status(user=Depends(current_user)):
    with connection() as conn:
        account = conn.execute('SELECT login,avatar_url,scope,expires_at,created_at,updated_at FROM github_connections WHERE user_id=%s', (user['id'],)).fetchone()
    ready = all(os.getenv(key, '').strip() for key in ('GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET', 'GITHUB_CALLBACK_URL', 'GITHUB_TOKEN_ENCRYPTION_KEY'))
    return {'connected': bool(account), 'account': account, 'configured': ready}


@router.get('/authorize')
def authorize(user=Depends(current_user)):
    client_id, _, callback, key = config()
    state = secrets.token_urlsafe(48)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with connection() as conn:
        conn.execute('DELETE FROM github_oauth_states WHERE expires_at<NOW()')
        conn.execute("INSERT INTO github_oauth_states(state_hash,user_id,code_verifier_ciphertext,expires_at) VALUES(%s,%s,%s,NOW()+INTERVAL '10 minutes')", (hashlib.sha256(state.encode()).hexdigest(), user['id'], encrypt(verifier,key)))
    query = urllib.parse.urlencode({'client_id': client_id, 'redirect_uri': callback, 'scope': 'repo', 'state': state, 'code_challenge':challenge, 'code_challenge_method':'S256', 'allow_signup': 'true'})
    return {'url': 'https://github.com/login/oauth/authorize?' + query}


@router.get('/callback', include_in_schema=False)
async def callback(code: str = '', state: str = '', error: str = ''):
    if error:
        return redirect_result('denied')
    if not code or not state:
        return redirect_result('invalid')
    try:
        client_id, secret, redirect_uri, key = config()
    except HTTPException:
        return redirect_result('not-configured')
    with connection() as conn:
        state_row = conn.execute('DELETE FROM github_oauth_states WHERE state_hash=%s AND expires_at>NOW() RETURNING user_id,code_verifier_ciphertext', (hashlib.sha256(state.encode()).hexdigest(),)).fetchone()
    if not state_row:
        return redirect_result('state-expired')
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            verifier = decrypt(state_row['code_verifier_ciphertext'],key)
            token_response = await client.post('https://github.com/login/oauth/access_token', data={'client_id': client_id, 'client_secret': secret, 'code': code, 'redirect_uri': redirect_uri,'code_verifier':verifier}, headers={**HEADERS, 'Accept': 'application/json'})
            token_response.raise_for_status()
            token_data = token_response.json()
            token = token_data.get('access_token')
            if token_data.get('error') or not token:
                raise ValueError('GitHub token exchange failed')
            profile_response = await client.get(API + '/user', headers={**HEADERS, 'Authorization': 'Bearer ' + token})
            profile_response.raise_for_status()
            profile = profile_response.json()
        if not profile.get('id') or not profile.get('login'):
            raise ValueError('GitHub profile is incomplete')
        expires = token_data.get('expires_in')
        refresh_expires = token_data.get('refresh_token_expires_in')
        with connection() as conn:
            conn.execute("""INSERT INTO github_connections(user_id,github_user_id,login,avatar_url,token_ciphertext,scope,token_type,expires_at,refresh_ciphertext,refresh_expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s IS NULL THEN NULL ELSE NOW()+(%s * INTERVAL '1 second') END,%s,
                    CASE WHEN %s IS NULL THEN NULL ELSE NOW()+(%s * INTERVAL '1 second') END)
                ON CONFLICT(user_id) DO UPDATE SET github_user_id=EXCLUDED.github_user_id,login=EXCLUDED.login,avatar_url=EXCLUDED.avatar_url,
                token_ciphertext=EXCLUDED.token_ciphertext,scope=EXCLUDED.scope,token_type=EXCLUDED.token_type,expires_at=EXCLUDED.expires_at,
                refresh_ciphertext=EXCLUDED.refresh_ciphertext,refresh_expires_at=EXCLUDED.refresh_expires_at,updated_at=NOW()""",
                (state_row['user_id'], profile['id'], profile['login'], profile.get('avatar_url', ''), encrypt(token, key), token_data.get('scope', ''), token_data.get('token_type', 'bearer'), expires, expires,
                 encrypt(token_data['refresh_token'], key) if token_data.get('refresh_token') else None, refresh_expires, refresh_expires))
        return redirect_result('connected')
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return redirect_result('failed')


async def access_token(user_id):
    client_id, secret, _, key = config()
    with connection() as conn:
        saved = conn.execute('SELECT * FROM github_connections WHERE user_id=%s', (user_id,)).fetchone()
    if not saved:
        raise HTTPException(409, '请先连接 GitHub 账户')
    if saved['expires_at'] and saved['expires_at'] <= datetime.now(timezone.utc):
        if not saved['refresh_ciphertext'] or not saved['refresh_expires_at'] or saved['refresh_expires_at'] <= datetime.now(timezone.utc):
            raise HTTPException(401, 'GitHub 授权已过期，请重新连接')
        try:
            refresh = decrypt(saved['refresh_ciphertext'], key)
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post('https://github.com/login/oauth/access_token', data={'client_id': client_id, 'client_secret': secret, 'grant_type': 'refresh_token', 'refresh_token': refresh}, headers={**HEADERS, 'Accept': 'application/json'})
                response.raise_for_status()
                token_data = response.json()
            if not token_data.get('access_token') or not token_data.get('refresh_token'):
                raise ValueError('GitHub token refresh failed')
            with connection() as conn:
                conn.execute("""UPDATE github_connections SET token_ciphertext=%s,expires_at=NOW()+(%s * INTERVAL '1 second'),refresh_ciphertext=%s,
                    refresh_expires_at=NOW()+(%s * INTERVAL '1 second'),updated_at=NOW() WHERE user_id=%s""",
                    (encrypt(token_data['access_token'], key), token_data['expires_in'], encrypt(token_data['refresh_token'], key), token_data['refresh_token_expires_in'], user_id))
            return token_data['access_token']
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(401, 'GitHub 授权已过期，请重新连接') from exc
    try:
        return decrypt(saved['token_ciphertext'], key)
    except (ValueError, TypeError) as exc:
        raise HTTPException(500, 'GitHub 令牌解密失败，请检查密钥配置') from exc


@router.get('/repositories')
async def repositories(user=Depends(current_user)):
    token = await access_token(user['id'])
    result = []
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, 11):
            response = await client.get(API + '/user/repos', params={'visibility': 'all', 'affiliation': 'owner,collaborator,organization_member', 'sort': 'updated', 'per_page': 100, 'page': page}, headers={**HEADERS, 'Authorization': 'Bearer ' + token})
            if response.status_code in (401, 403):
                raise HTTPException(401, 'GitHub 已拒绝授权，请重新连接')
            if response.is_error:
                raise HTTPException(502, f'GitHub 仓库列表请求失败（HTTP {response.status_code}）')
            batch = response.json()
            result.extend({'id': repo['id'], 'full_name': repo['full_name'], 'name': repo['name'], 'private': repo['private'], 'html_url': repo['html_url'], 'default_branch': repo['default_branch'], 'description': repo.get('description')} for repo in batch)
            if len(batch) < 100:
                break
    return result


@router.delete('')
async def disconnect(user=Depends(current_user)):
    client_id, secret, _, key = config()
    with connection() as conn:
        saved = conn.execute('SELECT token_ciphertext FROM github_connections WHERE user_id=%s', (user['id'],)).fetchone()
    if saved:
        token = decrypt(saved['token_ciphertext'], key)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f'{API}/applications/{urllib.parse.quote(client_id,safe="")}/grant', auth=(client_id,secret), json={'access_token':token}, headers=HEADERS)
        if response.status_code not in (204, 404):
            raise HTTPException(502, 'GitHub 尚未确认撤销授权，请稍后重试')
    with connection() as conn:
        conn.execute('DELETE FROM github_connections WHERE user_id=%s', (user['id'],))
    return {'connected': False}


@router.get('/projects')
def import_targets(user=Depends(current_user)):
    with connection() as conn:
        return conn.execute("SELECT id,title,status FROM projects WHERE owner_id=%s ORDER BY updated_at DESC", (user['id'],)).fetchall()


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository_id: int
    full_name: str = Field(min_length=3, max_length=200, pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
    default_branch: str = Field(min_length=1, max_length=200)


@router.post('/import')
async def import_repository(data: ImportRequest, user=Depends(current_user)):
    token = await access_token(user['id'])
    owner, name = data.full_name.split('/')
    archive_bytes = bytearray()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=True) as client:
        repository = await client.get(f'{API}/repos/{owner}/{name}', headers={**HEADERS, 'Authorization': 'Bearer ' + token})
        if repository.status_code == 404:
            raise HTTPException(404, '仓库不存在或当前 GitHub 授权无权访问')
        if repository.status_code in (401, 403):
            raise HTTPException(403, '当前 GitHub 授权无法读取该仓库')
        repository.raise_for_status()
        if repository.json().get('id') != data.repository_id or repository.json().get('default_branch') != data.default_branch:
            raise HTTPException(409, '仓库信息已变化，请刷新仓库列表后重试')
        async with client.stream('GET', f'{API}/repos/{owner}/{name}/zipball/{urllib.parse.quote(data.default_branch, safe="")}', headers={**HEADERS, 'Authorization': 'Bearer ' + token}) as response:
            if response.status_code == 404:
                raise HTTPException(404, '仓库不存在或当前 GitHub 授权无权访问')
            if response.status_code in (401, 403):
                raise HTTPException(403, '当前 GitHub 授权无法下载该仓库')
            response.raise_for_status()
            if int(response.headers.get('content-length', '0') or 0) > 50 * 1024 * 1024:
                raise HTTPException(413, '仓库压缩包超过 50 MB')
            async for chunk in response.aiter_bytes():
                archive_bytes.extend(chunk)
                if len(archive_bytes) > 50 * 1024 * 1024:
                    raise HTTPException(413, '仓库压缩包超过 50 MB')
    project_id = uuid.uuid4()
    workspace_relative = f'projects/{project_id.hex}'
    storage = Path(os.getenv('WORKSPACE_ROOT', '/workspaces')).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.github-import-', dir=storage))
    extracted = staging / 'repo'
    extracted.mkdir()
    root = storage / workspace_relative
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if len(members) > 10000:
                raise ValueError('too many repository files')
            total = 0
            prefix = None
            for member in members:
                parts = Path(member.filename).parts
                if not parts:
                    continue
                prefix = prefix or parts[0]
                if parts[0] != prefix or '..' in parts or member.filename.startswith('/') or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError('unsafe path in archive')
                relative = Path(*parts[1:])
                if not relative.parts:
                    continue
                target = (extracted / relative).resolve()
                if not target.is_relative_to(extracted.resolve()):
                    raise ValueError('unsafe archive path')
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    total += member.file_size
                    if total > 200 * 1024 * 1024:
                        raise ValueError('expanded repository is too large')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
        if (extracted / '.atoms').exists():
            shutil.rmtree(extracted / '.atoms')
        title = data.full_name.split('/')[-1][:120]
        prompt = f'Imported from GitHub repository {data.full_name}'
        from agent import ensure_workspace, set_workspace_owner, project_uid
        ensure_workspace(project_id, user['id'], title=title, prompt=prompt)
        for item in extracted.iterdir():
            destination = root / item.name
            if destination.exists():
                raise ValueError('repository conflicts with project metadata')
            item.rename(destination)
        set_workspace_owner(root, project_uid(project_id))
        with connection() as conn:
            conn.execute("""INSERT INTO projects(id,owner_id,title,prompt,kind,mode,model,status,preview_html,workspace_path)
                VALUES(%s,%s,%s,%s,'Web','Build','openai/gpt-6-luna','ready','',%s)""", (project_id,user['id'],title,prompt,workspace_relative))
            conn.execute("""INSERT INTO github_project_repositories(project_id,user_id,repository_id,full_name,html_url,default_branch)
                VALUES(%s,%s,%s,%s,%s,%s)""",(project_id,user['id'],data.repository_id,data.full_name,f'https://github.com/{data.full_name}',data.default_branch))
            from main import grant_project_role
            grant_project_role(conn,project_id)
        return {'project_id': str(project_id), 'repository': data.full_name, 'imported_files': sum(1 for path in root.rglob('*') if path.is_file())}
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(422, 'GitHub 仓库压缩包无效或超出导入限制') from exc
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@router.get('/linked-projects')
def linked_projects(user=Depends(current_user)):
    with connection() as conn:
        return conn.execute('SELECT g.project_id,g.full_name,g.html_url,g.default_branch,p.title,p.status FROM github_project_repositories g JOIN projects p ON p.id=g.project_id WHERE g.user_id=%s ORDER BY g.imported_at DESC',(user['id'],)).fetchall()


@router.post('/sync/{project_id}')
async def push_project(project_id: uuid.UUID, user=Depends(current_user)):
    token=await access_token(user['id'])
    with connection() as conn:
        project=conn.execute('SELECT id,owner_id,title,status,workspace_path FROM projects WHERE id=%s AND owner_id=%s',(project_id,user['id'])).fetchone()
        linked=conn.execute('SELECT * FROM github_project_repositories WHERE project_id=%s AND user_id=%s',(project_id,user['id'])).fetchone()
    if not project or not linked: raise HTTPException(404,'项目尚未关联 GitHub 仓库')
    if project['status'] in ('queued','running'): raise HTTPException(409,'项目构建运行时不能同步，请稍后重试')
    from agent import project_root
    root=project_root(project_id,user['id']).resolve()
    excluded={'node_modules','dist','build','.git','.venv','.python-packages','.python-cache','__pycache__','.atoms','.atoms-attachments','published','coverage','.next','.vite','vendor'}
    files=[];total=0
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name not in excluded and not (Path(directory)/name).is_symlink()]
        for name in names:
            path=Path(directory)/name
            if path.is_symlink() or not path.is_file(): continue
            rel=path.relative_to(root)
            if rel.name in {'.env','env.connector'} or rel.name.startswith('.env.') or rel.name in {'.npmrc','.pypirc','id_rsa','id_ed25519'} or rel.suffix.lower() in {'.pem','.key','.p12','.pfx'}: continue
            size=path.stat().st_size;total+=size
            if len(files)>=200 or total>25*1024*1024: raise HTTPException(413,'单次 GitHub 同步最多 200 个文件或 25 MB，请先缩小变更')
            files.append((rel.as_posix(),path.read_bytes()))
    if not files: raise HTTPException(422,'项目中没有可同步的源文件')
    owner,name=linked['full_name'].split('/')
    headers={**HEADERS,'Authorization':'Bearer '+token}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120,connect=15)) as client:
        async def request(method,path,**kwargs):
            response=await client.request(method,API+path,headers=headers,**kwargs)
            if response.status_code in (401,403): raise HTTPException(403,'GitHub 授权不足或 API 限流，请重新连接后重试')
            if response.status_code>=400: raise HTTPException(502,f'GitHub API 返回 {response.status_code}：{response.text[:300]}')
            return response.json()
        ref=await request('GET',f'/repos/{owner}/{name}/git/ref/heads/{urllib.parse.quote(linked["default_branch"],safe="")}')
        parent=ref['object']['sha']
        commit=await request('GET',f'/repos/{owner}/{name}/git/commits/{parent}')
        semaphore=asyncio.Semaphore(4)
        async def create_blob(path,content):
            async with semaphore:
                return await request('POST',f'/repos/{owner}/{name}/git/blobs',json={'content':base64.b64encode(content).decode(),'encoding':'base64'})
        blobs=await asyncio.gather(*(create_blob(path,content) for path,content in files))
        entries=[{'path':path,'mode':'100755' if (root/path).stat().st_mode & 0o111 else '100644','type':'blob','sha':blob['sha']} for (path,_),blob in zip(files,blobs)]
        tree=await request('POST',f'/repos/{owner}/{name}/git/trees',json={'base_tree':commit['tree']['sha'],'tree':entries})
        new_commit=await request('POST',f'/repos/{owner}/{name}/git/commits',json={'message':f'Update from Atoms: {project["title"]}','tree':tree['sha'],'parents':[parent]})
        await request('PATCH',f'/repos/{owner}/{name}/git/refs/heads/{urllib.parse.quote(linked["default_branch"],safe="")}',json={'sha':new_commit['sha'],'force':False})
    return {'repository':linked['full_name'],'branch':linked['default_branch'],'commit':new_commit['sha'],'url':new_commit['html_url'],'files':len(files)}
