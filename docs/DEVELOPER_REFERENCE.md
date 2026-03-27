# AXON Agent Scale Kit — Developer Reference

Quick-access canonical reference for AXON protocol and this project's tooling.
Everything here is authoritative; for the latest on-chain parameters always verify
against the official Axon docs (see §1 below).

---

## §1 — AXON Protocol Official Resources

| Resource | URL | Notes |
|----------|-----|-------|
| Official Website | https://axonchain.ai/ | Main project site |
| Official GitHub | https://github.com/axon-chain/axon | Canonical source of truth |
| Developer Guide | https://github.com/axon-chain/axon/blob/main/docs/DEVELOPER_GUIDE_EN.md | Chain ops & SDK reference |
| Whitepaper | https://github.com/axon-chain/axon/blob/main/docs/whitepaper_en.md | Protocol design |
| Mainnet Params | https://github.com/axon-chain/axon/blob/main/docs/MAINNET_PARAMS_EN.md | Full on-chain parameter table |
| Testnet Guide | https://github.com/axon-chain/axon/blob/main/docs/TESTNET_EN.md | |
| AI Challenge Source | `axon-chain/axon/x/agent/keeper/challenge.go` | `configs/network.yaml` bank_source_url points here |
| Public Node Scripts | `scripts/start_validator_node.sh`, `scripts/start_sync_node.sh` | In the official repo |

**Third-party tools** (use at your own risk):

| Resource | URL | Notes |
|----------|-----|-------|
| Agent Reputation Oracle | http://axonrep.xyz/ | On-chain agent reputation query, epoch/ranking info |
| Agent Monitor + OTC | https://ai-colony.top/explorer/ | Per-address monitoring, OTC market data |

---

## §2 — Chain Constants

