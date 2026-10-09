"""Harness-owned smoke scenes: optional project metadata never creates a repair loop."""
import json
import re
import uuid
from agent_session import digest
from coding_runtime import AgentState, model_output_limit
from tool_contract import parse_tool_arguments


async def resolve_scene(root, plan, observed, session, gateway, client, tools):
    """Generate one grounded scene from real controls, then reuse its contract.

    Assertions remain real browser operations; this is not a model verdict.
    Provider errors propagate normally rather than being treated as code defects.
    """
    supplied = plan.get('delivery_checks')
    configured_scene = None
    config = root/'.atoms-workspace.json'
    if config.exists():
        settings = json.loads(config.read_text())
        if not isinstance(settings, dict):
            raise ValueError('工作区配置必须为 JSON 对象')
        configured_scene = settings.get('demo')
        supplied = configured_scene or supplied
    # Planner/config scenes are proposals, not observed evidence. Execute valid
    # contracts against the real UI; regenerate from actual feedback on failure.
    phase = session.phase('delivery_scene', 'grounded-scene-v3:' + digest(plan), [])
    # This product has an explicit input-answer acceptance requirement. Merely
    # seeing a feedback container does not prove either success or rejection.
    answer_game = bool(re.search(r'24\s*点|24\s*points?', str(plan['goal']), re.I)
                       and re.search(r'游戏|game|算|四张|cards', str(plan['goal']), re.I))
    if observed.get('scene_failure'):
        phase.pop('scene', None)
        phase['completed'] = False
    elif phase.get('scene'):
        try:
            return validate_scene(phase['scene'], tools, answer_game, plan)
        except ValueError:
            phase.pop('scene', None)  # reject previously cached weak contracts
            phase['completed'] = False
    if configured_scene and not observed.get('scene_failure'):
        try:
            scene = validate_scene(supplied, tools, answer_game, plan)
            phase.update(scene=scene, completed=True, source='configured_contract')
            session.save_phase('delivery_scene', phase)
            return scene  # Only actual browser execution can make it evidence.
        except ValueError:
            pass  # Invalid proposals are corrected against the actual page.
    instructions = (
        '为当前已实际渲染的应用生成一个最短的真实核心玩法/功能演示场景。'
        '只使用给出的真实页面控件和文本，不猜选择器，不要求修改代码或填写配置。'
        '不要做删除用户数据、充值、外部发布等不可逆动作。优先点击核心按钮并断言其可观察结果。'
        '提交支持点击提交按钮，或者在填写的输入框 press Enter；两者都必须随后断言实际反馈。'
        '随机题目不要硬编码数字或答案；可检查查看答案/提示或换题的可观察行为。'
        '若目标是输入答案并检查正确性的游戏，而且页面同时有参考答案与输入/提交控件，必须用 fill_from_text 从实际答案填写并提交、断言成功，再提交明显错误输入并断言错误反馈；不能只检查显示参考答案。'
        '规划提供的场景只是建议，保留其中正确的功能验收目标，但修正与实际页面不匹配的选择器和随机数据；不要降低验收标准。'
        '必须检查给定目标的核心功能；若实际页面只有脚手架样例或缺少核心功能，返回 {"missing_functionality":"具体缺少的功能"}，不要把样例计数器当作游戏验收。'
        '账号、表单、数据类需求必须跨过提交/登录入口，检查实际结果与后续业务读取；只看到登录页或提交成功提示不足以验证完整链路。'
        '使用 test_run_id 创建唯一的独立测试账号/标记数据，不读取或删除客户既有数据。'
        '全栈功能用 assert_response 检查实际项目接口的 method/status/expect_json，并断言随后的页面结果；持久化写入后 reload 再读取测试数据确认。预期 401/422 等失败必须显式用 assert_response 验证，不能把所有网络错误忽略。'
        '只返回 JSON {"path":"/","actions":[...]}，最多40步，使用完成核心链路所需的最少步骤，至少一个具体结果的 assert_*。'
        '支持 click/fill/fill_from_text/press/check/uncheck/select/reload/assert_visible/assert_text/assert_value/assert_count/assert_response；assert_response 的 selector 是项目相对 API 路径，method 默认 GET，status 默认 200，可用 expect_json 验证实际 JSON 内容；'
        'fill_from_text 从 source_selector 的可见文本填写 selector 指向的输入框，适合随机题目参考答案；assert_value 检查输入值。selector 使用真实 CSS 或 Playwright 选择器；assert_text/fill/press/select 的 value 为字符串。'
        'body/html 可见不算核心检查。'
        '原生确认框用 click/press 的 dialog="accept" 显式同意，仅用于检查场景自己新建的测试记录；默认 dismiss。DOM 弹窗使用普通点击。验收会保留本项目浏览器会话；API 工具登录不等于浏览器登录。'
        '输入判定类游戏的成功和错误反馈必须分别用 assert_text 检查实际结果文案，不能只 assert_visible 反馈容器。'
        'CSS 属性选择器优先用单引号，例如 [data-testid=\'answer\']；JSON 字符串不要二次转义。'
        'fill 和 fill_from_text 都会替换输入框全部内容，不是追加；不要用连续 fill_from_text 拼接算式。'
        '若 observed_page 含 scene_failure，按浏览器实际反馈纠正选择器或断言文案，不猜“正确/错误”等页面未使用的词。'
        '保留原场景的全部核心验收目标，不得删除失败的功能检查或改成只检查容器可见；如果证据说明功能确实不正确，返回 missing_functionality。'
    )
    from build_tiers import tier_instructions
    instructions += tier_instructions(plan.get('build_tier'), 'verification')
    messages = [{'role':'system', 'content':instructions}, {'role':'user', 'content':json.dumps({
        'goal': plan['goal'], 'requirements': plan['requirements'], 'proposed_scene': supplied,
        'observed_page': observed, 'test_run_id': uuid.uuid4().hex}, ensure_ascii=False)}]
    phase['observations'] = observed
    session.save_phase('delivery_scene', phase)
    # Parsing/schema correction is local to this small phase, not an invitation
    # to reread/rewrite the application in the main engineering loop.
    output_limit = model_output_limit(gateway.model_for(AgentState.TEST), AgentState.TEST)
    last_error = ''
    for attempt in range(2):
        # max_tokens includes thinking. A 2k budget can produce no visible
        # JSON at all. Low effort also works with mandatory reasoning models;
        # unlike exclude=True, it reduces thinking rather than hiding it.
        # The caller's normal/repair budget wrapper still clamps this request.
        response = await gateway.chat(client, AgentState.TEST, messages,
                                      max_tokens=output_limit, reasoning={'effort': 'low'})
        text = response.get('content') or ''
        meta = response.get('_response_meta') or {}
        receipt = {'attempt': attempt + 1, 'requested_max_tokens': output_limit,
                   **meta, 'output': text[:6000]}
        phase.setdefault('attempts', []).append(receipt)
        phase['attempts'] = phase['attempts'][-8:]
        from agent_harness import json_object
        try:
            if meta.get('finish_reason') == 'length':
                raise ValueError('模型输出被截断：输出 token=%s，思考 token=%s，正文字符=%s' % (
                    meta.get('completion_tokens', 0), meta.get('reasoning_tokens', 0),
                    meta.get('content_chars', len(text))))
            parsed = json_object(text)
            if isinstance(parsed.get('missing_functionality'), str):
                raise MissingFunctionality(parsed['missing_functionality'])
            scene = validate_scene(parsed, tools, answer_game, plan)
            phase['scene'] = scene
            phase['completed'] = True
            session.save_phase('delivery_scene', phase)
            return scene
        except MissingFunctionality as exc:
            receipt['error'] = str(exc)
            session.save_phase('delivery_scene', phase)
            raise
        except ValueError as exc:
            last_error = str(exc)
            receipt['error'] = last_error
            session.save_phase('delivery_scene', phase)
            if meta.get('finish_reason') == 'length':
                output_limit = min(model_output_limit(gateway.model_for(AgentState.TEST), AgentState.TEST), 65_536)
                # Do not feed a partial JSON/length marker back as a valid
                # assistant response or replay a starved reasoning context.
                messages = messages[:2] + [{'role':'user','content':
                    '上次输出预算耗尽，未收到完整 JSON。这次直接输出最短的完整场景 JSON，保留核心功能断言，不写解释。'}]
            else:
                messages += [{'role':'assistant','content':text}, {'role':'user','content':'修正场景 JSON：'+last_error}]
    raise DeliverySceneError('演示检查场景生成失败（最多两次）：'+last_error+'。已保留检查点和调用诊断；这不是业务源码错误。')


