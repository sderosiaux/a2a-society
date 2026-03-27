# Hive Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a distributed network of Claude Code agents that collaborate like colleagues in a company, using the A2A protocol for inter-agent communication.

**Architecture:** Registry-based discovery + direct A2A communication between agent nodes. Each node is both A2A server and client. Org memory is a shared git repo. Budget circuit breaker per agent.

**Design doc:** `docs/plans/2026-03-27-hive-design.md`

**Recommended stack (to confirm before starting):**
- Python 3.12+ (both official SDKs are Python-first)
- `a2a-sdk` — official A2A Python SDK (v0.3)
- `claude-agent-sdk` — Anthropic's Claude Code SDK
- `click` or `typer` — CLI (`hive join`, `hive leave`)
- `pydantic` — config and data models
- `gitpython` — org-memory git operations
- `pytest` — testing

---

## Milestone 1: Minimal Agent Node

A single agent that receives an A2A task, runs Claude Code on it, and returns a result. The "hello world" of Hive.

### Task 1.1: Project scaffolding

**Files:**
- Create: `hive/`
- Create: `hive/__init__.py`
- Create: `hive/models.py`
- Create: `hive/config.py`
- Create: `pyproject.toml`
- Create: `tests/__init__.py`

**Step 1: Init the project**

```
hive/
+-- __init__.py
+-- models.py          # Pydantic models: AgentConfig, HiveAgentCard, TaskMetadata
+-- config.py          # Load agent-config.yaml, build HiveAgentCard
pyproject.toml         # dependencies: a2a-sdk, pydantic, click, gitpython, pytest
tests/
+-- __init__.py
```

**Step 2: Write AgentConfig and HiveAgentCard models in `models.py`**

Pydantic models matching the design doc's extended Agent Card schema:
- `AgentConfig`: what the YAML config file contains (role, skills, tools, objectives, budget, reports_to, knowledge path, etc.)
- `HiveAgentCard`: the A2A Agent Card + hive extensions (status, budget remaining, objectives, reporting frequency)
- `TaskMetadata`: the `metadata` block in A2A messages (from, priority, callback_url, artifact_ref)
- `BudgetState`: daily_max, weekly_max, per_task_max, spent_today, spent_week, status enum (active/warning/vacation)

**Step 3: Write config loader in `config.py`**

Read `agent-config.yaml`, validate with Pydantic, return `AgentConfig`. Example config:

```yaml
name: "seo-agent"
role: "SEO Specialist"
description: "SEO specialist with semrush access"
reports_to: "vp-marketing"
skills:
  - id: "seo-audit"
    name: "SEO site audit"
  - id: "keyword-research"
    name: "Keyword research"
tools: ["semrush"]
tools_exclusive: ["semrush"]
objectives:
  - "Increase organic traffic by 20% in Q2"
reporting:
  to: "vp-marketing"
  frequency: "weekly"
budget:
  daily_max_usd: 5.00
  weekly_max_usd: 25.00
  per_task_max_usd: 2.00
knowledge_dir: "./knowledge/"
initiative_interval_minutes: 30
peers: []
registry_url: null
org_memory_url: null
```

**Step 4: Write tests for config loading**

```
tests/test_config.py
```

Test: load a valid YAML, get back an `AgentConfig` with correct fields. Test: missing required field raises validation error. Test: budget defaults are applied.

**Step 5: Run tests, verify they pass**

Run: `pytest tests/test_config.py -q --tb=short`

**Step 6: Commit**

```
git commit -m "feat(hive): project scaffolding + config models"
```

---

### Task 1.2: A2A Server — receive tasks

**Files:**
- Create: `hive/server.py`
- Create: `hive/executor.py`
- Create: `tests/test_server.py`

**Step 1: Write the A2A server in `server.py`**

Using `a2a-sdk` server components:
- Build an A2A `AgentCard` from `HiveAgentCard` (map hive extensions into the standard card + metadata)
- Serve `/.well-known/agent-card.json`
- Handle `tasks/send` via `DefaultRequestHandler`
- Add a `/status` endpoint (returns agent name, status, budget remaining, queue depth)
- HTTP server on configurable host:port (default `0.0.0.0:8462`)

