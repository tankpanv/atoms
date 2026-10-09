"""Durable action-level loop detection for both model tools and harness checks.

Unlike counting adjacent calls, identity spans turns, excludes requirement labels,
tracks actual source/config changes, and never replays arbitrary successful shell
side effects. Repeated failures feed the original cause back into recovery.
"""
from agent_session import digest
import json
import re


def error_signature(output):
    text = re.sub(r'\x1b\[[0-9;]*m', '', output)
    # Error messages survive huge DOM dumps; line/receipt/time changes do not
    # constitute a different fault. Never key these counters on source hashes.
    lines = [line.strip() for line in text.splitlines() if re.search(
        r'(?:\w*(?:Error|Exception):|error TS\d+:|error:|command not found|No test files found)', line, re.I)]
    facts = '\n'.join(lines[-3:]) or text[-800:]
    facts = re.sub(r'\bV\d+\b|\b[0-9a-f]{16,}\b|:\d+(?::\d+)?|\b\d+(?:\.\d+)?\s*(?:ms|seconds?|s)\b', '<value>', facts)
    return digest(facts[:1600])


def failure_facts(name, output):
    if name not in ('browser_check', 'http_request', 'runtime_check'):
        return output[-2000:]
    try:
        value = json.loads(output[output.index('{'):])
        facts = {key:value[key] for key in ('error','errors','failed_responses','status','expected_status','body','controls','text') if key in value}
        for key in ('error','body','text'):
            if isinstance(facts.get(key),str): facts[key] = facts[key][:2500]
        for key in ('errors','failed_responses','controls'):
            if isinstance(facts.get(key),list): facts[key] = facts[key][:12]
        for key in ('errors','failed_responses'):
            if isinstance(facts.get(key),list): facts[key] = [str(v)[:2000] for v in facts[key]]
        for key in ('controls','failed_responses','errors'):
            while len(json.dumps(facts,ensure_ascii=False)) > 12000 and len(facts.get(key,[])) > 1:
                facts[key].pop()
        return facts
    except (ValueError, TypeError):
        return output[-3000:]


