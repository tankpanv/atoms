"""Opt-in, billed live-model evaluation. Never runs during unittest discovery."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

from agent import make_plan, run_agent
from coding_runtime import AgentState, AgentStateMachine
from runtime import stop_runtime
from agent_harness import resumable_plan


async def main():
    if os.getenv("RUN_LIVE_AGENT_EVAL") != "1":
        raise RuntimeError("Explicitly enable RUN_LIVE_AGENT_EVAL=1 to call the configured model")
    output = Path(os.getenv("EVAL_OUTPUT", "/tmp/harness-evaluation"))
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o711)
    resume_id = os.getenv('EVAL_RESUME_WORKSPACE')
    identifier = uuid.UUID(resume_id) if resume_id else uuid.uuid4()
    root = output / identifier.hex
    root.mkdir(mode=0o700, exist_ok=bool(resume_id))
    (root / '.atoms').mkdir(exist_ok=True)
    if not resume_id:
        (root / '.atoms/ARCHITECTURE.md').write_text('Architecture not decided yet')
    machine = AgentStateMachine()
    metrics = {"total_tokens": 0, "tool_calls": 0}
    def step(kind, label, detail, **extra):
        if kind == 'state':
            machine.transition(AgentState(label))
            print('STATE', label, flush=True)
        elif kind == 'usage':
            metrics['total_tokens'] += extra.get('token_usage', {}).get('total_tokens', 0)
        elif kind in {'tool', 'warning', 'result', 'thinking'}:
            if kind == 'tool': metrics['tool_calls'] += 1
            print(kind, label, detail[:180].replace('\n', ' '), flush=True)
    prompt = ('做一个带真实 FastAPI 后端的留言板。只要求输入名字和留言内容、新增留言和列出留言。'
              'SQLite 服务端持久化，刷新和服务重启后保留。React 前端用官方 CLI 生成，按页面、组件、API client 模块化。'
              '提供 README 和测试。不加账号、角色、部署或其他扩展。浏览器验收应新增留言后刷新，接口验收应校验真实数据。')
    model = os.getenv('EVAL_MODEL', 'openai/gpt-6-luna')
    report = {'passed': False, 'model': model, 'workspace': str(root)}
    with patch('agent.ensure_workspace', return_value=root), patch('runtime.ensure_workspace', return_value=root):
        try:
            if resume_id:
                restored = resumable_plan(root, 'continue')
                if not restored:
                    raise RuntimeError('Evaluation workspace has no unfinished checkpoint')
                prompt, parsed = restored
                for state in (AgentState.UNDERSTAND, AgentState.EXPLORE, AgentState.PLAN):
                    step('state', state.value, 'Resume existing requirements and tasks; revalidate all acceptance')
                plan = json.dumps(parsed, ensure_ascii=False)
            else:
                plan = await make_plan(identifier, prompt, model, on_step=step)
            print('PLAN_OK', len(json.loads(plan)['tasks']), flush=True)
            result = await run_agent(identifier, prompt, model, step, plan=plan, initial_tokens=metrics['total_tokens'])
            ledger = json.loads((root / '.atoms/task-state.json').read_text())
            report = {'passed': True, 'model': model, 'metrics': metrics, 'workspace': str(root),
                      'tasks_done': sum(item['status'] == 'done' for item in ledger['tasks']),
                      'evidence_count': len(ledger['evidence']),
                      'files': [name for name in result['files'] if not name.startswith('.atoms/')],
                      'summary': result['summary']}
            print('LIVE_EVAL_OK', json.dumps(report, ensure_ascii=False), flush=True)
        except Exception as exc:
            report = {'passed': False, 'model': model, 'metrics': metrics, 'workspace': str(root), 'error': str(exc)}
            print('LIVE_EVAL_FAILED', json.dumps(report, ensure_ascii=False), flush=True)
            raise
        finally:
            await stop_runtime(identifier)
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
