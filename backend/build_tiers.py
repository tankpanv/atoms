"""Build quality policies shared by planning, execution and acceptance.

A tier controls scope, quality and configured task budget multipliers.
The selected tier is captured on each job; old projects default to normal.
"""
import os
from typing import Literal

BuildTier = Literal['normal', 'deep', 'advanced']
LABELS = {'normal': '普通', 'deep': '深度', 'advanced': '高级'}
POLICY_VERSION = 'build-tiers-v1'
BUDGET_MULTIPLIERS = {'normal': 1, 'deep': 2, 'advanced': 3}
BUDGET_ENV_KEYS = ('AGENT_MAX_TOKENS', 'AGENT_MAX_ITERATIONS',
                   'AGENT_BUILD_NORMAL_MULTIPLIER', 'AGENT_BUILD_DEEP_MULTIPLIER',
                   'AGENT_BUILD_ADVANCED_MULTIPLIER', 'AGENT_PLANNING_MAX_CALLS',
                   'AGENT_PLANNING_MAX_TOKENS', 'AGENT_PLANNING_TIMEOUT_SECONDS')


def normalize_tier(value=None) -> BuildTier:
    value = 'normal' if value is None else value
    if value not in LABELS:
        raise ValueError('构建档位必须是 normal（普通）、deep（深度）或 advanced（高级）')
    return value


def tier_budget_limits(value=None, environ=None):
    """Scale the configured task allowance once; context windows are separate."""
    tier = normalize_tier(value)
    settings = os.environ if environ is None else environ
    key = f'AGENT_BUILD_{tier.upper()}_MULTIPLIER'
    raw = settings.get(key) or str(BUDGET_MULTIPLIERS[tier])
    try:
        multiplier = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{key} 必须为正整数') from exc
    if multiplier < 1:
        raise ValueError(f'{key} 必须为正整数')
    base_tokens = max(10000, int(settings.get('AGENT_MAX_TOKENS') or '15000000'))
    base_iterations = max(1, int(settings.get('AGENT_MAX_ITERATIONS') or '200'))
    return {
        'build_tier': tier, 'budget_multiplier': multiplier,
        'base_token_limit': base_tokens, 'base_iteration_limit': base_iterations,
        'token_limit': base_tokens * multiplier,
        'iteration_limit': base_iterations * multiplier,
    }


def worker_budget_changed(container_environment, environ=None):
    """Existing idle workers must adopt budget configuration after a restart."""
    settings = os.environ if environ is None else environ
    current = dict(item.split('=', 1) for item in (container_environment or [])
                   if '=' in item and item.split('=', 1)[0] in BUDGET_ENV_KEYS)
    return any((current.get(key) or None) != (settings.get(key) or None) for key in BUDGET_ENV_KEYS)


