# Hive — Distributed Agent Society over A2A

A peer network of Claude Code agents, each running on its own machine, that collaborate like colleagues in a company. They discover each other, delegate work, exchange artifacts, manage their own budgets, and self-organize around a shared organizational memory.

## Context and motivation

Two open-source projects already wrap Claude Code behind the A2A protocol:

- **jcwatson11/claude-a2a** — solid bridge (auth, budget, SQLite persistence, multi-persona), but strictly client-server. Agents can't discover or talk to each other. No outbound A2A calls.
- **ericabouaf/claude-a2a** — clean proof of concept (~600 LOC), but single-agent, no auth, no persistence, no roles.

Both are passive endpoints: they receive work, they never initiate it. Neither supports inter-agent communication, peer discovery, or autonomous behavior.

Hive fills that gap. Every node is both an A2A server and an A2A client. Agents find each other, delegate tasks, escalate decisions, and share work products through a git-backed organizational memory.

## Architecture: Registry Central + A2A Direct

```
                    +---------------+
                    |   Registry    |
                    |  (lightweight)|
                    |  agent cards  |
                    |  + heartbeat  |
                    +-------+-------+
                            | discovery only
           +----------------+----------------+
           v                v                v
     +----------+    +----------+    +----------+
     | CEO      |<-->|VP Market.|<-->| SEO      |
     | Agent    |    | Agent    |    | Agent    |
     +----------+    +----------+    +----------+
          ^                               ^
          +-------------------------------+
              direct A2A communication
```

The registry is a thin discovery index. All real communication goes agent-to-agent, direct HTTP, using the A2A protocol. The registry is not in the critical path — agents cache peer cards locally and can fall back to a static peer list.

**Migration path to full P2P (later):** replace the central registry with gossip-based propagation (SWIM/Serf-style). Each node maintains its own copy of the registry. The interface stays the same (`GET /agents`, `GET /agents/by-skill/...`), it just becomes local. Worth doing when the network exceeds ~20 nodes.

## Agent node anatomy

Every machine/VM runs one agent node. Same binary, different config.

```
+--------------------------------------------------+
|                  Agent Node                       |
|                                                   |
|  +--------------+  +---------------------------+  |
|  | Agent Card   |  | A2A Server (HTTP)         |  |
|  | (identity)   |  | - receive tasks           |  |
|  |              |  | - push notifications      |  |
|  | role         |  | - /status endpoint        |  |
|  | skills       |  +---------------------------+  |
|  | reports_to   |                                  |
|  | tools_excl.  |  +---------------------------+  |
|  | budget       |  | A2A Client                |  |
|  | status       |  | - discover peers          |  |
|  +--------------+  | - send tasks to others    |  |
|                     | - receive push notifs     |  |
|  +--------------+  +---------------------------+  |
|  | Claude Code  |                                  |
|  | (SDK or CLI) |  +---------------------------+  |
|  |              |  | Budget Manager            |  |
|  | system       |  | - track real spend        |  |
|  | prompt =     |  | - --max-budget-usd guard  |  |
|  | role card    |  | - vacation circuit breaker|  |
|  |              |  +---------------------------+  |
|  | tools =      |                                  |
|  | restricted   |  +---------------------------+  |
|  |              |  | Knowledge                 |  |
|  | knowledge/   |  | - domain docs, refs       |  |
|  | CLAUDE.md    |  | - CLAUDE.md per agent     |  |
|  +--------------+  | - MCP servers (semrush..) |  |
|                     +---------------------------+  |
|                                                    |
|                     +---------------------------+  |
|                     | Org Memory (git sync)     |  |
|                     | - pull / commit / push    |  |
|                     +---------------------------+  |
+--------------------------------------------------+
```

Each node is both server AND client. This is the fundamental difference with existing projects that are server-only.

## Extended Agent Card

Standard A2A Agent Cards describe capabilities. Hive extends them with organizational metadata:

