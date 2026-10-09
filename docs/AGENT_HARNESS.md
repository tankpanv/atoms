# Coding agent execution harness

## Why projects converged on a single frontend file

The previous initializer wrote a fixed root Vite/React app, including `src/App.tsx`, before the model had analyzed the request. The architecture document already described a frontend template and the system prompt encouraged keeping it. The model could change frameworks, but the easiest execution path was to replace one file.

Planning used a 300-character understanding summary and a 500-character plan. Neither output represented requirements, feature acceptance or task dependencies. Planning failure silently fell back to the raw prompt. The implementation loop periodically discarded all but its first messages and twelve recent messages, losing earlier decisions and command outcomes. Completion required changes, a passing configured build and a permissive review; it did not require proof that every requested flow existed. Fullstack services could be started manually, but the preview lifecycle managed only one process. Review source retrieval also omitted `frontend/` and `backend/` trees.

A populated backend directory in a reference screenshot does not establish that a particular simple application needs a backend. The decision should follow data sharing, identity, secrets, integrations, durable server state and other actual requirements. Similarly, an initial placeholder displayed while an agent is still running is not its final implementation. The code-level constraints above are the concrete reasons this implementation favored frontend demos.

## Current execution path

1. Create a persistent project directory and an undecided architecture document. Do not create application source yet.
2. Understand the request together with conversation history and attachments. Explore actual files using read-only tools. Produce validated structured architecture, requirements, acceptance steps, dependent tasks and initialization/build/test commands. Retry malformed plans; do not silently bypass planning.
3. Persist `.atoms/requirements.json`, `.atoms/PLAN.md` and `.atoms/task-state.json`. The harness focuses the next dependency-ready task and reminds the model to validate and close it before moving on. Repeated reads of unchanged ranges are suppressed only while the earlier full response is still in context; after compaction the model can read them again. The model uses shell, source search, paged reads, patches and file writes. `scaffold_project` executes the official noninteractive Vite CLI into a new frontend directory and creates root commands; it refuses to overwrite existing source. Other frameworks may use their own CLI via `run_shell`.
4. Keep domain responsibilities in real modules. The model chooses whether a backend is required and records why. A required backend always gets a real implementation; deep/advanced jobs require successful live HTTP validation, while normal web jobs keep unavailable integrations as explicit TODOs so they do not block a runnable frontend preview. SQLite under `APP_DATA_DIR` is a default for isolated durable application data; it is not the platform database. User-specified stores and frameworks take precedence.
5. Start frontend and backend dependencies through a common runtime lifecycle. Allocate distinct loopback ports, start dependencies first, wait for readiness, inject port variables into the frontend and stop the whole group on failure or shutdown. Vite proxies the real backend, and browser clients use the preview base path.
6. Execute the current tier's checks. Every job performs structural inspection, a real build when configured, and actual service/page startup. Deep/advanced jobs additionally execute planned HTTP status/JSON checks and Chromium user actions/assertions; normal web jobs do not run an API matrix or test suite merely for acceptance. Save available browser console/network errors, DOM output and screenshots. Associate verification receipts with requirement IDs and a hash of current source.
7. Deep/advanced completion is blocked if tasks remain unfinished, requirements lack current-source evidence, a frontend consists only of entry/App files, required backend implementation or HTTP evidence is missing, or README/build/browser acceptance is absent. Normal web jobs may reach `PREVIEW_READY` after a real build and page startup/preview check, while leaving deferred backend/external-configuration TODOs visible. Review and repair concrete source/runtime failures; do not turn a missing dependency into a repeated validation loop.
8. Persist task state and a checkpoint of recent actions across context compaction. Trim complete assistant/tool groups rather than breaking tool-call/result pairs. The previous run's ledger remains in `.atoms/LAST_RUN.json` for continuation context. Retrying the same unfinished request or sending a plain `继续`/`continue` restores the original plan and completed task progress. New requirements or attachments trigger fresh planning. Restored receipts are historical: all final acceptance must run again against current source. A completed ledger cannot silently resume. Existing database job/step logs and source versions remain authoritative history.

