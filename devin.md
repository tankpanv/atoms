下面我按照 **Devin 类 AI Software Engineer Agent 平台** 的思路，帮你设计一套完整方案。目标是：

> 用户通过 Atoms 页面提交开发任务 → 后端 Agent 自动分析、修改代码、运行测试、反馈结果 → 多项目隔离 → 可扩展到大量用户。

这不是普通 ChatGPT 套壳，而是一个 **AI Coding Agent Platform**。

---

# 一、整体目标

系统能力：

用户：

> “帮我给这个 React 项目增加登录功能”

系统自动：

1. 理解需求
2. 分析项目
3. 制定计划
4. 修改代码
5. 执行命令
6. 运行测试
7. 修复错误
8. 提交结果
9. 展示修改记录


类似：

- Devin
- Claude Code Agent
- OpenAI Codex Agent

---

# 二、总体架构

```
                     Atoms Web UI
                          |
                          |
                    API Gateway
                          |
        ---------------------------------
        |                               |
   Project Service              Task Service
        |                               |
        |                               |
 PostgreSQL                      Redis Queue
        |
        |
 Project Workspace


                          |
                          |
                 Agent Orchestrator
                          |
        -----------------------------------
        |                |                |
   Planner Agent    Coding Agent    Review Agent
        |                |                |
        -----------------------------------
                         |
                  Sandbox Manager
                         |
        --------------------------------
        |              |               |
     Docker        Docker          Docker
    Sandbox       Sandbox        Sandbox

                         |
                Persistent Storage
              (Git + S3 + Database)
```

---

# 三、核心概念设计

整个系统核心四个对象：

```
User

  |
  |
Project

  |
  |
Task

  |
  |
Sandbox
```

关系：

```
User
 |
 +---- Project A
 |          |
 |          +---- Task 001
 |                    |
 |                    Sandbox
 |
 +---- Project B
            |
            +---- Task 002
```

---

# 四、Project（项目管理）

## 数据模型

PostgreSQL：

```sql
projects

id
user_id

name

repository_url

workspace_path

tech_stack

created_at

updated_at
```

例如：

```json
{
"id":"p1001",

"name":"shopping-web",

"tech_stack":{
 "frontend":"React",
 "backend":"Node",
 "database":"Postgres"
}
}
```

---

## Project存储

不要存在Docker里面。

使用：

```
Project Storage

    |
    |
 Git Repository

    +

Object Storage

    +
 Database
```

例如：

```
/projects/p1001

src/
package.json
README.md
.git
```

---

# 五、Task系统（核心）

用户每一次请求：

创建Task。

例如：

> 增加微信支付


Task：

```sql
tasks

id

project_id

user_prompt

status

agent_version

created_at
```


状态机：

```
PENDING

↓

PLANNING

↓

CODING

↓

TESTING

↓

REVIEWING

↓

SUCCESS


失败：

FAILED
```

---

# 六、Agent Orchestrator（大脑）

不要让一个Agent干全部事情。


采用多Agent：

```
                 Task

                  |
                  |

             Orchestrator

                  |

 ---------------------------------

 Planner       Coder        Tester

```


---

# 1. Planner Agent

职责：

理解需求。

输入：

```
增加用户登录
```

输出：

计划：

```
1. 分析现有认证系统

2. 创建login API

3. 增加JWT

4. 添加前端页面

5. 测试
```


保存：

```
task_plan
```

---

# 2. Coding Agent

负责：

修改代码。


拥有：

Tools:

```
filesystem

terminal

git

search

editor
```


例如：

调用：

```
read_file()

write_file()

run_command()
```

---

# 3. Tester Agent

负责：

验证。


执行：

```
npm test

pytest

go test
```

发现错误：

反馈 Coding Agent。

形成：

```
修改

↓

测试

↓

错误

↓

修复
```

循环。

---

# 七、Sandbox设计（最重要）

## 不采用：

❌ 一个用户一个永久Docker


采用：

## Task级Sandbox


