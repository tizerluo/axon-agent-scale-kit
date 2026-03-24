# AXON Agent Scale Kit

Automation toolkit for AXON agent daily scaling workflows.

## Scope

- Validate network and agent configuration
- Deploy and scale agent service instances
- Check status and run repair actions

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/axonctl.py validate --network configs/network.yaml --agents configs/agents.yaml
python scripts/axonctl.py scale --network configs/network.yaml --agents configs/agents.yaml --add 1
python scripts/axonctl.py status --network configs/network.yaml --agents configs/agents.yaml
```

## Layout

- `configs/`: network and agent declaration files
- `scripts/`: CLI and execution scripts
- `templates/`: systemd templates
- `state/`: local state data
