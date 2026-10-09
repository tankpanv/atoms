"""A coherent system plan and honest, executable acceptance contract."""
import json
import re


INSTRUCTIONS = '''
系统性交付合同：规划一次形成完整系统，不把架构决策留到实现中反复猜测。
软件计划提供 system_contract:{"version":1,"journeys":[{"id":"J1","requirement_ids":["R1"],"entry":"实际入口","steps":["实际操作→调用→状态/数据变化→结果回读"],"success_signal":"具体成功结果","failure_signals":["明确的失败/回退结果"]}],"api_contracts":[{"method":"POST","path":"/api/业务路径","request_media_type":"application/json|multipart/form-data|application/x-www-form-urlencoded","request_fields":["字段"],"success_status":200,"success_assertions":{"具体返回字段":"期望值"},"success_schema":{"type":"object","required":["outfits"],"properties":{"outfits":{"type":"array","minItems":3}}},"error_statuses":[422],"requirement_ids":["R1"],"producer":"backend/实际路由.py","consumers":["frontend/实际客户端.ts"]}],"dependencies":[{"name":"真实外部服务","requirement_ids":["R1"],"required_env":["项目服务需要的环境变量名"],"binding":"平台已提供的能力或用户配置，不能猜测存在","probe":"不泄密的真实连接/调用检查","on_unavailable":"记录配置 TODO、模拟演示适配、继续其他功能；不冒充真实服务成功"}],"invariants":["权限/并发/持久化/额度等与目标相关的不变量"],"development_order":["先验证关键依赖并打通一个真实端到端流程，再集中完成全部模块与交互状态，最后完整验收"]}。
只设计本需求必要的系统；简单 CLI/展示页面也可用简洁 journeys，api_contracts/dependencies 无需凭空增加。
每项明确需求必须被 journey 覆盖；接口必须确定请求编码、字段、成功结果、错误状态及真正的生产者/消费者。
外部 AI/支付/邮件等服务不是写环境变量名就已绑定。平台项目目前注入托管数据库连接；平台代码智能体的模型凭据不会自动提供给生成的业务服务。逐项声明实际依赖和配置方法，先探测风险。缺失凭据不能阻塞整个项目：记录配置 TODO，保留真实 provider，提供明确标注 simulated:true 的演示适配器/结果并继续整体开发；预期503、健康或额度查询仍不能当真实生成成功。
开发前核对调用方与接口合同，批量写入同一业务链路的完整模块，包含真实持久化、输入校验、认证边界、并发/幂等、超时和错误恢复（按实际需求选择），不以目录结构或文档代替实现。
最终验收逐项检查成功路径和必要失败路径。错误路径通过只证明错误处理，不能证明成功能力；build不能代替业务验收。用户优先要求最终可启动演示：尽可能解决发现的问题；外部依赖或难解决部分可以延期/绕过，交付真实可运行的演示，并列出实现、模拟、缺失及后续配置 TODO。不因完整验收缺口停止交付，也不宣称模拟能力已通过真实服务验收。
'''


