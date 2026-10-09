# 外部配置与可用演示

只有必须连接真实外部服务、且无法合理模拟的配置，才交给用户。不能仅因接口报告缺少 `*_SECRET` 或 `*_API_KEY` 就认定外部服务不可用。

| 情况 | Agent 行为 | 用户待配置项 |
| --- | --- | --- |
| 项目签名密钥、注册登录、演示数据、本地业务配置 | 自动生成或本地实现并验证 | 无 |
| 托管项目数据库 | 使用已有 APP_DATABASE_URL/APP_DATABASE_SCHEMA；修复实际接线 | 无 |
| 可模拟的邮件、短信、支付沙箱等依赖 | 主动接通默认可用的 mock/local adapter，标注 simulated:true，验证实际页面/API 操作 | 不作为演示阻塞；真实接入说明单独记录 |
| 必须使用的真实模型 URL/API key、私有服务或确实无法模拟的能力 | 保留真实接口与明确配置说明，完成其他可用功能和合理演示替代 | 仅列实际缺少的真实服务配置 |

不能把“接口返回未配置503”和配置 TODO 当成可用 mock。模拟结果不能冒充真实供应方成功；本地登录签名密钥自动配置也不意味着关闭鉴权或将所有用户设为登录。

`system_contract.dependencies` 可用 `demo_strategy: auto_configure | mock | real_service` 和 `real_service_required: true` 表明真实边界。已有计划仍兼容。执行器区分本地修复、模拟适配和真实服务配置；模拟成功后清除对应模拟阻塞，真正的真实服务配置仍需真实调用证据才能清除。切换为 mock 的新计划会重新分类旧 TODO。本地密钥的历史误判也会清理。普通档不再将这类明确可修复的本地配置错误直接转为 API 延期。

## 项目密钥

`project_secrets.environment()` 为各项目独立生成常用对称签名密钥（包含 AUTH_TOKEN_SECRET、JWT_SECRET、SESSION_SECRET、SECRET_KEY），保存在 `.atoms-data/application-secrets.json`，权限为 0600。命令、构建和所有预览服务读取同一份值；并发启动使用文件锁和原子替换，重启保持稳定，不继承平台签名密钥或模型凭据。

该文件不进入源码版本、下载、GitHub 导出和源码克隆。克隆项目生成自己的密钥；持久化工作区备份必须保留私有数据。已有私有配置保持稳定，损坏配置明确报错，不静默重置导致已登录会话失效。项目自己的显式命令环境可覆盖默认值。

独立部署时也应通过部署脚本生成并持久保存本地签名密钥，避免共享固定默认值。

## 验证

`tests/test_project_secrets.py` 覆盖密钥持久化、项目隔离、并发初始化、克隆隔离、私有文件保护、平台凭据隔离，以及真实 HTTP 服务与命令共享密钥并跨重启保持稳定。

`tests/test_system_contract.py` 覆盖 AUTH_TOKEN_SECRET 等本地配置误判、可模拟依赖、实际模型 URL/API key、显式真实服务/模拟策略、旧 TODO 清理和交付文案。原有“模拟不等于真实供应方成功”的规则保持有效。

## 2026-10-09 部署验证

平台 API、调度器、预览网关和项目 Worker 已采用修复。相关 28 项回归通过；另以 Worker 相同的只读根文件系统和 Linux capabilities（无 CAP_FOWNER）验证重复读取项目用户所有的密钥文件，检查通过。密钥文件权限只在初始化或实际需要修复时处理，不在每次启动对项目用户文件执行不必要的 chmod。

截图项目 `c290438ff08d48549d9596d0b0f4681b` 已保存版本 5。通过实际 API 验证注册、登录、账户回读、重复邮箱409、错误密码401、无效token401、未登录生成401，以及服务重启后原token继续有效。实际浏览器完成注册、刷新恢复、退出、重新登录、上传衣物并生成三套明确标注的模拟穿搭；页面运行错误为零。

项目 README、配置依赖说明、会话待办和交付待办已移除“用户必须配置 AUTH_TOKEN_SECRET”的误判。旧任务执行证据保留，不将未验收边界或真实模型服务标成已完成。原文件与会话备份放在项目私有 `.atoms-data/configuration-recovery-*` 下，临时验证账户已删除。登录对话框改为用户可理解的账户保存说明。

本次没有模型调用或外部供应方请求。真实多模态模型 URL/API key 仅在需要接入真实服务时配置，当前模拟演示可以直接使用。

证据：`.deploy/dependency-policy-final-tests.log`、`.deploy/dependency-policy-worker-capabilities.log`、`.deploy/stylemate-configuration-live.json`、`.deploy/stylemate-configuration-migration.json` 和 `.deploy/stylemate-config-recovery.png`。