**Step 2: Write the executor stub in `executor.py`**

Implement the `AgentExecutor` interface from `a2a-sdk`. For now, a simple echo executor that returns the received message prefixed with `[{agent_name}] received: `. Claude integration comes in Task 1.3.

**Step 3: Write integration test**

```
tests/test_server.py
```

Test: start server in background, fetch `/.well-known/agent-card.json`, verify it contains the agent name and skills. Test: send a `tasks/send` JSON-RPC request, verify 200 response with task status. Test: hit `/status`, verify JSON with agent name and status.

**Step 4: Run tests**

Run: `pytest tests/test_server.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): A2A server with echo executor"
```

---

### Task 1.3: Claude Code executor

**Files:**
- Create: `hive/claude.py`
- Modify: `hive/executor.py`
- Create: `hive/prompt_builder.py`
- Create: `tests/test_claude.py`

**Step 1: Write the system prompt builder in `prompt_builder.py`**

Given an `AgentConfig`, build the system prompt string:
- Role and reporting line
- Direct reports (populated later when peers are known)
- Objectives
- Decision rules (evaluate, delegate, escalate, synthesize)
- Tool restrictions (what the agent has, what it must delegate)
- Org conventions (loaded from knowledge dir if present)

**Step 2: Write the Claude Code wrapper in `claude.py`**

Wrap the Claude Code SDK `query()` function:
- Input: user message (string), session_id (optional, for resume), system_prompt, allowed_tools, max_budget_usd
- Output: response text, cost_usd, session_id
- Handle the SDK's async generator pattern
- Extract cost from the result message

**Step 3: Wire the executor**

Replace the echo executor with the real Claude call:
1. Receive A2A task message
2. Extract text from message parts
3. Extract metadata (from, priority, callback_url)
4. Call Claude via `claude.py` with built system prompt and budget guard
5. Return response as A2A task result

**Step 4: Write tests**

```
tests/test_claude.py
```

Test prompt builder: given a config, verify the system prompt contains the role, reports_to, objectives, and tool restrictions. Test Claude wrapper: mock the SDK `query()`, verify the wrapper extracts text and cost correctly. (Real Claude calls tested manually, not in CI.)

**Step 5: Run tests**

Run: `pytest tests/test_claude.py -q --tb=short`

**Step 6: Manual smoke test**

Start the server with a test config. Send an A2A task via curl:

```bash
curl -X POST http://localhost:8462/a2a \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"message":{"role":"user","parts":[{"text":"What is your role?"}]}}}'
```

Verify Claude responds with something reflecting its role from the system prompt.

**Step 7: Commit**

```
git commit -m "feat(hive): Claude Code executor with role-based system prompt"
```

---

## Milestone 2: Registry

A lightweight HTTP service where agents register and discover each other.

### Task 2.1: Registry server

**Files:**
- Create: `hive/registry/server.py`
- Create: `hive/registry/store.py`
- Create: `tests/test_registry.py`

**Step 1: Write the in-memory store in `store.py`**

- `register(agent_card: HiveAgentCard)` — upsert, set `last_seen` timestamp
- `get_all()` — return all agent cards where status != offline
- `get_by_name(name)` — single agent
- `get_by_skill(skill_id)` — agents with matching skill
- `get_by_role(role)` — agents with matching role
- `check_heartbeats()` — mark agents as `offline` if `last_seen` > 3 * heartbeat_interval

**Step 2: Write the HTTP server in `server.py`**

Endpoints matching the design doc:
```
GET  /agents
POST /agents/register
GET  /agents/:name
GET  /agents/by-skill/:id
GET  /agents/by-role/:role
```

A separate process/binary: `hive registry --port 8080`

**Step 3: Write tests**

Test: register an agent, GET /agents returns it. Test: register two agents with different skills, query by-skill returns only the matching one. Test: register, wait past TTL, check_heartbeats marks it offline. Test: re-register refreshes the TTL.

**Step 4: Run tests**

Run: `pytest tests/test_registry.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): registry server with discovery endpoints"
```

---

### Task 2.2: Agent registration + heartbeat

