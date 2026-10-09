"""Validate model tool requests before any handler or filesystem side effect.

Provider finish labels and JSON schemas are hints, never an execution boundary.
The schema subset here is the subset used by the Agent's tool definitions.
"""
import json
import math
from tool_limits import SHELL_COMMAND_MAX, SHELL_TIMEOUT_MAX, READ_LIMIT_MAX, READ_BATCH_MAX, READ_BATCH_CHARS_MAX, WRITE_BATCH_MAX


class ToolArgumentError(ValueError):
    def __init__(self, code, detail, name):
        self.code, self.name = code, name
        super().__init__(detail)

    def result(self):
        if self.code == 'OUTPUT_TRUNCATED':
            hint = '本响应被截断，先拆小当前调用并生成完整 JSON；只重新提交未执行的调用。'
        else:
            hint = {
                'run_shell': f'提供 1–{SHELL_COMMAND_MAX} 字符的字符串 command；timeout 是正整数秒，超过 {SHELL_TIMEOUT_MAX} 秒会自动限到上限。命令只执行一次，超时后检查日志并拆分长步骤。',
                'write_files': f'每项必须提供明确的 path 和完整 content，支持 1–{WRITE_BATCH_MAX} 个文件；建议大代码拆小批。整批校验通过前不会写入。',
                'write_file': '明确提供 path 和完整字符串 content；不能推断缺失路径。已有文件局部修改可使用 replace_in_file。',
                'read_file': f'从 list_files 确认准确路径；offset 为非负字符偏移，limit 为正整数，单页最多 {READ_LIMIT_MAX} 字符，按实际返回的区间继续分页。',
                'read_files': f'一次提供 1–{READ_BATCH_MAX} 个真实相关文件路径；总结果预算 {READ_BATCH_CHARS_MAX} 字符。执行器会在超出预算时按文件顺序自动缩小 limit，不会拒绝整次读取；先批量读取接口、类型、路由和调用方，再进行跨文件修改。',
                'read_document': f'从 list_documents 获取真实 id；offset 为非负字符偏移，单页最多 {READ_LIMIT_MAX} 字符。',
                'read_tool_output': f'使用先前返回的 output_id；offset 为非负偏移，单页最多 {READ_LIMIT_MAX} 字符。找不到引用时不要重复执行原命令。',
                'replace_in_file': '重新读取目标范围，用当前源码中唯一且逐字相同的 old 文本和明确 new 文本重新提交；不要模糊替换。',
                'apply_patch': '读取相关当前源码，使用唯一精确上下文的 @@ 或有效 unified diff；也可用 replace_in_file，不能猜旧内容。',
                'update_task': '用 get_tasks 确认任务和依赖；done 必须引用当前源码实际成功的验证 ID。先完成依赖或执行缺少的验证。',
                'browser_check': '只修正未执行的调用。每个 action 按其独立 schema 提供参数：fill/press/assert_text/assert_value 的 value 是字符串；assert_count 的 value 是非负整数，例如 0 或 2，不能写 >=1；只检查存在则用 assert_visible。select 为字符串或字符串列表；fill_from_text 需 source_selector。建议每次 3–12 个动作，需求验收保留具体业务 assert_*，不能伪造通过。',
                'http_request': '提供实际 path、100–599 的 expect_status；headers 必须为字符串键值；确认运行服务和真实接口，不伪造返回值。',
            }.get(self.name, '按当前工具声明补齐字段、类型和范围；只修正未执行的调用。')
        return json.dumps({'ok': False, 'executed': False, 'error': self.code, 'tool': self.name,
                           'detail': str(self), 'recovery': hint}, ensure_ascii=False)


