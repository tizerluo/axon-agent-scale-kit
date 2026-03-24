# AXON Agent Scale Kit

Automation toolkit for AXON agent daily scaling workflows.

## Scope

- Validate network and agent configuration
- Create funded scale requests and funding gate checks
- Generate scale plans with budget and batch strategy
- Execute idempotent scaling, status reports and repair actions

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/axonctl.py validate --network configs/network.yaml --agents configs/agents.yaml
python scripts/axonctl.py run-intent \
  --network configs/network.yaml \
  --agents configs/agents.yaml \
  --intent "我打1500 AXON，扩容10个Agents"
```

## Layout

- `configs/`: network and agent declaration files
- `scripts/`: CLI and execution scripts
- `templates/`: systemd templates
- `state/`: local state data