**Files:**
- Create: `hive/discovery.py`
- Modify: `hive/server.py`
- Create: `tests/test_discovery.py`

**Step 1: Write the discovery client in `discovery.py`**

- `register(registry_url, agent_card)` — POST to registry
- `discover_all(registry_url)` — GET /agents, cache locally
- `discover_by_skill(registry_url, skill_id)` — GET /agents/by-skill/:id
- `discover_by_role(registry_url, role)` — GET /agents/by-role/:role
- `start_heartbeat(registry_url, agent_card, interval=60)` — background async task that POSTs register every N seconds
- Local cache: dict of agent cards, refreshed on each discover call, used as fallback if registry is down

**Step 2: Wire into agent startup**

In `server.py`, on startup:
1. Build agent card from config
2. If `registry_url` is set: register + start heartbeat + discover all peers
3. If `peers` list is set: fetch each peer's `/.well-known/agent-card.json` directly
4. Cache known peers in memory

**Step 3: Write tests**

Test: mock registry, register succeeds, verify POST body contains the full extended agent card. Test: discover_by_skill returns filtered results. Test: heartbeat fires every N seconds (use short interval in test). Test: fallback to cached peers when registry is down.

**Step 4: Run tests**

Run: `pytest tests/test_discovery.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): agent registration, heartbeat, peer discovery"
```

---

## Milestone 3: A2A Client — Outbound Communication

An agent can now send tasks to other agents and receive callbacks. This is the core "agents talking to each other" feature.

### Task 3.1: A2A client — send tasks

**Files:**
- Create: `hive/client.py`
- Create: `tests/test_client.py`

**Step 1: Write the A2A client in `client.py`**

- `send_task(peer_url, message_text, metadata: TaskMetadata) -> task_id` — POST tasks/send to a peer, return the task ID
- `metadata` includes: `from` (this agent's name), `priority`, `callback_url` (this agent's A2A URL)
- Handle responses: `submitted` (202), `rejected` (with reason), error

**Step 2: Write tests**

Test: mock a peer A2A server, send_task posts correct JSON-RPC, parses task_id from response. Test: rejected response is handled (at_capacity, on_vacation).

**Step 3: Run tests**

Run: `pytest tests/test_client.py -q --tb=short`

**Step 4: Commit**

```
git commit -m "feat(hive): A2A client for outbound task sending"
```

---

### Task 3.2: Callback handler — receive push notifications

**Files:**
- Create: `hive/subtask_tracker.py`
- Modify: `hive/server.py`
- Modify: `hive/executor.py`
- Create: `tests/test_callbacks.py`

**Step 1: Write the subtask tracker in `subtask_tracker.py`**

- `SubtaskTracker`: tracks parent_task_id -> list of {subtask_id, peer, status, result}
- `register_subtask(parent_task_id, subtask_id, peer_name)`
- `complete_subtask(subtask_id, result)` -> returns parent_task_id if all subtasks done
- `is_parent_ready(parent_task_id)` -> bool
- `get_subtask_results(parent_task_id)` -> list of results

**Step 2: Wire callback handling in server.py**

When the A2A server receives a push notification (task status update from a peer):
1. Extract task_id and status from the notification
2. Call `subtask_tracker.complete_subtask(task_id, result)`
3. If parent task is ready: re-queue it with collected results

**Step 3: Wire fan-out in executor.py**

When Claude's response indicates delegation (detected by structured output or tool call):
1. Parse the delegation intent (peer skill, message, priority)
2. Look up peer via discovery client
3. Send task via A2A client
4. Register subtask in tracker
5. Return "working" status to original requester (task stays open)

**Step 4: Write tests**

Test: register 2 subtasks, complete one, parent not ready. Complete second, parent ready. Test: callback notification triggers re-queue. Test: rejected subtask triggers retry with alternative peer.

**Step 5: Run tests**

Run: `pytest tests/test_callbacks.py -q --tb=short`

**Step 6: Commit**

```
git commit -m "feat(hive): callback handler + subtask tracking for fan-out"
```

---

### Task 3.3: Integration test — two agents talking