def _validate(value, schema, path, name):
    if 'oneOf' in schema:
        matches = 0
        for alternative in schema['oneOf']:
            try:
                _validate(value, alternative, path, name)
                matches += 1
            except ToolArgumentError:
                pass
        if matches != 1:
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 不符合该 action 的必填字段与类型；assert_count.value 是整数数量（例如 2），不是表达式', name)
    kind = schema.get('type')
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str),
             'integer': lambda: isinstance(value, int) and not isinstance(value, bool),
             'number': lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
             'boolean': lambda: isinstance(value, bool)}
    if kind in valid and not valid[kind]():
        raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 必须是 {kind}', name)
    if isinstance(value, str):
        if len(value) < schema.get('minLength',0) or len(value) > schema.get('maxLength',float('inf')):
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 字符长度需在 {schema.get("minLength",0)}–{schema.get("maxLength","不限")} 之间',name)
        if schema.get('x-nonblank') and not value.strip():
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 不能只包含空白字符',name)
        if schema.get('x-noNul') and '\x00' in value:
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 不允许 NUL 字符',name)
        if len(value.encode('utf-8')) > schema.get('x-maxUtf8Bytes',float('inf')):
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 超过 {schema["x-maxUtf8Bytes"]} UTF-8 字节限制',name)
    if isinstance(value,float) and not math.isfinite(value):
        raise ToolArgumentError('INVALID_ARGUMENTS',f'{path} 必须是有限数值',name)
    if isinstance(value,list) and schema.get('uniqueItems') and len({json.dumps(v,sort_keys=True) for v in value}) != len(value):
        raise ToolArgumentError('INVALID_ARGUMENTS',f'{path} 不允许重复项',name)
    if 'enum' in schema and value not in schema['enum']:
        raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 必须属于 {schema["enum"]}', name)
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                raise ToolArgumentError('MISSING_ARGUMENT', f'缺少必填字段 {path}.{key}；请明确提供，不能猜测文件路径', name)
        properties = schema.get('properties', {})
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                extra = schema.get('additionalProperties', True)
                if extra is False:
                    raise ToolArgumentError('INVALID_ARGUMENTS', f'未知字段 {path}.{key}', name)
                child = extra if isinstance(extra, dict) else {}
            _validate(item, child, f'{path}.{key}', name)
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', float('inf')):
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 数量需为 {schema.get("minItems", 0)}–{schema.get("maxItems", "不限")}', name)
        for i, item in enumerate(value):
            _validate(item, schema.get('items', {}), f'{path}[{i}]', name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get('minimum', -float('inf')) or value > schema.get('maximum', float('inf')):
            raise ToolArgumentError('INVALID_ARGUMENTS', f'{path} 需在 {schema.get("minimum", "不限")}–{schema.get("maximum", "不限")} 范围内', name)


def parse_tool_arguments(call, tools, response=None, adjustments=None):
    function = call.get('function') or {}
    name = function.get('name', '')
    schema = next((t['function']['parameters'] for t in tools if t['function']['name'] == name), None)
    if schema is None:
        raise ToolArgumentError('TOOL_NOT_ALLOWED', '该工具不在当前阶段允许的工具列表中', name)
    meta = (response or {}).get('_response_meta') or {}
    if meta.get('finish_reason') == 'length':
        raise ToolArgumentError('OUTPUT_TRUNCATED', '模型响应达到输出上限，当前响应的工具调用均未执行', name)
    try:
        raw = function.get('arguments')
        if not isinstance(raw, str):
            raise ValueError('arguments 必须是 JSON 字符串')
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError(f'重复 JSON 字段 {key}')
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError('无效 JSON 数值 '+constant)))
    except (ValueError, TypeError) as exc:
        limit = meta.get('max_tokens') or 0
        capped = limit and (meta.get('completion_tokens') or 0) >= limit
        detail = ('输出 tokens 已达到请求上限，工具 JSON 不完整，未执行。'
                  if capped else '工具 JSON 不完整或格式无效，未执行。')
        raise ToolArgumentError('OUTPUT_TRUNCATED' if capped else 'INVALID_JSON', detail + str(exc)[:180], name) from exc
    # These are bounded execution hints, not filesystem identities or assertions.
    # Normalize only positive integers. Missing paths/types remain hard errors.
    if isinstance(value, dict):
        hints = {'run_shell': ('timeout', SHELL_TIMEOUT_MAX),
                 'read_code': ('max_lines', 160), 'locate_change': ('limit', 20),
                 'glob_files': ('limit', 100), 'search_code': ('limit', 80)}
        key, upper = hints.get(name, ('limit', READ_LIMIT_MAX) if name in
                               ('read_file', 'read_document', 'read_tool_output') else (None, None))
        requested = value.get(key) if key else None
        if isinstance(requested, int) and not isinstance(requested, bool) and requested > upper:
            value[key] = upper
            if adjustments is not None:
                adjustments.append({'field': key, 'requested': requested, 'effective': upper})
    # Lossless representation repair only; never invent or weaken expectations.
    if name == 'browser_check' and isinstance(value, dict) and isinstance(value.get('actions'), list):
        for index, action in enumerate(value['actions']):
            if not isinstance(action, dict) or action.get('action') != 'assert_count':
                continue
            count = action.get('value')
            normalized = None
            if isinstance(count, str) and count.isascii() and count.isdecimal() and len(count) <= 9:
                normalized = int(count)
            elif isinstance(count, float) and count.is_integer() and 0 <= count <= 1000000000:
                normalized = int(count)
            if normalized is not None:
                action['value'] = normalized
                if adjustments is not None:
                    adjustments.append({'field': f'actions[{index}].value', 'requested': count, 'effective': normalized})
    try:
        _validate(value, schema, 'arguments', name)
    except ToolArgumentError as exc:
        if meta.get('max_tokens') and (meta.get('completion_tokens') or 0) >= meta['max_tokens']:
            raise ToolArgumentError('OUTPUT_TRUNCATED', '响应用满输出上限且参数不符合合同，未执行。' + str(exc), name) from exc
        raise
    if not isinstance(value, dict):
        raise ToolArgumentError('INVALID_ARGUMENTS', 'arguments 必须是 JSON 对象', name)
    if name in ('http_request','browser_check') and 'path' in value:
        path=value['path']
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise ToolArgumentError('INVALID_ARGUMENTS','arguments.path 必须为当前项目相对 URL 路径，例如 /api/health',name)
    if name=='read_document':
        import uuid
        try:uuid.UUID(value['id'])
        except ValueError as exc:raise ToolArgumentError('INVALID_ARGUMENTS','arguments.id 必须为 list_documents 返回的 UUID',name) from exc
    if name == 'update_task' and value['status'] == 'done' and not value.get('evidence_ids'):
        raise ToolArgumentError('MISSING_ARGUMENT', 'status=done 必须提供非空 evidence_ids；使用已返回且实际成功的验证 ID，不能只声明完成', name)
    if name == 'read_files':
        items = value.get('files', [])
        if len(items) > READ_BATCH_MAX:
            raise ToolArgumentError('INVALID_ARGUMENTS', f'批量读取最多 {READ_BATCH_MAX} 个文件', name)
        total_requested = sum(int(item.get('limit', READ_LIMIT_MAX)) for item in items)
        if total_requested > READ_BATCH_CHARS_MAX:
            # Models naturally select a coherent 8–16 file set and often put
            # the single-file maximum on every item. Fit that request locally
            # so a predictable size mistake does not consume another model
            # turn. Leave room for path/range headers in the returned text.
            budget = max(len(items), READ_BATCH_CHARS_MAX - len(items) * 256)
            original = total_requested
            requested_limits = [int(item.get('limit', READ_LIMIT_MAX)) for item in items]
            scale = budget / original
            effective_limits = [max(1, int(limit * scale)) for limit in requested_limits]
            # Distribute rounding slack to the largest fractional remainders.
            slack = budget - sum(effective_limits)
            fractions = sorted(
                range(len(items)),
                key=lambda index: requested_limits[index] * scale - int(requested_limits[index] * scale),
                reverse=True,
            )
            for index in fractions[:max(0, slack)]:
                effective_limits[index] += 1
            for item, effective in zip(items, effective_limits):
                item['limit'] = effective
            if adjustments is not None:
                adjustments.append({'field': 'files[].limit', 'requested_total': original,
                                     'effective_total': sum(int(item.get('limit', 0)) for item in items),
                                     'reason': f'批量读取总预算 {READ_BATCH_CHARS_MAX}，已自动按文件顺序缩小'})
    if name == 'browser_check':
        for i, action in enumerate(value['actions']):
            kind = action['action']
            prefix = f'arguments.actions[{i}]'
            if kind in ('click', 'fill', 'fill_from_text', 'press', 'check', 'uncheck', 'select', 'assert_value'):
                if not isinstance(action.get('selector'), str) or not action['selector'].strip():
                    raise ToolArgumentError('MISSING_ARGUMENT', prefix + '.selector 必须是明确的非空选择器', name)
            if kind in ('fill', 'press', 'assert_text', 'assert_value') and not isinstance(action.get('value'), str):
                raise ToolArgumentError('INVALID_ARGUMENTS', prefix + '.value 必须是字符串', name)
            if kind == 'select' and not (isinstance(action.get('value'), str) or
                                        isinstance(action.get('value'), list) and all(isinstance(v, str) for v in action['value'])):
                raise ToolArgumentError('INVALID_ARGUMENTS', prefix + '.value 必须是字符串或字符串列表', name)
            if kind == 'fill_from_text' and (not isinstance(action.get('source_selector'), str) or not action['source_selector'].strip()):
                raise ToolArgumentError('MISSING_ARGUMENT', prefix + '.source_selector 必须是可见文本元素的选择器', name)
            if kind == 'assert_response':
                path = action.get('selector')
                if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path or '?' in path:
                    raise ToolArgumentError('INVALID_ARGUMENTS', prefix + '.selector 必须是项目 API 相对路径，不带 query/凭据', name)
            if kind == 'assert_count' and (not isinstance(action.get('value'), int) or
                                           isinstance(action.get('value'), bool) or action['value'] < 0):
                raise ToolArgumentError('INVALID_ARGUMENTS', prefix + '.value 必须是非负整数', name)
        if value.get('requirement_ids'):
            from delivery_checks import validate_workflow_contract
            try:
                validate_workflow_contract(value)
            except ValueError as exc:
                if adjustments is None:
                    # Final acceptance scenes remain strict. Model-issued
                    # setup operations can execute without claiming coverage.
                    raise ToolArgumentError('INVALID_ARGUMENTS', str(exc), name) from exc
                adjustments.append({'field':'requirement_ids','requested':value['requirement_ids'],'used':[],
                                    'reason':'此调用仅作为准备/观察操作执行，不能认定需求验收通过：'+str(exc)+
                                    '。接下来在当前浏览器会话验证真实业务结果，再关联需求 ID；不要重做已成功的准备操作。'})
                value['requirement_ids'] = []
        if value.get('requirement_ids') and not any(a['action'].startswith('assert_') and a.get('selector') not in (None,'body','html') for a in value['actions']):
            if adjustments is None:
                raise ToolArgumentError('INVALID_ARGUMENTS', '需求验收必须有真实 assert_* 动作', name)
            adjustments.append({'field':'requirement_ids','requested':value['requirement_ids'],'used':[],
                                'reason':'准备/观察操作已执行，但没有具体业务结果断言，不能认定需求通过。后续验证真实结果再关联需求 ID。'})
            value['requirement_ids'] = []
    return value