## Model tools

Existing tools: `list_files`, `read_file`, `write_file`, `delete_file`, `replace_in_file`, `apply_patch`, `search_files`, `symbol_search`, `show_diff`, `list_documents`, `read_document`, `run_shell`, `run_build`.

New tools: `scaffold_project`, `get_tasks`, `update_task`, `runtime_check`, `http_request`, `browser_check`.

Normal web delivery does not run a full unit-test suite at the finish boundary. Focused checks remain available during implementation for a concrete code change; deep/advanced jobs may run the planned targeted integration checks at their acceptance boundary.

Execution tools return `verification_id=V...`. `update_task` requires successful receipts for `done`. Acceptance tools take `requirement_ids`, for example `["R1"]`. Receipts become obsolete if source changes; the agent must recheck acceptance against current source. The ledger does not accept a plain model claim as execution evidence. Independent code review still matters: assigning a receipt to a requirement is not, by itself, proof that the assertion meaningfully tests it.

## Example fullstack runtime configuration

```json
{
  "dev": "npm --prefix frontend run dev -- --host 127.0.0.1 --port $PORT --strictPort --base $BASE_PATH",
  "build": "npm install --ignore-scripts --no-audit --no-fund && npm run build",
  "test": ["npm run test", "python -m unittest discover -s backend/tests"],
  "services": [
    {
      "name": "api",
      "command": "python -m uvicorn backend.app.main:app --host 127.0.0.1 --port $PORT",
      "port_env": "API_PORT",
      "ready_path": "/api/health"
    }
  ]
}
```

For Vite, match `${BASE_PATH}api` and rewrite that prefix to `/api` before forwarding. A plain `/api` proxy also matches the platform's `/api/runtime/...` frontend page route and can make readiness probes return backend 404s. Readiness errors report the actual HTTP status/body together with service logs.

The API port is dynamic. Never hardcode it in frontend code. The runtime provides `API_PORT` to Vite; a base-aware proxy forwards `${BASE_PATH}api/...` to `/api/...`. `APP_DATA_DIR` points to `.atoms-data`, excluded from source snapshots/downloads and preserved across service restarts. Each project has a private `.venv` with system/user site-packages disabled. Shell, build and preview share its interpreter, `.python-packages` target and `.python-cache` (including executable temporary build space). Both `pip` and `python -m pip install` default to this project-local target. Declare Python dependencies in `backend/requirements.txt` or root `requirements.txt`; build and runtime startup prepare them automatically. Legacy root-owned dependency trees are repaired before dropping privileges. Manifest changes install into a fresh staging directory and replace the previous installation only after success. Environment/cache directories are excluded from snapshots, downloads, clones and GitHub export. Never relocate Python dependencies to `/tmp`: worker tmpfs is intentionally noexec. Frontend production output stays at root `dist`, compatible with the existing resource upload path.

The preview gateway provides CORS for opaque sandbox origins and forwards project Bearer authorization through trusted internal preview routes. Normal main-site API authorization and cookies remain stripped from project requests. Preview login should use project tokens rather than platform cookies. This preserves real authenticated API interactions without mixing platform and project sessions.

## 验证结果（2026-10-02）

