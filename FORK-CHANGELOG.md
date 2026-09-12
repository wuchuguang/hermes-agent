# FORK-CHANGELOG — 自研定制需求变更日志

> 用途：记录本 fork 相对上游（NousResearch/hermes-agent）的全部自研功能。
> 每次大版本 merge 冲突过多时，以本文件为蓝图在新 release 上重新二开。
> **约定：每合并一个新自研功能，必须同步更新此文件。**

## 快照

- 基准：上游 `upstream/main`
- 领先：7 commits（4 个功能 + 3 个 merge 节点）
- 变更量：13 文件，+1518/-26
- 本地开发树：`~/.hermes/scratch/hermes-dev`（共享 clone，live checkout 受保护禁 merge）

## 功能清单

### 1. Per-project memory store（项目记忆库）
- **Commit**: 34d773866c
- **需求**：记忆分 user/memory/project 三层；project 层按 session 所在 git root 定位存储，不同仓库记忆互不污染
- **核心文件**：
  - `tools/memory_tool.py` — `_project_root_for_session()`（git root 解析 + package.json/pyproject.toml 兜底）、`MemoryStore` project target、`_path_for_project()`
- **配置**：`memory.target=project` 路由
- **测试**：`tests/tools/test_memory_project_target.py`

### 2. Linked project memory（依赖项目记忆只读注入）
- **Commit**: 5a5f8c4a00
- **需求**：session 在 repo A 时，若 A 的 package.json/pyproject 声明的依赖在本机存在对应 repo 且有自己的项目记忆，则将依赖的项目记忆**只读**注入系统提示（不占可写额度）
- **核心文件**：
  - `tools/memory_tool.py` — `_project_dependency_names()`、`_resolve_linked_projects()`、`_linked_entries`、`format_for_system_prompt("linked_projects")`
  - `hermes_cli/config_defaults.py` — `memory.project_code_roots` 配置（本机设 `~/git-repo, ~/node-projects, ~/wcg-repo`）
- **限制**：MAX_LINKED_PROJECTS=5；manifest 目录无 .git 也算 project root（yhyun-pkg-server 场景）
- **测试**：`tests/tools/test_memory_linked_projects.py`

### 3. Project-aware multi-lib workspace（多 lib 项目工作区）★
- **Commit**: 4812b6fbf4
- **需求**：`hermes project`（projects.db 多目录工作区）的成员目录间开发时：
  1. 记忆统一锚定到项目 primary 目录（任一成员目录的 session 共享一个 project store）
  2. 系统提示启动注入 workspace map（全部成员目录绝对路径 + primary 标记），agent 直接知道兄弟 lib 路径
- **核心文件**：
  - `tools/memory_tool.py` — `_primary_root_for_session()`（projects.db 只读查询，primary 优先于 git root，fail-open 回退）
  - `agent/prompt_builder.py` — `_project_workspace_map()`（`build_context_files_prompt` 内一次性注入，prompt-cache 安全）
- **配套 CLI**（上游已有）：`hermes project create/add-folder/set-primary/use`；`project_for_path` 最长前缀匹配
- **测试**：`tests/tools/test_project_workspace_memory.py`

### 4. Text runtime router（本地 Ollama 文本路由，半成品）
- **Commit**: 5a6c34f2f2
- **需求**：文本请求路由到本地 Ollama
- **核心文件**：`agent/text_runtime_router.py` + `tests/agent/test_text_runtime_router.py`
- **状态**：**未接入任何调用点**，merge 时可弃可留

## 重新二开操作要点

1. projects.db schema（`hermes_cli/projects_db.py`）是上游代码，无需重写；重写的只是两个消费点（memory 锚点 + prompt 注入）
2. `_project_root_for_session` 的调用点在 `MemoryStore.__init__`（`self._project_root = _project_root_for_session()`）
3. workspace map 注入点在 `build_context_files_prompt` 的 `project_context` 之后、SOUL.md 之前
4. 测试基建：`monkeypatch agent.runtime_cwd.resolve_agent_cwd` + `hermes_cli.projects_db.create_project` 造多目录 fixture
5. 上游 merge 前先 `git fetch upstream && git merge-base`，冲突热点历来是 `tools/memory_tool.py`（上游也在频繁改）

## 验证命令

```bash
cd ~/.hermes/scratch/hermes-dev
uv sync --frozen --extra dev
uv run --no-sync python -m pytest tests/tools/test_memory_project_target.py tests/tools/test_memory_linked_projects.py tests/tools/test_project_workspace_memory.py -o 'addopts=' -q
uv run --no-sync ruff check tools/memory_tool.py agent/prompt_builder.py
# 生效：live checkout git pull --ff-only → 重启 gateway（launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway）→ 新 session
```