**Files:**
- Create: `tests/integration/test_two_agents.py`
- Create: `tests/integration/configs/agent-a.yaml`
- Create: `tests/integration/configs/agent-b.yaml`

**Step 1: Write end-to-end test**

1. Start registry on port 8080
2. Start agent-a ("VP Marketing", skills: ["marketing-strategy"], port 8462)
3. Start agent-b ("SEO Specialist", skills: ["seo-audit"], port 8463)
4. Both register with registry
5. Send a task to agent-a: "I need an SEO audit for conduktor.io"
6. Agent-a's Claude (mocked) decides to delegate to the SEO specialist
7. Agent-a sends A2A task to agent-b
8. Agent-b processes and responds via callback
9. Agent-a synthesizes and responds to original requester
10. Verify the full round-trip

Use mocked Claude responses to keep the test deterministic and free.

**Step 2: Run integration test**

Run: `pytest tests/integration/test_two_agents.py -q --tb=short`

**Step 3: Commit**

```
git commit -m "test(hive): integration test — two agents exchanging work via A2A"
```

---

## Milestone 4: Task Queue

### Task 4.1: Priority inbox

**Files:**
- Create: `hive/queue.py`
- Modify: `hive/executor.py`
- Create: `tests/test_queue.py`

**Step 1: Write the task queue in `queue.py`**

- `TaskQueue`: priority-sorted inbox
- Priority order: escalation (1) > from_superior (2) > consultation (3) > broadcast (4)
- Priority is derived from: task metadata `priority` field + relationship to sender (is sender my superior? → boost)
- `enqueue(task, sender_name)` — insert sorted
- `dequeue() -> task` — pop highest priority
- `reject_if_full(task)` — if len >= max_backlog, reject with "at_capacity"
- `size()`, `is_empty()`

**Step 2: Wire into executor**

Replace direct execution with queue-based flow:
1. A2A server receives task -> enqueue
2. Background worker dequeues one at a time -> run Claude -> respond
3. If queue full -> immediate reject

**Step 3: Write tests**

Test: enqueue 3 tasks with different priorities, dequeue returns highest first. Test: task from superior ranks above peer consultation. Test: backlog full -> reject. Test: concurrent enqueue/dequeue safety.

**Step 4: Run tests**

Run: `pytest tests/test_queue.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): priority task queue with capacity rules"
```

---

## Milestone 5: Budget Manager

### Task 5.1: Cost tracking + circuit breaker

**Files:**
- Create: `hive/budget.py`
- Modify: `hive/claude.py`
- Modify: `hive/executor.py`
- Create: `tests/test_budget.py`

**Step 1: Write the budget manager in `budget.py`**

- `BudgetManager(config: BudgetState)`
- `check_before_execution() -> (allowed: bool, max_budget_for_task: float)` — returns min(per_task_max, remaining)
- `record_cost(cost_usd: float)` — increment spent_today, spent_week
- `get_status() -> BudgetStatus` — active/warning/vacation based on thresholds (80%, 100%)
- `reset_daily()` — called by scheduler at midnight or configurable time
- `reset_weekly()` — called on configured day
- `to_log_entry() -> dict` — for budget-logs JSONL

**Step 2: Wire into Claude executor**

Before each Claude call:
1. `check_before_execution()` — if not allowed, enter vacation, reject task
2. Pass `max_budget_for_task` to Claude SDK's `--max-budget-usd`

After each Claude call:
1. `record_cost(actual_cost)`
2. If status changed to vacation: notify reports_to via A2A client

**Step 3: Wire into heartbeat**

Budget remaining and status are included in each heartbeat POST to registry.

**Step 4: Write tests**

Test: fresh budget, check returns allowed. Test: spend to 80%, status is warning. Test: spend to 100%, status is vacation, check returns not allowed. Test: daily reset brings status back to active. Test: per_task_max caps the max_budget_for_task. Test: record_cost updates spent_today and spent_week correctly.

**Step 5: Run tests**

Run: `pytest tests/test_budget.py -q --tb=short`

**Step 6: Commit**

```
git commit -m "feat(hive): budget manager with circuit breaker + vacation mode"
```

---

## Milestone 6: Org Memory

