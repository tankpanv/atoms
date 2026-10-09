"""Small explicit JSON shape assertions for real responses; no executable predicates."""
KEYS = {'type', 'required', 'properties', 'items', 'minItems', 'maxItems', 'minLength',
        'maxLength', 'enum', 'const', 'minimum', 'maximum', 'additionalProperties'}


def shape_issues(value, schema, path='$', depth=0):
    if depth > 12 or not isinstance(schema, dict) or set(schema) - KEYS:
        raise ValueError('expect_schema 仅支持明确的类型/字段/集合长度/枚举/数值边界，深度最多 12')
    kinds = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': isinstance(value, int) and not isinstance(value, bool),
             'number': isinstance(value, (int, float)) and not isinstance(value, bool),
             'boolean': isinstance(value, bool), 'null': value is None}
    kind = schema.get('type')
    if kind is not None and kind not in kinds:
        raise ValueError('expect_schema.type 无效')
    if kind and not kinds[kind]:
        return [f'{path}: expected {kind}']
    issues = []
    if 'const' in schema and value != schema['const']:
        issues.append(f'{path}: const does not match')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or value not in schema['enum']):
        issues.append(f'{path}: enum does not match')
    if isinstance(value, dict):
        required = schema.get('required', [])
        properties = schema.get('properties', {})
        if not isinstance(required, list) or not all(isinstance(v, str) for v in required) or not isinstance(properties, dict):
            raise ValueError('expect_schema.required/properties 无效')
        for name in required:
            if name not in value:
                issues.append(f'{path}.{name}: missing required field')
        for name, child in properties.items():
            if name in value:
                issues += shape_issues(value[name], child, path + '.' + name, depth + 1)
        if schema.get('additionalProperties') is False:
            issues += [f'{path}.{name}: unexpected field' for name in set(value) - set(properties)]
    if isinstance(value, list):
        for key, compare in (('minItems', lambda limit: len(value) < limit), ('maxItems', lambda limit: len(value) > limit)):
            if key in schema:
                if isinstance(schema[key], bool) or not isinstance(schema[key], int) or schema[key] < 0:
                    raise ValueError('expect_schema 集合长度必须为非负整数')
                if compare(schema[key]):
                    issues.append(f'{path}: {key}={schema[key]} not satisfied; actual={len(value)}')
        if 'items' in schema:
            for index, item in enumerate(value):
                issues += shape_issues(item, schema['items'], f'{path}[{index}]', depth + 1)
    if isinstance(value, str):
        for key, compare in (('minLength', lambda limit: len(value) < limit), ('maxLength', lambda limit: len(value) > limit)):
            if key in schema:
                if isinstance(schema[key], bool) or not isinstance(schema[key], int) or schema[key] < 0:
                    raise ValueError('expect_schema 字符串长度必须为非负整数')
                if compare(schema[key]):
                    issues.append(f'{path}: {key} not satisfied')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, compare in (('minimum', lambda limit: value < limit), ('maximum', lambda limit: value > limit)):
            if key in schema:
                if isinstance(schema[key], bool) or not isinstance(schema[key], (int, float)):
                    raise ValueError('expect_schema 数值边界必须为数值')
                if compare(schema[key]):
                    issues.append(f'{path}: {key} not satisfied')
    return issues[:40]