class ExecutionGuard:
    def __init__(self, state):
        self.entries = state.setdefault('execution_attempts', {})
        self.progress = state.setdefault('execution_progress', {})
        self.limits = state.setdefault('verification_limits', {'failures': {}, 'signatures': {}, 'policy_rejections': 0})

    def request_recovery(self, name, output, reason):
        self.limits['pending_recovery'] = {
            'tool': name, 'reason': reason, 'error': failure_facts(name, output),
            'signature': error_signature(output),
        }

    def policy_rejected(self, detail):
        self.limits['policy_rejections'] += 1
        self.request_recovery('verification_policy', detail, '验证方法违反当前局部测试/最终验收合同，修正调用方法，不修改正确业务。')

    def validation_result(self, name, code, output):
        """Failures trigger a different diagnosis, never a failure-count stop."""
        family = 'runtime' if name in ('runtime_check', 'delivery_check', 'browser_check', 'http_request') else name
        if code == 0:
            self.limits['signatures'].pop(name, None)
            return
        failures = self.limits['failures']
        failures[family] = failures.get(family, 0) + 1
        signatures = self.limits['signatures'].setdefault(name, {})
        signature = error_signature(output)
        signatures[signature] = signatures.get(signature, 0) + 1
        if signatures[signature] >= 2 or failures[family] % 3 == 0:
            self.request_recovery(name, output,
                '区分未实现功能、真实执行错误、验证步骤错误和证据缺口；换用尚未验证的假设及判别检查。')

    def begin_recovery(self, source=None):
        incident = self.limits.pop('pending_recovery', None) or {
            'tool': 'progress_check', 'reason': '没有有效进展，需要新的可验证假设。',
            'error': self.diagnostic(source), 'signature': self.progress.get('fingerprint'),
        }
        self.limits['recovery_round'] = self.limits.get('recovery_round', 0) + 1
        incident['round'] = self.limits['recovery_round']
        lenses = ('证据与验证合同：区分期望错误、陈旧证据、缺失关联与真实业务失败',
                  '逆向因果追踪：从期望结果沿数据回读、写入、API、调用方追踪第一个差异',
                  '最小判别实验：隔离请求载荷、编码、认证、依赖和环境，证伪至少两种竞争假设',
                  '生命周期与不变量：检查运行版本、服务重启、持久化、状态转换及边界条件')
        incident['lens'] = lenses[(incident['round'] - 1) % len(lenses)]
        history = self.limits.setdefault('recoveries', [])
        incident['previous_attempts'] = [
            {'lens': item['lens'], 'diagnosis': item.get('diagnosis'), 'error': item['error'],
             'actual_observations': item.get('observations', [])}
            for item in history[-3:]]
        history.append(incident)
        del history[:-8]
        return incident

    def recovery_diagnosed(self, incident, diagnosis):
        # A hypothesis is not an accepted root cause or successful repair.
        incident['diagnosis'] = diagnosis
        incident['status'] = 'awaiting_real_probe_and_repair'

    def recovery_action(self, name, args, code, result):
        history = self.limits.get('recoveries', [])
        if not history:
            return
        incident = history[-1]
        if name not in ('read_files', 'read_file', 'read_code', 'search_files', 'search_code', 'symbol_search',
                        'run_shell', 'run_build', 'runtime_check', 'http_request', 'browser_check',
                        'write_file', 'write_files', 'replace_in_file', 'apply_patch', 'update_task'):
            return
        observation = {'tool': name, 'args': {key: str(value)[:500] for key, value in args.items()
                       if key not in ('content', 'files', 'patch', 'old', 'new')}, 'exit_code': code,
                       'result': str(failure_facts(name, result))[:1600]}
        observations = incident.setdefault('observations', [])
        signature = digest(observation)
        if any(item.get('signature') == signature for item in observations):
            return  # An identical result is not new diagnostic evidence.
        observation['signature'] = signature
        observations.append(observation)
        del observations[:-6]
        if name in ('write_file', 'write_files', 'replace_in_file', 'apply_patch'):
            incident['status'] = 'repair_applied_awaiting_real_validation'
        elif code is not None:
            if code == 0:
                self.limits['probe_revision'] = self.limits.get('probe_revision', 0) + 1
            incident['status'] = 'probe_passed' if code == 0 else 'hypothesis_needs_revision'
        else:
            incident['status'] = 'source_observed_awaiting_discriminating_probe'

    def key(self, name, args, source):
        args = {k: v for k, v in args.items() if k not in ('requirement_ids', 'timeout')}
        return digest({'tool': name, 'args': args, 'source': source})

    def prior(self, key, *, reusable=False):
        old = self.entries.get(key)
        if old and ((old['code'] == 0 and reusable) or (old['code'] != 0 and old['count'] >= 2
                         and old.get('probe_revision', 0) == self.limits.get('probe_revision', 0))):
            return old
        return None

    def record(self, key, name, args, source, code, output):
        old = self.entries.get(key, {})
        if key not in self.entries and len(self.entries) >= 64:
            self.entries.pop(next(iter(self.entries)))
        if code != 0 and name in ('browser_check','http_request','runtime_check'):
            facts = failure_facts(name,output)
            if isinstance(facts,dict): output = json.dumps(facts,ensure_ascii=False)
        self.entries[key] = {'name': name, 'args': args, 'source': source, 'code': code,
                             'output': output[-12000:], 'count': old.get('count', 0) + 1,
                             'probe_revision': self.limits.get('probe_revision', 0)}

    def observe(self, source, tasks, evidence=()):
        # New receipt IDs, repeated summaries and task notes are not progress.
        verified = sorted({(v['kind'], rid) for v in evidence
                           if v.get('exit_code') == 0 and not v.get('historical') and v.get('source_digest') == source
                           for rid in v.get('requirement_ids', [])})
        current = digest({'source': source, 'tasks': [(t['id'], t['status']) for t in tasks], 'verified': verified})
        same = current == self.progress.get('fingerprint')
        self.progress['stagnant'] = self.progress.get('stagnant', 0) + 1 if same else 0
        self.progress['fingerprint'] = current
        stagnant = self.progress['stagnant']
        # Recovery prompts alone are not progress. Several opportunities to
        # inspect, probe and repair must eventually yield a concrete result.
        if stagnant >= 18:
            from agent_delivery import DeliveryLimitReached
            raise DeliveryLimitReached('连续 18 轮没有源码变化、任务状态变化或新的需求成功验证，已停止无进展循环并保存检查点。'
                                       + self.diagnostic(source))
        return bool(self.limits.get('pending_recovery')) or stagnant in (3, 6, 10) or (stagnant > 10 and (stagnant - 10) % 3 == 0)

    def failures(self, source=None):
        return [v for v in self.entries.values() if v['code'] != 0
                and (source is None or v['source'] == source)][-3:]

    def diagnostic(self, source=None):
        failures = self.failures(source)
        return ('执行恢复：连续操作未改变源码或任务状态。停止重复相同的构建/测试/状态查询。'
                '先诊断故障层：计划/协调器命令、工作区配置、依赖/环境、运行时，最后才是业务代码。'
                '针对最新错误定位其来源，解释为什么上次修改无效，然后做一个可验证的不同修复。'
                '若实际代码正确而验收配置错误，修复实际运行配置；不要削减功能、删除测试、伪造成功或重建项目。'
                '当前重复失败的真实结果：' + str([{'tool': v['name'], 'arguments': v['args'],
                                                   'exit_code': v['code'], 'error': failure_facts(v['name'],v['output'])} for v in failures]))