### Task 6.1: Git sync layer

**Files:**
- Create: `hive/org_memory.py`
- Create: `tests/test_org_memory.py`

**Step 1: Write git operations in `org_memory.py`**

- `OrgMemory(repo_url, local_path, agent_name)`
- `clone_or_pull()` — clone if not exists, pull if exists
- `commit_and_push(file_path, message)` — add, commit, push. Retry on conflict (pull --rebase, push again, max 3 retries)
- `read_file(path) -> str`
- `write_artifact(domain, filename, content) -> artifact_ref dict` — write to `artifacts/{domain}/{filename}`, commit, push, return {repo, path, commit, size_lines}
- `append_event(agent, event_type, data)` — write to `events/YYYY-MM-DD/HH-MM-SS-{agent}-{event}.yaml`
- `append_budget_log(agent, entry)` — append to `budget-logs/{agent}/YYYY-MM-DD.jsonl`
- `acquire_lock(file_path) -> bool` — write `.lock/{path}.lock`, push. Return false if lock exists and not expired.
- `release_lock(file_path)`

**Step 2: Write tests**

Use a local bare git repo (tmpdir) as the remote. Test: clone, write artifact, commit, push, verify file exists in remote. Test: two agents write to different paths, no conflict. Test: append_event creates the correct YAML file. Test: lock acquire/release cycle.

**Step 3: Run tests**

Run: `pytest tests/test_org_memory.py -q --tb=short`

**Step 4: Commit**

```
git commit -m "feat(hive): org-memory git sync layer"
```

---

### Task 6.2: Artifact exchange in executor

**Files:**
- Modify: `hive/executor.py`
- Modify: `hive/client.py`
- Create: `tests/test_artifacts.py`

**Step 1: Wire artifact exchange into task completion**

When an agent completes a task and the result is > 50 lines:
1. Commit result to org-memory via `write_artifact()`
2. Include `artifact_ref` in the A2A response metadata instead of inline text
3. Include a short summary inline ("SEO Q1 report committed, 340 lines")

When an agent receives a task with `artifact_ref` in metadata:
1. `org_memory.clone_or_pull()`
2. Read the referenced file
3. Inject content into Claude's context

**Step 2: Write tests**

Test: long response triggers artifact commit, A2A response contains artifact_ref. Test: received artifact_ref is resolved to file content.

**Step 3: Run tests**

Run: `pytest tests/test_artifacts.py -q --tb=short`

**Step 4: Commit**

```
git commit -m "feat(hive): artifact exchange via org-memory git repo"
```

---

## Milestone 7: Initiative Loop + Proactive Reporting

### Task 7.1: Periodic initiative loop

**Files:**
- Create: `hive/initiative.py`
- Modify: `hive/server.py`
- Create: `tests/test_initiative.py`

**Step 1: Write the initiative loop in `initiative.py`**

- `InitiativeLoop(config, claude, org_memory, client, budget, queue)`
- Runs every `initiative_interval_minutes` (default: 30)
- On each tick:
  1. Skip if budget status is warning or vacation
  2. `org_memory.clone_or_pull()`
  3. Read recent events relevant to this agent's domain
  4. Build a special prompt for Claude:
     "Here are your objectives: [...]. Here is recent org activity: [...]. Evaluate your progress. Decide: (a) nothing to do, (b) self-assign a task, (c) delegate to a report, (d) send a status report to your superior. Respond with a structured JSON decision."
  5. Parse Claude's decision
  6. Execute: enqueue self-task, send A2A task, or trigger report

**Step 2: Wire into agent startup**

Start the initiative loop as a background async task after registration completes.

**Step 3: Write tests**

Test: mock Claude returns "nothing to do" -> no action taken. Test: mock Claude returns "self-assign" -> task enqueued. Test: mock Claude returns "delegate" -> A2A task sent to peer. Test: budget in warning -> loop skipped.

**Step 4: Run tests**

Run: `pytest tests/test_initiative.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): initiative loop — agents evaluate objectives and act proactively"
```

---

### Task 7.2: Proactive reporting

**Files:**
- Create: `hive/reporting.py`
- Modify: `hive/initiative.py`
- Create: `tests/test_reporting.py`

