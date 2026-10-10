"""Evidence-aware planning repair; validation feedback stays inside the model loop."""
import copy
import json
import os
import time

from agent_delivery import DeliveryLimitReached
from agent_session import digest, estimate_tokens
from billing_context import active_job
from incremental_planning import plan_object


class PlanningCheckpointReached(DeliveryLimitReached):
    state_detail = '规划恢复检查点已保存'
    stop_label = '规划尚待解决，进度已保存'
    stop_detail = '需求、计划草稿、源码观察及未解决冲突已保存；继续时从检查点恢复，原有可用版本保留。'


def unfinished_planning_request(root, request):
    from agent_harness import is_continuation
    from agent_session import AgentSession, DIRECTORY
    if not is_continuation(request) or not (root / '.atoms' / DIRECTORY / 'session.sqlite3').is_file():
        return None
    phase = AgentSession(root).saved_phase('planning')
    original = phase.get('request')
    return original if not phase.get('completed', True) and isinstance(original, str) and original.strip() else None


class PlanningRecovery:
    """Separate discovery, synthesis and evidence repair with durable progress.

    Limits are a final safety net. Ordinary errors are repaired in the same
    conversation with real tools and a candidate that can be patched in place.
    """
    def __init__(self, session, phase, discovery_rounds, has_tools, *, initial_tokens=None):
        self.session, self.phase = session, phase
        self.discovery_rounds, self.has_tools = discovery_rounds, has_tools
        state = phase.setdefault('recovery', {})
        job = active_job.get()
        new_job = state.get('job_id') != job
        if new_job:
            state.clear()
        state.setdefault('job_id', job)
        state.setdefault('calls', 0)
        state.setdefault('tokens', 0)
        if initial_tokens is not None:
            # Job usage is authoritative on recovery, including earlier vision
            # calls. Project history never contributes to this counter.
            state['tokens'] = int(initial_tokens)
        state.setdefault('started_at', time.time())
        state.setdefault('discovery_calls', 0)
        state.setdefault('facts', [])
        state.setdefault('failures', {})
        state.setdefault('mode', 'synthesis' if phase.get('synthesis') else 'explore')
        # Old failed checkpoints disabled tools even while requiring searches.
        if phase.get('last_error'):
            if 'candidate' not in phase:
                for message in reversed(phase['messages']):
                    if message.get('role') == 'assistant' and not message.get('tool_calls'):
                        try:
                            phase['candidate'] = plan_object(message.get('content') or '')
                            break
                        except ValueError:
                            pass
            if has_tools and new_job:
                state.update(mode='evidence', evidence_calls_left=2)
        self.state = state
        from build_tiers import tier_budget_limits
        task_limit = tier_budget_limits(phase.get('build_tier'))['token_limit']
        configured = os.getenv('AGENT_PLANNING_MAX_TOKENS', '').strip()
        self.token_limit = min(task_limit, int(configured)) if configured else task_limit

    @property
    def mode(self):
        return self.state['mode']

    def tools_enabled(self):
        if self.mode == 'explore' and self.state['discovery_calls'] >= self.discovery_rounds:
            self.state['mode'] = 'synthesis'
        if self.mode == 'evidence' and self.state.get('evidence_calls_left', 0) <= 0:
            self.state['mode'] = 'synthesis'
        return self.has_tools and self.mode in {'explore', 'evidence'}

    def before_call(self, messages, tools, max_tokens=None):
        self.input_tokens = estimate_tokens(messages, tools)
        remaining = self.token_limit - self.state['tokens'] - self.input_tokens
        reason = None
        if self.state['calls'] >= int(os.getenv('AGENT_PLANNING_MAX_CALLS', '16')):
            reason = '达到规划模型调用预算'
        elif remaining < 512:
            reason = (f'达到本次对话的规划 token 预算（已用 {self.state["tokens"]:,}，'
                      f'预计输入 {self.input_tokens:,}，上限 {self.token_limit:,}）')
        elif time.time() - self.state['started_at'] >= int(os.getenv('AGENT_PLANNING_TIMEOUT_SECONDS', '480')):
            reason = '达到规划时间预算'
        if reason:
            self.pause(reason)
        self.phase['synthesis'] = not bool(tools)
        return min(max_tokens, remaining) if max_tokens is not None else remaining

    def record_response(self, response):
        self.state['calls'] += 1
        meta = response.get('_response_meta', {})
        actual = meta.get('total_tokens')
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
            prompt = meta.get('prompt_tokens', self.input_tokens)
            completion = meta.get('completion_tokens')
            actual = prompt + (completion if isinstance(completion, int) else estimate_tokens([response]))
        self.state['tokens'] += actual
        self.phase['rounds'] = self.phase.get('rounds', 0) + 1
        if response.get('tool_calls'):
            if self.mode == 'explore':
                self.state['discovery_calls'] += 1
            elif self.mode == 'evidence':
                self.state['evidence_calls_left'] -= 1

    def observe(self, name, args, result, source_versions=None):
        # Identical reads/searches cannot pretend to be new diagnostic evidence.
        fact = digest({'tool': name, 'args': args, 'source_versions': source_versions,
                       'result': result if source_versions is None else None})
        if fact not in self.state['facts']:
            self.state['facts'].append(fact)

    def candidate(self, text):
        value = plan_object(text)
        if 'plan_patch' in value:
            if set(value) != {'plan_patch'} or not isinstance(value['plan_patch'], dict):
                raise ValueError('局部修正须为 {"plan_patch":{待修正字段}}')
            if not isinstance(self.phase.get('candidate'), dict):
                raise ValueError('没有可修正的计划草稿，请提供完整计划 JSON')
            value = merge_patch(self.phase['candidate'], value['plan_patch'])
        self.phase['candidate'] = copy.deepcopy(value)
        candidate_hash = digest(value)
        if self.phase.get('candidate_hash') != candidate_hash:
            archived = self.session.output({'function': {'name': 'planning_draft', 'arguments': '{}'}},
                                           json.dumps(value, ensure_ascii=False))
            self.phase['candidate_output_id'] = archived.splitlines()[0].removeprefix('output_id=')
            self.phase['candidate_hash'] = candidate_hash
        return value

    def feedback(self, exc, locator):
        issues = getattr(exc, 'issues', [{'code': 'plan_validation', 'message': str(exc)}])
        signature = digest({'issues': issues, 'facts': sorted(self.state['facts'])})
        failures = self.state['failures']
        failures[signature] = failures.get(signature, 0) + 1
        self.phase['last_error'] = str(exc)
        self.phase['validation_issues'] = issues
        self.state['mode'] = 'evidence' if getattr(exc, 'needs_evidence', False) and self.has_tools else 'synthesis'
        self.state['evidence_calls_left'] = 2
        if failures[signature] >= 3:
            self.pause('相同冲突在没有新证据的情况下多次修正仍未解决')
        facts = {'issues': issues, 'tools_available': self.mode == 'evidence',
                 'draft_output_id': self.phase.get('candidate_output_id'),
                 'existing_files': sorted(locator.entries)[:200],
                 'source_of_truth': '文件索引和读取结果；历史计划、架构文档、平台模板说明都不能证明模块已存在'}
        guidance = ('先分析冲突原因，按需使用只读工具确认真实入口、调用方和配置；新增模块必须明确 create，补齐连接它的入口及运行配置。'
                    if self.mode == 'evidence' else
                    '根据具体校验反馈修正结构；缺少 build/dev、接口或验收时提供真实设计，不虚构默认值。')
        draft_access = '需要草稿全文时可用 read_tool_output 读取 draft_output_id。' if self.mode == 'evidence' else '未出错字段由草稿保留，无需重新输出。'
        return ('计划校验反馈（可恢复，尚未进入实现，不代表业务执行失败）：\n' + json.dumps(facts, ensure_ascii=False) + '\n' + guidance +
                '\n已保存最近的完整计划草稿。' + draft_access + '优先仅返回 {"plan_patch":{待修正的顶层字段}}；对象递归合并，数组须完整替换，null 删除字段。'
                '也可返回完整计划 JSON。保持当前用户目标和需求覆盖，不通过删除需求、伪造已有文件或空验证来绕过校验。'
                '不要重复整篇设计或仅输出解释；修正 change_map 时同步 tasks.files 与接口消费者。')

    def pause(self, reason):
        self.phase['checkpoint_reason'] = reason
        self.session.save_phase('planning', self.phase)
        pending = self.phase.get('last_error') or '模型尚未提交可执行计划'
        raise PlanningCheckpointReached(f'规划尚未完成：{reason}。已保存计划草稿和源码观察，可继续恢复。待解决：{pending}')


def merge_patch(candidate, patch):
    """JSON Merge Patch; operate on a copy so an invalid patch is atomic."""
    result = copy.deepcopy(candidate) if isinstance(candidate, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge_patch(result.get(key), value)
        else:
            result[key] = copy.deepcopy(value)
    return result
