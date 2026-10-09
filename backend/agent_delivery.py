"""Task budgets govern scope; delivery repairs retain real billing and stop controls."""
from dataclasses import dataclass, field
import time


def complexity(plan):
    requirements = len(plan['requirements'])
    tasks = len(plan['tasks'])
    backend = bool(plan['architecture']['backend']['required'])
    level = 'simple' if not backend and requirements <= 6 and tasks <= 3 else ('medium' if requirements <= 6 and tasks <= 5 else 'complex')
    return level, {'simple': 16, 'medium': 40, 'complex': 120}[level]


class ExecutionPacing:
    """Soft progress checkpoints coach the full scope; never change budgets."""
    def __init__(self, target):
        self.target = target
        self.cursor = 0
        self.previous = (0, 0)
        self.stagnant = 0
        self.stage = None

    def guide(self, ledger, journal, budget):
        done = sum(t['status'] == 'done' for t in ledger.tasks)
        passed = sum(e['exit_code'] == 0 and not e.get('historical') for e in ledger.evidence)
        writes = {'write_file','write_files','replace_in_file','apply_patch','scaffold_project','delete_file'}
        actions = journal[self.cursor:]
        productive = (done, passed) != self.previous or any(
            a.get('tool') in writes and not any(marker in a.get('result','') for marker in
                ('"ok": false', '工具错误', 'TOOL_PRECONDITION_FAILED')) for a in actions)
        self.stagnant = 0 if productive else self.stagnant + bool(budget.task_iterations)
        self.cursor = len(journal)
        self.previous = (done, passed)
        milestone = min(3, budget.task_iterations * 3 // max(1,self.target))
        stage = (milestone, self.stagnant >= 3)
        changed = self.stage is not None and stage != self.stage
        self.stage = stage
        focus = ('集中实现贯穿界面、数据和业务的完整用户流程；先确认模块接口，再集中写入相关文件。',
                 '检查全部明确需求的覆盖情况；把剩余功能按依赖合并推进，避免逐文件读取、重复设计和只写文档。',
                 '把已实现功能逐项关联真实验证 ID，补齐当前需求缺口；复用同一源码的已有成功验证，避免重复执行同一个检查。',
                 '建议执行节奏已超出预期，保留全部需求，调整执行方法：定位具体阻塞并集中完成剩余功能，对外部依赖和难解问题记录 TODO，推进其他实现并保证可启动演示。')[milestone]
        if self.stagnant >= 3:
            focus += '最近几轮没有新实现、成功验证或任务完成记录。利用现有源码观察和工具反馈定位一个具体阻塞，下一步实施修复或针对性验证，不重新扫描项目。'
        pending = [{'id':t['id'],'title':t['title'][:120]} for t in ledger.tasks if t['status'] != 'done']
        return (f'完整需求推进：已验收 {done}/{len(ledger.tasks)} 个任务；建议 {self.target} 轮是节奏参考，'
                f'不是限额。当前实际配置上限 {budget.iteration_limit} 次，已用 {budget.task_calls} 次模型调用。'
                + focus + ' 当前剩余任务：' + str(pending[:6])), changed


@dataclass
class DeliveryBudget:
    token_limit: int
    iteration_limit: int
    deadline: float
    task_tokens: int = 0
    task_calls: int = 0
    task_iterations: int = 0
    repair_tokens: int = 0
    repair_calls: int = 0
    repair_iterations: int = 0
    phase: str = 'implementation'
    started: float = field(default_factory=time.monotonic)
    repair_token_limit: int | None = None
    repair_iteration_limit: int | None = None
    configured_iteration_limit: int | None = None

    def __post_init__(self):
        if self.repair_token_limit is None:
            self.repair_token_limit = max(1, self.token_limit // 2)
        if self.repair_iteration_limit is None:
            self.repair_iteration_limit = max(1, (self.configured_iteration_limit or self.iteration_limit) // 2)

    def repair_exhaustion(self, include_iterations=True):
        if self.phase != 'stabilization':
            return None
        if self.repair_tokens >= self.repair_token_limit:
            return 'token'
        if self.repair_calls >= self.repair_iteration_limit:
            return 'model_calls'
        if include_iterations and self.repair_iterations >= self.repair_iteration_limit:
            return 'iterations'
        return None

    def record(self, tokens):
        if self.phase == 'stabilization':
            self.repair_tokens += tokens
            self.repair_calls += 1
        else:
            self.task_tokens += tokens
            self.task_calls += 1

    def due(self, reserve_tokens=0):
        return self.phase == 'implementation' and (
            self.task_tokens >= self.token_limit * .85 or
            self.task_tokens + reserve_tokens >= self.token_limit or
            max(self.task_iterations, self.task_calls) >= max(1, int(self.iteration_limit * .8)) or
            time.monotonic() >= self.started + (self.deadline - self.started) * .9)

    def snapshot(self):
        return {key: getattr(self, key) for key in ('phase', 'token_limit', 'iteration_limit', 'task_tokens',
                'task_calls', 'task_iterations', 'repair_tokens', 'repair_calls', 'repair_iterations',
                'repair_token_limit', 'repair_iteration_limit', 'configured_iteration_limit')} | {
                'total_tokens': self.task_tokens + self.repair_tokens,
                'total_calls': self.task_calls + self.repair_calls}


DELIVERY_INSTRUCTION = '''现在进入演示交付阶段。保留原需求和已实现代码，优先保证用户可以启动、打开并演示当前版本。
先修复依赖、构建、运行服务、页面渲染、关键流程报错；不新增无关功能，不重复全项目扫描，不为了通过验证删除测试或替换成空页面。
Web 必须配置真实 build/dev 命令、必要后端服务，路由和资源适配 $BASE_PATH；执行器会根据真实页面生成并执行核心演示场景，也可以通过 browser_check 直接提供真实功能断言；demo 配置可选，不要为补平台元数据反复改业务代码；artifact任务必须生成用户要求的实际文件并通过正文/格式和真实预览检查，不要求软件服务或无关测试；CLI/library 必须配置可执行的真实 test/演示命令并输出可观察结果。
保留原验收合同，未验证功能不能标记 done；可标记 deferred 并注明实现/模拟/缺失与配置 TODO，继续依赖任务。外部依赖不足或难解问题允许明确模拟/绕过，不阻止交付实际可启动的演示。该阶段使用独立的 token/轮数预算，达到上限保存检查点并停止；全部真实调用仍计费。工具调用必须符合 schema。浏览器验收按源码、运行配置和场景指纹去重；同一版本已有成功证据时不要再次调用 browser_check，直接复用证据继续完成任务。
每次只修复具体故障，准备好后简要总结已可演示内容及下一步可优化方向，由系统执行真实启动和检查。'''


class DeliveryLimitReached(RuntimeError):
    """The independent delivery allowance is exhausted; preserve resumable work."""