流程：

用户提交：

```
Task001
```

创建：

```
Sandbox001
```

里面：

```
/workspace

   project code

   node

   python

   tools
```


---

Docker：

```
docker run

agent-runtime:v1

-v project:/workspace

--memory=8g

--cpus=4

```

---

任务完成：

```
保存代码

保存日志

销毁Sandbox
```

---

# 八、Sandbox安全设计

## 资源限制

CPU：

```
4 core
```

Memory：

```
8GB
```

Timeout：

```
60分钟
```


---

## 网络控制

不要：

```
Agent
 |
 Internet
```

使用：

```
Agent

 |

Proxy

 |

Allow List

```

允许：

```
npm
github
pypi
```

禁止：

```
内网
数据库
```

---

# 九、Agent Tool系统

不要让Agent直接shell。


设计Tool层：

```
Agent

 |

Tool Gateway

 |

-------------------

File Tool

Terminal Tool

Git Tool

Browser Tool

Database Tool

```


---

## File Tool

```json
{
"action":"read",
"path":"src/app.ts"
}
```

---

## Terminal Tool

限制：

允许：

```
npm install

npm test
```

禁止：

```
rm -rf /
```

---

# 十、Memory设计


## 1. Task Memory

当前任务：

Redis：

```
task:p001:t001
```


保存：

- 对话
- 工具调用
- 中间结果


---

## 2. Project Memory

长期：

例如：

```
项目使用：

React18

Tailwind

Postgres

禁止修改:

auth模块
```


Vector DB：

```
project_memory
```


---

# 十一、实时通信

用户页面：

看到：

```
Agent正在分析...

读取package.json

修改auth.ts

运行测试...

发现错误...

修复中...
```


采用：

## SSE


后端：

```
text/event-stream
```


事件：

```json
{
"type":"tool_call",

"message":
"npm test running"
}
```


前端：

实时展示。


---

# 十二、代码修改安全机制

不要让Agent直接覆盖。

流程：

```
Agent修改

↓

Git Diff

↓

Test

↓

Review

↓

Commit
```


保存：

```
before

after

diff
```


用户可以：

```
Accept

Reject
```

---

# 十三、失败恢复

非常重要。


例如：

Agent改坏代码。


机制：

## Snapshot

任务开始：

```
git commit checkpoint
```


失败：

```
rollback
```

---

## Retry

例如：

LLM失败：

```
retry 3 times
```

---

# 十四、数据库设计

核心表：

## users

```
id
email
```


## projects

```
id
user_id
repo
```


## tasks

```
id
project_id
status
prompt
```


## agent_runs

```
id
task_id

agent_type

input

output

tokens

latency
```


## tool_calls

```
id

task_id

tool_name

arguments

result
```





---

```


---

## Agent Runtime
参考 metagpt 或opencoder 者 或者claude codecode
```


---

## Storage

代码：

```
Git

MinIO/S3 (单独脚本容器管理 并且)
```


---

## Sandbox

第一版：

```
Docker
```


```

---

# 十六、部署架构

小规模：

```
             Server

        ----------------

        API

        Worker

        PostgreSQL

        Redis

        Docker
```



---

# 十七、完整一次任务流程


用户：

```
创建商城后台
```


系统：

```
1.
Create Task


2.
Planner分析


3.
创建Sandbox


4.
Clone项目


5.
Coding Agent修改


6.
Terminal执行


7.
Tester测试


8.
Review Agent检查


9.
Git Commit


10.
SSE返回结果

11.
销毁Sandbox

```


---


设计的是一个类似 Devin 的 Agent Software Engineer 平台。核心采用 Project、Task、Sandbox 三层模型。Project负责长期代码和上下文管理，Task负责一次开发任务生命周期，Sandbox提供隔离执行环境。Agent采用Planner、Coder、Tester多Agent架构，通过Tool Gateway访问文件、终端、Git等能力，并通过Git checkpoint、测试门禁和可观测系统保证代码修改安全。