```yaml
# Standard A2A fields
name: "seo-agent"
description: "SEO specialist with semrush access"
skills:
  - id: "seo-audit"
    name: "SEO site audit"
  - id: "keyword-research"
    name: "Keyword research and competitive analysis"
capabilities:
  streaming: true
  pushNotifications: true

# Hive extensions
hive:
  role: "SEO Specialist"
  reports_to: "vp-marketing"
  tools_exclusive:
    - semrush
  objectives:
    - "Increase conduktor.io organic traffic by 20% in Q2"
    - "Produce monthly competitive analysis reports"
    - "Reduce average keyword ranking position from 12 to 8"
  reporting:
    to: "vp-marketing"
    frequency: "weekly"          # how often to send a status report unprompted
  budget:
    daily_max_usd: 5.00
    weekly_max_usd: 25.00
    remaining_today_usd: 3.40   # updated on each heartbeat
  status: active                 # active | busy | warning | vacation | offline
```

Objectives drive proactive behavior. An agent with objectives doesn't just wait for tasks — it periodically evaluates progress and initiates work on its own.

## Registry and discovery

### Registry API

```
GET  /agents              -> all agent cards
POST /agents/register     -> register or refresh (heartbeat)
GET  /agents/:name        -> single agent card
GET  /agents/by-skill/:id -> who can do X?
GET  /agents/by-role/:r   -> who is X?
```

Backed by a YAML file in org-memory (`registry/agents.yaml`) as source of truth. The HTTP service is a cache/index on top.

### Node lifecycle

1. Agent starts, reads local config (role, skills, tools, budget)
2. `POST /agents/register` — publishes extended Agent Card
3. `GET /agents` — fetches all peers, caches locally
4. Heartbeat every 60s — refreshes TTL, updates budget remaining and status
5. 3 missed heartbeats — registry marks agent `offline`
6. On join, sends an introduction message (A2A) to its `reports_to`:
   "I just joined as SEO Specialist. My skills: [...]. How can I help?"
7. The superior updates its knowledge of available reports, may delegate work immediately

### Fallback (no registry)

```yaml
# agent-config.yaml
peers:
  - url: https://vm-ceo:8462
  - url: https://vm-vp-marketing:8462
```

Each peer serves its Agent Card at `/.well-known/agent-card.json` (A2A standard). Discovery works without the registry — just slower for capability lookups.

## Work exchange

All communication is asynchronous. An agent sends a task, continues working, and gets notified when the result is ready.

### The flow

1. VP Marketing needs an SEO report
2. Queries registry: `GET /agents/by-skill/seo-audit`
3. Gets back `seo-agent` with its URL
4. Sends A2A task:
   ```
   POST seo-agent-url/a2a
   {
     method: "tasks/send",
     params: {
       message: {
         role: "user",
         parts: [{ text: "Generate Q1 SEO report for conduktor.io, focus organic traffic vs competitors" }]
       },
       metadata: {
         from: "vp-marketing",
         priority: "high",
         callback_url: "https://vm-vp-marketing:8462/a2a"
       }
     }
   }
   ```
5. SEO agent returns `202 Accepted` with `taskId`
6. VP Marketing continues other work
7. SEO agent processes the task (runs Claude with semrush tools)
8. SEO agent pushes notification to `callback_url` with result + artifact references
9. VP Marketing receives, reads the artifact, continues

### Four exchange patterns

**DELEGATE (top-down):** CEO tells VP Marketing "Prepare Q1 board deck." The VP decomposes and cascades to reports.

**CONSULT (peer-to-specialist):** VP Marketing asks SEO Agent for a traffic report. The requester lacks a skill the specialist has.

**ESCALATE (bottom-up):** Dev Agent asks CTO "Monorepo or polyrepo? I need a decision above my scope."

**BROADCAST (one-to-many):** CEO tells all reports "Strategic pivot to enterprise. Adjust your plans." Each recipient adapts priorities.

### Task queue (per agent)

Each agent maintains a local inbox, sorted by priority:

1. Escalations received (urgent by nature)
2. Tasks from hierarchical superior
3. Peer consultations
4. Broadcasts

Capacity rules:
- Max 1 task running (Claude = single thread of thought)
- Configurable backlog max (default: 10)
- Backlog full -> reject with `reason: "at_capacity"`
- Budget depleted -> reject with `reason: "on_vacation", resume_at: "..."`

### Task decomposition (fan-out)

