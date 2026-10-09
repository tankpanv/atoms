#!/usr/bin/env python3
"""Single-host deployment. Never execute .env as shell code or expose its secrets."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)


def fail(message):
    raise RuntimeError(message)


def run(command, *, capture=False, check=True, timeout=None):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout)
    if check and result.returncode:
        # Compose's resolved config contains passwords. Never echo its stdout.
        if capture and result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        fail(f"命令失败 (exit {result.returncode}): {' '.join(command[:5])}")
    return result


def resolved_config(compose):
    output = run(compose + ['config', '--format', 'json'], capture=True).stdout
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        # Compose 2.25.0 ignores --format json (docker/compose#11627).
        # No dependency on PyYAML on a new host with a current Compose version.
        try:
            import yaml
        except ImportError:
            fail('此版本 Compose 错误地输出 YAML；请升级 docker-compose-plugin 至 2.26+ 后重试。')
        return yaml.safe_load(output)


def env_scalars(path):
    """Only deployment flags; Compose is the authoritative parser for service config."""
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)$', line)
        if match:
            value = match[2].strip()
            if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
                value = value[1:-1]
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
            values[match[1]] = value
    return values


def update_env(path, updates):
    text = path.read_text()
    for key, value in updates.items():
        # These values are generated hex or validated URLs, not arbitrary shell input.
        replacement = f'{key}={value}'
        pattern = rf'(?m)^\s*(?:export\s+)?{re.escape(key)}\s*=.*$'
        if re.search(pattern, text):
            text = re.sub(pattern, lambda _: replacement, text)
        else:
            text = text.rstrip() + '\n' + replacement + '\n'
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(text)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['start', 'restart', 'stop', 'status', 'check'], default='start')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--host', help='Public IP or hostname; without scheme/path/port')
    parser.add_argument('--no-build', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if args.timeout < 30:
        fail('--timeout 至少 30 秒')
    if args.host and (not re.fullmatch(r'[A-Za-z0-9.-]+', args.host) or '..' in args.host):
        fail('--host 仅接受 IPv4 或域名，不带 http://、端口或路径；IPv6/HTTPS 请直接编辑 .env 的完整 URL')
    docker = ['docker']
    if run(docker + ['info'], capture=True, check=False).returncode:
        if shutil.which('sudo') and not run(['sudo', 'docker', 'info'], capture=True, check=False).returncode:
            docker = ['sudo', 'docker']
        else:
            fail('Docker daemon 不可用。请检查 sudo 权限、systemctl status docker；非 Linux Docker 主机不受支持。')
    run(docker + ['compose', 'version'], capture=True)
    info = json.loads(run(docker + ['info', '--format', '{{json .}}'], capture=True).stdout)
    if info.get('OSType') != 'linux' or 'rootless' in ' '.join(info.get('SecurityOptions', [])):
        fail('项目 Worker 需要本机 rootful Linux Docker（挂载 /var/run/docker.sock），不支持 rootless/远程 daemon。')
    # Mount paths are host paths, so a remote Docker context cannot deploy this source tree.
    context = json.loads(run(docker + ['context', 'inspect'], capture=True).stdout)[0]
    endpoint = os.getenv('DOCKER_HOST') or context.get('Endpoints', {}).get('docker', {}).get('Host', '')
    if endpoint != 'unix:///var/run/docker.sock':
        fail('请切换到本机 Docker: docker context use default；需使用 unix:///var/run/docker.sock。')
    state_dir = ROOT / '.deploy'
    state_dir.mkdir(mode=0o700, exist_ok=True)
    lock = None
    if args.action != 'status':
        lock = (state_dir / 'deploy.lock').open('w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('已有部署正在运行，请等待其完成。')
    env_file = Path(args.env_file).resolve()
    if not env_file.exists():
        if args.action in {'stop', 'status', 'check'}:
            fail(f'{env_file.name} 不存在，请先部署，或指定原来的 --env-file。')
        env_file.parent.mkdir(parents=True, exist_ok=True)
        # Write privately from the first byte (not cp followed by chmod).
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write((ROOT / '.env.example').read_text())
        update_env(env_file, {'POSTGRES_PASSWORD': secrets.token_hex(24),
                              'MINIO_ROOT_PASSWORD': secrets.token_hex(24),
                              'AGENT_SECRET': secrets.token_hex(32),
                              'GITHUB_TOKEN_ENCRYPTION_KEY': __import__('base64').urlsafe_b64encode(secrets.token_bytes(32)).decode()})
        print(f'已从 .env.example 创建 {env_file.name}，并生成独立随机密钥（未输出密钥）。', flush=True)
    os.chmod(env_file, 0o600)
    flags = env_scalars(env_file)
    def flag(key):
        value = os.getenv(key, flags.get(key, 'false')).lower()
        if value not in {'true', 'false', '1', '0'}:
            fail(f'{key} 必须为 true 或 false')
        return value in {'true', '1'}
    vision, gpu = flag('ENABLE_LOCAL_VISION'), flag('VISION_GPU')
    if gpu and not vision:
        fail('VISION_GPU=true 时也需要 ENABLE_LOCAL_VISION=true')
    compose = docker + ['compose', '--env-file', str(env_file), '-f', str(ROOT / 'docker-compose.yml')]
    if gpu:
        compose += ['-f', str(ROOT / 'docker-compose.gpu.yml')]
    if vision:
        compose += ['--profile', 'vision']
    config = resolved_config(compose)
    if args.host:
        ports = {name: config['services'][name]['ports'][0]['published'] for name in ['frontend', 'preview']}
        update_env(env_file, {'APP_PUBLIC_URL': f'http://{args.host}:{ports["frontend"]}',
                              'VITE_PREVIEW_ORIGIN': f'http://{args.host}:{ports["preview"]}'})
        config = resolved_config(compose)
    project = config['name']
    services = config['services']
    agent_env = services['agent-service']['environment']
    if agent_env['COMPOSE_PROJECT_NAME'] != project:
        fail('Compose 项目名与 Agent 不一致，请设置 COMPOSE_PROJECT_NAME。')
    if config['volumes']['agent_workspaces']['name'] != agent_env['WORKSPACE_VOLUME']:
        fail('Agent 工作区卷名称不一致')
    if services['agent-service']['image'] != agent_env['AGENT_IMAGE']:
        fail('Agent Worker 镜像名称不一致')
    if args.action == 'status':
        run(compose + ['--profile', '*', 'ps', '-a'])
        return
    def stop_workers():
        workers = run(docker + ['ps', '-q', '--filter', 'label=atoms.role=project-worker',
                                '--filter', f'label=atoms.compose={project}'], capture=True).stdout.split()
        if workers:
            print(f'停止本部署的 {len(workers)} 个项目 Worker（保留容器与工作区）。', flush=True)
            run(docker + ['stop', '--time', '30', *workers])
    if args.action == 'stop':
        run(compose + ['--profile', '*', 'stop', '--timeout', '30'])
        stop_workers()
        print('服务已停止，持久化数据已保留。')
        return
    db_env = services['db']['environment']
    if not re.fullmatch(r'[A-Za-z0-9._~-]+', str(db_env['POSTGRES_PASSWORD'])):
        fail('POSTGRES_PASSWORD 请使用字母、数字、点、下划线、~、-（推荐随机 hex），以保证数据库 URL 正确。')
    for key in ['POSTGRES_USER', 'POSTGRES_DB']:
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', db_env[key]):
            fail(f'{key} 请使用小写字母、数字和下划线，以字母或下划线开头')
    minio_env = services['minio']['environment']
    if len(str(minio_env['MINIO_ROOT_PASSWORD'])) < 8:
        fail('MINIO_ROOT_PASSWORD 至少 8 个字符')
    if str(db_env['POSTGRES_PASSWORD']) == 'replace-with-random-hex' or str(minio_env['MINIO_ROOT_PASSWORD']) == 'replace-with-random-hex':
        fail('请替换 .env 中的占位密码，或让脚本在缺少 .env 时自动生成随机密码。')
    for key in ['APP_PUBLIC_URL', 'VITE_PREVIEW_ORIGIN']:
        value = services['backend']['environment'].get(key) if key == 'APP_PUBLIC_URL' else services['frontend']['environment'].get(key)
        if value:
            url = urlsplit(value)
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.query or url.fragment or url.path not in {'', '/'}:
                fail(f'{key} 必须是完整 http(s) 源地址，不带路径、查询参数或账号密码')
    core = ['db', 'minio', 'backend', 'agent-service', 'preview', 'frontend']
    def check_ports():
        # Allow only ports currently held by this exact Compose deployment.
        own = run(compose + ['--profile', '*', 'ps', '-q'], capture=True).stdout.split()
        owned_ports = set()
        if own:
            for item in json.loads(run(docker + ['inspect', *own], capture=True).stdout):
                for bindings in item['NetworkSettings'].get('Ports', {}).values():
                    for binding in bindings or []:
                        owned_ports.add((binding['HostIp'], int(binding['HostPort'])))
        foreign_ports = set()
        running = run(docker + ['ps', '-q'], capture=True).stdout.split()
        if running:
            for container in json.loads(run(docker + ['inspect', *running], capture=True).stdout):
                if container['Id'] in own or any(container['Id'].startswith(value) for value in own):
                    continue
                for bindings in container['NetworkSettings'].get('Ports', {}).values():
                    for binding in bindings or []:
                        foreign_ports.add((binding['HostIp'], int(binding['HostPort'])))
        checked = set()
        for name in core:
            for item in services[name].get('ports', []):
                host, port = item.get('host_ip', '0.0.0.0'), int(item['published'])
                if not 1 <= port <= 65535:
                    fail(f'{name} 端口超出范围')
                if (host, port) in checked or ('0.0.0.0', port) in checked or (host == '0.0.0.0' and any(p == port for _, p in checked)):
                    fail(f'端口重复: {host}:{port}，请修改 .env')
                checked.add((host, port))
                if any(p == port and (h == host or h in {'0.0.0.0', '::'} or host in {'0.0.0.0', '::'}) for h, p in foreign_ports):
                    fail(f'{name} 端口 {host}:{port} 已被其他 Docker 容器占用，请修改 .env')
                if any(p == port and (h == host or h == '0.0.0.0' or host == '0.0.0.0') for h, p in owned_ports):
                    continue
                family = socket.AF_INET6 if ':' in host else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    try:
                        probe.bind((host, port))
                    except OSError:
                        fail(f'{name} 端口 {host}:{port} 已被其他程序占用；修改 .env 中对应端口后重试，不会自动改成随机端口。')
    if args.action != 'check':
        check_ports()
        if gpu:
            if not shutil.which('nvidia-smi') or run(['nvidia-smi'], capture=True, check=False).returncode:
                fail('VISION_GPU=true 需要 NVIDIA 驱动与 NVIDIA Container Toolkit；普通 CPU 部署请设置 false。')
        if not args.no_build:
            print('[1/5] 构建镜像（复用依赖缓存，构建失败时不停止原服务）', flush=True)
            run(compose + ['build', 'backend', 'frontend'])
        else:
            for name in ['backend', 'frontend']:
                run(docker + ['image', 'inspect', services[name]['image']], capture=True)
            image = json.loads(run(docker + ['image', 'inspect', services['frontend']['image']], capture=True).stdout)[0]
            labels = image['Config'].get('Labels') or {}
            if any(str(services['frontend']['build']['args'][key]) != labels.get(label) for key, label in [('VITE_PREVIEW_ORIGIN', 'atoms.preview-origin'), ('VITE_PREVIEW_PORT', 'atoms.preview-port')]):
                fail('前端镜像的预览地址/端口与 .env 不一致，请去掉 --no-build 重新构建。')
        # Pull missing infrastructure before stopping a working deployment.
        for name in ['db', 'minio'] + (['vision'] if vision else []):
            image = services[name]['image']
            if run(docker + ['image', 'inspect', image], capture=True, check=False).returncode:
                run(docker + ['pull', image])
        print('[2/5] 停止本部署的旧服务与项目 Worker，保留数据', flush=True)
        run(compose + ['--profile', '*', 'stop', '--timeout', '30'])
        stop_workers()
        # Migrate the original bind-mounted login key once; future keys live in a named volume.
        run(compose + ['run', '--rm', '--no-deps', '-T', '--entrypoint', 'python', 'backend', '-c',
            "from pathlib import Path; import shutil,os; a=Path('/app/.auth_private.pem'); b=Path('/var/lib/atoms/auth_private.pem'); b.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(a,b) if a.exists() and not b.exists() else None; os.chmod(b,0o600) if b.exists() else None"])
        print('[3/5] 启动并等待所有核心服务健康', flush=True)
        run(compose + ['up', '-d', '--no-build', '--force-recreate', '--renew-anon-volumes',
                       '--wait', '--wait-timeout', str(args.timeout), *core])
        if vision:
            run(compose + ['up', '-d', '--wait', '--wait-timeout', str(args.timeout), 'vision'])
            print('下载/复用可选本地视觉模型；首次下载耗时取决于网络。', flush=True)
            run(compose + ['run', '--rm', '--no-deps', '-T', 'vision-init'])
    print('[4/5] 验证数据库、对象存储读写、API 代理、浏览器页面、预览和 Worker 环境', flush=True)
    run(compose + ['exec', '-T', 'backend', 'python', 'deployment_check.py'] + (['--runtime'] if args.action != 'check' else []))
    run(compose + ['exec', '-T', 'agent-service', 'python', 'deployment_check.py', '--worker-environment'])
    # Check the actual published host ports as well, not just container networking.
    for name, path in [('frontend', '/zh/dashboard'), ('frontend', '/api/health'), ('preview', '/health')]:
        port = services[name]['ports'][0]
        host = port.get('host_ip', '127.0.0.1')
        host = '127.0.0.1' if host == '0.0.0.0' else '::1' if host == '::' else host
        host = f'[{host}]' if ':' in host else host
        run(['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '5', '--max-time', '20',
             '-o', '/dev/null', f'http://{host}:{port["published"]}{path}'])
    public = services['backend']['environment'].get('APP_PUBLIC_URL') or f'http://localhost:{services["frontend"]["ports"][0]["published"]}'
    print(f'[5/5] 服务启动与本机真实链路检查通过\n控制台: {public.rstrip("/")}/zh/dashboard\n配置文件: {env_file}\n状态: bash start.sh status\n日后重启: bash deploy.sh --no-build', flush=True)
    if not str(services['backend']['environment'].get('AI_API_KEY', '')).strip():
        print('AI_API_KEY 尚未配置：可以注册/登录和访问平台，但 AI 生成需要填写可用的 API Key / 模型，再重新运行脚本。')
    if 'localhost' in public:
        print('远程访问请用 bash deploy.sh --host 你的服务器公网IP，并在云安全组放行前端和预览端口。')
    print('上述检查不调用付费模型；云安全组、外部域名/HTTPS、模型额度需按 README 配置并从浏览器确认。')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
        print(f'\n部署未通过：{error}\n请运行 bash start.sh status，或 docker compose logs --tail=80 <服务名> 排查；数据卷不会被删除。', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\n已中断；数据卷保留，可重新执行脚本。', file=sys.stderr)
        sys.exit(130)