class DeliverySceneError(RuntimeError):
    """The harness could not describe a test; not an application failure."""


class MissingFunctionality(ValueError):
    """Actual page lacks the requested capability; engineering must implement it."""


def validate_scene(scene, tools, answer_game=False, plan=None):
    args = parse_tool_arguments({'function':{'name':'browser_check','arguments':json.dumps(scene)}}, tools)
    if not any(a['action'].startswith('assert_') and a.get('selector') not in (None,'body','html') for a in args.get('actions',[])):
        raise ValueError('演示场景需要具体功能/控件断言')
    validate_workflow_contract(args, plan)
    if answer_game:
        actions = args.get('actions', [])
        source_index = next((i for i,a in enumerate(actions) if a['action'] == 'fill_from_text'), None)
        if source_index is None:
            raise ValueError('随机24点游戏必须读取实际参考答案并提交，不能硬编码或跳过正确答案验收')
        positive = next((i for i,a in enumerate(actions) if i > source_index and a['action'] == 'assert_text'), None)
        negative = next((i for i,a in enumerate(actions) if positive is not None and i > positive and a['action'] == 'fill'), None)
        if (positive is None or not submitted(actions[source_index+1:positive], actions[source_index]['selector'])
                or negative is None or not any(a['action']=='assert_text' for a in actions[negative+1:])
                or not submitted(actions[negative+1:], actions[negative]['selector'])):
            raise ValueError('24点场景必须分别提交真实正确答案和错误输入，并分别 assert_text 判定结果；不能仅检查反馈容器可见')
    return args