When an agent receives a complex task, Claude analyzes it and decomposes:

```
CEO: "Prepare Q1 board deck"
  |
  VP Marketing receives, Claude decomposes:
  |
  +-> SEO Agent:     "Q1 traffic report"         -> task-001
  +-> Content Agent: "Q1 content metrics"         -> task-002
  +-> self:          "Marketing synthesis for Q1"  -> blocked on 001 + 002
```

The VP tracks subtask completion locally. When all subtasks complete, it synthesizes and responds to the CEO.

### Callback wake-up (subtask completion)

When an agent delegates subtasks, it goes idle (or works on other queue items). The delegated agent sends a push notification to the `callback_url` when done. The receiving node's A2A server handles this:

1. Push notification arrives with `taskId` + `status: completed` + artifact refs
2. Node matches the `taskId` to a tracked subtask
3. Updates local subtask tracker (e.g. `task-001: completed`)
4. If all subtasks for a parent task are done, re-queues the parent task with all collected results injected into the Claude conversation
5. Claude resumes work: synthesizes subtask outputs, produces the final deliverable, responds to the original requester

If a subtask is rejected (`at_capacity`, `on_vacation`), the agent can retry with another peer that has the same skill, or escalate to its superior if no alternative exists.

### How Claude decides what to do

No workflow engine. The system prompt gives Claude enough context to decide:

```
You are the VP Marketing of this organization.
You report to the CEO.
Your direct reports: seo-agent, content-agent.

When you receive a task:
1. Evaluate if you can do it alone with your tools
2. If you need a skill you don't have, query the registry and delegate via A2A
3. If the decision is above your scope, escalate to your superior
4. When you delegate, create subtasks and track completion
5. When all subtasks complete, synthesize and respond

Your tools: [Google Analytics, HubSpot]
You do NOT have access to: semrush (delegate to SEO agent)
```

Claude reads this prompt + the incoming task + its queue state, and decides naturally what to delegate, what to do, what to escalate.

### Agent knowledge (deep specialization)

A system prompt alone doesn't make a specialist. Each agent can load domain knowledge:

```
agent-config/
+-- knowledge/              # domain reference docs
|   +-- seo-playbook.md
|   +-- semrush-api-guide.md
|   +-- competitor-list.yaml
+-- CLAUDE.md               # agent-specific instructions, conventions, patterns
+-- mcp-servers.json        # specialized MCP servers (semrush, google analytics...)
```

On `hive join`, the `--knowledge` flag points to a directory of domain docs. These get loaded into the Claude Code context (via CLAUDE.md references, knowledge/ folder, or MCP servers). The SEO agent doesn't just have semrush access — it has the playbook, the methodology, the reference data.

### Agent initiative loop

Agents don't just react to incoming tasks. A periodic loop wakes each agent to evaluate its own objectives:

```
Every N minutes (configurable, default: 30min):
  1. Agent wakes up
  2. Reads its objectives from config
  3. Pulls org-memory, reads recent events and artifacts relevant to its domain
  4. Claude evaluates:
     - "Am I making progress on my objectives?"
     - "Is there something I should do proactively?"
     - "Should I report status to my superior?"
  5. Claude decides:
     - Nothing to do -> go back to sleep
     - Self-assign a task -> add to own queue
     - Delegate work -> send A2A task to a report or peer
     - Send a status report -> A2A message to reports_to
  6. Budget check applies: if warning/vacation, skip the loop
```

This is what makes agents feel like colleagues rather than tools. The SEO agent notices traffic dropped and alerts the VP Marketing without being asked. The VP Marketing sends a weekly recap to the CEO because that's what VPs do.

### Proactive reporting

Each agent has a `reporting.frequency` config (daily, weekly, or custom). When the initiative loop fires and the reporting interval has elapsed:

1. Agent pulls its own recent activity from org-memory (events, artifacts, budget spend)
2. Claude synthesizes a status report
3. Commits the report to `artifacts/{domain}/reports/`
4. Sends it to `reports_to` via A2A with artifact_ref

The CEO gets weekly reports from all VPs. VPs get reports from their specialists. Nobody has to ask for them.

## Artifact exchange

A2A messages are lightweight envelopes. Heavy content lives in the shared git repo.

