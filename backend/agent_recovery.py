"""Ground fresh recovery hypotheses in actual outcomes and acceptance gaps."""
import json
from dependency_policy import INSTRUCTIONS as DEPENDENCY_POLICY

from agent_harness import json_object


def recovery_prompt(incident):
    return (DEPENDENCY_POLICY +
        '你是当前任务的故障定位者，不重做规划、不削减需求、不声称未执行的验证通过。'
        '本地配置自动补齐，可模拟依赖主动接通默认可用的 mock/local adapter，不能仅留503/TODO。只有必须连接真实服务且无法合理模拟的缺口才需要用户配置，其他功能继续完成。模拟不冒充真实供应方成功，最终页面与核心演示操作必须通过。'
        '当前必须换用诊断视角：' + incident['lens'] + '。'
        '先区分业务缺陷、步骤/请求合同错误、证据陈旧或未关联、任务未更新。'
        '以实际工具 exit_code、expected_status 和响应体为准：期望且通过的 4xx 是错误路径证据，'
        '不能仅凭运行日志中的 422/500 断定业务失败。没有失败证据就不能猜故障。'
        'runtime 为每个 services 命令设置它自己的 $PORT，port_env 用于把该端口提供给前端；'
        '不要把 $PORT 和 API_PORT 的差异自动认定配置错误。'
        '构建/健康检查不证明完整业务，但证据缺口也不证明代码错误。'
        '提出至少两个竞争假设，用最小真实检查区分，沿实际调用方→请求编码/载荷→路由/schema→'
        '业务→数据→回读定位第一个差异。不为过期断言改正确业务，不运行全量单元测试。'
        '与此前诊断比较：未产生新证据的方案不能换措辞重试。先执行判别检查，再做最小修复，'
        '最后重新启动受影响真实服务并验证完整链路，关联 requirement_ids 并 update_task。'
        '只返回 JSON {"layer":"故障或证据层", "root_cause":"已知事实、未知原因及证据",'
        '"hypotheses":[{"cause":"具体假设","probe":"具体真实工具/命令、请求载荷或相关代码位置",'
        '"supports":"何种观察支持","refutes":"何种观察排除"}],'
        '"next_actions":["下一步判别检查","依据其结果实施的最小修复","当前源码真实验收及证据关联"]}。'
        '不调用工具，此处推断必须交给实现模型执行验证。'
        '\n真实阻塞与前次方案：' + json.dumps(incident, ensure_ascii=False)
    )


def parse_recovery(content):
    result = json_object(content)
    if not isinstance(result.get('root_cause'), str) or not result['root_cause'].strip():
        raise ValueError('诊断必须区分已知事实和待验证原因')
    hypotheses = result.get('hypotheses')
    if not isinstance(hypotheses, list) or len(hypotheses) < 2:
        raise ValueError('诊断需要至少两种可证伪假设')
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict) or not all(
                isinstance(hypothesis.get(key), str) and hypothesis[key].strip()
                for key in ('cause', 'probe', 'supports', 'refutes')):
            raise ValueError('每个假设必须提供判别检查及支持/排除条件')
    actions = result.get('next_actions')
    if not isinstance(actions, list) or not actions or not all(isinstance(a, str) and a.strip() for a in actions):
        raise ValueError('恢复诊断必须提供可执行步骤')
    return result


def recovery_facts(root, ledger, journal, source, runtime, failures):
    receipts = [{key: item.get(key) for key in
                 ('id', 'kind', 'command', 'exit_code', 'requirement_ids', 'historical')}
                | {'current_source': item.get('source_digest') == source,
                   'result': item.get('output', '')[-2400:]}
                for item in ledger.evidence[-12:]]
    facts = {'goal': ledger.plan['goal'], 'requirements': ledger.plan['requirements'],
             'tasks': ledger.tasks, 'acceptance_gaps': [],
             'recent_receipts': receipts,
             'actual_failed_operations': failures,
             'recent_actions': journal[-4:],
             'runtime': {'running': bool(runtime and runtime.get('running')),
                         'note': '历史运行日志不能取代具体工具结果；预期错误状态可能已验证通过。'},
             'source_digest': source}
    # A rolling tail drops old probes and changes the cache key without any
    # new fact. Use all current-version probe identities; generated records,
    # tokens and elapsed timings in successful outputs are not new faults.
    facts['successful_probes'] = sorted({json.dumps({key: item.get(key) for key in
        ('kind', 'command', 'requirement_ids')}, ensure_ascii=False, sort_keys=True)
        for item in ledger.evidence if item.get('exit_code') == 0
        and not item.get('historical') and item.get('source_digest') == source})
    # Import lazily: agent owns snapshots and imports this helper at recovery time.
    from agent import snapshot_files
    files = snapshot_files(root)
    facts['acceptance_gaps'] = ledger.completion_issues(files)
    paths = ['.atoms-workspace.json', 'package.json', 'frontend/package.json']
    active = next((task for task in ledger.tasks if task['status'] != 'done'), None)
    if active:
        related = [name for name in active.get('files', []) if name.endswith(('.py', '.ts', '.tsx', '.js'))]
        paths += sorted(related, key=lambda name: (not any(marker in name for marker in ('routes', 'api.', 'main.py', 'db.py')), name))[:4]
    facts['relevant_source'] = {name: files[name][:3000] for name in paths
                                if name in files and isinstance(files[name], str)}
    return facts