- 24 项回归及真实 CLI/全栈集成测试通过。集成测试使用真实项目 Unix UID 与 0700 私有目录，覆盖官方 Vite 初始化、模块化 React + FastAPI/SQLite、构建、浏览器新增/刷新，以及整个服务组重启后的数据保留。
- 真实 `~deepseek/deepseek-flash-latest` 模型完成留言板：7 个任务、6 项需求；前端包含 pages/components/hooks/api/types，App.tsx 为 180 字符的组合入口；后端包含配置、数据库、schema、业务服务、路由及应用装配。
- 生成项目通过 15 项 pytest、15 项 Vitest，以及真实 API 状态/数据断言、浏览器新增/校验/刷新、服务重启后的 SQLite 持久化断言。对完整前后端源码的额外独立复核批准交付，没有发现缺失实现。
- 此案例首次在 1500000 token 的评测预算下中止，随后从检查点保留已完成进度并重新验收成功。初次用量 1523052、续跑用量 1283941，合计 2806993 个模型报告的 token（包含重复输入上下文）。默认上限现为 15000000，环境变量可覆盖。这是已验证的一个完整案例，不代表所有复杂任务都能一次成功。
- 另外验证了架构规划：共享团队日历的账号/角色/跨设备数据要求选择真实后端；明确要求离线本地的 RGB/HEX 工具选择无后端，并说明原因。

## Verification and practical limits

`python -m unittest discover -s tests -v` runs requirement/evidence/compaction and service lifecycle regressions inside the backend image. `RUN_CLI_SMOKE=1` adds a real Vite CLI, modular React frontend, FastAPI/SQLite backend, production build, proxied browser create/reload assertions and persisted data after the entire service group restarts.

Defaults are 120 model iterations, 15M reported tokens and 3600 seconds per implementation run. Existing environment overrides remain effective. Transient model/network failures retry with bounded backoff. This remains one coding executor with separate planning/review model phases, not a claim of equivalence to Claude Code or Cursor. It needs continued evaluation on diverse tasks and model-specific tuning. Budgets still stop a job; checkpoints support a subsequent continuation, not unlimited unattended execution.

Model responses are streamed and fragmented tool-call arguments are assembled before execution. Contiguous reasoning text/summary deltas are reassembled; signed metadata and opaque encrypted blocks retain their order. The harness passes structured reasoning once rather than duplicating it as both plaintext and details, and preserves empty reasoning detail arrays for providers that require them. Incomplete streams never execute partial tool calls. Shell output is bounded during collection, and Bash `pipefail` preserves failed command status when a model pipes output through `tail`. Rayon/Tokio/libuv threads are bounded for native Vite/Rolldown builds; project process limits remain below the worker's container PID limit. Build and test caches are excluded from source acceptance hashes.

`RUN_LIVE_AGENT_EVAL=1 PYTHONPATH=/app python tests/live_agent_eval.py` runs an opt-in, billed live-model message-board evaluation against an isolated persistent evaluation directory. It reports phases, tasks, files, tool counts and usage without changing a user's platform project. Set `EVAL_RESUME_WORKSPACE` to resume an unfinished isolated evaluation with fresh acceptance checks. Set `EVAL_OUTPUT` and `EVAL_MODEL` to compare harness versions; it is deliberately excluded from routine unittest discovery.

Chromium receives a private browser-owned temporary directory outside the UID-owned project tree, because its capability-dropping subprocesses cannot traverse project directories with mode 0700. Project permissions are preserved. Screenshot failures retain the original browser/action error.

Browser checks exercise DOM interactions and report actual console/network failures; saved screenshots are available for inspection but do not constitute automatic visual quality scoring. Preview sandbox behavior, authentication-dependent cookies and external integrations require separate application-specific validation. Publication uploads immutable frontend resources to MinIO and routes published `api/` requests to the project's managed runtime, starting it when cold. This does not deploy a separate immutable production backend: backend code uses the current workspace. See `docs/public-delivery.md` for storage isolation and delivery limitations.

References: [OpenRouter reasoning round-trip protocol](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [Anthropic's long-running-agent harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), [Cursor harness engineering](https://cursor.com/blog/continually-improving-agent-harness), [Vite CLI initialization](https://vite.dev/guide/), [Playwright Python browsers](https://playwright.dev/python/docs/browsers).

## Durable sessions

Full active model context, incremental read caches and complete conversation audit events now survive restarts. Automatic semantic compaction bounds the active window independently of cumulative budgets. See [Agent session implementation](AGENT_SESSIONS.md) for recovery, invalidation, provider caching and billing behavior.