### Principle: A2A for coordination, git for content

Never transit a 500-line file through an A2A message. Commit it in org-memory, send the reference.

### Org-memory repo structure

```
org-memory/
+-- org-chart.yaml                      # hierarchy, roles, reporting lines
+-- registry/
|   +-- agents.yaml                     # agent cards (registry fallback)
+-- decisions/
|   +-- 2026-03-27-pivot-enterprise.md
+-- artifacts/
|   +-- seo/
|   |   +-- 2026-Q1-traffic-report.md
|   +-- marketing/
|   |   +-- 2026-Q1-board-deck.md
|   +-- engineering/
|       +-- rfc-monorepo-migration.md
+-- reviews/
|   +-- 2026-03-27-api-refactor/
|       +-- diff.patch
|       +-- request.yaml
|       +-- review.md
+-- inbox/
|   +-- {agent-name}/
|       +-- {task-id}/
+-- events/
|   +-- 2026-03-27/
|       +-- 10-42-15-seo-agent-task-received.yaml
+-- budget-logs/
|   +-- {agent-name}/
|       +-- 2026-03-27.jsonl
+-- .org/
    +-- templates/
    +-- conventions.md
```

### Three exchange modes

**INLINE (< 50 lines):** directly in the A2A message parts. For short answers, decisions, status updates.

**COMMIT & REF (structured artifacts):** agent commits to org-memory, pushes, sends A2A message with reference:

```yaml
metadata:
  artifact_ref:
    repo: "org-memory"
    path: "artifacts/seo/2026-Q1-traffic-report.md"
    commit: "a3f7b2c"
    size_lines: 340
```

Receiver does `git pull`, reads the file locally.

**REVIEW REQUEST (code/content to evaluate):**

```yaml
# reviews/2026-03-27-api-refactor/request.yaml
requested_by: dev-agent
reviewer: cto-agent
repo: https://github.com/company/api
branch: feat/refactor-auth
files: [src/auth.ts, src/middleware.ts]
context: "Refactor auth to support OAuth2 + SAML"
focus: ["security", "backward-compat"]
```

Reviewer clones the repo, reviews, writes `review.md` in the same folder, pushes, notifies the requester.

### Git conflict avoidance

Claim-based paths: each agent writes only to its own directories (`artifacts/{domain}/`, `inbox/{name}/`).

Shared files (`org-chart.yaml`, `decisions/`) use advisory locking:

1. Commit `.lock/org-chart.yaml.lock` with `{agent: "ceo-agent", until: "..."}`
2. Modify, commit, push
3. Remove the lock file

With 5-10 agents writing to isolated paths, collisions will be rare in practice.

## Budget and vacation mode

### Per-agent budget config

```yaml
budget:
  daily_max_usd: 5.00
  weekly_max_usd: 25.00
  per_task_max_usd: 2.00
```

Budget reflects seniority: a CEO needs $20/day (long contexts, complex decisions), an IC might need $3/day (targeted tasks).

### Circuit breaker states

```
ACTIVE         spent < 80% daily budget
    |
WARNING        spent >= 80% daily budget
    |          agent prioritizes aggressively, rejects low-priority tasks
    |          heartbeat propagates "warning" status
    |
VACATION       spent >= 100% daily budget (or weekly budget hit)
               agent rejects ALL incoming tasks
               reason: "on_vacation", resume_at: next reset time
               notifies reports_to: "Budget depleted, on leave until tomorrow"
               superior redistributes pending tasks
    |
ACTIVE         midnight reset (or configurable reset time)
               announces return to registry + reports_to
               resumes processing queue
```

### Execution guard

Before each Claude call:
1. Check `budget_remaining > 0`. If no, enter vacation.
2. Pass `--max-budget-usd = min(per_task_max, remaining)` to Claude Code. This is a hard cap enforced by the CLI/SDK itself.
3. No cost estimation heuristic. Predicting LLM costs before execution is unreliable.

After each Claude call:
1. Read real cost (returned by Claude CLI/SDK)
2. Increment `spent_today`, `spent_week`
3. Append to `budget-logs/{agent-name}/YYYY-MM-DD.jsonl` in org-memory
4. Re-evaluate circuit breaker state

