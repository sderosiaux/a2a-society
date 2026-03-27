# a2a-society

A distributed network of Claude Code agents that collaborate like colleagues in a company, using Google's [A2A protocol](https://github.com/a2aproject/A2A) for inter-agent communication.

Each agent runs on its own machine, has a role (CEO, VP Marketing, SEO Specialist...), discovers peers, delegates work, manages its own budget, and self-organizes around a shared git-backed organizational memory.

```
                    +---------------+
                    |   Registry    |
                    +-------+-------+
                            | discovery
           +----------------+----------------+
           v                v                v
     +----------+    +----------+    +----------+
     | CEO      |<-->|VP Market.|<-->| SEO      |
     | Agent    |    | Agent    |    | Agent    |
     +----------+    +----------+    +----------+
              direct A2A communication
```

## What makes this different

Existing A2A + Claude projects ([jcwatson11/claude-a2a](https://github.com/jcwatson11/claude-a2a), [ericabouaf/claude-a2a](https://github.com/ericabouaf/claude-a2a)) are passive bridges: they receive work but never initiate it. Agents can't discover or talk to each other.

**a2a-society** agents are active participants:
- Every node is both A2A **server and client**
- Agents **discover** each other via registry or static peers
- Agents **delegate**, **escalate**, **consult**, and **broadcast** work
- Agents have **objectives** and a periodic **initiative loop** — they don't just wait for tasks
- Agents **report** to their superior unprompted, on schedule
- Agents go **on vacation** when their budget runs out, and come back when it resets

## Quick start

```bash
pip install -e .

# Start the registry
hive registry --port 8080 &

# Start 3 agents
hive join --config examples/startup/ceo.yaml --registry http://127.0.0.1:8080 &
hive join --config examples/startup/vp-marketing.yaml --registry http://127.0.0.1:8080 &
hive join --config examples/startup/seo-agent.yaml --registry http://127.0.0.1:8080 &

# Watch them work
hive dashboard --registry http://127.0.0.1:8080
```

Or use the example script:
```bash
cd examples/startup && ./run.sh
```

## Architecture

| Component | What it does |
|-----------|-------------|
| **Agent Node** | A2A server + client + Claude Code executor + budget manager + task queue |
| **Registry** | Lightweight HTTP discovery service (agent cards + heartbeat) |
| **Org Memory** | Shared git repo for artifacts, events, decisions, budget logs |
| **Initiative Loop** | Periodic wake-up where agents evaluate objectives and act proactively |
| **Dashboard** | Read-only terminal UI showing agent status and activity |

## Agent config

```yaml
name: "seo-agent"
role: "SEO Specialist"
reports_to: "vp-marketing"
skills:
  - id: "seo-audit"
    name: "SEO site audit"
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
```

## How agents interact

**DELEGATE** (top-down): CEO tells VP Marketing "Prepare Q1 board deck." The VP decomposes and cascades.

**CONSULT** (peer-to-specialist): VP Marketing asks SEO Agent for a traffic report. The requester lacks a skill the specialist has.

**ESCALATE** (bottom-up): Dev Agent asks CTO "Monorepo or polyrepo?" — a decision above their scope.

**BROADCAST** (one-to-many): CEO tells all reports "Pivot to enterprise. Adjust plans."

## Budget and vacation

Each agent has a daily/weekly budget. When spent:
- **80%** → `warning` — agent rejects low-priority tasks
- **100%** → `vacation` — agent rejects everything, notifies superior, comes back at midnight

## Design docs

- [Design spec](docs/plans/2026-03-27-hive-design.md) — full architecture and design decisions
- [Implementation plan](docs/plans/2026-03-27-hive-implementation.md) — 10 milestones, task-by-task

## Status

Early stage. The core works: agents register, discover peers, exchange tasks via A2A, manage budgets, log events to shared git memory, and run initiative loops. Not production-ready yet.

## Future

- Gossip-based P2P discovery (replace central registry)
- Local model support (Qwen, Llama) for IC-level agents
- Agent spawning (CTO spins up new dev agents when workload demands)
- Cross-organization federation

## License

MIT
