# Hive Example: 3-Agent Startup

A minimal Hive network with 3 agents simulating a startup:

| Agent | Role | Port | Reports To | Exclusive Tools |
|-------|------|------|------------|-----------------|
| ceo | CEO | 8462 | — | — |
| vp-marketing | VP Marketing | 8463 | ceo | — |
| seo-agent | SEO Specialist | 8464 | vp-marketing | semrush |

## Run

```bash
cd examples/startup
chmod +x run.sh
./run.sh
```

## What happens

1. The registry starts on port 8080
2. Three agents join and register
3. Each agent starts its initiative loop, evaluating objectives periodically
4. The SEO agent proactively reports to VP Marketing (weekly)
5. VP Marketing reports to CEO (weekly)

## Send work

Send a task to the CEO:

```bash
curl -X POST http://127.0.0.1:8462 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":"1","params":{"message":{"role":"user","parts":[{"kind":"text","text":"Prepare Q1 board deck"}]}}}'
```

The CEO will analyze the task and may delegate sub-tasks to VP Marketing, who may further delegate to the SEO agent.

## Monitor

```bash
# Quick status
hive status --registry http://127.0.0.1:8080

# Live dashboard
hive dashboard --registry http://127.0.0.1:8080 --org-memory ./org-memory
```

## Estimated cost

With default budgets: ~$35/day max across all agents. Actual spend depends on activity.
