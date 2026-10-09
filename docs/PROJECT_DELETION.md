# 项目删除

侧栏项目行悬停显示更多按钮。悬停按钮打开菜单；按钮和菜单周围保留 24px 容错区域，离开区域 280ms 自动关闭。菜单使用 portal，避免侧栏滚动容器裁切。支持点击、键盘、Escape 和外部点击关闭。

删除使用页面内原生 dialog，默认焦点在取消按钮，要求确认不可逆清理风险，清理期间禁用重复提交。侧栏、项目卡片、构建页删除入口共用确认和处理逻辑。

后端仅允许项目所有者删除。数据库 session advisory exclusive lock 与普通项目修改请求的 shared lock 配合，排除并发创建、发布、运行与删除；UUID 标准格式和不带横线格式使用相同锁身份。先保存 deleting 状态并停止排队任务，再删除工作容器和其网络，清除 MinIO 所有项目历史对象版本、delete markers、未完成上传；随后移除完整项目目录及旧目录布局，终止项目数据库连接、清理角色所属对象和权限，最后提交项目记录级联删除。

目录清理校验项目 UUID、根目录和父目录真实路径，拒绝越界路径。只解除项目自身的符号链接，不跟随它删除目标。Docker 容器删除核对 project/compose labels。S3 批量删除检查逐对象 Errors，拒绝吞掉部分失败。任何外部清理错误均保留项目记录和 deleting 状态，返回 503，允许重试删除并阻止再构建。

MinIO 实测中 `list_multipart_uploads(Prefix=项目路径)` 漏返仍能通过 list_parts 访问的上传，因此未完成上传采用 bucket 分页枚举后按完整项目前缀过滤，不删除其他项目上传。

项目专属连接器关联、对话、任务、附件记录、检查点、预览存储、路由、服务端配置通过 FK CASCADE 删除；文件缓存和附件在完整目录删除中清除。账户共享 GitHub 授权、远程 GitHub 仓库、共享 Docker 卷及其他项目不属于本次删除范围。账户账单与已发生模型用量继续准确结算，解除项目关联、清空模型 response 缓存；删除后迟到的模型响应不再入库，冻结积分仍按真实用量结算。

验证：

- `test_project_cleanup`：目录/缓存和旧布局清理、邻居项目保留、父目录 symlink 拦截、权限失败不吞错、历史版本/删除标记/未完成上传、对象逐项失败和非本项目 key 拦截。
- `test_project_delete_api`：真实 PostgreSQL 与 coordinator，独立测试账户/项目；越权 404、并发锁 409、存储故障 503 后保留记录、禁止重新构建、重试完成、running job 与 steps 级联清理。
- `test_billing`：删除项目后结算真实格式用量、释放冻结额度、清空响应缓存；deleting 项目禁止新调用。
- Playwright MCP：真实侧栏 hover、12px 外保持/移远关闭、取消不删除、确认删除后列表消失；独立项目真实 Docker、MinIO、数据库、目录残留检查。

运行有数据库和 coordinator 的回归：

```sh
docker compose exec -T backend sh -c 'cd /app/tests && PYTHONPATH=/app python -m unittest test_project_cleanup test_project_delete_api test_billing test_bootstrap_contract -q'
```