def submitted(actions, input_selector):
    return any(a['action'] == 'click' or (a['action'] == 'press' and a.get('value') == 'Enter'
               and a.get('selector') == input_selector) for a in actions)


def validate_workflow_contract(scene, plan=None):
    """Reject generic false positives independent of business names or headers."""
    actions = scene.get('actions', [])
    input_kinds = {'fill', 'fill_from_text', 'check', 'uncheck', 'select'}
    for index, action in enumerate(actions):
        if action['action'] not in input_kinds:
            continue
        following = actions[index+1:]
        # A series of field fills constitutes one operation; validate its last
        # field, not every preceding field as an independent form submission.
        if following and following[0]['action'] in input_kinds:
            continue
        outcome = next((i for i, a in enumerate(following)
                        if a['action'].startswith('assert_') and a.get('selector')
                        not in (None, 'body', 'html', action.get('selector'))), None)
        if outcome is None:
            raise ValueError('输入/编辑操作之后必须断言独立的业务结果，不能只检查输入框回显或页面存在')
        # change/check/select can submit automatically; fill needs an explicit
        # trigger unless an actual API response is asserted (autosave).
        if (action['action'] in {'fill', 'fill_from_text'}
                and not any(a['action'] in {'click', 'press', 'assert_response', 'assert_text', 'assert_count'} for a in following[:outcome+1])):
            raise ValueError('填写内容后必须提交/触发操作并断言实际结果')
    if plan and (plan.get('architecture', {}).get('backend') or {}).get('required'):
        if not any(a['action']=='assert_response' and 200 <= a.get('status',200) < 300 for a in actions):
            raise ValueError('全栈需求演示必须 assert_response 验证实际业务 API 成功，不能只验证页面或本地模拟结果')
        response_index = next(i for i,a in enumerate(actions) if a['action']=='assert_response' and 200 <= a.get('status',200) < 300)
        if not any(a['action'].startswith('assert_') and a['action']!='assert_response'
                   and a.get('selector') not in (None,'body','html') for a in actions[response_index+1:]):
            raise ValueError('全栈需求必须同时断言 API 返回之后的页面业务结果')
