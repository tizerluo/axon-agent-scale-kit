# AXON Agent Scale Kit

Scale, operate, and secure AXON Agents with one CLI-first workflow.

Production-oriented automation for funded scaling, remote deployment, heartbeat, AI challenge execution, lifecycle repair, and wallet governance.

## Capabilities

- Config validation for network, agents, and operational guardrails
- Funding-gated scaling pipeline with request, plan, execute, status, and repair stages
- Remote server deployment with 1-agent-1-container orchestration
- Heartbeat automation with retry/backoff and due-window checks
- AI challenge flow with gate checks, local answer bank, and batch execution
- Lifecycle reporting with health grading and repair actions
- Wallet governance with masked export, secure backup export, and backup verification
- Built-in GitHub Actions unittest workflow for regression checks

## 10-Line Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/axonctl.py init-step --mode local
python scripts/axonctl.py wallet-generate --role funding --label "my-funding"
python scripts/axonctl.py validate --network configs/network.yaml --agents configs/agents.yaml
python scripts/axonctl.py run-intent --network configs/network.yaml --agents configs/agents.yaml --intent "I fund 250 AXON, scale 2 agents"
python scripts/axonctl.py remote-deploy --state-file state/deploy_state.json --request-id <request_id> --hosts configs/hosts.yaml --host your-server --network configs/network.yaml --agents configs/agents.yaml
python scripts/axonctl.py heartbeat-batch --network configs/network.yaml --request-id <request_id>
python scripts/axonctl.py challenge-batch --network configs/network.yaml --request-id <request_id>
python scripts/axonctl.py lifecycle-report --network configs/network.yaml --request-id <request_id>
```

## Agent Invocation Rules

- Always run `validate` before any state-changing action
- Prefer deterministic, non-interactive commands with explicit flags
- Run `challenge-gate-check` before `challenge-run-once` or `challenge-batch`
- Treat non-open challenge windows and inactive validator gates as runtime conditions, not code failures
- Use masked output by default; use `--reveal-secret` only in a secure environment
- Finish every run with `wallet-backup-export` and `wallet-backup-verify`

## State Source of Truth

- Runtime state source of truth is `state/deploy_state.json`
- Historical snapshots are not used by the CLI runtime
- Use `--state-file` only when you intentionally manage an isolated state context

## Scope

- Validate network and agent configuration
- Create funded scale requests and funding gate checks
- Generate scale plans with budget and batch strategy
- Execute idempotent scaling, status reports and repair actions
- Generate, list, export and backup all wallet keys (funding + agent wallets)

## On-Chain Register (payable)

Registration must go through `IAgentRegistry.register(string,string)` at
`0x0000000000000000000000000000000000000801` with `msg.value` stake.

`scale` now uses on-chain register and only marks `registered/staked=true` after
successful on-chain post-check (`isAgent/getAgent`).

```bash
# dry-run intent only (no state mutation, no on-chain tx)
python scripts/axonctl.py register-onchain-once \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --agent agent-001 \
  --stake-axon 100 \
  --dry-run

# real register for one agent
python scripts/axonctl.py register-onchain-once \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --agent agent-001 \
  --stake-axon 100

# batch register from request plan
python scripts/axonctl.py register-onchain-batch \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --request-id <request_id> \
  --stake-axon 100
```

Registration evidence is persisted under each agent in `state/deploy_state.json`
as `registration.*`:
- `tx_hash/receipt_status/block_number/from/to/value_axon/method`
- `post_check.is_agent/agent_id/reputation/is_online`
- `burn_expected_axon=20`
- `evidence_mode=register_payable_path_proof`

## Registration Audit (Read-Only)

Use `registration-audit` to cross-check local state and on-chain registration
status without sending any transaction.

```bash
# explicit agent list (highest priority)
python scripts/axonctl.py registration-audit \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --agent agent-001 --agent agent-002

# by request plan
python scripts/axonctl.py registration-audit \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --request-id <request_id>

# strict mode: non-zero exit when unregistered_onchain or query errors exist
python scripts/axonctl.py registration-audit \
  --state-file state/deploy_state.json \
  --network configs/network.yaml \
  --agent agent-001 --agent agent-002 \
  --strict
```

Per-agent output fields include:
- `local.registered/staked`
- `onchain.is_agent/agent_id/reputation/is_online`
- `registration_path` (`precompile_register_payable | legacy_or_unknown | not_registered`)
- `burn_evidence_level` (`onchain_burn_field | receipt_only | none`)
- `classification` and `recommended_action`

`lifecycle-report` also includes `registration_path` and `burn_evidence_level`
for each agent, plus summary counters:
- `summary.registration_path_counts`
- `summary.burn_evidence_counts`

## Step 0 Initialization

```bash
# local dependency check
python scripts/axonctl.py init-step --mode local

# server dependency check/install (docker + directories)
python scripts/axonctl.py init-step --mode server --hosts configs/hosts.yaml --host your-server
```

## One-Command Release (Push -> Deploy -> Restart -> Verify)

```bash
# dry-run rehearsal (no mutation)
scripts/release_deploy_verify.sh --dry-run --allow-dirty --skip-tests

# real release
scripts/release_deploy_verify.sh
```

The release script will:
- run local regression (`python3 -m unittest tests.test_axonctl -q`, unless `--skip-tests`)
- push `HEAD` to `origin/main`
- deploy tracked files to server workdir via `git archive`
- restart `axon-heartbeat-daemon.service`
- verify service status, docker status, and lifecycle report

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

### Recover legacy agents from existing private keys
```bash
# single agent import
python scripts/axonctl.py agent-wallet-import \
  --agent agent-legacy-001 \
  --private-key <hex_private_key> \
  --address <optional_expected_address>

# batch import template
python scripts/axonctl.py agent-wallets-template --output configs/recovery/legacy_agents.template.yaml
# copy template to private runtime path, fill it, then import
cp configs/recovery/legacy_agents.template.yaml state/recovery/legacy_agents.yaml
python scripts/axonctl.py agent-wallets-import --wallet-file state/recovery/legacy_agents.yaml
```
Imported agents are attached as `label=agent:<name>` and become manageable by heartbeat/challenge/lifecycle workflows.

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

## Runtime Private Config Layer

Use `configs/runtime/*.template.yaml` as source templates, then copy to private
runtime files (ignored by git):

```bash
cp configs/runtime/network.runtime.template.yaml configs/runtime/network.runtime.yaml
cp configs/runtime/agents.runtime.template.yaml configs/runtime/agents.runtime.yaml
cp configs/runtime/hosts.runtime.template.yaml configs/runtime/hosts.runtime.yaml
```

Run commands with private runtime files explicitly:

```bash
python scripts/axonctl.py validate \
  --network configs/runtime/network.runtime.yaml \
  --agents configs/runtime/agents.runtime.yaml
```

## Layout

- `configs/`: network and agent declaration files
- `configs/recovery/`: recovery import templates (safe to version)
- `scripts/`: CLI and execution scripts
- `scripts/archive/`: historical one-off scripts (not part of active workflow)
- `templates/archive/`: historical systemd template artifacts
- `state/`: local state data (contains private keys — keep it safe)
- `tests/`: regression test suite
