# AXON Agent Scale Kit

Automation toolkit for AXON agent daily scaling workflows.

## Scope

- Validate network and agent configuration
- Create funded scale requests and funding gate checks
- Generate scale plans with budget and batch strategy
- Execute idempotent scaling, status reports and repair actions
- Generate, list, export and backup all wallet keys (funding + agent wallets)

## Step 0 Initialization

```bash
# local dependency check
python scripts/axonctl.py init-step --mode local

# server dependency check/install (docker + directories)
python scripts/axonctl.py init-step --mode server --hosts configs/hosts.yaml --host your-server
```

## Wallet Management

All wallets (funding address + per-agent wallets) are generated locally with
real keys. Default CLI output is masked for private key and mnemonic.

### Funding wallet (receives AXON transfers)
```bash
# option A: reuse existing funding wallet automatically (if exists)
python scripts/axonctl.py wallet-generate --role funding --label "my-funding-wallet"

# option B: create import template
python scripts/axonctl.py funding-wallet-template --output funding_wallet.template.yaml
# fill file and import
python scripts/axonctl.py funding-wallet-import --wallet-file funding_wallet.template.yaml

python scripts/axonctl.py wallet-list
```
Use the generated address as the destination for your AXON transfers.

To set an existing address as the funding wallet:
```bash
python scripts/axonctl.py funding-wallet-set --address 0x...
python scripts/axonctl.py funding-wallet-get
```

### Agent wallets (created automatically during scale)
```bash
python scripts/axonctl.py wallet-list
python scripts/axonctl.py wallet-export --key-id <key_id>
python scripts/axonctl.py wallet-export --key-id <key_id> --reveal-secret
python scripts/axonctl.py wallet-backup-export --output-file backups/wallets.secure.json
python scripts/axonctl.py wallet-backup-verify --backup-file backups/wallets.secure.json
```
**Backup all agent wallets after each scale run.** Keep backup file offline and encrypted.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1. generate a funding wallet and use its address for transfers
python scripts/axonctl.py wallet-generate --role funding --label "my-funding"
# ^ copy the address from output and transfer AXON to it

# 2. validate configuration
python scripts/axonctl.py validate \
  --network configs/network.yaml \
  --agents configs/agents.yaml

# 3. trigger scaling with natural language
python scripts/axonctl.py run-intent \
  --network configs/network.yaml \
  --agents configs/agents.yaml \
  --intent "I fund 250 AXON, scale 2 agents"

# 4. deploy to remote server and start 1-agent-1-container
python scripts/axonctl.py remote-deploy \
  --state-file state/deploy_state.json \
  --request-id <request_id> \
  --hosts configs/hosts.yaml \
  --host your-server \
  --network configs/network.yaml \
  --agents configs/agents.yaml

# 5. check remote container status
python scripts/axonctl.py remote-status \
  --state-file state/deploy_state.json \
  --request-id <request_id> \
  --hosts configs/hosts.yaml \
  --host your-server

# 6. export and backup all agent wallet keys
python scripts/axonctl.py wallet-list
python scripts/axonctl.py wallet-export --key-id <key_id>
python scripts/axonctl.py wallet-backup-export --output-file backups/wallets.secure.json
python scripts/axonctl.py wallet-backup-verify --backup-file backups/wallets.secure.json

# 7. challenge and lifecycle management
python scripts/axonctl.py challenge-gate-check --network configs/network.yaml --agent agent-001
python scripts/axonctl.py challenge-run-once --network configs/network.yaml --agent agent-001
python scripts/axonctl.py challenge-batch --network configs/network.yaml --request-id <request_id>
python scripts/axonctl.py lifecycle-report --network configs/network.yaml --request-id <request_id>
python scripts/axonctl.py lifecycle-repair --network configs/network.yaml --request-id <request_id>
```

## Remote Host Config

`configs/hosts.yaml` defines real deployment target hosts for SSH/SCP + Docker:

```yaml
hosts:
  - name: "your-server"
    deployment_mode: "server"
    host: "YOUR_SERVER_IP"
    user: "YOUR_SSH_USER"
    os_type: "linux"
    os_version: "YOUR_OS_VERSION"
    ssh_key: "/path/to/your/private-key.pem"
    workdir: "/home/YOUR_SSH_USER/axon-agent-scale"
    python_bin: "python3"
    use_sudo: true
    docker:
      expected_engine: "docker"
      expected_compose: "docker compose"
```

## Layout

- `configs/`: network and agent declaration files
- `scripts/`: CLI and execution scripts
- `templates/`: legacy templates
- `state/`: local state data (contains private keys — keep it safe)
- `tests/`: regression test suite
