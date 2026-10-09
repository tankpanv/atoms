"""Complete public tool contracts. Applied to the same schemas used for validation."""
from tool_limits import READ_LIMIT_MAX, READ_BATCH_MAX, READ_BATCH_CHARS_MAX, SHELL_COMMAND_MAX, SHELL_TIMEOUT_DEFAULT


def complete_specs(tools):
    defaults = {
        'read_file': {'offset':0,'limit':10000},
        'read_document': {'offset':0,'limit':12000},
        'read_tool_output': {'offset':0,'limit':6000},
        'run_shell': {'timeout':SHELL_TIMEOUT_DEFAULT},
        'scaffold_project': {'directory':'frontend','template':'react-ts'},
        'locate_change': {'include':'','limit':12},
        'glob_files': {'offset':0,'limit':50},
        'search_code': {'include':'','regex':False,'offset':0,'limit':40},
        'read_code': {'start_line':1,'max_lines':100},
        'http_request': {'method':'GET','headers':{}},
        'browser_check': {'path':'/'},
    }
    explanations = {
        'path':'Source tools: exact project-relative file path, forward slashes, no absolute path, .., credentials, generated/cache directories or symlink escape. HTTP/browser tools: current project URL path starting with /, never an external URL.',
        'directory':'Project-relative new directory; same restrictions as source paths. Default frontend. Existing nonempty directories are never overwritten.',
        'offset':'Zero-based character offset for read_file/read_document/read_tool_output; zero-based result offset for glob_files/search_code. Continue from the actual returned end offset.',
        'files':'For read_files, 1–%d related source paths with optional offset/limit. The combined result budget is %d characters; if requested limits exceed it, the executor automatically shrinks limits in file order and reports the adjustment. Use one batch for a coherent cross-file change and do not repeat unchanged paths.' % (READ_BATCH_MAX, READ_BATCH_CHARS_MAX),
        'start_line':'One-based source line, default 1. When symbol is supplied its definition start overrides start_line.',
        'max_lines':'Maximum source lines, default 100, range 1–160; larger positive integers are capped to 160 and reported.',
        'query':'Nonempty search text. search_code uses literal substring unless regex=true; search_files is case-insensitive literal text, not regex. symbol_search looks for definitions. No shell execution.',
        'queries':'1–12 nonempty search terms from the current request. Include identifiers and actual UI labels; no matches means broaden terms.',
        'include':'Optional filename glob; empty string means all indexed source files. Example frontend/src/**/*.tsx.',
        'pattern':'Nonempty filename glob, not regex or shell command. Example **/*auth*.',
        'regex':'Boolean true enables Python regular-expression search; default false means literal text. Invalid regex is rejected.',
        'content':'Complete UTF-8 text, no NUL; empty string is allowed to create an empty file. Prefer focused patches for large existing files; suggested generated source batch <=12000 characters is guidance, not a file size limit.',
        'old':'Nonempty exact text occurring exactly once in the current file; no regex/fuzzy matching. Read current source first.',
        'new':'Exact replacement text; empty string deletes the matching old block.',
        'patch':'Nonempty unified diff or unique exact-context @@ hunks. Prefix each hunk line with space, + or -. No fuzzy matching, no multi-file patch; path identifies one existing file.',
        'command':f'Noninteractive Bash command, 1–{SHELL_COMMAND_MAX} characters, not whitespace-only, no NUL. Runs in project root with errexit and pipefail: failed assertions stop the command. Use if/|| explicitly for expected failures. Long-lived services use workspace dev/services and runtime_check. Nonzero exit is real failure evidence.',
        'requirement_ids':'Optional unique requirement IDs copied from get_tasks/current plan; only actual checked requirements. Unknown IDs are rejected; omit or [] for observations not proving acceptance.',
        'evidence_ids':'Unique real successful verification IDs from execution tools/get_tasks. status=done requires at least one fresh relevant ID; never invent IDs.',
        'note':'Optional factual progress text, at most 8000 characters; a note is not proof of completion.',
        'output_id':'Exact archived output_id returned earlier in this project session; never a filename or guessed ID. Unknown IDs are rejected; do not rerun commands to retrieve output.',
        'service':'Optional exact configured service name from runtime_check. Omit to use the frontend/proxy route; never a hostname or port.',
        'expect_status':'Required exact integer HTTP status 100–599, including expected rejection such as 401/422. No strings or status expressions.',
        'body':'JSON objects/arrays/scalars are JSON encoded; strings are raw request text. For form APIs use form; for multipart use form and actual project files. Never disguise CSS or unrelated files as images.',
        'expect_schema':'JSON response shape: type, required, properties, items, minItems/maxItems, minLength/maxLength, minimum/maximum, enum/const. Unknown keywords are rejected; no executable predicates.',
        'expect_json':'Optional JSON subset expected in the real response. Object keys recursively match; each expected array item must match an actual item. Scalars compare exactly; no JavaScript expressions.',
        'headers':'Optional JSON object of header names to string values; default {}. Use only the application own credentials when required, never platform credentials.',
        'symbol':'Optional exact indexed definition name in path. Overrides start_line; unknown symbols fail instead of guessing.',
    }
    for tool in tools:
        function = tool['function']; name = function['name']; schema = function['parameters']
        def walk(node, field='', free_json=False):
            if free_json:
                node.update(description=explanations[field])
                return
            kind=node.get('type')
            if kind=='object':
                if 'properties' in node:
                    node['additionalProperties']=False
                for key, child in node.get('properties',{}).items():
                    walk(child,key,key in ('body','expect_json','expect_schema'))
            if kind=='array':
                node.setdefault('maxItems',100)
                if field in ('requirement_ids','evidence_ids'):
                    node['uniqueItems']=True
                walk(node.get('items',{}),'id' if field in ('requirement_ids','evidence_ids') else field)
            if kind=='string':
                node.setdefault('maxLength',8000)
                if field not in ('content','new','value','include','note','service','expect_body'):
                    node.setdefault('minLength',1)
                if field in ('path','directory','pattern','include','selector','source_selector'):
                    node['maxLength']=2048
                if field in ('content','old','new','patch'):
                    node.pop('maxLength', None)
                if field in ('id','output_id'):node['maxLength']=128
                if field=='command':node['maxLength']=SHELL_COMMAND_MAX
                if field not in ('content','new','value','include','note','service','expect_body'):
                    node['x-nonblank']=True
                node['x-noNul']=True
            if kind=='integer' and field=='offset':node['minimum']=0
            if field in explanations:
                node['description']=explanations[field] + (' '+node['description'] if node.get('description') else '')
            for branch in node.get('oneOf',[]):walk(branch,field)
        walk(schema)
        if name == 'http_request':
            function['description'] += ' For login/register use save_as with a separate name per account, then auth_from={response: name}; never manually copy opaque JWTs. JSON Pointer references {{http:name:/field}} in path/body/assertions copy exact saved values; several dependent tool calls can run sequentially in one model response. expect_json empty arrays require actual emptiness; use expect_body="" for 204. Keep fixture creation, update, readback and cleanup in order; never assert a deleted record still exists.'
        if name == 'browser_check':
            function['description'] += ' Browser cookies and application session storage persist between calls for this project; logout clears the session. Use actual returned controls/selectors and current DOM, not guessed HTML attributes. API tool login does not authenticate the browser. For login/setup-only calls omit requirement_ids. Incomplete model-issued verification flows execute as setup with coverage removed and reported; only full actual business assertions can verify requirements.'
            actions=schema['properties']['actions']; actions['minItems']=1
            actions['description']='1–40 ordered actions, executed sequentially in one browser session; stops at first failed action. Prefer 3–12 per call. Side effects before an assertion failure remain; do not replay successful mutations blindly.'
            for variant in actions['items']['oneOf']:
                action=variant['properties']['action']['enum'][0]
                permitted={'action'}
                if action in ('click','press'):permitted.add('dialog')
                if action != 'reload':permitted.add('selector')
                if action in ('fill','press','select','assert_text','assert_value','assert_count'):permitted.add('value')
                if action=='fill_from_text':permitted.add('source_selector')
                if action=='assert_response':permitted.update(('method','status','expect_json'))
                variant['properties']={key:child for key,child in variant['properties'].items() if key in permitted}
                if 'selector' in variant['properties']:
                    variant['properties']['selector']['description']='CSS or Playwright selector, max 2048 characters; omitted read-only selectors mean body. Interactions need an explicit target.' if action!='assert_response' else 'Project API path starting with /; no query, credentials, external URL or backslash. Match a real response caused by a preceding interaction; does not send a new request.'
                if action=='assert_response':variant['properties']['status']['default']=200
                if action=='assert_text':variant['properties']['value']['description']='Expected literal substring contained in target text; not regex. Empty text is allowed but cannot prove functionality.'
                if action=='assert_value':variant['properties']['value']['description']='Expected exact input value, including empty string when testing reset; not regex.'
                if action=='fill':variant['properties']['value']['description']='String replacing the ENTIRE input value; empty string clears it. Does not append.'
                if action=='press':variant['properties']['value']['description']='Playwright key string, e.g. Enter or Control+a; nonempty, not a key code integer.';variant['properties']['value']['minLength']=1
        if name in ('http_request','browser_check'):
            schema['properties']['path']['description']='Project URL path starting with /; default / for browser_check, required for http_request. No external URL, // prefix or backslash. http_request permits query strings.'
        for field,value in defaults.get(name,{}).items():schema['properties'][field]['default']=value
        if name=='read_document':schema['properties']['id']['description']='UUID attachment ID returned by list_documents for this project; not a filename. Example 123e4567-e89b-12d3-a456-426614174000.'
        if name=='update_task':schema['properties']['id']['description']='Exact task ID from get_tasks/current plan, e.g. T1; not a requirement ID or filename.'
        if name=='runtime_check':function['description']='Start configured services and check readiness/logs. Restarts services when source or runtime configuration changed; reuses unchanged live services. A running process is not proof of successful business operations. No parameters: {}. Configure dev/services in .atoms-workspace.json first.'
        if name=='search_files':schema['properties']['query']['description']='Nonempty literal case-insensitive text, not regex. Returns at most 120 matching lines from project source.'
        function['description'] += ' Arguments must be a JSON object; omit optional fields to use documented defaults. Unknown keys, null for typed fields, wrong types, empty required identifiers and NUL strings are rejected before execution. Limits are hard unless explicitly described as capped or guidance. Do not replay successful calls after a sibling error.'