def validate_system_contract(plan):
    """Optional for stored legacy plans; strict once a new contract is supplied."""
    contract = plan.get('system_contract')
    if contract is None:
        return plan
    if not isinstance(contract, dict) or contract.get('version') != 1:
        raise ValueError('system_contract.version 必须为 1')
    requirements = {r['id'] for r in plan['requirements']}
    for key in ('journeys', 'api_contracts', 'dependencies', 'invariants', 'development_order'):
        if not isinstance(contract.get(key), list):
            raise ValueError('system_contract.' + key + ' 必须为数组')
    if not contract['development_order'] or not all(isinstance(s, str) and s.strip() for s in contract['development_order']):
        raise ValueError('系统合同必须给出完整开发顺序')
    if not all(isinstance(s, str) and s.strip() for s in contract['invariants']):
        raise ValueError('系统不变量必须是具体约束')
    covered = set()
    identifiers = set()
    def references(item):
        values = item.get('requirement_ids')
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values) or not set(values) <= requirements:
            raise ValueError('系统合同必须关联真实 requirement_ids')
        return values
    def text(item, key):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError('系统合同缺少具体 ' + key)
    for journey in contract['journeys']:
        if not isinstance(journey, dict):
            raise ValueError('journey 必须为对象')
        for key in ('id', 'entry', 'success_signal'):
            text(journey, key)
        if journey['id'] in identifiers:
            raise ValueError('journey.id 重复')
        identifiers.add(journey['id'])
        if not isinstance(journey.get('steps'), list) or not journey['steps'] or not all(isinstance(s, str) and s.strip() for s in journey['steps']):
            raise ValueError('journey 必须包含完整真实链路 steps')
        covered.update(references(journey))
    if covered != requirements:
        raise ValueError('系统用户链路未覆盖全部需求：' + ', '.join(sorted(requirements - covered)))
    for api in contract['api_contracts']:
        if not isinstance(api, dict):
            raise ValueError('API 合同必须为对象')
        references(api)
        for key in ('path', 'request_media_type', 'producer'):
            text(api, key)
        if not api['path'].startswith('/') or api.get('method') not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
            raise ValueError('API 合同必须使用实际 HTTP method/path')
        if isinstance(api.get('success_status'), bool) or not isinstance(api.get('success_status'), int) or not 200 <= api['success_status'] < 300:
            raise ValueError('API 成功合同必须为 2xx，不能用预期拒绝冒充成功')
        if not ((isinstance(api.get('success_assertions'), dict) and api['success_assertions']) or (isinstance(api.get('success_schema'), dict) and api['success_schema'])):
            raise ValueError('API 成功合同需要具体 success_assertions')
        if not isinstance(api.get('request_fields'), list) or not isinstance(api.get('consumers'), list):
            raise ValueError('API 合同需要 request_fields 和 consumers')
    for dependency in contract['dependencies']:
        if not isinstance(dependency, dict):
            raise ValueError('dependency 必须为对象')
        references(dependency)
        for key in ('name', 'binding', 'probe', 'on_unavailable'):
            text(dependency, key)
        if not isinstance(dependency.get('required_env'), list) or not all(
                isinstance(name, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*', name)
                for name in dependency['required_env']):
            raise ValueError('dependency.required_env 必须是环境变量名，不含密钥值')
    return plan


def repair_system_contract_coverage(plan):
    """Fill a mechanical journey-coverage omission from the explicit requirements.

    A planner can produce a complete implementation/tasks plan while forgetting
    to list one detail or extension requirement in ``journeys``. That is a
    contract-shape error, not a reason to discard the whole build. The repair
    is deliberately conservative: it copies only that requirement's concrete
    acceptance steps, marks the journey as harness-repaired, and leaves API,
    dependency and implementation validation strict.
    """
    contract = plan.get('system_contract')
    requirements = plan.get('requirements') or []
    if not isinstance(contract, dict) or not isinstance(contract.get('journeys'), list):
        return plan
    required = {item.get('id'): item for item in requirements if isinstance(item, dict) and item.get('id')}
    covered = set()
    for journey in contract['journeys']:
        if isinstance(journey, dict) and isinstance(journey.get('requirement_ids'), list):
            covered.update(journey['requirement_ids'])
    missing = [identifier for identifier in required if identifier not in covered]
    if not missing:
        return plan
    journeys = contract['journeys']
    existing_ids = {item.get('id') for item in journeys if isinstance(item, dict)}
    repaired = []
    for identifier in missing:
        requirement = required[identifier]
        acceptance = [str(step).strip() for step in (requirement.get('acceptance') or []) if str(step).strip()]
        journey_id = 'J-auto-' + identifier
        suffix = 2
        while journey_id in existing_ids:
            journey_id = f'J-auto-{identifier}-{suffix}'
            suffix += 1
        existing_ids.add(journey_id)
        journeys.append({
            'id': journey_id,
            'requirement_ids': [identifier],
            'entry': requirement.get('description', '需求对应的应用入口')[:400],
            'steps': acceptance or ['按需求入口执行当前功能并观察结果'],
            'success_signal': (acceptance[0] if acceptance else requirement.get('description', '需求结果已满足'))[:500],
            'failure_signals': ['对应功能、状态或可访问结果未达到需求验收条件'],
            'harness_repaired': True,
        })
        repaired.append(identifier)
    contract['coverage_repairs'] = sorted(set(contract.get('coverage_repairs', [])) | set(repaired))
    return plan


def require_system_contract(plan):
    """Fresh web planning must not silently opt out; legacy checkpoints remain readable."""
    if plan.get('task_type', 'software') == 'software' and plan.get('application_type') == 'web' and not plan.get('system_contract'):
        raise ValueError('新的 Web 系统计划必须提供 system_contract：完整用户链路、接口成功合同、依赖绑定及开发顺序')
    return validate_system_contract(repair_system_contract_coverage(plan))


def receipt_response(item):
    if isinstance(item.get('response'), dict):
        return item['response']
    try:
        output = item.get('output', '')
        value, _ = json.JSONDecoder().raw_decode(output[output.index('{'):])
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def success_receipt(item):
    if item.get('exit_code') != 0 or item.get('historical') or item.get('kind') == 'local_unit_check':
        return False
    if item.get('kind') == 'http_request':
        response = receipt_response(item)
        if response.get('simulated') is True:
            return False
        try:
            payload = json.loads(response.get('body', ''))
            if isinstance(payload, dict) and payload.get('simulated') is True:
                return False  # Useful demo evidence, not actual provider acceptance.
        except (ValueError, TypeError):
            pass
        path = response.get('path', item.get('command', '')).split('?')[0].rstrip('/')
        return (isinstance(response.get('status'), int) and 200 <= response['status'] < 300
                and not path.endswith(('/health', '/healthz', '/ready', '/readyz')))
    return item.get('kind') in ('browser_check', 'run_shell', 'artifact_check')


def simulated_receipt(item):
    response = receipt_response(item)
    if response.get('simulated') is True:
        return True
    try:
        body = json.loads(response.get('body', ''))
        return isinstance(body, dict) and body.get('simulated') is True
    except (ValueError, TypeError):
        return False


def contract_acceptance_issues(plan, evidence, source, *, strict=True):
    contract = plan.get('system_contract')
    if not contract:
        return []
    current = [e for e in evidence if e.get('source_digest') == source and success_receipt(e)]
    issues = []
    for api in contract['api_contracts']:
        if not strict:
            continue
        matching = []
        for item in current:
            response = receipt_response(item)
            args = item.get('arguments', {})
            path = args.get('path', response.get('path', ''))
            # Runtime-prefix receipts retain the exact project-relative arguments.
            if (item['kind'] == 'http_request' and path.split('?')[0] == api['path']
                    and args.get('method', response.get('method', 'GET')) == api['method']
                    and response.get('status') == api['success_status']
                    and (args.get('expect_json') or args.get('expect_schema')) and set(api['requirement_ids']) <= set(item['requirement_ids'])):
                from agent_checks import contains_json
                from runtime_assertions import shape_issues
                try:
                    actual = json.loads(response.get('body', ''))
                    if api.get('success_assertions') and not contains_json(actual, api['success_assertions']):
                        continue
                    if api.get('success_schema') and shape_issues(actual, api['success_schema']):
                        continue
                except (ValueError, TypeError):
                    # Large image responses are truncated for model context. The
                    # executor validated the complete response before truncation.
                    if not response.get('passed'):
                        continue
                    if api.get('success_assertions') and not contains_json(response.get('validated_json'), api['success_assertions']):
                        continue
                    if api.get('success_schema') and response.get('validated_schema') != api['success_schema']:
                        continue
                matching.append(item)
        if not matching:
            issues.append(f"缺少真实成功接口证据：{api['method']} {api['path']}（具体 JSON 断言及需求关联）；错误路径/健康检查不能替代")
    for journey in contract['journeys']:
        if not all(any(rid in e.get('requirement_ids', []) for e in current) for rid in journey['requirement_ids']):
            issues.append(f"{journey['id']} 核心链路缺少成功结果证据：{journey['success_signal']}")
    return issues


def confirmed_dependency_blocker(evidence, source):
    """Only an actual, current service response proves a configuration blocker."""
    resolved = set()
    for item in reversed(evidence):
        if item.get('kind') != 'http_request' or item.get('historical') or item.get('source_digest') != source:
            continue
        response = receipt_response(item)
        args = item.get('arguments', {})
        endpoint = (args.get('method', response.get('method', 'GET')),
                    args.get('path', response.get('path', item.get('command', ''))).split('?')[0])
        if success_receipt(item):
            resolved.add(endpoint)
        if endpoint in resolved:
            continue
        if response.get('status') != 503:
            continue
        body = response.get('body', '')
        if isinstance(body, str):
            try:
                body = json.dumps(json.loads(body), ensure_ascii=False)
            except ValueError:
                pass
        if not isinstance(body, str) or not re.search(r'未配置|尚未配置|not configured|missing.{0,30}(?:key|credential)', body, re.I):
            continue
        env_names = sorted(set(re.findall(r'\b(?:[A-Z][A-Z0-9_]*_API_KEY|[A-Z][A-Z0-9_]*_SECRET)\b', body)))
        if not env_names:
            continue  # Do not guess from transient 503s or generic error messages.
        return {'evidence_id': item['id'], 'method': endpoint[0], 'path': item.get('arguments', {}).get('path', item.get('command', '')),
                'required_env': env_names, 'requirement_ids': item.get('requirement_ids', []),
                'message': '实际接口响应明确缺少 ' + ', '.join(env_names), 'status': 'awaiting_configuration'}
    return None


def defer_dependency(state, blocker):
    """A confirmed missing configuration is a TODO, never a whole-job stop."""
    pending = state.setdefault('deferred_dependencies', [])
    key = (blocker.get('method', 'GET'), blocker.get('path'), tuple(blocker.get('required_env', [])))
    if any((d.get('method', 'GET'), d.get('path'), tuple(d.get('required_env', []))) == key for d in pending):
        return False
    pending.append({**blocker, 'status': 'todo_configuration', 'simulation': 'implement_explicit_demo_adapter'})
    state.pop('dependency_blocker', None)
    return True


def refresh_deferred_dependencies(state, evidence, source):
    proven = {(e.get('arguments', {}).get('method', 'GET'), e.get('arguments', {}).get('path', '').split('?')[0])
              for e in evidence if e.get('kind') == 'http_request'
              and e.get('source_digest') == source and success_receipt(e)}
    state['deferred_dependencies'] = [d for d in state.get('deferred_dependencies', [])
                                      if (d.get('method', 'GET'), d.get('path', '').split('?')[0]) not in proven]


def delivery_todo(root, ledger, state, files):
    lines = ['演示交付 TODO（不代表全部真实业务已验收）']
    lines += ['- ' + note for note in state.get('delivery_limitations', [])]
    for dependency in state.get('deferred_dependencies', []):
        lines.append('- 外部配置：' + dependency['message'] + '；配置并切换真实适配器后重新验证。')
    for task in ledger.tasks:
        if task['status'] != 'done':
            lines.append(f"- {task['id']} {task['title']}：{task.get('note') or '尚待完善或验证'}")
    gaps = ledger.completion_issues(files)
    if gaps:
        lines += ['- 未通过/未验证项：' + gap for gap in gaps]
    if len(lines) == 1:
        lines.append('- 全面边界场景与生产服务验收仍可继续完善。')
    text = '\n'.join(lines)
    (root / 'DELIVERY_TODO.md').write_text('# ' + text + '\n', encoding='utf-8')
    return text


def decision_frame(ledger, files, state):
    from agent_harness import source_digest
    source = source_digest(files)
    current = [e for e in ledger.evidence if e.get('source_digest') == source and not e.get('historical')]
    coverage = []
    for requirement in ledger.plan['requirements']:
        related = [e for e in current if requirement['id'] in e.get('requirement_ids', [])]
        coverage.append({'id': requirement['id'], 'goal': requirement['description'][:200],
                         'success_evidence': [e['id'] for e in related if success_receipt(e)],
                         'other_checks': [e['id'] for e in related if not success_receipt(e)]})
    return {'system_goal': ledger.plan['goal'], 'system_contract': ledger.plan.get('system_contract'), 'current_tasks': [
        {'id': t['id'], 'status': t['status'], 'requirements': t['requirement_ids'], 'files': t['files']}
        for t in ledger.tasks], 'coverage': coverage, 'acceptance_gaps': ledger.completion_issues(files),
        'external_blocker': confirmed_dependency_blocker(ledger.evidence, source),
        'recent_decision': state.get('decision'),
        'deferred_dependencies': state.get('deferred_dependencies', []),
        'next_action_policy': '演示优先：批量完成其他功能，外部配置缺口及难以修复部分记 TODO；可使用明确标注的模拟适配器推进真实演示流程。完成可实现的功能后用 deferred 注记未完成部分，继续依赖任务。不能把模拟当真实业务验收。不要重复配置探测或停止整个项目；最终真实构建、页面挂载及无致命运行错误必须通过。'}


def diagnosis_fingerprint(facts):
    """Repeated receipt IDs, notes and counters are not new diagnostic facts."""
    receipts = sorted({json.dumps({k: r.get(k) for k in ('kind', 'command', 'exit_code', 'requirement_ids', 'current_source', 'result')},
                                  ensure_ascii=False, sort_keys=True) for r in facts['recent_receipts']})
    failed = [{k: e.get(k) for k in ('name', 'args', 'source', 'code', 'output')} for e in facts['actual_failed_operations']]
    return {'source': facts['source_digest'], 'gaps': facts['acceptance_gaps'],
            'tasks': [(t['id'], t['status']) for t in facts['tasks']], 'receipts': receipts, 'failed': failed}