**Step 1: Write reporting logic in `reporting.py`**

- `ReportGenerator(config, org_memory, claude)`
- `should_report() -> bool` — check if reporting interval has elapsed since last report
- `generate_report() -> (summary: str, artifact_ref: dict)`:
  1. Read own recent events, artifacts, and budget logs from org-memory
  2. Ask Claude to synthesize a status report
  3. Commit report to `artifacts/{domain}/reports/YYYY-MM-DD-status.md`
  4. Return summary + artifact_ref
- `last_report_timestamp` persisted in a local file (survives restarts)

**Step 2: Wire into initiative loop**

After objective evaluation, check `should_report()`. If yes, generate and send to `reports_to` via A2A.

**Step 3: Write tests**

Test: reporting interval not elapsed -> should_report returns false. Test: interval elapsed -> generates report, commits to org-memory, returns artifact_ref. Test: last_report_timestamp is persisted and read on restart.

**Step 4: Run tests**

Run: `pytest tests/test_reporting.py -q --tb=short`

**Step 5: Commit**

```
git commit -m "feat(hive): proactive reporting to superior on schedule"
```

---

## Milestone 8: hive CLI

### Task 8.1: `hive join` command

**Files:**
- Create: `hive/cli.py`
- Create: `tests/test_cli.py`

**Step 1: Write the CLI in `cli.py`**

Using click or typer:

```
hive join \
    --role "Content Writer" \
    --reports-to "vp-marketing" \
    --skills "copywriting,blog-writing" \
    --tools "WordPress,Grammarly" \
    --objectives "Publish 4 posts/week,Grow newsletter" \
    --knowledge ./knowledge/ \
    --report-frequency weekly \
    --budget-daily 4.00 \
    --initiative-interval 30 \
    --registry https://registry.local:8080 \
    --org-memory git@github.com:company/org-memory.git \
    --port 8462
```

What it does:
1. Generate `agent-config.yaml` from flags
2. Clone org-memory, read `org-chart.yaml` and `.org/conventions.md`
3. Build system prompt from config + conventions
4. Copy knowledge directory into agent's working dir
5. Start the full agent node (A2A server + registration + heartbeat + initiative loop)
6. Send introduction A2A message to `reports_to`

Also: `hive join --config agent-config.yaml` for pre-written configs.

**Step 2: Write `hive leave` command**

```
hive leave --graceful
```

1. Set status to `leaving`
2. Wait for current task to complete (timeout configurable)
3. Reject new tasks
4. Send goodbye message to `reports_to`
5. Deregister from registry
6. Push final event to org-memory
7. Shutdown

**Step 3: Write `hive registry` command**

```
hive registry --port 8080
```

Start the registry server as a standalone process.

**Step 4: Write `hive status` command**

```
hive status --registry https://registry.local:8080
```

Print a table of all agents (name, role, status, budget, queue depth). Quick CLI dashboard.

**Step 5: Write tests**

Test: `hive join` with all flags generates correct agent-config.yaml. Test: `hive status` with mocked registry prints formatted table.

**Step 6: Run tests**

Run: `pytest tests/test_cli.py -q --tb=short`

**Step 7: Commit**

```
git commit -m "feat(hive): CLI — join, leave, registry, status commands"
```

---

## Milestone 9: Dashboard

### Task 9.1: Live CLI dashboard

**Files:**
- Create: `hive/dashboard.py`
- Modify: `hive/cli.py`

**Step 1: Write the dashboard in `dashboard.py`**

```
hive dashboard --registry https://registry.local:8080 --org-memory ./org-memory
```

A `rich`-based (or plain ANSI) live terminal UI that refreshes every 5s:
- Top: agent table (name, role, status, budget used/max, queue depth)
- Bottom: recent activity feed from org-memory events
- Footer: total org spend today, tasks completed/active

Data sources:
- Agent status: GET /agents from registry
- Activity: read `events/` directory in org-memory (git pull on each refresh)
- Budget: aggregate from agent cards (budget remaining is in heartbeat data)

**Step 2: Add `hive dashboard` to CLI**

**Step 3: Manual test**