def tier_instructions(value, phase='planning'):
    tier = normalize_tier(value)
    common = (
        f'\n构建质量策略 {POLICY_VERSION}：本次档位 {tier}（{LABELS[tier]}）。'
        '用户明确要求、禁止事项、技术栈与范围始终优先，所有档位都必须完整实现明确需求。'
        '档位是设计与交付标准，不能用更多对话、重复读文件、重复验收来代替质量。'
        '保留已有功能，按需定位改动；软件任务沿用适用的默认模板和数据库连接器；PPT/报告/文档/表格以实际成果完整性、内容深度、可读性和格式质量衡量档位，不强加软件服务。'
        '不因档位自动引入支付、团队协作、管理员后台、外部付费依赖或无关业务。'
    )
    scope = {
        'normal': '普通：准确完整实现用户说明和演示原型所需的核心能力；优先完成前端页面、视觉结构、交互状态和可启动预览，合理填补实现空白，不添加额外产品功能。后端保留真实接口和正确实现，但未配置的数据库、AI、支付等外部依赖不得阻塞演示交付。',
        'deep': '深度：在普通标准上分析真实使用中的遗漏细节，补充与目标直接相关的输入校验、空态、加载/错误反馈、边界行为、适用的持久化和交互一致性；系统化设计模块和接口。提示简单时给出合理细化；不扩展无关业务。',
        'advanced': '高级：在深度标准上全面设计从进入到完成目标的完整用户旅程、数据流与前后端契约，分析必要的同目标扩展、兼容性、可靠性、安全边界、可访问性与响应式体验。只纳入能明确解释用户价值的必要扩展；按实际风险设计验证，以完整需求和高质量可演示结果交付。简单需求仍用简洁架构，不为了规格增加服务或文档。',
    }[tier]
    phase_policy = {
        'planning': (
            '在 design 中说明本档位的具体设计决策与取舍。requirements 为每项标注 '
            'origin:"explicit|detail|extension"；explicit 是用户要求，detail 是必要细节，extension 是高级档的必要扩展。'
            'detail/extension 要给 rationale 说明与用户目标的直接关系；不确定的高影响决策放 assumptions，禁止擅自改变用户约束。'
            '把纳入本次交付的补充项编入 tasks 和可观察 acceptance，不能只写在设计文案里。'
            '普通不纳入 extension，深度仅纳入 detail，高级可纳入有理由的 extension。'
            '验证与需求规模和风险相称，简单需求合并为 1–3 个连贯任务；不要把每个细节拆成模型轮次。'
            '普通档计划必须保持精简：design 控制在约 1200 字符内，requirements 最多 5 项、tasks 最多 3 项；不要重复原需求、模板文档或完整 API 矩阵。'
        ),
        'execution': (
            '按计划完整实现明确需求及本档位已规划的细节/扩展，优先批量完成连贯模块，再针对实际失败修复。'
            '开发时保证契约、错误处理和边界正确，验收主要检查服务启动、核心真实功能及本档位重点风险。'
            '只在源码或验证输入变化、实际失败或缺少证据时重新验证；已通过的相同验收复用证据。'
            '普通档优先交付可看的前端演示：除非用户明确要求 API 级验收，默认不向模型开放 http_request；若用户明确要求 API，只做一次代表性检查，出现未配置/不可用就记录 TODO 并继续。普通档不要为了验收新建或运行大批单元测试，局部测试只用于定位当前具体代码错误。'
            '普通档优先复用模板和已有依赖；不要在代码实现过程中反复执行 npm install、pip install 或下载依赖，只有启动/构建明确报告缺失依赖时才集中安装一次。'
            '最终总结依据真实结果说明可用能力，不能因选择高级档就声称所有质量指标已保证。'
        ),
        'verification': (
            '普通验证源码结构、真实构建、服务启动和前端首屏/核心演示状态；除非用户明确要求 API 或后端链路，不要求逐接口、错误矩阵、持久化回读或外部服务成功证据。普通档 API 依赖失败记为 TODO，不阻塞可启动演示。'
            '深度额外覆盖与本次需求有关的关键错误/边界或状态保持；'
            '高级额外覆盖计划中必要扩展及最高风险的跨模块路径与回归。'
            '以当前计划的 acceptance 为准，合并同一用户旅程的检查，不要求逐文件审查。'
            '在一个最短场景中复用操作验证多个目标；API/命令已有证据不重复在浏览器验证；'
            '对不存在或不适用的能力不凭空新增验收，失败才修复和重跑相关步骤。'
        ),
    }
    return common + scope + phase_policy[phase]


def stamp_plan(plan, value):
    """Preserve model design while enforcing the trusted job scope policy."""
    tier = normalize_tier(value)
    for requirement in plan.get('requirements', []):
        origin = requirement.get('origin', 'explicit')
        if origin not in {'explicit', 'detail', 'extension'}:
            raise ValueError('需求 origin 必须是 explicit、detail 或 extension')
        if origin == 'extension' and tier != 'advanced':
            raise ValueError('仅高级档可规划必要扩展；当前档位应聚焦用户要求及必要细节')
        if origin != 'explicit' and not str(requirement.get('rationale') or '').strip():
            raise ValueError('补充细节和扩展必须提供 rationale，说明与用户目标的直接关系')
    plan['build_tier'] = tier
    plan['build_policy_version'] = POLICY_VERSION
    return plan


def continuation_for_tier(continuation, value, reuse_allowed=True):
    """A changed tier replans the original goal, never the bare word 'continue'."""
    if continuation is None:
        return None, None
    prompt, plan = continuation
    reusable = (reuse_allowed and normalize_tier(plan.get('build_tier')) == normalize_tier(value)
                and plan.get('build_policy_version') in (None, POLICY_VERSION))
    return prompt, plan if reusable else None