## Observability

The system is observable without participating — watching a colony work.

### Event log

Every agent appends structured events to org-memory:

```yaml
# events/2026-03-27/10-42-15-seo-agent-task-received.yaml
timestamp: 2026-03-27T10:42:15Z
agent: seo-agent
event: task_received
task_id: task-abc123
from: vp-marketing
summary: "Q1 SEO report for conduktor.io"
cost_usd: null  # filled on completion
```

### Dashboard

```
+-- Hive Dashboard -------------------------------------------+
|                                                             |
|  AGENTS                STATUS    BUDGET      QUEUE          |
|  --------------------------------------------------------- |
|  * ceo-agent           active    $14.20/$20  2 tasks       |
|  * vp-marketing        active    $6.50/$10   1 task        |
|  * seo-agent           busy      $4.80/$5    3 tasks       |
|  o content-agent       vacation  $5.00/$5    0 tasks       |
|  * cto-agent           active    $8.00/$15   1 task        |
|  * dev-agent-1         active    $1.20/$3    0 tasks       |
|                                                             |
|  RECENT ACTIVITY                                            |
|  --------------------------------------------------------- |
|  10:45  seo-agent -> vp-marketing  "SEO Q1 report done"    |
|  10:42  vp-marketing -> seo-agent  "Generate SEO report"   |
|  10:38  ceo-agent -> vp-marketing  "Prepare board deck"    |
|  10:35  content-agent              "Budget depleted, leave" |
|  10:30  dev-agent-1 -> cto-agent   "Review RFC monorepo"   |
|                                                             |
|  ORG SPEND TODAY: $39.70  |  TASKS: 12 done, 7 active     |
+-------------------------------------------------------------+
```

Data sources:
- Agent status: query registry (heartbeat data)
- Activity feed: `git log` on org-memory/events/
- Budget: aggregate budget-logs
- Queue depth: each agent exposes `/status` locally

The dashboard is read-only. It does not participate in the network.

## Joining and leaving the network

### Join

```
$ hive join \
    --role "Content Writer" \
    --reports-to "vp-marketing" \
    --skills "copywriting,blog-writing,social-media" \
    --tools "WordPress,Grammarly" \
    --objectives "Publish 4 blog posts/week,Grow newsletter to 5k subs" \
    --knowledge ./content-writer-knowledge/ \
    --report-frequency weekly \
    --budget-daily 4.00 \
    --initiative-interval 30m \
    --registry https://registry.local:8080 \
    --org-memory git@github.com:company/org-memory.git
```

What happens:
1. Clone org-memory, read `org-chart.yaml` and `conventions.md`
2. Generate extended Agent Card from flags (including objectives)
3. Load knowledge directory into agent's context (CLAUDE.md, domain docs, MCP server configs)
4. Build system prompt from role card + objectives + org conventions
5. Start A2A server (HTTP)
6. Register with registry
7. Send introduction message to `reports_to`:
   "I just joined as Content Writer. My skills: [...]. My objectives: [...]. How can I help?"
8. Superior receives, updates its knowledge, may delegate work immediately
9. Start initiative loop (periodic wake-up)
10. Agent is operational

### Leave

```
$ hive leave --graceful
```

1. Status -> `leaving`
2. Finish current task
3. Reject new tasks with redirect to a peer
4. Notify `reports_to`: "I'm leaving the organization"
5. Deregister from registry
6. Push final state to org-memory
7. Shutdown

## Future considerations (not in scope for v1)

- **Gossip-based P2P discovery** — replace central registry when network grows past ~20 nodes
- **Local model support** — cheaper models (Qwen, Llama) for IC-level agents via compatible CLI/SDK interface
- **Agent spawning** — an agent (e.g. CTO) could decide to spin up a new agent if workload demands it
- **Reputation/trust scores** — track which agents produce good work, weight task routing accordingly
- **External artifact storage** — S3/GCS for large binary files that don't belong in git
- **Cross-organization federation** — two Hive networks connecting their registries
- **Emergent hierarchy** — let agents renegotiate `reports_to` and team structure based on workload and performance

## Naming

Name: **a2a-society**
