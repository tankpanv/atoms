"""Separate local setup and usable demo adapters from real service binding."""
import re

from project_secrets import LOCAL_SECRET_NAMES


INSTRUCTIONS = '''
外部配置的判定：先判断是否能由项目自行生成/本地实现，或通过 mock 完成可操作演示；只有必须连接真实外部服务且无法合理模拟的部分，才列为用户待配置项。
1. AUTH_TOKEN_SECRET、JWT/Session 签名密钥、普通账号注册登录、演示数据和本地业务配置不属于外部服务。平台为各项目自动提供独立、随机、持久化的 AUTH_TOKEN_SECRET/JWT_SECRET/SECRET_KEY/SESSION_SECRET 等签名密钥，构建/命令/预览共享，重启不变，克隆不共享。直接从服务端环境读取并实现账号流程，不要求用户填写，不写入前端/源码/日志，也不关闭鉴权。其他可本地生成配置由 Agent 在项目私有目录安全生成。
2. 可模拟的依赖应主动实现可用的 mock/local adapter，并让页面默认走通它；不能只返回“未配置”503、留空按钮、写配置 TODO 或将任务延期后交付。涉及账号可用真实本地账号或明确演示账号；模拟不得赋予真实越权权限。邮件/短信/支付沙箱等可在演示中模拟，清楚标注模拟边界，保留后续真实适配入口。
3. 用户必须调用的真实大模型服务 URL/API key、用户私有服务或确实无法模拟的能力，才需要外部配置。不得伪造服务凭据、借用平台模型密钥或声称 mock 是真实服务成功；即便这些部分等待配置，其他功能和可行的演示替代也必须可用。
system_contract.dependencies 可标注 demo_strategy:"auto_configure"|"mock"|"real_service"，以及 real_service_required:true。规划与恢复要说明为什么无法本地实现或模拟；不能仅因变量名含 SECRET/API_KEY 就认定需要用户配置。可模拟服务用 mock，已走通的模拟链路不再显示为“外部配置阻塞”；接入真实供应方的说明与当前演示缺陷分开记录。
独立部署的本地签名密钥也应由部署脚本安全生成、持久保存，不把手填随机字符串作为用户先决条件。不得使用所有项目共享的默认密钥。
'''

LOCAL_ENV_NAMES = LOCAL_SECRET_NAMES | {'APP_DATABASE_URL', 'APP_DATABASE_SCHEMA', 'APP_DATA_DIR'}
AI_ENV = re.compile(r'(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_AI|AZURE_OPENAI|DEEPSEEK|LLM|AI|MODEL|AI_PROVIDER)_(?:API_KEY|BASE_URL|URL|ENDPOINT|MODEL|TOKEN|SECRET)$')


def resolution_for(names, plan=None):
    names = set(names)
    if names and names <= LOCAL_ENV_NAMES:
        return 'auto_configure'
    remaining = names - LOCAL_ENV_NAMES
    dependencies = ((plan or {}).get('system_contract') or {}).get('dependencies', [])
    matched = [d for d in dependencies if remaining.intersection(d.get('required_env', []))]
    if any(d.get('real_service_required') is True or d.get('demo_strategy') == 'real_service' for d in matched):
        return 'external_configuration'
    mocked = set().union(*(set(d.get('required_env', [])) for d in matched if d.get('demo_strategy') == 'mock'))
    if remaining and remaining <= mocked:
        return 'mock'
    if any(AI_ENV.fullmatch(name) for name in remaining):
        return 'external_configuration'
    # Unknown service configuration requires a local/mock assessment first.
    # An env variable alone does not prove an irreducible external dependency.
    return 'mock'


def issue_resolution(issue, plan=None):
    names = issue.get('required_env', [])
    if names and (set(names) <= LOCAL_ENV_NAMES or plan is not None):
        return resolution_for(names, plan)
    return issue.get('resolution') or resolution_for(names)


def repair_guidance(issue):
    names = ', '.join(issue.get('required_env', []))
    if issue['resolution'] == 'auto_configure':
        return ('修复项目本地配置', names + ' 属于项目本地配置，不是外部依赖，不要求用户填写或延期。'
                '项目签名密钥已由平台自动生成并持久化，APP_DATABASE_* 已由平台托管。'
                '检查实际服务环境、变量名和读取方式，重启受影响服务并走通注册/登录或对应业务；不要输出密钥，不关闭鉴权。')
    if issue['resolution'] == 'mock':
        return ('接通可用演示适配', names + ' 应优先本地实现或接通 mock/local adapter。'
                '不能仅交付未配置503或要求用户先填配置；让当前页面默认走通模拟链路，标注 simulated:true，'
                '实际验证成功操作。只有确实不能模拟且必须调用真实服务时，才在合同说明原因并标为 real_service。')
    return ('真实服务配置已记入 TODO，继续开发', names + ' 涉及必须调用的真实服务。'
            '保留真实适配入口和配置说明，其他功能不得阻塞；若有合理演示替代先接通并标注 simulated:true。'
            '不可伪造供应方凭据、借用平台模型密钥或将模拟当成真实服务验收。')