Start registry + 2 agents + dashboard. Verify dashboard shows both agents, updates when tasks are exchanged.

**Step 4: Commit**

```
git commit -m "feat(hive): live CLI dashboard"
```

---

## Milestone 10: End-to-End Scenario

### Task 10.1: Three-agent company simulation

**Files:**
- Create: `examples/startup/`
- Create: `examples/startup/ceo.yaml`
- Create: `examples/startup/vp-marketing.yaml`
- Create: `examples/startup/seo-agent.yaml`
- Create: `examples/startup/README.md`
- Create: `examples/startup/run.sh`

**Step 1: Write agent configs for a 3-person startup**

CEO: broad objectives, high budget, no specialized tools.
VP Marketing: marketing objectives, reports to CEO, delegates to SEO.
SEO Agent: SEO objectives, reports to VP Marketing, has semrush tool access.

**Step 2: Write the run script**

```bash
#!/bin/bash
# Start the org
hive registry --port 8080 &
sleep 2
hive join --config ceo.yaml --port 8462 &
hive join --config vp-marketing.yaml --port 8463 &
hive join --config seo-agent.yaml --port 8464 &
sleep 5
# Open dashboard
hive dashboard --registry http://localhost:8080 --org-memory ./org-memory
```

**Step 3: Write the README**

Document: what this example does, how to run it, what to expect, estimated cost.

**Step 4: Manual test**

Run the scenario. Send a task to the CEO: "Prepare Q1 board deck." Watch as:
1. CEO delegates to VP Marketing
2. VP Marketing decomposes, delegates SEO report to SEO Agent
3. SEO Agent produces report, pushes to org-memory
4. VP Marketing synthesizes, responds to CEO
5. Dashboard shows the full activity chain

**Step 5: Commit**

```
git commit -m "feat(hive): 3-agent startup example with end-to-end scenario"
```

---

## File tree summary

```
hive/
+-- __init__.py
+-- models.py            # M1: Pydantic models
+-- config.py            # M1: Config loader
+-- server.py            # M1: A2A server
+-- executor.py          # M1: Task executor (orchestrates queue + claude + artifacts)
+-- claude.py            # M1: Claude Code SDK wrapper
+-- prompt_builder.py    # M1: System prompt generator
+-- client.py            # M3: A2A outbound client
+-- subtask_tracker.py   # M3: Fan-out subtask tracking
+-- queue.py             # M4: Priority task queue
+-- budget.py            # M5: Budget manager + circuit breaker
+-- org_memory.py        # M6: Git sync layer
+-- initiative.py        # M7: Periodic initiative loop
+-- reporting.py         # M7: Proactive reporting
+-- cli.py               # M8: Click/Typer CLI
+-- dashboard.py         # M9: Live terminal dashboard
+-- registry/
|   +-- server.py        # M2: Registry HTTP server
|   +-- store.py         # M2: Agent card store
+-- discovery.py         # M2: Registry client + peer cache
tests/
+-- test_config.py
+-- test_server.py
+-- test_claude.py
+-- test_client.py
+-- test_callbacks.py
+-- test_queue.py
+-- test_budget.py
+-- test_org_memory.py
+-- test_initiative.py
+-- test_reporting.py
+-- test_cli.py
+-- test_registry.py
+-- test_discovery.py
+-- test_artifacts.py
+-- integration/
    +-- test_two_agents.py
    +-- configs/
examples/
+-- startup/
    +-- ceo.yaml
    +-- vp-marketing.yaml
    +-- seo-agent.yaml
    +-- README.md
    +-- run.sh
```

## Dependency graph

```
M1 (Minimal Agent) -----> M2 (Registry) -----> M3 (A2A Client)
                                                     |
                                M4 (Queue) <---------+
                                     |
                                M5 (Budget) -------> M6 (Org Memory)
                                                         |
                                                    M7 (Initiative + Reporting)
                                                         |
                                                    M8 (CLI)
                                                         |
                                                    M9 (Dashboard)
                                                         |
                                                    M10 (E2E Scenario)
```

Milestones are sequential. Within each milestone, tasks are sequential. Each task ends with a commit and passing tests.