Sourced from [Mainnet Params](https://github.com/axon-chain/axon/blob/main/docs/MAINNET_PARAMS_EN.md).

### Network Identity

| Parameter | Value |
|-----------|-------|
| Cosmos Chain ID | `axon_8210-1` |
| EVM Chain ID | `8210` |
| Native Token | AXON (`aaxon` = smallest unit) |

### Public RPC & API Endpoints

| Service | Public URL |
|---------|-----------|
| EVM JSON-RPC | `https://mainnet-rpc.axonchain.ai/` |
| EVM RPC (direct) | `http://mainnet.axonchain.ai:8545` |
| REST API | `https://mainnet-api.axonchain.ai/` |
| REST API Docs | `https://mainnet-api.axonchain.ai/docs/` |
| CometBFT RPC | `https://mainnet-cometbft.axonchain.ai/` |

### Agent Module Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Min Registration Stake | 100 AXON | |
| Registration Burn | 20 AXON | Burned inside `register(payable)` — no separate burn call |
| Initial Reputation | 10 | |
| Max Reputation | 100 | |
| Epoch Length | 720 blocks (~1 hour) | |
| Heartbeat Interval | 100 blocks | Minimum on-chain spacing |
| Heartbeat Timeout | 720 blocks | Agent goes offline if exceeded |
| AI Challenge Window | 50 blocks | First 50 blocks of each epoch |
| Heartbeat Daemon Interval | 60 s | Scale-kit daemon polling rate |

### Precompiled Contracts (EVM Layer)

| Address | Interface | Description |
|---------|---------|-------------|
| `0x...0801` | `IAgentRegistry` | register, heartbeat, stake mgmt, deregister |
| `0x...0802` | `IAgentReputation` | Reputation query (L1+L2 combined) |
| `0x...0803` | `IAgentWallet` | Owner/operator/guardian wallet policy |

Full ABI for `0x0801` is maintained in `scripts/axonctl.py` (11 methods).

---

## §3 — Project Repository & GitHub

### Remotes

| Remote | URL | Role |
|--------|-----|------|
| `origin` | `https://github.com/tizerluo/axon-agent-scale-kit.git` | Your personal fork |
| `upstream` | `https://github.com/6tizer/axon-agent-scale-kit.git` | Owner — protected main branch |

### Key People

| Person | GitHub | Role |
|--------|--------|------|
| tizerluo (you) | https://github.com/tizerluo | Contributor — fork workflow |
| 6tizer | — | Owner — final reviewer, server operator, Mac mini owner |

### Branches

| Branch | Purpose |
|--------|---------|
| `main` | Stable — only receives PR merges, protected |
| `docs/collaboration-workflow` | Active development (this branch) |
| `feature/cursor-dev` | Feature work |

### Collaboration Workflow

See `docs/ops/collaboration_workflow.md` for the full Fork + PR process.
Key rule: **never push directly to `main`; all changes go through PR.**

---

## §4 — Local Environment

### SSH Access to Production Server

| Item | Value |
|------|-------|
| Host | `ubuntu@43.165.195.71` (jakarta-node) |
| SSH Key | `/Users/tizerluo/Downloads/QQClaw.pem` |
| Connect | `ssh -i /Users/tizerluo/Downloads/QQClaw.pem ubuntu@43.165.195.71` |

### Project Paths

| Environment | Path |
|-------------|------|
| **Local repo root** | `/Users/tizerluo/Cursor2026/AXON/axon-agent-scale-kit` |
| **Server workdir** | `/home/ubuntu/axon-agent-scale` |
| **Server state file** | `/home/ubuntu/axon-agent-scale/state/deploy_state.json` |
| **QQClaw daemon** | `/opt/axon-node/scripts/agent_daemon.py` |

### Server Services

| Service | Unit File | Status |
|---------|-----------|--------|
| Heartbeat Daemon (scale-kit) | `axon-heartbeat-daemon.service` | active |
| QQClaw Validator (migrated) | `axon-agent-qqclaw.service` | disabled (migrated 2026-03-27) |

> `qqclaw-validator` agent 已完成迁徙，由 `axon-heartbeat-daemon.service` 统一维护心跳。
> 旧 daemon `axon-agent-qqclaw.service` 已 disabled，内容归档至 `scripts/archive/`。

---

## §5 — Scale-Kit Project Layout

```
axon-agent-scale-kit/
├── scripts/
│   ├── axonctl.py              # Primary CLI entry point (heartbeat, challenge, lifecycle…)
│   └── release_deploy_verify.sh # One-command push → deploy → restart → verify
├── configs/
│   ├── network.yaml            # Chain constants, RPC, AI Challenge settings
│   ├── agents.yaml             # Agent definitions
│   └── runtime/                # Private runtime overrides (gitignored)
│       ├── hosts.runtime.yaml  # Server host mapping
│       ├── network.runtime.yaml
│       └── agents.runtime.yaml
├── state/
│   └── deploy_state.json       # Source of truth for all agent state (gitignored)
├── tests/
│   └── test_axonctl.py         # Regression suite
├── docs/
│   ├── ops/                    # Production ops docs
│   └── plans/                  # Implementation plans
├── HOTFIX_2026-03-27_QQCLAW_AI_CHALLENGE_HEARTBEAT.md  # Incident record
├── agent_daemon_hotfix.py      # Hotfix before/after diff archive
└── README.md                   # CLI usage reference
```

### Key CLI Commands

```bash
# Validate before any state change
python scripts/axonctl.py validate --network configs/network.yaml --agents configs/agents.yaml

# Heartbeat
python scripts/axonctl.py heartbeat-once  --state-file state/deploy_state.json --network configs/network.yaml --agent agent-001
python scripts/axonctl.py heartbeat-batch  --state-file state/deploy_state.json --network configs/network.yaml --request-id <id>

# AI Challenge
python scripts/axonctl.py challenge-gate-check --network configs/network.yaml --agent agent-001
python scripts/axonctl.py challenge-run-once  --state-file state/deploy_state.json --network configs/network.yaml --agent agent-001

# Lifecycle
python scripts/axonctl.py lifecycle-report --network configs/network.yaml --request-id <id>
python scripts/axonctl.py registration-audit --network configs/network.yaml --agent agent-001 --strict

# Release
scripts/release_deploy_verify.sh --dry-run --allow-dirty --skip-tests   # rehearsal
scripts/release_deploy_verify.sh                                           # real release
```

---

## §6 — Common Tasks

### Import an Existing Agent Private Key

```bash
python scripts/axonctl.py agent-wallet-import \
  --agent agent-legacy-006 \
  --private-key <hex_private_key> \
  --address <optional_expected_address>
```

### Extract Full Private Key (Masked by Default)

```bash
python scripts/axonctl.py wallet-export --key-id <key_id>
python scripts/axonctl.py wallet-export --key-id <key_id> --reveal-secret  # secure env only

# Backup all wallets
python scripts/axonctl.py wallet-backup-export --output-file backups/wallets.secure.json
python scripts/axonctl.py wallet-backup-verify --backup-file backups/wallets.secure.json
```

### Sync Latest Changes from Upstream

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

---

## §7 — Registration Burn Mechanic (Authoritative口径)

> The 20 AXON burn is built into `IAgentRegistry.register(payable)` — it is not a separate transaction.

- Registration stake: 100 AXON (sent as `msg.value`)
- Registration burn: 20 AXON (deducted inside the `register` call)
- Net staked: 80 AXON

There is **no separate burn interface** to "top up" or repair a degraded registration.
If an agent has reputation issues, use `registration-audit` + `lifecycle-repair` instead.
If you suspect the on-chain behavior differs from this description, file an issue against
https://github.com/axon-chain/axon.
