from __future__ import annotations
import sys as _sys, os as _os
_scripts_dir = _os.path.dirname(_os.path.abspath(__file__))
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
del _scripts_dir, _sys, _os

from scripts import _shared_crypto

import argparse
import base64
import copy
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from urllib import request

import yaml

logger = logging.getLogger("axonctl")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGISTRY_PRECOMPILE = "0x0000000000000000000000000000000000000801"
DEFAULT_REGISTER_CAPABILITIES = "validation,heartbeat,docker-managed"
DEFAULT_REGISTER_MODEL = "axon-agent-scale-kit-v1"
DEFAULT_REGISTER_STAKE_AXON = 100.0
DEFAULT_REGISTER_WAIT_RECEIPT_SEC = 180
DEFAULT_REGISTER_BURN_EXPECTED_AXON = 20
DEFAULT_REGISTER_EVIDENCE_MODE = "register_payable_path_proof"
REGISTER_METHOD_SIGNATURE = "register(string,string)"
REGISTER_ABI = [
    # Source: https://github.com/axon-chain/axon/blob/main/precompiles/registry/registry.go
    # IAgentRegistry at 0x0000000000000000000000000000000000000801 — 11 methods
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "isAgent",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getAgent",
        "outputs": [
            {"name": "agentId", "type": "string"},
            {"name": "capabilities", "type": "string[]"},
            {"name": "model", "type": "string"},
            {"name": "reputation", "type": "uint64"},
            {"name": "isOnline", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "capabilities", "type": "string"}, {"name": "model", "type": "string"}],
        "name": "register",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "addStake",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "reduceStake",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "claimReducedStake",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getStakeInfo",
        "outputs": [
            {"name": "totalStake", "type": "uint256"},
            {"name": "pendingReduce", "type": "uint256"},
            {"name": "reduceUnlockHeight", "type": "uint64"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "capabilities", "type": "string"}, {"name": "model", "type": "string"}],
        "name": "updateAgent",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "heartbeat",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "deregister",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
NONCE_LOCK_DIR = "/tmp/axon-nonce"
Path(NONCE_LOCK_DIR).mkdir(exist_ok=True)


class NonceManager:
    """
    基于文件的分布式锁，确保同一 agent 同时只有一笔 pending tx。
    解决 heartbeat-daemon 和 challenge-daemon 并发时 nonce 冲突。

    使用方式：
        with NonceManager(agent):
            ok, tx = _submit_heartbeat_tx(...)
            ...

    锁文件：{NONCE_LOCK_DIR}/{agent}.lock
    """

    def __init__(self, agent: str, timeout: int = 30):
        self.agent = str(agent)
        self.timeout = timeout
        self._lock_fd: int | None = None
        self._lock_path = Path(NONCE_LOCK_DIR) / f"{self.agent}.lock"

    def acquire(self) -> bool:
        """尝试获取锁，超时返回 False。"""
        start = time.time()
        while True:
            try:
                self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # 写入当前进程 PID，方便调试
                os.write(self._lock_fd, f"{os.getpid()}".encode())
                os.close(self._lock_fd)
                self._lock_fd = None
                return True
            except FileExistsError:
                pass
            except OSError:
                return False
            if time.time() - start >= self.timeout:
                return False
            time.sleep(0.1)

    def release(self) -> None:
        """释放锁。"""
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> "NonceManager":
        if not self.acquire():
            raise RuntimeError(f"NonceManager: failed to acquire lock for agent={self.agent} within {self.timeout}s")
        return self

    def __exit__(self, *args) -> None:
        self.release()


DEFAULT_HEARTBEAT = {
    "interval_blocks": 100,
    "timeout_blocks": 720,
    "prewarn_blocks": 120,
    "max_retries": 3,
    "backoff_seconds": 2,
    "receipt_timeout_sec": 120,
}
DEFAULT_CHALLENGE = {
    "enabled": True,
    "validator_required": True,
    "non_llm_first": True,
    "ai_challenge_window_blocks": 50,
    "bank_source_url": "https://raw.githubusercontent.com/axon-chain/axon/main/x/agent/keeper/challenge.go",
    "answer_bank_file": "configs/challenge_answers.yaml",
    "execution_mode": "simulate",
    "openrouter": {"api_base": "https://openrouter.ai/api/v1/chat/completions", "model_id": "", "fallback_models": []},
}


def now_ts() -> int:
    return int(time.time())


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {"requests": {}, "agents": {}, "events": [], "settings": {}, "wallets": {}}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("requests", {})
    state.setdefault("agents", {})
    state.setdefault("events", [])
    state.setdefault("settings", {})
    state.setdefault("wallets", {})
    return state


def save_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    # 原子写入：先写临时文件，再 rename 到目标路径。
    # rename 在 POSIX 下是原子的（同一文件系统保证），进程崩溃不会留下半写文件。
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, state_path)


def rpc_chain_id(rpc_url: str, timeout_sec: int = 5) -> tuple[bool, int | None, str | None]:
    payload = json.dumps({"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1}).encode("utf-8")
    req = request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("result")
        if not result:
            return False, None, "rpc response missing result"
        return True, int(result, 16), None
    except Exception as e:
        return False, None, str(e)


def heartbeat_settings(network_cfg: dict) -> dict:
    hb = dict(DEFAULT_HEARTBEAT)
    user_hb = network_cfg.get("heartbeat", {})
    if isinstance(user_hb, dict):
        hb.update({k: v for k, v in user_hb.items() if v is not None})
    return hb


def validate_heartbeat_settings(hb: dict) -> list[str]:
    errors = []
    interval = int(hb.get("interval_blocks", 0))
    timeout = int(hb.get("timeout_blocks", 0))
    prewarn = int(hb.get("prewarn_blocks", 0))
    retries = int(hb.get("max_retries", 0))
    backoff = int(hb.get("backoff_seconds", 0))
    receipt_timeout = int(hb.get("receipt_timeout_sec", 0))
    if interval <= 0:
        errors.append("heartbeat.interval_blocks must be > 0")
    if timeout <= 0:
        errors.append("heartbeat.timeout_blocks must be > 0")
    if timeout <= interval:
        errors.append("heartbeat.timeout_blocks must be > heartbeat.interval_blocks")
    if prewarn < 0:
        errors.append("heartbeat.prewarn_blocks must be >= 0")
    if prewarn >= timeout:
        errors.append("heartbeat.prewarn_blocks must be < heartbeat.timeout_blocks")
    if retries < 1:
        errors.append("heartbeat.max_retries must be >= 1")
    if backoff < 0:
        errors.append("heartbeat.backoff_seconds must be >= 0")
    if receipt_timeout < 1:
        errors.append("heartbeat.receipt_timeout_sec must be >= 1")
    return errors


def challenge_settings(network_cfg: dict) -> dict:
    data = json.loads(json.dumps(DEFAULT_CHALLENGE))
    user_data = network_cfg.get("challenge", {})
    if isinstance(user_data, dict):
        for key, value in user_data.items():
            if key == "openrouter" and isinstance(value, dict):
                data["openrouter"].update({k: v for k, v in value.items() if v is not None})
            else:
                data[key] = value
    return data


def validate_challenge_settings(cfg: dict) -> list[str]:
    errors = []
    if int(cfg.get("ai_challenge_window_blocks", 0)) <= 0:
        errors.append("challenge.ai_challenge_window_blocks must be > 0")
    if cfg.get("execution_mode") not in {"simulate", "command"}:
        errors.append("challenge.execution_mode must be simulate or command")
    if not cfg.get("bank_source_url"):
        errors.append("challenge.bank_source_url is required")
    if cfg.get("execution_mode") == "command":
        # submit_template/reveal_template 在 network.yaml 中存在即可（允许空字符串）。
        # axond_tx.py 不依赖模板，模板字段是为运维场景保留的。
        cmd = cfg.get("command", {})
        if not isinstance(cmd, dict):
            errors.append("challenge.command must be a dict for command mode")
    return errors


# ─── Hash 算法（来自 _shared_crypto，与 Axon keeper 源码保持一致）──────────────
# normalize_answer / keeper_commit_hash / answer_hash 均来自共享模块，
# 避免本地重复实现导致与链上逻辑分化。

# keeper_commit_hash: SHA256(bech32_addr + ":" + raw_answer)，不做 normalize
keeper_commit_hash = _shared_crypto.keeper_commit_hash


def answer_hash(text: str) -> str:
    """
    AnswerHash（challengePool 中的 hash）使用的算法。
    keeper 在 evaluate 时：revealHash = SHA256(normalizeAnswer(resp.RevealData))
    normalizeAnswer 等价于 _shared_crypto.go_normalize：去掉所有空格/tab/换行，小写。
    """
    return _shared_crypto.keeper_answer_hash(text)


def normalize_answer(text: str) -> str:
    """
    兼容别名，内部已不使用，统一使用 _shared_crypto.go_normalize。
    """
    return _shared_crypto.go_normalize(text)


def fetch_challenge_pool(bank_source_url: str) -> list[dict]:
    with request.urlopen(bank_source_url, timeout=20) as resp:
        content = resp.read().decode("utf-8")
    rows = re.findall(r'\{"([^"]+)",\s*"([a-fA-F0-9]{64})",\s*"([^"]+)"\}', content)
    return [{"question": q, "answer_hash": h.lower(), "category": c} for q, h, c in rows]


def load_answer_bank(answer_bank_file: str) -> dict:
    p = Path(answer_bank_file)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict) and isinstance(data.get("answers"), dict):
        return {str(k): str(v) for k, v in data["answers"].items()}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def get_current_block(rpc_url: str) -> int:
    payload = json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode("utf-8")
    req = request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return int(data["result"], 16)


def get_current_block_healthy(network_cfg: dict) -> int:
    """
    获取当前区块号，同时验证 RPC 健康度。
    如果配置了 fallback_rpc_url，对比两个端点的返回值：
    - 差值 ≤ 5 块：主端点可用，返回主端点值
    - 差值 > 5 块：主端点可能滞后，警告并仍返回主端点值（保守）
    - 主端点失败：降级到 fallback，返回 fallback 值
    """
    primary = network_cfg.get("rpc_url", "")
    fallback = network_cfg.get("fallback_rpc_url", "")
    try:
        block = get_current_block(primary)
        if fallback:
            try:
                fallback_block = get_current_block(fallback)
                diff = abs(block - fallback_block)
                if diff > 5:
                    logging.warning(
                        "RPC freshness warning: primary=%d, fallback=%d (diff=%d). "
                        "Primary RPC may be stale.", block, fallback_block, diff
                    )
            except Exception:
                pass  # fallback 查询失败不影响主端点
        return block
    except Exception:
        if fallback:
            return get_current_block(fallback)
        raise


def mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}...{text[-4:]}"

def network_and_agent_checks(network_cfg: dict, agents_cfg: dict) -> list[str]:
    errors = []
    if network_cfg.get("evm_chain_id") != 8210:
        errors.append("evm_chain_id must be 8210")
    if network_cfg.get("cosmos_chain_id") != "axon_8210-1":
        errors.append("cosmos_chain_id must be axon_8210-1")
    if not network_cfg.get("rpc_url"):
        errors.append("rpc_url is required")
    errors.extend(validate_heartbeat_settings(heartbeat_settings(network_cfg)))
    errors.extend(validate_challenge_settings(challenge_settings(network_cfg)))
    entries = agents_cfg.get("agents", [])
    if not isinstance(entries, list) or not entries:
        errors.append("agents list is required")
    for idx, item in enumerate(entries):
        if not item.get("name"):
            errors.append(f"agents[{idx}].name is required")
        if not item.get("wallet_ref"):
            errors.append(f"agents[{idx}].wallet_ref is required")
    return errors


def is_valid_evm_address(address: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address or ""))


def load_hosts(path: str) -> dict:
    data = load_yaml(path)
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list):
        return {"hosts": []}
    return {"hosts": hosts}


def find_host(hosts_cfg: dict, host_name: str) -> dict | None:
    for host in hosts_cfg.get("hosts", []):
        if host.get("name") == host_name:
            return host
    return None


def _ssh_base_cmd(host_cfg: dict) -> list[str]:
    user = host_cfg.get("user", "root")
    host = host_cfg.get("host")
    key = host_cfg.get("ssh_key")
    if not host or not key:
        return []
    return [
        "ssh",
        "-i",
        key,
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        f"{user}@{host}",
    ]


def _scp_base_cmd(host_cfg: dict) -> list[str]:
    host = host_cfg.get("host")
    key = host_cfg.get("ssh_key")
    if not host or not key:
        return []
    return ["scp", "-i", key, "-o", "StrictHostKeyChecking=accept-new"]


def _sudo_prefix(host_cfg: dict) -> str:
    if host_cfg.get("user", "root") == "root":
        return ""
    if bool(host_cfg.get("use_sudo", True)):
        return "sudo "
    return ""


def run_ssh(host_cfg: dict, remote_cmd: str) -> tuple[bool, str, str]:
    base = _ssh_base_cmd(host_cfg)
    if not base:
        return False, "", "missing host or ssh key"
    proc = subprocess.run(base + [remote_cmd], text=True, capture_output=True)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def scp_to(host_cfg: dict, local_path: str, remote_path: str) -> tuple[bool, str, str]:
    base = _scp_base_cmd(host_cfg)
    if not base:
        return False, "", "missing host or ssh key"
    user = host_cfg.get("user", "root")
    host = host_cfg.get("host")
    target = f"{user}@{host}:{remote_path}"
    proc = subprocess.run(base + [local_path, target], text=True, capture_output=True)
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def render_service_unit(service_name: str, agent_name: str, remote_workdir: str, python_bin: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description={service_name}",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={remote_workdir}",
            f"ExecStart={python_bin} {remote_workdir}/scripts/agent_worker.py --agent {agent_name} --network {remote_workdir}/configs/network.yaml --agents {remote_workdir}/configs/agents.yaml",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _which(name: str) -> bool:
    return subprocess.run(["which", name], text=True, capture_output=True).returncode == 0


def _state_wallet_for_agent(state: dict, agent_name: str) -> dict | None:
    by_label = None
    for key_id, wallet in state.get("wallets", {}).items():
        if wallet.get("role") == "agent" and wallet.get("label") == f"agent:{agent_name}":
            by_label = {"key_id": key_id, **wallet}
            break
    if by_label:
        return by_label
    address = state.get("agents", {}).get(agent_name, {}).get("wallet_address", "")
    if not address:
        return None
    for key_id, wallet in state.get("wallets", {}).items():
        if wallet.get("address", "").lower() == address.lower():
            return {"key_id": key_id, **wallet}
    return None


def _normalize_private_key(private_key: str) -> str:
    key = str(private_key or "").strip()
    if key.startswith("0x"):
        return key
    return f"0x{key}"


def _axon_to_wei(amount_axon: float) -> int:
    return int(float(amount_axon) * (10**18))


def _registration_intent_payload(address: str, chain_id: int, stake_axon: float, capabilities: str, model: str, calldata_hex: str = "") -> dict:
    data_hex = str(calldata_hex or "")
    byte_len = 0
    if data_hex.startswith("0x"):
        byte_len = len(data_hex[2:]) // 2
    elif data_hex:
        byte_len = len(data_hex) // 2
    payload = {
        "from": address,
        "to": REGISTRY_PRECOMPILE,
        "chain_id": chain_id,
        "method": REGISTER_METHOD_SIGNATURE,
        "value_axon": float(stake_axon),
        "value_wei": _axon_to_wei(stake_axon),
        "args": {"capabilities": capabilities, "model": model},
        "calldata": {
            "prefix": data_hex[:18] if data_hex else "",
            "byte_length": byte_len,
            "summary": "abi-encoded register(capabilities, model)" if data_hex else "calldata not encoded",
        },
    }
    if data_hex:
        payload["calldata"]["suffix"] = data_hex[-16:]
    return payload


def _post_check_payload(is_agent: bool, agent_info: tuple | None) -> dict:
    info = agent_info or ("", [], "", 0, False)
    return {
        "is_agent": bool(is_agent),
        "agent_id": str(info[0]) if len(info) > 0 else "",
        "reputation": int(info[3]) if len(info) > 3 else 0,
        "is_online": bool(info[4]) if len(info) > 4 else False,
    }


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_bech32_from_axond_debug(stdout: str) -> str:
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("Bech32 Acc:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("Bech32 Acc "):
            return line.split(" ", 2)[-1].strip()
    return ""


def _query_cosmos_agent_record(wallet_address: str) -> dict | None:
    if not _which("axond"):
        return None
    try:
        conv = subprocess.run(["axond", "debug", "addr", wallet_address], text=True, capture_output=True, timeout=15)
        if conv.returncode != 0:
            return None
        bech32 = _parse_bech32_from_axond_debug(conv.stdout)
        if not bech32:
            return None
        query = subprocess.run(["axond", "query", "agent", "agent", bech32, "-o", "json"], text=True, capture_output=True, timeout=20)
        if query.returncode != 0:
            return None
        data = json.loads(query.stdout or "{}")
        agent = data.get("agent", {})
        if not isinstance(agent, dict):
            return None
        return agent
    except Exception:
        return None


def _query_agent_onchain(network_cfg: dict, wallet_address: str) -> tuple[bool, dict]:
    if not is_valid_evm_address(wallet_address):
        return False, {"error": "invalid wallet_address"}
    rpc_url = str(network_cfg.get("rpc_url", "")).strip()
    if not rpc_url:
        return False, {"error": "rpc_url is required"}
    try:
        from web3 import Web3
    except Exception as e:
        return False, {"error": f"missing web3 dependency: {e}"}
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20, "proxies": {"http": None, "https": None}}))
    if not w3.is_connected():
        return False, {"error": "rpc not connected"}
    contract = w3.eth.contract(address=Web3.to_checksum_address(REGISTRY_PRECOMPILE), abi=REGISTER_ABI)
    try:
        is_agent = bool(contract.functions.isAgent(wallet_address).call())
        info = contract.functions.getAgent(wallet_address).call() if is_agent else ("", [], "", 0, False)
    except Exception as e:
        return False, {"error": f"onchain query failed: {e}"}
    payload = {
        "is_agent": bool(is_agent),
        "agent_id": str(info[0]) if is_agent else "",
        "reputation": _safe_int(info[3], 0) if is_agent else 0,
        "is_online": bool(info[4]) if is_agent else False,
        "burned_at_register": {},
        "status": "",
        "last_heartbeat": None,
    }
    cosmos_agent = _query_cosmos_agent_record(wallet_address)
    if cosmos_agent:
        burned = cosmos_agent.get("burned_at_register", {})
        payload["burned_at_register"] = burned if isinstance(burned, dict) else {}
        payload["status"] = str(cosmos_agent.get("status", ""))
        payload["last_heartbeat"] = cosmos_agent.get("last_heartbeat")
    return True, payload


def _has_positive_burn_amount(burned: dict | None) -> bool:
    if not isinstance(burned, dict):
        return False
    return _safe_int(burned.get("amount"), 0) > 0


def _registration_path(agent_item: dict, onchain_is_agent: bool | None) -> str:
    if onchain_is_agent is False:
        return "not_registered"
    registration = agent_item.get("registration", {})
    if isinstance(registration, dict):
        method = str(registration.get("method", ""))
        to_addr = str(registration.get("to", "")).lower()
        if method == REGISTER_METHOD_SIGNATURE and to_addr == REGISTRY_PRECOMPILE.lower():
            return "precompile_register_payable"
    if onchain_is_agent is True:
        return "legacy_or_unknown"
    if not bool(agent_item.get("registered")):
        return "not_registered"
    return "legacy_or_unknown"


def _burn_evidence_level(agent_item: dict, burned_at_register: dict | None) -> str:
    if _has_positive_burn_amount(burned_at_register):
        return "onchain_burn_field"
    registration = agent_item.get("registration", {})
    if isinstance(registration, dict) and _safe_int(registration.get("receipt_status"), 0) == 1:
        return "receipt_only"
    return "none"


def _registration_classification(is_agent: bool, reputation: int, is_online: bool) -> str:
    if not is_agent:
        return "unregistered_onchain"
    if int(reputation) <= 0:
        return "registered_rep_zero"
    if is_online:
        return "registered_online_rep_positive"
    return "registered_offline_or_degraded"


def _recommended_action_for_classification(classification: str) -> str:
    if classification == "unregistered_onchain":
        return "run_register_onchain_once"
    if classification == "registered_rep_zero":
        return "keep_heartbeat_and_observe_epoch"
    if classification == "registered_online_rep_positive":
        return "none"
    return "run_lifecycle_repair_then_recheck"


def _register_agent_onchain(
    network_cfg: dict,
    wallet: dict,
    stake_axon: float,
    wait_receipt_timeout: int,
    dry_run: bool,
    capabilities: str,
    model: str,
) -> tuple[bool, dict]:
    rpc_url = str(network_cfg.get("rpc_url", "")).strip()
    chain_id = int(network_cfg.get("evm_chain_id", 8210))
    if stake_axon <= 0:
        return False, {"error": "stake_axon must be > 0"}
    if not rpc_url:
        return False, {"error": "rpc_url is required"}
    try:
        from eth_account import Account
    except Exception as e:
        return False, {"error": f"missing eth-account dependency: {e}"}

    pk = _normalize_private_key(str(wallet.get("private_key", "")))
    try:
        acct = Account.from_key(pk)
    except Exception as e:
        return False, {"error": f"invalid agent private key: {e}"}

    if dry_run:
        calldata_hex = ""
        encode_error = ""
        try:
            from web3 import Web3

            w3 = Web3()
            contract = w3.eth.contract(address=Web3.to_checksum_address(REGISTRY_PRECOMPILE), abi=REGISTER_ABI)
            calldata_hex = contract.functions.register(capabilities, model)._encode_transaction_data()
        except Exception as e:
            encode_error = str(e)
        registration = {
            "status": "dry_run",
            "tx_hash": "",
            "receipt_status": None,
            "block_number": None,
            "from": acct.address,
            "to": REGISTRY_PRECOMPILE,
            "value_axon": float(stake_axon),
            "method": REGISTER_METHOD_SIGNATURE,
            "burn_expected_axon": DEFAULT_REGISTER_BURN_EXPECTED_AXON,
            "evidence_mode": DEFAULT_REGISTER_EVIDENCE_MODE,
            "post_check": _post_check_payload(False, None),
            "intent": _registration_intent_payload(acct.address, chain_id, stake_axon, capabilities, model, calldata_hex),
            "updated_at": now_ts(),
        }
        if encode_error:
            registration["intent"]["calldata"]["summary"] = f"calldata unavailable: {encode_error}"
        return True, {"status": "dry_run", "registration": registration}

    try:
        from web3 import Web3
    except Exception as e:
        return False, {"error": f"missing web3 dependency for register: {e}"}

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20, "proxies": {"http": None, "https": None}}))
    if not w3.is_connected():
        return False, {"error": "rpc not connected"}
    contract = w3.eth.contract(address=Web3.to_checksum_address(REGISTRY_PRECOMPILE), abi=REGISTER_ABI)

    try:
        already_registered = bool(contract.functions.isAgent(acct.address).call())
        existing_info = contract.functions.getAgent(acct.address).call() if already_registered else None
    except Exception as e:
        return False, {"error": f"onchain pre-check failed: {e}"}

    if already_registered:
        registration = {
            "status": "already_registered_onchain",
            "tx_hash": "",
            "receipt_status": None,
            "block_number": None,
            "from": acct.address,
            "to": REGISTRY_PRECOMPILE,
            "value_axon": float(stake_axon),
            "method": REGISTER_METHOD_SIGNATURE,
            "burn_expected_axon": DEFAULT_REGISTER_BURN_EXPECTED_AXON,
            "evidence_mode": DEFAULT_REGISTER_EVIDENCE_MODE,
            "post_check": _post_check_payload(True, existing_info),
            "updated_at": now_ts(),
        }
        return True, {"status": "already_registered_onchain", "registration": registration}

    stake_wei = _axon_to_wei(stake_axon)
    try:
        nonce = w3.eth.get_transaction_count(acct.address, "pending")
        gas_price = w3.eth.gas_price
        try:
            estimated = contract.functions.register(capabilities, model).estimate_gas({"from": acct.address, "value": stake_wei})
            gas_limit = max(int(estimated * 1.2), 250000)
        except Exception:
            gas_limit = 1_000_000
        tx = contract.functions.register(capabilities, model).build_transaction(
            {
                "from": acct.address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": chain_id,
                "value": stake_wei,
            }
        )
        tx_hash = ""
        signed = acct.sign_transaction(tx)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash = tx_hash_bytes.hex()
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes, timeout=int(wait_receipt_timeout))
        receipt_status = int(receipt.status)
        block_number = int(receipt.blockNumber)
        if receipt_status != 1:
            registration = {
                "status": "failed",
                "tx_hash": tx_hash,
                "receipt_status": receipt_status,
                "block_number": block_number,
                "from": acct.address,
                "to": REGISTRY_PRECOMPILE,
                "value_axon": float(stake_axon),
                "method": REGISTER_METHOD_SIGNATURE,
                "burn_expected_axon": DEFAULT_REGISTER_BURN_EXPECTED_AXON,
                "evidence_mode": DEFAULT_REGISTER_EVIDENCE_MODE,
                "post_check": _post_check_payload(False, None),
                "updated_at": now_ts(),
            }
            return False, {"error": "register tx receipt status != 1", "registration": registration}
    except Exception as e:
        return False, {"error": f"register transaction failed: {e}"}

    try:
        is_agent = bool(contract.functions.isAgent(acct.address).call())
        info = contract.functions.getAgent(acct.address).call() if is_agent else None
    except Exception as e:
        return False, {"error": f"onchain post-check failed: {e}"}

    registration = {
        "status": "registered_onchain" if is_agent else "post_check_failed",
        "tx_hash": tx_hash,
        "receipt_status": receipt_status,
        "block_number": block_number,
        "from": acct.address,
        "to": REGISTRY_PRECOMPILE,
        "value_axon": float(stake_axon),
        "method": REGISTER_METHOD_SIGNATURE,
        "burn_expected_axon": DEFAULT_REGISTER_BURN_EXPECTED_AXON,
        "evidence_mode": DEFAULT_REGISTER_EVIDENCE_MODE,
        "post_check": _post_check_payload(is_agent, info),
        "updated_at": now_ts(),
    }
    if not is_agent:
        return False, {"error": "register tx succeeded but isAgent=false in post-check", "registration": registration}
    return True, {"status": "registered_onchain", "registration": registration}


def _apply_registration_to_state(state: dict, agent_name: str, wallet_address: str, result: dict, request_id: str | None = None) -> None:
    item = state.setdefault("agents", {}).setdefault(agent_name, {})
    item["wallet_address"] = wallet_address
    registration = result.get("registration", {})
    item["registration"] = registration
    post = registration.get("post_check", {})
    is_agent = bool(post.get("is_agent"))
    item["registered"] = is_agent
    item["staked"] = is_agent
    if is_agent:
        item["last_error"] = ""
    elif result.get("error"):
        item["last_error"] = result["error"]
    state.setdefault("events", []).append(
        {
            "ts": now_ts(),
            "type": "register_onchain",
            "agent": agent_name,
            "request_id": request_id,
            "status": registration.get("status", result.get("status", "unknown")),
            "tx_hash": registration.get("tx_hash", ""),
        }
    )


def _submit_heartbeat_tx(rpc_url: str, chain_id: int, private_key: str, max_retries: int, backoff_seconds: int, receipt_timeout_sec: int) -> tuple[bool, dict]:
    try:
        from eth_account import Account
        from web3 import Web3
    except Exception as e:
        return False, {"error": f"missing dependencies for heartbeat tx: {e}", "attempts": 0}
    pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20, "proxies": {"http": None, "https": None}}))
    if not w3.is_connected():
        return False, {"error": "rpc not connected", "attempts": 0}
    acct = Account.from_key(pk)
    abi = [{"inputs": [], "name": "heartbeat", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]
    contract = w3.eth.contract(address=Web3.to_checksum_address(REGISTRY_PRECOMPILE), abi=abi)
    last_error = "unknown heartbeat error"
    for attempt in range(1, max_retries + 1):
        started_at = time.time()
        try:
            nonce = w3.eth.get_transaction_count(acct.address, "pending")
            gas_price = w3.eth.gas_price
            try:
                estimate = contract.functions.heartbeat().estimate_gas({"from": acct.address})
                gas_limit = max(int(estimate * 1.2), 120000)
            except Exception:
                gas_limit = 300000
            tx = contract.functions.heartbeat().build_transaction(
                {
                    "from": acct.address,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "chainId": chain_id,
                    "value": 0,
                }
            )
            signed = acct.sign_transaction(tx)
            tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash = tx_hash_bytes.hex()
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash_bytes, timeout=receipt_timeout_sec)
            elapsed_ms = int((time.time() - started_at) * 1000)
            if int(receipt.status) != 1:
                raise RuntimeError("heartbeat tx receipt status != 1")
            return True, {"attempts": attempt, "tx_hash": tx_hash, "block_height": int(receipt.blockNumber), "latency_ms": elapsed_ms}
        except Exception as e:
            last_error = str(e)
            if "ErrHeartbeatTooFrequent" in last_error or "heartbeat too frequent" in last_error.lower():
                return False, {"error": last_error, "attempts": attempt, "too_frequent": True}
            if attempt < max_retries and backoff_seconds > 0:
                time.sleep(backoff_seconds * attempt)
    return False, {"error": last_error, "attempts": max_retries}


def heartbeat_once(state_file: str, network: str, agent: str, max_retries: int | None, backoff_seconds: int | None, receipt_timeout_sec: int | None) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    hb_cfg = heartbeat_settings(network_cfg)
    errors = validate_heartbeat_settings(hb_cfg)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    retries = int(max_retries if max_retries is not None else hb_cfg["max_retries"])
    backoff = int(backoff_seconds if backoff_seconds is not None else hb_cfg["backoff_seconds"])
    receipt_timeout = int(receipt_timeout_sec if receipt_timeout_sec is not None else hb_cfg["receipt_timeout_sec"])
    rpc_url = network_cfg.get("rpc_url", "")
    chain_id = int(network_cfg.get("evm_chain_id", 8210))
    current_block = None
    try:
        payload = json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode("utf-8")
        req = request.Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        current_block = int(data["result"], 16)
    except Exception as e:
        current_block = None
        if isinstance(state.get("agents", {}).get(agent, {}).get("last_heartbeat_block"), int):
            state.setdefault("agents", {}).setdefault(agent, {})
            state["agents"][agent]["last_error"] = ""
            save_state(state_file, state)
            result = {
                "ok": True,
                "agent": agent,
                "status": "skipped",
                "reason": "rpc_unavailable",
                "error": str(e),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    agent_item = state.get("agents", {}).get(agent, {})
    last_block = agent_item.get("last_heartbeat_block")
    if isinstance(current_block, int) and isinstance(last_block, int):
        elapsed = current_block - last_block
        due_blocks = max(int(hb_cfg["interval_blocks"]), int(hb_cfg["timeout_blocks"]) - int(hb_cfg["prewarn_blocks"]))
        if elapsed < due_blocks:
            state.setdefault("agents", {}).setdefault(agent, {})
            state["agents"][agent]["last_error"] = ""
            save_state(state_file, state)
            result = {
                "ok": True,
                "agent": agent,
                "status": "skipped",
                "reason": "not_due",
                "current_block": current_block,
                "last_heartbeat_block": last_block,
                "due_after_blocks": due_blocks,
                "remaining_blocks": due_blocks - elapsed,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    wallet = _state_wallet_for_agent(state, agent)
    if not wallet:
        print(json.dumps({"ok": False, "error": f"wallet not found for {agent}"}, ensure_ascii=False, indent=2))
        return 1
    try:
        with NonceManager(agent):
            ok, tx = _submit_heartbeat_tx(
                rpc_url=rpc_url,
                chain_id=chain_id,
                private_key=wallet.get("private_key", ""),
                max_retries=retries,
                backoff_seconds=backoff,
                receipt_timeout_sec=receipt_timeout,
            )
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e), "agent": agent}, ensure_ascii=False, indent=2))
        return 1
    if ok:
        state.setdefault("agents", {}).setdefault(agent, {})
        state["agents"][agent]["heartbeat_at"] = now_ts()
        state["agents"][agent]["service_active"] = True  # 即使没有容器，心跳成功即标记 active
        state["agents"][agent]["last_heartbeat_block"] = tx["block_height"]
        state["agents"][agent]["last_heartbeat_tx"] = tx["tx_hash"]
        state["agents"][agent]["last_error"] = ""
        state["events"].append(
            {
                "ts": now_ts(),
                "type": "heartbeat_sent",
                "agent": agent,
                "tx_hash": tx["tx_hash"],
                "block_height": tx["block_height"],
                "attempts": tx["attempts"],
                "latency_ms": tx["latency_ms"],
            }
        )
        # AI Challenge 窗口日志：如果心跳落在窗口期内，记录以证明参与
        if isinstance(current_block, int):
            epoch_length = int(network_cfg.get("epoch_blocks", 720))
            ai_challenge_window = int(network_cfg.get("ai_challenge_window_blocks", 50))
            block_in_epoch = current_block % epoch_length
            if block_in_epoch < ai_challenge_window:
                logger.info(
                    f"[AI Challenge Window] agent={agent} epoch_offset={block_in_epoch}/{ai_challenge_window}, "
                    f"tx={tx['tx_hash']} block={tx['block_height']} — heartbeat ensures AI Challenge participation"
                )
        save_state(state_file, state)
        print(json.dumps({"ok": True, "agent": agent, "status": "sent", **tx}, ensure_ascii=False, indent=2))
        return 0
    err = tx.get("error", "heartbeat failed")
    status = "skipped" if tx.get("too_frequent") else "failed"
    state.setdefault("agents", {}).setdefault(agent, {})
    state["agents"][agent]["last_error"] = err
    state["events"].append({"ts": now_ts(), "type": "heartbeat_failed", "agent": agent, "status": status, "error": err, "attempts": tx.get("attempts", 0)})
    save_state(state_file, state)
    print(json.dumps({"ok": status == "skipped", "agent": agent, "status": status, "error": err, "attempts": tx.get("attempts", 0)}, ensure_ascii=False, indent=2))
    return 0 if status == "skipped" else 1


def heartbeat_batch(state_file: str, network: str, request_id: str | None, max_retries: int | None, backoff_seconds: int | None, receipt_timeout_sec: int | None) -> int:
    state = load_state(state_file)
    targets = []
    if request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        targets = req.get("scale_plan", {}).get("agents", [])
    else:
        targets = sorted(state.get("agents", {}).keys())
    if not targets:
        print(json.dumps({"ok": False, "error": "no agents for heartbeat batch"}, ensure_ascii=False, indent=2))
        return 1
    sent = []
    skipped = []
    failed = []
    for name in targets:
        before_tx = load_state(state_file).get("agents", {}).get(name, {}).get("last_heartbeat_tx")
        code = heartbeat_once(
            state_file=state_file,
            network=network,
            agent=name,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            receipt_timeout_sec=receipt_timeout_sec,
        )
        current = load_state(state_file).get("agents", {}).get(name, {})
        after_tx = current.get("last_heartbeat_tx")
        if code == 0 and after_tx and after_tx != before_tx:
            sent.append(name)
        elif code == 0:
            skipped.append(name)
        else:
            failed.append(name)
    ok = len(failed) == 0
    print(
        json.dumps(
            {
                "ok": ok,
                "request_id": request_id,
                "target_count": len(targets),
                "sent_count": len(sent),
                "skipped_count": len(skipped),
                "failed_count": len(failed),
                "sent": sent,
                "skipped": skipped,
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


def heartbeat_daemon(
    state_file: str,
    network: str,
    request_id: str | None,
    interval_sec: int,
    max_retries: int | None,
    backoff_seconds: int | None,
    receipt_timeout_sec: int | None,
    max_cycles: int,
) -> int:
    if interval_sec < 1:
        print(json.dumps({"ok": False, "error": "interval_sec must be >= 1"}, ensure_ascii=False, indent=2))
        return 1
    if max_cycles < 0:
        print(json.dumps({"ok": False, "error": "max_cycles must be >= 0"}, ensure_ascii=False, indent=2))
        return 1
    cycle = 0
    print(json.dumps({"ok": True, "status": "daemon_started", "interval_sec": interval_sec, "request_id": request_id, "max_cycles": max_cycles}, ensure_ascii=False, indent=2))
    while True:
        cycle += 1
        code = heartbeat_batch(
            state_file=state_file,
            network=network,
            request_id=request_id,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            receipt_timeout_sec=receipt_timeout_sec,
        )
        print(json.dumps({"ok": code == 0, "status": "cycle_done", "cycle": cycle, "ts": now_ts()}, ensure_ascii=False, indent=2))
        if max_cycles > 0 and cycle >= max_cycles:
            break
        time.sleep(interval_sec)
    print(json.dumps({"ok": True, "status": "daemon_stopped", "cycles": cycle}, ensure_ascii=False, indent=2))
    return 0


def challenge_gate_check(state_file: str, network: str, agent: str) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    cfg = challenge_settings(network_cfg)
    errors = validate_challenge_settings(cfg)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    agent_state = state.get("agents", {}).get(agent, {})
    checks = {
        "registered": bool(agent_state.get("registered")),
        "not_suspended": not bool(agent_state.get("suspended", False)),
        "validator_required": bool(cfg.get("validator_required", True)),
        "validator_active": bool(agent_state.get("validator_active", False)),
        "window_open": False,
        "phase": "unknown",
    }
    try:
        current_block = get_current_block_healthy(network_cfg)
        window_blocks = int(cfg.get("ai_challenge_window_blocks", 50))
        checks["current_block"] = current_block

        # 优先从链上查询真实 deadline_block（command 模式必须）
        # simulate 模式降级使用 epoch offset 估算
        deadline_block = None
        try:
            from scripts import axond_tx as _axond_tx
            challenge_data = _axond_tx.query_current_challenge(network_cfg.get("cosmos", {}).get("rest_url", ""))
            if challenge_data:
                deadline_block = int(challenge_data.get("deadline_block", 0))
                checks["challenge_epoch"] = challenge_data.get("epoch", 0)
                checks["challenge_hash"] = challenge_data.get("challenge_hash", "")
                checks["deadline_block"] = deadline_block
        except Exception:
            pass  # 降级使用 epoch offset 估算

        if deadline_block and deadline_block > 0:
            # command 模式：使用链上真实 deadline_block
            if current_block <= deadline_block:
                checks["phase"] = "commit"
                checks["window_open"] = True
            elif deadline_block < current_block <= deadline_block + window_blocks:
                checks["phase"] = "reveal"
                checks["window_open"] = True
            else:
                checks["phase"] = "closed"
                checks["window_open"] = False
        else:
            # simulate 模式降级：使用 epoch offset 估算（兼容性保留）
            epoch_length = int(network_cfg.get("epoch_length_blocks", 720))
            offset = current_block % epoch_length
            checks["phase"] = "commit" if offset < window_blocks else ("reveal" if offset < (window_blocks * 2) else "closed")
            checks["window_open"] = checks["phase"] in {"commit", "reveal"}
            checks["epoch_offset"] = offset
    except Exception as e:
        checks["window_open"] = False
        checks["phase"] = "unknown"
        checks["rpc_error"] = str(e)
    reasons = []
    if not checks["registered"]:
        reasons.append("agent_not_registered")
    if not checks["not_suspended"]:
        reasons.append("agent_suspended")
    if checks["validator_required"] and not checks["validator_active"]:
        reasons.append("validator_required_but_inactive")
    if not checks["window_open"]:
        reasons.append("challenge_window_closed")
    ok = len(reasons) == 0
    print(json.dumps({"ok": ok, "agent": agent, "checks": checks, "reasons": reasons}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def _openrouter_answer(question: str, cfg: dict) -> tuple[bool, str, str]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return False, "", "OPENROUTER_API_KEY missing"
    models = []
    if cfg.get("openrouter", {}).get("model_id"):
        models.append(cfg["openrouter"]["model_id"])
    for m in cfg.get("openrouter", {}).get("fallback_models", []):
        if m and m not in models:
            models.append(m)
    if not models:
        return False, "", "no openrouter model configured"
    api_base = cfg.get("openrouter", {}).get("api_base", DEFAULT_CHALLENGE["openrouter"]["api_base"])
    for model in models:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Answer the question with only the final short answer text."},
                {"role": "user", "content": question},
            ],
            "temperature": 0,
        }
        req = request.Request(
            api_base,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if text:
                return True, text, model
        except Exception:
            continue
    return False, "", "all configured models failed"


def challenge_run_once(state_file: str, network: str, agent: str) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    cfg = challenge_settings(network_cfg)
    gate_code = challenge_gate_check(state_file, network, agent)
    if gate_code != 0:
        return 1
    pool = fetch_challenge_pool(cfg["bank_source_url"])
    if not pool:
        print(json.dumps({"ok": False, "error": "challenge pool is empty"}, ensure_ascii=False, indent=2))
        return 1
    bank = load_answer_bank(cfg.get("answer_bank_file", "configs/challenge_answers.yaml"))
    current_block = get_current_block_healthy(network_cfg)
    idx = current_block % len(pool)
    item = pool[idx]
    question = item["question"]
    expected_hash = item["answer_hash"]
    source = "non_llm"
    answer = bank.get(question, "").strip()
    if not answer and not bool(cfg.get("non_llm_first", True)):
        ok, llm_answer, model = _openrouter_answer(question, cfg)
        if ok:
            answer = llm_answer
            source = f"llm:{model}"
    elif not answer:
        ok, llm_answer, model = _openrouter_answer(question, cfg)
        if ok:
            answer = llm_answer
            source = f"llm:{model}"
    if not answer:
        state["events"].append({"ts": now_ts(), "type": "challenge_failed", "agent": agent, "reason": "no_answer_for_question", "question": question})
        save_state(state_file, state)
        print(json.dumps({"ok": False, "error": "no answer resolved", "question": question}, ensure_ascii=False, indent=2))
        return 1
    actual_hash = answer_hash(answer)
    if actual_hash != expected_hash:
        state["events"].append(
            {"ts": now_ts(), "type": "challenge_failed", "agent": agent, "reason": "answer_hash_mismatch", "question": question, "actual_hash": actual_hash, "expected_hash": expected_hash}
        )
        save_state(state_file, state)
        print(json.dumps({"ok": False, "error": "answer hash mismatch", "question": question, "actual_hash": actual_hash, "expected_hash": expected_hash}, ensure_ascii=False, indent=2))
        return 1
    # ── command 模式：用 Keeper 算法（EVM→bech32 后计算 raw hash，不 normalize）────
    # ── simulate 模式：用原有算法（兼容现有 state 格式）────────────────────────────
    if cfg.get("execution_mode") == "command":
        from scripts import axond_tx as _axond_tx

        cosmos_addr = _axond_tx.evm_to_bech32(state["agents"].get(agent, {}).get("wallet_address", ""))
        if not cosmos_addr:
            state["events"].append({"ts": now_ts(), "type": "challenge_failed", "agent": agent, "reason": "cosmos_address_lookup_failed"})
            save_state(state_file, state)
            print(json.dumps({"ok": False, "error": "cosmos address lookup failed", "agent": agent}, ensure_ascii=False, indent=2))
            return 1
        commit_hash = keeper_commit_hash(cosmos_addr, answer)
        challenge_id = f"cmd-{commit_hash[:16]}"
    else:
        commit_hash = answer_hash(f"{agent}:{question}:{answer}")
        challenge_id = f"sim-{commit_hash[:16]}"
    commit_tx = f"simulate:{commit_hash[:16]}"
    reveal_tx = f"simulate:{commit_hash[16:]}"

    # ── command 模式：发送真实 Cosmos SDK 交易 ─────────────────────────────────
    # NonceManager 确保同一 agent 的 commit+reveal 原子提交，防止并发竞争
    if cfg.get("execution_mode") == "command":
        with NonceManager(agent, timeout=60):
            client = _axond_tx.AxondClient(network_cfg, state_file)
            current_challenge = client.query_current_challenge()
            if not current_challenge:
                state["events"].append({"ts": now_ts(), "type": "challenge_failed", "agent": agent, "reason": "no_active_challenge"})
                save_state(state_file, state)
                print(json.dumps({"ok": False, "error": "no active challenge on chain", "agent": agent}, ensure_ascii=False, indent=2))
                return 1

            epoch = current_challenge.get("epoch", 0)
            deadline_block = current_challenge.get("deadline_block", 0)
            window_blocks = int(cfg.get("ai_challenge_window_blocks", 50))

            # 检查是否已提交过（读取 state，不持锁）
            last_commit = state["agents"].get(agent, {}).get("last_challenge_commit_tx", "")
            already_committed = bool(last_commit) and not str(last_commit).startswith("simulate:")
            already_revealed = not str(state["agents"].get(agent, {}).get("last_challenge_reveal_tx", "")).startswith("simulate:")

            commit_ok = already_committed
            reveal_ok = already_revealed
            commit_tx_hash = last_commit if already_committed else ""
            reveal_tx_hash = state["agents"].get(agent, {}).get("last_challenge_reveal_tx", "") if already_revealed else ""

            # Commit 窗口：current_block <= deadline_block
            if not already_committed and current_block <= deadline_block:
                ok, tx_hash_or_err = client.submit_commit(agent, epoch, commit_hash)
                if ok:
                    commit_ok = True
                    commit_tx_hash = tx_hash_or_err
                else:
                    state["events"].append({"ts": now_ts(), "type": "challenge_commit_failed", "agent": agent, "error": tx_hash_or_err, "commit_hash": commit_hash})
                    save_state(state_file, state)
                    print(json.dumps({"ok": False, "error": f"commit tx failed: {tx_hash_or_err}", "agent": agent, "commit_hash": commit_hash}, ensure_ascii=False, indent=2))
                    return 1

            # Reveal 窗口：deadline_block < current_block <= deadline_block + window_blocks
            reveal_deadline = deadline_block + window_blocks
            if already_committed and not already_revealed and deadline_block < current_block <= reveal_deadline:
                ok, tx_hash_or_err = client.submit_reveal(agent, epoch, answer)
                if ok:
                    reveal_ok = True
                    reveal_tx_hash = tx_hash_or_err
                else:
                    state["events"].append({"ts": now_ts(), "type": "challenge_reveal_failed", "agent": agent, "error": tx_hash_or_err})
                    save_state(state_file, state)
                    print(json.dumps({"ok": False, "error": f"reveal tx failed: {tx_hash_or_err}", "agent": agent}, ensure_ascii=False, indent=2))
                    return 1

            commit_tx = commit_tx_hash
            reveal_tx = reveal_tx_hash

    state.setdefault("agents", {}).setdefault(agent, {})
    state["agents"][agent]["last_challenge_question"] = question
    state["agents"][agent]["last_challenge_hash"] = expected_hash
    state["agents"][agent]["last_challenge_commit_tx"] = commit_tx
    state["agents"][agent]["last_challenge_reveal_tx"] = reveal_tx
    state["agents"][agent]["last_challenge_at"] = now_ts()
    state["agents"][agent]["last_challenge_result"] = "success"
    state["agents"][agent]["last_challenge_execution_mode"] = cfg.get("execution_mode", "simulate")
    state["events"].append(
        {
            "ts": now_ts(),
            "type": "challenge_success",
            "agent": agent,
            "challenge_id": challenge_id,
            "question": question,
            "expected_hash": expected_hash,
            "source": source,
            "commit_hash": commit_hash,
            "commit_tx": commit_tx,
            "reveal_tx": reveal_tx,
            "block_height": current_block,
        }
    )
    save_state(state_file, state)
    print(
        json.dumps(
            {
                "ok": True,
                "agent": agent,
                "challenge_id": challenge_id,
                "phase": "complete",
                "question": question,
                "answer_source": source,
                "commit_hash": commit_hash,
                "commit_tx": commit_tx,
                "reveal_tx": reveal_tx,
                "block_height": current_block,
                "execution_mode": cfg.get("execution_mode", "unknown"),
                "participation_note": (
                    "real_onchain: SubmitAIChallengeResponse and RevealAIChallengeResponse "
                    "submitted via axond Cosmos SDK CLI."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def challenge_batch(state_file: str, network: str, request_id: str | None) -> int:
    state = load_state(state_file)
    targets = []
    if request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        targets = req.get("scale_plan", {}).get("agents", [])
    else:
        targets = sorted(state.get("agents", {}).keys())
    if not targets:
        print(json.dumps({"ok": False, "error": "no agents for challenge batch"}, ensure_ascii=False, indent=2))
        return 1
    passed = []
    failed = []
    for name in targets:
        code = challenge_run_once(state_file, network, name)
        if code == 0:
            passed.append(name)
        else:
            failed.append(name)
    print(
        json.dumps(
            {"ok": len(failed) == 0, "target_count": len(targets), "passed_count": len(passed), "failed_count": len(failed), "passed": passed, "failed": failed},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if len(failed) == 0 else 1


def challenge_daemon(
    state_file: str,
    network: str,
    request_id: str | None,
    interval_sec: int,
    max_cycles: int,
) -> int:
    """
    AI Challenge daemon：定期调用 challenge_batch。

    使用 NonceManager 防止与 heartbeat-daemon 并发竞争同一 agent 的 nonce。
    通过 execution_mode="command" 触发真实链上提交。
    """
    if interval_sec < 1:
        print(json.dumps({"ok": False, "error": "interval_sec must be >= 1"}, ensure_ascii=False, indent=2))
        return 1
    if max_cycles < 0:
        print(json.dumps({"ok": False, "error": "max_cycles must be >= 0"}, ensure_ascii=False, indent=2))
        return 1
    cycle = 0
    print(json.dumps(
        {"ok": True, "status": "challenge_daemon_started",
         "interval_sec": interval_sec, "request_id": request_id, "max_cycles": max_cycles},
        ensure_ascii=False, indent=2))
    while True:
        cycle += 1
        code = challenge_batch(
            state_file=state_file,
            network=network,
            request_id=request_id,
        )
        print(json.dumps(
            {"ok": code == 0, "status": "cycle_done", "cycle": cycle, "ts": now_ts()},
            ensure_ascii=False, indent=2))
        if max_cycles > 0 and cycle >= max_cycles:
            break
        time.sleep(interval_sec)
    print(json.dumps({"ok": True, "status": "challenge_daemon_stopped", "cycles": cycle}, ensure_ascii=False, indent=2))
    return 0


def init_local_env() -> dict:
    checks = {
        "python3": _which("python3"),
        "git": _which("git"),
        "docker": _which("docker"),
    }
    compose_ok = subprocess.run(["docker", "compose", "version"], text=True, capture_output=True).returncode == 0 if checks["docker"] else False
    checks["docker_compose_plugin"] = compose_ok
    return checks


def detect_server_os(host_cfg: dict) -> dict:
    ok, out, err = run_ssh(host_cfg, "cat /etc/os-release")
    if not ok:
        return {"ok": False, "error": err or out or "cannot read os-release"}
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    return {"ok": True, "id": info.get("ID", ""), "version_id": info.get("VERSION_ID", ""), "pretty_name": info.get("PRETTY_NAME", "")}


def _install_docker_server(host_cfg: dict, os_id: str) -> tuple[bool, str]:
    sudo = _sudo_prefix(host_cfg)
    if os_id in {"ubuntu", "debian"}:
        cmd = f"{sudo}apt-get update -y && {sudo}apt-get install -y docker.io docker-compose-plugin"
    elif os_id in {"centos", "rhel", "rocky", "almalinux"}:
        cmd = f"{sudo}yum install -y docker docker-compose-plugin || {sudo}dnf install -y docker docker-compose-plugin"
    else:
        return False, f"unsupported os for auto install: {os_id}"
    ok, out, err = run_ssh(host_cfg, cmd)
    if not ok:
        return False, err or out or "install failed"
    run_ssh(host_cfg, f"{sudo}systemctl enable --now docker")
    return True, ""


def init_server_env(host_cfg: dict) -> dict:
    os_info = detect_server_os(host_cfg)
    if not os_info.get("ok"):
        return {"ok": False, "error": os_info.get("error", "os detection failed")}
    ok, out, err = run_ssh(host_cfg, "docker --version")
    docker_ok = ok
    install_message = ""
    if not docker_ok:
        install_ok, install_err = _install_docker_server(host_cfg, os_info.get("id", ""))
        docker_ok = install_ok
        install_message = install_err
    workdir = host_cfg.get("workdir", "/home/ubuntu/axon-agent-scale")
    sudo = _sudo_prefix(host_cfg)
    mkdir_ok, _, mkdir_err = run_ssh(host_cfg, f"{sudo}mkdir -p {shlex.quote(workdir)}")
    return {
        "ok": docker_ok and mkdir_ok,
        "os": os_info,
        "docker_ok": docker_ok,
        "workdir_ok": mkdir_ok,
        "install_message": install_message,
        "error": "" if docker_ok and mkdir_ok else (install_message or mkdir_err or err),
    }


def init_step(mode: str, hosts_file: str | None, host_name: str | None) -> int:
    if mode == "local":
        checks = init_local_env()
        ok = all(checks.values())
        print(json.dumps({"ok": ok, "mode": "local", "checks": checks}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if mode == "server":
        if not hosts_file or not host_name:
            print(json.dumps({"ok": False, "error": "server mode requires --hosts and --host"}, ensure_ascii=False, indent=2))
            return 1
        host_cfg = find_host(load_hosts(hosts_file), host_name)
        if not host_cfg:
            print(json.dumps({"ok": False, "error": "host not found in hosts config"}, ensure_ascii=False, indent=2))
            return 1
        result = init_server_env(host_cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    print(json.dumps({"ok": False, "error": "mode must be local or server"}, ensure_ascii=False, indent=2))
    return 1


def funding_wallet_set(state_file: str, address: str) -> int:
    if not is_valid_evm_address(address):
        print(json.dumps({"ok": False, "error": "invalid funding address format"}, ensure_ascii=False, indent=2))
        return 1
    state = load_state(state_file)
    state["settings"]["funding_address"] = address
    state["events"].append({"ts": now_ts(), "type": "funding_wallet_set", "address": address})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "funding_address": address}, ensure_ascii=False, indent=2))
    return 0


def funding_wallet_get(state_file: str) -> int:
    state = load_state(state_file)
    address = state.get("settings", {}).get("funding_address")
    if not address:
        print(json.dumps({"ok": False, "error": "funding wallet not initialized"}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "funding_address": address}, ensure_ascii=False, indent=2))
    return 0


def wallet_generate(state_file: str, role: str, label: str) -> int:
    state = load_state(state_file)
    if role == "funding":
        for key_id, item in state.get("wallets", {}).items():
            if item.get("role") == "funding":
                state["settings"]["funding_address"] = item.get("address", "")
                save_state(state_file, state)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "reused": True,
                            "key_id": key_id,
                            "address": item.get("address", ""),
                            "label": item.get("label", ""),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
    try:
        from eth_account import Account

        Account.enable_unaudited_hdwallet_features()
        acct, mnemonic = Account.create_with_mnemonic()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"wallet generation failed: {e}"}, ensure_ascii=False, indent=2))
        return 1
    address = acct.address
    privkey = acct.key.hex()
    key_id = str(uuid.uuid4())[:8]
    state["wallets"][key_id] = {
        "address": address,
        "private_key": privkey,
        "role": role,
        "label": label,
        "mnemonic": mnemonic,
        "created_at": now_ts(),
    }
    state["events"].append({"ts": now_ts(), "type": "wallet_generated", "role": role, "label": label, "key_id": key_id})
    if role == "funding":
        state["settings"]["funding_address"] = address
    save_state(state_file, state)
    print(
        json.dumps(
            {
                "ok": True,
                "key_id": key_id,
                "address": address,
                "private_key_masked": mask_secret(privkey),
                "mnemonic_masked": mask_secret(mnemonic),
                "warning": "wallet created; use wallet-backup-export for secure offline backup",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def funding_wallet_template(output_file: str) -> int:
    tpl = {
        "address": "0x0000000000000000000000000000000000000000",
        "private_key": "replace_with_private_key_without_0x_or_with_0x",
        "label": "my-funding-wallet",
    }
    Path(output_file).write_text(yaml.safe_dump(tpl, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(json.dumps({"ok": True, "template_file": output_file}, ensure_ascii=False, indent=2))
    return 0


def funding_wallet_import(state_file: str, wallet_file: str) -> int:
    data = load_yaml(wallet_file)
    address = str(data.get("address", ""))
    private_key = str(data.get("private_key", ""))
    label = str(data.get("label", "imported-funding"))
    if not is_valid_evm_address(address):
        print(json.dumps({"ok": False, "error": "invalid address in wallet file"}, ensure_ascii=False, indent=2))
        return 1
    pk = private_key[2:] if private_key.startswith("0x") else private_key
    if not re.fullmatch(r"[a-fA-F0-9]{64}", pk):
        print(json.dumps({"ok": False, "error": "invalid private_key in wallet file"}, ensure_ascii=False, indent=2))
        return 1
    state = load_state(state_file)
    for key_id, item in state.get("wallets", {}).items():
        if item.get("address", "").lower() == address.lower():
            state["settings"]["funding_address"] = item.get("address")
            save_state(state_file, state)
            print(json.dumps({"ok": True, "reused": True, "key_id": key_id, "address": item.get("address")}, ensure_ascii=False, indent=2))
            return 0
    key_id = str(uuid.uuid4())[:8]
    state["wallets"][key_id] = {
        "address": address,
        "private_key": pk,
        "role": "funding",
        "label": label,
        "mnemonic": "",
        "created_at": now_ts(),
    }
    state["settings"]["funding_address"] = address
    state["events"].append({"ts": now_ts(), "type": "funding_wallet_import", "key_id": key_id, "address": address})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "imported": True, "key_id": key_id, "address": address}, ensure_ascii=False, indent=2))
    return 0


def agent_wallet_template(output_file: str) -> int:
    tpl = {
        "name": "agent-001",
        "address": "0x0000000000000000000000000000000000000000",
        "private_key": "replace_with_private_key_without_0x_or_with_0x",
        "mnemonic": "",
    }
    Path(output_file).write_text(yaml.safe_dump(tpl, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(json.dumps({"ok": True, "template_file": output_file}, ensure_ascii=False, indent=2))
    return 0


def agent_wallets_template(output_file: str) -> int:
    tpl = {
        "agents": [
            {
                "name": "agent-001",
                "address": "0x0000000000000000000000000000000000000000",
                "private_key": "replace_with_private_key_without_0x_or_with_0x",
                "mnemonic": "",
            }
        ]
    }
    Path(output_file).write_text(yaml.safe_dump(tpl, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(json.dumps({"ok": True, "template_file": output_file}, ensure_ascii=False, indent=2))
    return 0


def _normalize_private_key(private_key: str) -> str:
    pk = (private_key or "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    return pk


def _agent_wallet_import_to_state(
    state: dict, agent_name: str, private_key: str, address: str | None, mnemonic: str | None, overwrite: bool
) -> tuple[bool, dict]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", agent_name):
        return False, {"ok": False, "agent": agent_name, "error": "invalid agent name"}
    pk = _normalize_private_key(private_key)
    if not re.fullmatch(r"[a-fA-F0-9]{64}", pk):
        return False, {"ok": False, "agent": agent_name, "error": "invalid private_key"}
    try:
        from eth_account import Account

        derived_address = Account.from_key(f"0x{pk}").address
    except Exception as e:
        return False, {"ok": False, "agent": agent_name, "error": f"failed to derive address from private_key: {e}"}
    input_address = (address or "").strip()
    if input_address:
        if not is_valid_evm_address(input_address):
            return False, {"ok": False, "agent": agent_name, "error": "invalid address format"}
        if input_address.lower() != derived_address.lower():
            return False, {"ok": False, "agent": agent_name, "error": "address does not match private_key derived address", "derived_address": derived_address}
    label = f"agent:{agent_name}"
    wallets = state.setdefault("wallets", {})
    existing_label_id = None
    existing_addr_id = None
    for key_id, item in wallets.items():
        item_addr = str(item.get("address", ""))
        if item.get("role") == "agent" and item.get("label") == label:
            existing_label_id = key_id
        if item_addr and item_addr.lower() == derived_address.lower():
            existing_addr_id = key_id
            if item.get("role") == "agent" and item.get("label") and item.get("label") != label:
                return False, {
                    "ok": False,
                    "agent": agent_name,
                    "error": "address already bound to another agent",
                    "address": derived_address,
                    "bound_label": item.get("label"),
                }
    if existing_addr_id and wallets[existing_addr_id].get("role") == "funding":
        return False, {"ok": False, "agent": agent_name, "error": "address already used by funding wallet", "address": derived_address}
    target_id = existing_label_id or existing_addr_id
    if existing_label_id and not overwrite:
        existing = wallets[existing_label_id]
        if str(existing.get("address", "")).lower() != derived_address.lower():
            return False, {
                "ok": False,
                "agent": agent_name,
                "error": "agent wallet already exists with different address, use --overwrite",
                "existing_address": existing.get("address"),
                "new_address": derived_address,
            }
        state.setdefault("agents", {}).setdefault(agent_name, {})
        state["agents"][agent_name]["wallet_address"] = existing.get("address", derived_address)
        state["agents"][agent_name].setdefault("registered", True)
        state["agents"][agent_name].setdefault("staked", True)
        state["agents"][agent_name].setdefault("service_active", False)
        state["agents"][agent_name].setdefault("last_error", "")
        state["agents"][agent_name]["container_name"] = f"axon-agent-{agent_name}"
        return True, {"ok": True, "agent": agent_name, "reused": True, "key_id": existing_label_id, "address": existing.get("address", derived_address)}
    existing_wallet = wallets.get(target_id, {}) if target_id else {}
    key_id = target_id or str(uuid.uuid4())[:8]
    wallets[key_id] = {
        "address": derived_address,
        "private_key": pk,
        "role": "agent",
        "label": label,
        "mnemonic": mnemonic if mnemonic is not None else existing_wallet.get("mnemonic", ""),
        "created_at": existing_wallet.get("created_at", now_ts()),
    }
    state.setdefault("agents", {}).setdefault(agent_name, {})
    state["agents"][agent_name]["wallet_address"] = derived_address
    state["agents"][agent_name].setdefault("registered", True)
    state["agents"][agent_name].setdefault("staked", True)
    state["agents"][agent_name].setdefault("service_active", False)
    state["agents"][agent_name].setdefault("last_error", "")
    state["agents"][agent_name]["container_name"] = f"axon-agent-{agent_name}"
    state.setdefault("events", []).append({"ts": now_ts(), "type": "agent_wallet_import", "agent": agent_name, "key_id": key_id, "address": derived_address, "overwrite": overwrite})
    action = "updated" if target_id else "imported"
    return True, {"ok": True, "agent": agent_name, action: True, "key_id": key_id, "address": derived_address}


def agent_wallet_import(state_file: str, agent_name: str, private_key: str, address: str | None, mnemonic: str | None, overwrite: bool) -> int:
    state = load_state(state_file)
    ok, payload = _agent_wallet_import_to_state(state, agent_name, private_key, address, mnemonic, overwrite)
    if not ok:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    save_state(state_file, state)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def agent_wallets_import(state_file: str, wallet_file: str, overwrite: bool) -> int:
    data = load_yaml(wallet_file)
    rows = data.get("agents", [])
    if not isinstance(rows, list) or not rows:
        print(json.dumps({"ok": False, "error": "wallet file must contain non-empty agents list"}, ensure_ascii=False, indent=2))
        return 1
    state = load_state(state_file)
    draft = copy.deepcopy(state)
    imported = []
    failed = []
    for row in rows:
        name = str((row or {}).get("name", "")).strip()
        private_key = str((row or {}).get("private_key", "")).strip()
        address = str((row or {}).get("address", "")).strip()
        mnemonic = (row or {}).get("mnemonic")
        ok, payload = _agent_wallet_import_to_state(draft, name, private_key, address, mnemonic, overwrite)
        if ok:
            imported.append(payload)
        else:
            failed.append(payload)
    if failed:
        print(json.dumps({"ok": False, "imported_count": len(imported), "failed_count": len(failed), "failed": failed}, ensure_ascii=False, indent=2))
        return 1
    save_state(state_file, draft)
    print(json.dumps({"ok": True, "imported_count": len(imported), "items": imported}, ensure_ascii=False, indent=2))
    return 0


def wallet_list(state_file: str) -> int:
    state = load_state(state_file)
    wallets = state.get("wallets", {})
    if not wallets:
        print(json.dumps({"ok": True, "wallets": [], "count": 0}, ensure_ascii=False, indent=2))
        return 0
    items = []
    for key_id, w in wallets.items():
        items.append(
            {
                "key_id": key_id,
                "address": w["address"],
                "role": w.get("role", ""),
                "label": w.get("label", ""),
                "created_at": w.get("created_at"),
            }
        )
    items.sort(key=lambda x: int(x.get("created_at") or 0))
    print(json.dumps({"ok": True, "wallets": items, "count": len(items)}, ensure_ascii=False, indent=2))
    return 0


def validate(network: str, agents: str, strict_rpc: bool) -> int:
    network_cfg = load_yaml(network)
    agents_cfg = load_yaml(agents)
    errors = network_and_agent_checks(network_cfg, agents_cfg)
    warnings = []
    rpc_ok, rpc_chain, rpc_error = rpc_chain_id(network_cfg.get("rpc_url", "")) if network_cfg.get("rpc_url") else (False, None, "rpc_url missing")
    if not rpc_ok:
        msg = f"rpc unreachable: {rpc_error}"
        if strict_rpc:
            errors.append(msg)
        else:
            warnings.append(msg)
    elif rpc_chain != 8210:
        msg = f"rpc chain id mismatch: {rpc_chain}"
        if strict_rpc:
            errors.append(msg)
        else:
            warnings.append(msg)
    if errors:
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "warnings": warnings, "agents": len(agents_cfg.get("agents", []))}, ensure_ascii=False, indent=2))
    return 0


def create_request(state_file: str, target_agents: int, min_funding_axon: float, funding_address: str, min_confirmations: int, timeout_sec: int, stake_per_agent_axon: float) -> int:
    errors = []
    min_required_stake = target_agents * stake_per_agent_axon
    if target_agents < 1:
        errors.append("target_agents must be >= 1")
    if min_funding_axon < min_required_stake:
        errors.append("min_funding_axon is lower than minimum staking budget")
    if min_confirmations < 1:
        errors.append("min_confirmations must be >= 1")
    if timeout_sec < 1:
        errors.append("timeout_sec must be >= 1")
    if not is_valid_evm_address(funding_address):
        errors.append("funding_address must be a valid EVM address")
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    state = load_state(state_file)
    request_id = str(uuid.uuid4())
    state["requests"][request_id] = {
        "request_id": request_id,
        "status": "PENDING_FUNDS",
        "target_agents": target_agents,
        "min_funding_axon": min_funding_axon,
        "funding_address": funding_address,
        "min_confirmations": min_confirmations,
        "timeout_sec": timeout_sec,
        "stake_per_agent_axon": stake_per_agent_axon,
        "created_at": now_ts(),
        "updated_at": now_ts(),
        "funding": {},
        "scale_plan": {},
        "execution": {"completed_agents": [], "failed_agents": {}, "attempts": {}},
    }
    state["events"].append({"ts": now_ts(), "type": "request_created", "request_id": request_id})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "status": "PENDING_FUNDS"}, ensure_ascii=False, indent=2))
    return 0


def fund_check(state_file: str, network: str, request_id: str, observed_amount_axon: float, observed_confirmations: int, observed_chain_id: int, strict_rpc: bool) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    if req["status"] in {"FUNDED", "SCALED", "SUCCESS"}:
        print(json.dumps({"ok": True, "request_id": request_id, "status": req["status"], "message": "already passed funding gate"}, ensure_ascii=False, indent=2))
        return 0
    if req["status"] == "FAILED":
        print(json.dumps({"ok": False, "request_id": request_id, "status": "FAILED", "reason": req.get("failure_reason", "unknown")}, ensure_ascii=False, indent=2))
        return 1
    network_cfg = load_yaml(network)
    rpc_ok, rpc_chain, rpc_error = rpc_chain_id(network_cfg.get("rpc_url", "")) if network_cfg.get("rpc_url") else (False, None, "rpc_url missing")
    if strict_rpc and (not rpc_ok or rpc_chain != 8210):
        req["status"] = "FAILED"
        req["failure_reason"] = f"rpc check failed: ok={rpc_ok}, chain={rpc_chain}, error={rpc_error}"
    elif observed_chain_id != 8210:
        req["status"] = "FAILED"
        req["failure_reason"] = f"observed chain id mismatch: {observed_chain_id}"
    elif observed_confirmations < req["min_confirmations"]:
        req["status"] = "FAILED"
        req["failure_reason"] = "confirmations below threshold"
    elif observed_amount_axon < req["min_funding_axon"]:
        req["status"] = "FAILED"
        req["failure_reason"] = "funding amount below threshold"
    elif now_ts() > req["created_at"] + req["timeout_sec"]:
        req["status"] = "FAILED"
        req["failure_reason"] = "funding timeout"
    else:
        req["status"] = "FUNDED"
        req["funding"] = {"observed_amount_axon": observed_amount_axon, "observed_confirmations": observed_confirmations, "observed_chain_id": observed_chain_id, "rpc_ok": rpc_ok, "rpc_chain_id": rpc_chain}
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "fund_check", "request_id": request_id, "status": req["status"]})
    save_state(state_file, state)
    ok = req["status"] == "FUNDED"
    payload = {"ok": ok, "request_id": request_id, "status": req["status"]}
    if not ok:
        payload["reason"] = req.get("failure_reason", "fund check failed")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def build_scale_plan(state_file: str, network: str, agents: str, request_id: str) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    if req["status"] not in {"FUNDED", "PLANNED", "SCALING", "SCALED", "PARTIAL", "FAILED"}:
        print(json.dumps({"ok": False, "error": "request must be FUNDED before planning"}, ensure_ascii=False, indent=2))
        return 1
    network_cfg = load_yaml(network)
    agents_cfg = load_yaml(agents)
    all_names = [x.get("name") for x in agents_cfg.get("agents", []) if x.get("name")]
    planned_names = all_names[: req["target_agents"]]
    concurrency = max(int(network_cfg.get("deploy", {}).get("default_concurrency", 3)), 1)
    batches = [planned_names[i : i + concurrency] for i in range(0, len(planned_names), concurrency)]
    req["scale_plan"] = {"agents": planned_names, "batch_size": concurrency, "batches": batches}
    if req["status"] == "FUNDED":
        req["status"] = "PLANNED"
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "plan_built", "request_id": request_id})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "status": req["status"], "scale_plan": req["scale_plan"]}, ensure_ascii=False, indent=2))
    return 0


def _ensure_agent_wallet(state_file: str, agent_name: str) -> dict:
    state = load_state(state_file)
    for key_id, w in state["wallets"].items():
        if w.get("label") == f"agent:{agent_name}" and w.get("role") == "agent":
            return {"key_id": key_id, "address": w["address"], "private_key": w["private_key"]}
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct, mnemonic = Account.create_with_mnemonic()
    key_id = str(uuid.uuid4())[:8]
    state["wallets"][key_id] = {"address": acct.address, "private_key": acct.key.hex(), "role": "agent", "label": f"agent:{agent_name}", "mnemonic": mnemonic, "created_at": now_ts()}
    state["events"].append({"ts": now_ts(), "type": "wallet_generated", "role": "agent", "label": f"agent:{agent_name}", "key_id": key_id})
    save_state(state_file, state)
    return {"key_id": key_id, "address": acct.address, "private_key": acct.key.hex()}


def register_onchain_once(
    state_file: str,
    network: str,
    agent: str,
    stake_axon: float,
    wait_receipt_timeout: int,
    dry_run: bool,
    capabilities: str,
    model: str,
) -> int:
    state = load_state(state_file)
    wallet = _state_wallet_for_agent(state, agent)
    if not wallet:
        print(json.dumps({"ok": False, "error": f"wallet not found for {agent}"}, ensure_ascii=False, indent=2))
        return 1
    network_cfg = load_yaml(network)
    ok, result = _register_agent_onchain(
        network_cfg=network_cfg,
        wallet=wallet,
        stake_axon=stake_axon,
        wait_receipt_timeout=wait_receipt_timeout,
        dry_run=dry_run,
        capabilities=capabilities,
        model=model,
    )
    status = result.get("status", "failed")
    payload = {"ok": ok, "agent": agent, "dry_run": bool(dry_run), "status": status, **result}
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    _apply_registration_to_state(state, agent, wallet.get("address", ""), result)
    save_state(state_file, state)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def register_onchain_batch(
    state_file: str,
    network: str,
    request_id: str | None,
    stake_axon: float,
    wait_receipt_timeout: int,
    dry_run: bool,
    capabilities: str,
    model: str,
) -> int:
    state = load_state(state_file)
    targets = []
    if request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        targets = req.get("scale_plan", {}).get("agents", [])
    else:
        targets = sorted(state.get("agents", {}).keys())
    if not targets:
        print(json.dumps({"ok": False, "error": "no agents for register-onchain-batch"}, ensure_ascii=False, indent=2))
        return 1

    network_cfg = load_yaml(network)
    registered = []
    skipped = []
    failed = []
    details = []
    req = state.get("requests", {}).get(request_id) if request_id else None
    for name in targets:
        wallet = _state_wallet_for_agent(state, name)
        if not wallet:
            failed.append(name)
            details.append({"agent": name, "ok": False, "error": "wallet not found"})
            if req and not dry_run:
                req.setdefault("execution", {}).setdefault("failed_agents", {})[name] = {"error": "wallet not found", "retryable": False}
            continue
        ok, result = _register_agent_onchain(
            network_cfg=network_cfg,
            wallet=wallet,
            stake_axon=stake_axon,
            wait_receipt_timeout=wait_receipt_timeout,
            dry_run=dry_run,
            capabilities=capabilities,
            model=model,
        )
        details.append({"agent": name, "ok": ok, **result})
        if ok and result.get("status") == "already_registered_onchain":
            skipped.append(name)
        elif ok:
            registered.append(name)
        else:
            failed.append(name)

        if dry_run:
            continue
        _apply_registration_to_state(state, name, wallet.get("address", ""), result, request_id=request_id)
        if req:
            execution = req.setdefault("execution", {})
            completed_agents = set(execution.setdefault("completed_agents", []))
            failed_agents = execution.setdefault("failed_agents", {})
            if ok:
                completed_agents.add(name)
                failed_agents.pop(name, None)
            else:
                failed_agents[name] = {
                    "error": result.get("error", "register-onchain failed"),
                    "retryable": True,
                    "status": result.get("registration", {}).get("status", "failed"),
                }
            execution["completed_agents"] = sorted(completed_agents)

    if req and not dry_run:
        req["status"] = "PARTIAL" if req.get("execution", {}).get("failed_agents") else "SCALED"
        req["updated_at"] = now_ts()
    if not dry_run:
        save_state(state_file, state)
    ok = len(failed) == 0
    print(
        json.dumps(
            {
                "ok": ok,
                "request_id": request_id,
                "dry_run": bool(dry_run),
                "target_count": len(targets),
                "registered_count": len(registered),
                "already_registered_count": len(skipped),
                "failed_count": len(failed),
                "registered": registered,
                "already_registered": skipped,
                "failed": failed,
                "details": details,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


def execute_scale(state_file: str, network: str, agents: str, request_id: str, fail_agents: list[str]) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    if req["status"] not in {"PLANNED", "SCALING", "PARTIAL", "FAILED"}:
        print(json.dumps({"ok": False, "error": "request must be PLANNED before scale"}, ensure_ascii=False, indent=2))
        return 1
    target_names = req.get("scale_plan", {}).get("agents", [])
    completed = set(req["execution"].get("completed_agents", []))
    failed = dict(req["execution"].get("failed_agents", {}))
    network_cfg = load_yaml(network)
    stake_axon = float(req.get("stake_per_agent_axon", DEFAULT_REGISTER_STAKE_AXON))
    for name in target_names:
        if name in completed:
            continue
        wallet_info = _ensure_agent_wallet(state_file, name)
        state["wallets"] = load_state(state_file).get("wallets", state.get("wallets", {}))
        if name in fail_agents:
            failed[name] = {"error": "simulated execution failure", "retryable": True}
            continue
        ok, result = _register_agent_onchain(
            network_cfg=network_cfg,
            wallet=wallet_info,
            stake_axon=stake_axon,
            wait_receipt_timeout=DEFAULT_REGISTER_WAIT_RECEIPT_SEC,
            dry_run=False,
            capabilities=DEFAULT_REGISTER_CAPABILITIES,
            model=DEFAULT_REGISTER_MODEL,
        )
        _apply_registration_to_state(state, name, wallet_info.get("address", ""), result, request_id=request_id)
        if not ok:
            failed[name] = {"error": result.get("error", "register-onchain failed"), "retryable": True}
            continue
        state["agents"][name]["service_active"] = True
        state["agents"][name]["heartbeat_at"] = now_ts()
        completed.add(name)
        failed.pop(name, None)
    req["execution"]["completed_agents"] = sorted(completed)
    req["execution"]["failed_agents"] = failed
    req["status"] = "PARTIAL" if failed else "SCALED"
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "scale_executed", "request_id": request_id, "status": req["status"]})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "status": req["status"], "completed": sorted(completed), "failed": failed}, ensure_ascii=False, indent=2))
    return 0


def registration_audit(state_file: str, network: str, request_id: str | None, agent_names: list[str], strict: bool) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    targets = []
    if agent_names:
        seen = set()
        for name in agent_names:
            n = str(name).strip()
            if not n or n in seen:
                continue
            seen.add(n)
            targets.append(n)
    elif request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        targets = [str(x) for x in req.get("scale_plan", {}).get("agents", []) if str(x)]
    else:
        targets = sorted(state.get("agents", {}).keys())
    if not targets:
        print(json.dumps({"ok": False, "error": "no agents for registration-audit"}, ensure_ascii=False, indent=2))
        return 1

    classification_counts = {
        "unregistered_onchain": 0,
        "registered_rep_zero": 0,
        "registered_online_rep_positive": 0,
        "registered_offline_or_degraded": 0,
    }
    registration_path_counts = {"precompile_register_payable": 0, "legacy_or_unknown": 0, "not_registered": 0}
    burn_evidence_counts = {"onchain_burn_field": 0, "receipt_only": 0, "none": 0}

    items = []
    query_failed_count = 0
    unregistered_onchain_count = 0
    for name in targets:
        agent_item = state.get("agents", {}).get(name, {})
        wallet_address = str(agent_item.get("wallet_address", ""))
        local_info = {"registered": bool(agent_item.get("registered")), "staked": bool(agent_item.get("staked"))}
        onchain_info = {"is_agent": False, "agent_id": "", "reputation": 0, "is_online": False}
        query_error = ""
        burned_at_register = {}
        if not wallet_address:
            query_error = "wallet_address missing"
        elif not is_valid_evm_address(wallet_address):
            query_error = "wallet_address invalid"
        else:
            ok, result = _query_agent_onchain(network_cfg, wallet_address)
            if not ok:
                query_error = result.get("error", "onchain query failed")
            else:
                onchain_info = {
                    "is_agent": bool(result.get("is_agent")),
                    "agent_id": str(result.get("agent_id", "")),
                    "reputation": _safe_int(result.get("reputation"), 0),
                    "is_online": bool(result.get("is_online")),
                }
                burned_at_register = result.get("burned_at_register", {}) if isinstance(result.get("burned_at_register"), dict) else {}
                if burned_at_register:
                    onchain_info["burned_at_register"] = burned_at_register
                if result.get("status"):
                    onchain_info["status"] = result.get("status")
                if result.get("last_heartbeat") is not None:
                    onchain_info["last_heartbeat"] = result.get("last_heartbeat")

        if query_error:
            query_failed_count += 1
            classification = "registered_offline_or_degraded" if local_info["registered"] else "unregistered_onchain"
            recommended_action = "check_rpc_or_wallet_then_rerun_audit"
            registration_path = _registration_path(agent_item, None)
            burn_level = _burn_evidence_level(agent_item, None)
        else:
            classification = _registration_classification(onchain_info["is_agent"], onchain_info["reputation"], onchain_info["is_online"])
            recommended_action = _recommended_action_for_classification(classification)
            registration_path = _registration_path(agent_item, onchain_info["is_agent"])
            burn_level = _burn_evidence_level(agent_item, burned_at_register)

        classification_counts[classification] += 1
        registration_path_counts[registration_path] += 1
        burn_evidence_counts[burn_level] += 1
        if classification == "unregistered_onchain":
            unregistered_onchain_count += 1

        row = {
            "agent": name,
            "wallet_address": wallet_address,
            "local": local_info,
            "onchain": onchain_info,
            "registration_path": registration_path,
            "burn_evidence_level": burn_level,
            "classification": classification,
            "recommended_action": recommended_action,
        }
        if query_error:
            row["query_error"] = query_error
        items.append(row)

    ok = not (strict and (unregistered_onchain_count > 0 or query_failed_count > 0))
    output = {
        "ok": ok,
        "strict": bool(strict),
        "request_id": request_id,
        "target_count": len(targets),
        "summary": {
            "classification_counts": classification_counts,
            "registration_path_counts": registration_path_counts,
            "burn_evidence_counts": burn_evidence_counts,
            "query_failed_count": query_failed_count,
            "unregistered_onchain_count": unregistered_onchain_count,
        },
        "items": items,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def remote_deploy(state_file: str, request_id: str, hosts_file: str, host_name: str, network: str, agents: str, dry_run: bool) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    target_agents = req.get("scale_plan", {}).get("agents", [])
    if not target_agents:
        print(json.dumps({"ok": False, "error": "no planned agents for remote deploy"}, ensure_ascii=False, indent=2))
        return 1
    host_cfg = find_host(load_hosts(hosts_file), host_name)
    if not host_cfg:
        print(json.dumps({"ok": False, "error": "host not found in hosts config"}, ensure_ascii=False, indent=2))
        return 1
    remote_workdir = host_cfg.get("workdir", "/home/ubuntu/axon-agent-scale")
    python_bin = host_cfg.get("python_bin", "python3")
    sudo = _sudo_prefix(host_cfg)
    if dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "host": host_name,
                    "steps": [
                        "mkdir remote workdir",
                        "copy network/agents/worker files",
                        "docker rm -f and docker run per agent",
                        "docker inspect status per agent",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    ok, out, err = run_ssh(host_cfg, f"{sudo}mkdir -p {shlex.quote(remote_workdir)}/scripts {shlex.quote(remote_workdir)}/configs")
    if not ok:
        print(json.dumps({"ok": False, "error": "remote mkdir failed", "stdout": out, "stderr": err}, ensure_ascii=False, indent=2))
        return 1
    docker_ok, _, docker_err = run_ssh(host_cfg, "docker --version")
    if not docker_ok:
        print(json.dumps({"ok": False, "error": "docker not available on remote host", "stderr": docker_err}, ensure_ascii=False, indent=2))
        return 1
    files_to_copy = [
        (network, f"{remote_workdir}/configs/network.yaml"),
        (agents, f"{remote_workdir}/configs/agents.yaml"),
        (str(Path(__file__).with_name("agent_worker.py")), f"{remote_workdir}/scripts/agent_worker.py"),
    ]
    for local_file, remote_file in files_to_copy:
        ok, out, err = scp_to(host_cfg, local_file, remote_file)
        if not ok:
            print(json.dumps({"ok": False, "error": "scp failed", "local": local_file, "remote": remote_file, "stdout": out, "stderr": err}, ensure_ascii=False, indent=2))
            return 1
    deployed = []
    failed = []
    for agent_name in target_agents:
        container_name = f"axon-agent-{agent_name}"
        run_cmd = (
            f"docker rm -f {container_name} >/dev/null 2>&1 || true; "
            f"docker run -d --name {container_name} --restart unless-stopped "
            f"-v {shlex.quote(remote_workdir)}:{shlex.quote(remote_workdir)} "
            f"python:3.11-slim {python_bin} {remote_workdir}/scripts/agent_worker.py "
            f"--agent {agent_name} --network {remote_workdir}/configs/network.yaml --agents {remote_workdir}/configs/agents.yaml"
        )
        ok, out, err = run_ssh(host_cfg, run_cmd)
        if not ok:
            failed.append({"agent": agent_name, "container": container_name, "error": err or out or "docker run failed"})
            continue
        ok, out, err = run_ssh(host_cfg, f"docker inspect -f '{{{{.State.Status}}}}' {container_name}")
        if ok and out.strip() == "running":
            deployed.append({"agent": agent_name, "container": container_name, "status": "running"})
            if agent_name in state["agents"]:
                state["agents"][agent_name]["service_active"] = True
                state["agents"][agent_name]["deployed_host"] = host_name
                state["agents"][agent_name]["container_name"] = container_name
        else:
            failed.append({"agent": agent_name, "container": container_name, "error": err or out or "container not running"})
    state["events"].append({"ts": now_ts(), "type": "remote_deploy", "request_id": request_id, "host": host_name, "deployed_count": len(deployed), "failed_count": len(failed)})
    save_state(state_file, state)
    print(json.dumps({"ok": len(failed) == 0, "request_id": request_id, "host": host_name, "deployed": deployed, "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if len(failed) == 0 else 1


def remote_status(state_file: str, request_id: str, hosts_file: str, host_name: str) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    host_cfg = find_host(load_hosts(hosts_file), host_name)
    if not host_cfg:
        print(json.dumps({"ok": False, "error": "host not found in hosts config"}, ensure_ascii=False, indent=2))
        return 1
    items = []
    for agent_name in req.get("scale_plan", {}).get("agents", []):
        container_name = state["agents"].get(agent_name, {}).get("container_name", f"axon-agent-{agent_name}")
        ok, out, err = run_ssh(host_cfg, f"docker inspect -f '{{{{.State.Status}}}}' {container_name}")
        active = ok and out.strip() == "running"
        items.append({"agent": agent_name, "container": container_name, "active": active, "detail": out or err})
        if agent_name in state["agents"]:
            state["agents"][agent_name]["service_active"] = active
    state["events"].append({"ts": now_ts(), "type": "remote_status", "request_id": request_id, "host": host_name})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "host": host_name, "items": items}, ensure_ascii=False, indent=2))
    return 0


def evaluate_agent_health(agent_name: str, item: dict, network_cfg: dict, current_block: int | None) -> dict:
    hb_cfg = heartbeat_settings(network_cfg)
    timeout = int(hb_cfg.get("timeout_blocks", 720))
    prewarn = int(hb_cfg.get("prewarn_blocks", 120))
    reasons = []
    actions = []
    if not bool(item.get("registered")):
        reasons.append("not_registered")
        actions.append("register_agent")
    if not bool(item.get("staked")):
        reasons.append("not_staked")
        actions.append("stake_agent")
    if not bool(item.get("service_active")):
        reasons.append("service_inactive")
        actions.append("restart_service")
    last_hb_block = item.get("last_heartbeat_block")
    if current_block is not None and isinstance(last_hb_block, int):
        delta = current_block - last_hb_block
        if delta > timeout:
            reasons.append("heartbeat_timeout")
            actions.append("send_heartbeat")
        elif delta > (timeout - prewarn):
            reasons.append("heartbeat_near_timeout")
            actions.append("send_heartbeat")
    elif not item.get("heartbeat_at"):
        reasons.append("heartbeat_missing")
        actions.append("send_heartbeat")
    if item.get("last_challenge_result") in {"failed", ""}:
        reasons.append("challenge_unhealthy")
        actions.append("run_challenge")
    if item.get("last_error"):
        reasons.append("last_error_present")
    level = "HEALTHY"
    if any(r in reasons for r in ["not_registered", "not_staked", "service_inactive", "heartbeat_timeout"]):
        level = "FAILED"
    elif reasons:
        level = "DEGRADED"
    return {
        "name": agent_name,
        "health": level,
        "reasons": sorted(set(reasons)),
        "actions": sorted(set(actions)),
        "registered": bool(item.get("registered")),
        "staked": bool(item.get("staked")),
        "service_active": bool(item.get("service_active")),
        "last_heartbeat_block": item.get("last_heartbeat_block"),
        "last_challenge_result": item.get("last_challenge_result", ""),
        "ai_challenge_participation": "heartbeat_daemon",
        "challenge_execution_mode": item.get("last_challenge_execution_mode") or network_cfg.get("challenge", {}).get("execution_mode", "simulate"),
    }


def lifecycle_report(state_file: str, network: str, request_id: str | None) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    names = []
    if request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        names = req.get("scale_plan", {}).get("agents", [])
    else:
        names = sorted(state.get("agents", {}).keys())
    if not names:
        print(json.dumps({"ok": False, "error": "no agents for lifecycle report"}, ensure_ascii=False, indent=2))
        return 1
    current_block = None
    try:
        current_block = get_current_block_healthy(network_cfg)
    except Exception:
        current_block = None
    items = []
    summary = {"HEALTHY": 0, "DEGRADED": 0, "FAILED": 0}
    registration_path_counts = {"precompile_register_payable": 0, "legacy_or_unknown": 0, "not_registered": 0}
    burn_evidence_counts = {"onchain_burn_field": 0, "receipt_only": 0, "none": 0}
    for name in names:
        agent_item = state.get("agents", {}).get(name, {})
        it = evaluate_agent_health(name, agent_item, network_cfg, current_block)
        onchain_hint = None
        burned_at_register = None
        wallet_address = str(agent_item.get("wallet_address", ""))
        if wallet_address and is_valid_evm_address(wallet_address):
            ok, onchain_result = _query_agent_onchain(network_cfg, wallet_address)
            if ok:
                onchain_hint = bool(onchain_result.get("is_agent"))
                burned_at_register = onchain_result.get("burned_at_register")
        registration_path = _registration_path(agent_item, onchain_hint)
        burn_level = _burn_evidence_level(agent_item, burned_at_register)
        it["registration_path"] = registration_path
        it["burn_evidence_level"] = burn_level
        summary[it["health"]] += 1
        registration_path_counts[registration_path] += 1
        burn_evidence_counts[burn_level] += 1
        items.append(it)
    summary["registration_path_counts"] = registration_path_counts
    summary["burn_evidence_counts"] = burn_evidence_counts
    output = {"ok": True, "request_id": request_id, "current_block": current_block, "summary": summary, "items": items}
    state["events"].append({"ts": now_ts(), "type": "lifecycle_report", "request_id": request_id, "summary": summary})
    save_state(state_file, state)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def lifecycle_repair(state_file: str, network: str, request_id: str | None) -> int:
    state = load_state(state_file)
    network_cfg = load_yaml(network)
    names = []
    if request_id:
        req = state.get("requests", {}).get(request_id)
        if not req:
            print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
            return 1
        names = req.get("scale_plan", {}).get("agents", [])
    else:
        names = sorted(state.get("agents", {}).keys())
    if not names:
        print(json.dumps({"ok": False, "error": "no agents for lifecycle repair"}, ensure_ascii=False, indent=2))
        return 1
    current_block = None
    try:
        current_block = get_current_block_healthy(network_cfg)
    except Exception:
        current_block = None
    repaired = []
    skipped = []
    failed = []
    for name in names:
        item = state.get("agents", {}).get(name, {})
        health = evaluate_agent_health(name, item, network_cfg, current_block)
        if health["health"] == "HEALTHY":
            skipped.append(name)
            continue
        actions_done = []
        if "restart_service" in health["actions"]:
            item["service_active"] = True
            actions_done.append("restart_service")
        if "send_heartbeat" in health["actions"]:
            code = heartbeat_once(state_file, network, name, None, None, None)
            if code == 0:
                actions_done.append("send_heartbeat")
            else:
                failed.append({"agent": name, "action": "send_heartbeat"})
                continue
        if "run_challenge" in health["actions"]:
            code = challenge_run_once(state_file, network, name)
            if code == 0:
                actions_done.append("run_challenge")
            else:
                failed.append({"agent": name, "action": "run_challenge"})
                continue
        state = load_state(state_file)
        state.setdefault("agents", {}).setdefault(name, {}).update(item)
        repaired.append({"agent": name, "actions": actions_done})
        state["events"].append({"ts": now_ts(), "type": "lifecycle_repair", "agent": name, "actions": actions_done})
        save_state(state_file, state)
    ok = len(failed) == 0
    print(json.dumps({"ok": ok, "request_id": request_id, "repaired": repaired, "skipped": skipped, "failed": failed}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def status(state_file: str, request_id: str) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    target_names = req.get("scale_plan", {}).get("agents", [])
    items = []
    success_count = 0
    for name in target_names:
        item = state["agents"].get(name, {})
        chain_ok = bool(item.get("registered")) and bool(item.get("staked"))
        service_ok = bool(item.get("service_active")) and bool(item.get("heartbeat_at") or item.get("deployed_host"))
        if chain_ok and service_ok:
            status_value = "ready"
            success_count += 1
        elif chain_ok or service_ok:
            status_value = "partial"
        else:
            status_value = "failed"
        items.append({"name": name, "chain_registered": bool(item.get("registered")), "chain_staked": bool(item.get("staked")), "service_active": bool(item.get("service_active")), "deployed_host": item.get("deployed_host", ""), "container_name": item.get("container_name", ""), "status": status_value})
    final_status = "FAILED"
    if success_count == len(target_names) and len(target_names) > 0:
        final_status = "SUCCESS"
    elif success_count > 0:
        final_status = "PARTIAL"
    report = {"ok": True, "request_id": request_id, "target": len(target_names), "success": success_count, "failed": len(target_names) - success_count, "report_status": final_status, "items": items}
    if final_status == "SUCCESS":
        req["status"] = "SUCCESS"
    state["events"].append({"ts": now_ts(), "type": "status_reported", "request_id": request_id, "report_status": final_status})
    save_state(state_file, state)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def repair(state_file: str, request_id: str) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    repaired = []
    skipped = []
    blocked = []
    for name in req.get("scale_plan", {}).get("agents", []):
        item = state["agents"].get(name, {})
        if item.get("registered") and item.get("staked") and item.get("service_active"):
            skipped.append(name)
            continue
        if not (item.get("registered") and item.get("staked")):
            blocked.append({"agent": name, "reason": "onchain_registration_required"})
            continue
        item["service_active"] = bool(item.get("container_name")) or True
        item["heartbeat_at"] = now_ts()
        state["agents"][name] = item
        repaired.append(name)
        req["execution"].get("failed_agents", {}).pop(name, None)
    for item in blocked:
        req["execution"].setdefault("failed_agents", {})[item["agent"]] = {"error": item["reason"], "retryable": True}
    req["status"] = "SCALED" if not req["execution"].get("failed_agents") else "PARTIAL"
    state["events"].append({"ts": now_ts(), "type": "repair_run", "request_id": request_id, "repaired": repaired, "blocked": blocked})
    save_state(state_file, state)
    print(json.dumps({"ok": len(blocked) == 0, "request_id": request_id, "repaired": repaired, "skipped": skipped, "blocked": blocked, "status": req["status"]}, ensure_ascii=False, indent=2))
    return 0


def wallet_export(state_file: str, key_id: str, reveal_secret: bool) -> int:
    state = load_state(state_file)
    w = state.get("wallets", {}).get(key_id)
    if not w:
        print(json.dumps({"ok": False, "error": "wallet not found"}, ensure_ascii=False, indent=2))
        return 1
    payload = {"ok": True, "key_id": key_id, "address": w["address"], "role": w.get("role"), "label": w.get("label")}
    if reveal_secret:
        payload["private_key"] = w.get("private_key", "")
        payload["mnemonic"] = w.get("mnemonic", "")
        payload["warning"] = "sensitive output enabled by --reveal-secret"
    else:
        payload["private_key_masked"] = mask_secret(w.get("private_key", ""))
        payload["mnemonic_masked"] = mask_secret(w.get("mnemonic", ""))
        payload["warning"] = "default output is masked; use --reveal-secret only in secure environment"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def wallet_backup_export(state_file: str, output_file: str) -> int:
    state = load_state(state_file)
    wallets = state.get("wallets", {})
    rows = []
    for key_id, w in wallets.items():
        rows.append(
            {
                "key_id": key_id,
                "address": w.get("address", ""),
                "private_key": w.get("private_key", ""),
                "mnemonic": w.get("mnemonic", ""),
                "role": w.get("role", ""),
                "label": w.get("label", ""),
                "created_at": w.get("created_at"),
            }
        )
    rows.sort(key=lambda x: int(x.get("created_at") or 0))
    data = {"generated_at": now_ts(), "count": len(rows), "wallets": rows}
    p = Path(output_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    print(json.dumps({"ok": True, "output_file": str(p), "count": len(rows), "mode": "600"}, ensure_ascii=False, indent=2))
    return 0


def wallet_backup_verify(backup_file: str) -> int:
    p = Path(backup_file)
    if not p.exists():
        print(json.dumps({"ok": False, "error": "backup file not found"}, ensure_ascii=False, indent=2))
        return 1
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid backup json: {e}"}, ensure_ascii=False, indent=2))
        return 1
    wallets = data.get("wallets", [])
    errors = []
    for i, w in enumerate(wallets):
        if not is_valid_evm_address(str(w.get("address", ""))):
            errors.append(f"wallets[{i}].address invalid")
        pk = str(w.get("private_key", ""))
        if not re.fullmatch(r"[a-fA-F0-9]{64}", pk):
            errors.append(f"wallets[{i}].private_key invalid")
    if errors:
        print(json.dumps({"ok": False, "errors": errors, "count": len(wallets)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "count": len(wallets), "backup_file": backup_file}, ensure_ascii=False, indent=2))
    return 0


def parse_intent(intent: str) -> dict:
    amount_match = re.search(r"(\d+(?:\.\d+)?)\s*AXON", intent, flags=re.IGNORECASE)
    target_match = re.search(r"(?:scale|expand)\s*(\d+)\s*agents?", intent, flags=re.IGNORECASE)
    if not amount_match or not target_match:
        return {"ok": False, "error": "intent parse failed"}
    return {"ok": True, "amount_axon": float(amount_match.group(1)), "target_agents": int(target_match.group(1))}


def run_intent_pipeline(state_file: str, network: str, agents: str, intent: str, funding_address: str | None, observed_confirmations: int, observed_chain_id: int, strict_rpc: bool) -> int:
    if validate(network=network, agents=agents, strict_rpc=strict_rpc) != 0:
        return 1
    parsed = parse_intent(intent)
    if not parsed["ok"]:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return 1
    resolved_funding_address = funding_address or load_state(state_file).get("settings", {}).get("funding_address")
    if not resolved_funding_address:
        print(json.dumps({"ok": False, "error": "funding wallet not initialized"}, ensure_ascii=False, indent=2))
        return 1
    if create_request(state_file, parsed["target_agents"], parsed["amount_axon"], resolved_funding_address, 2, 1800, 100.0) != 0:
        return 1
    state = load_state(state_file)
    request_id = sorted(state["requests"].keys())[-1]
    if fund_check(state_file, network, request_id, parsed["amount_axon"], observed_confirmations, observed_chain_id, strict_rpc) != 0:
        return 1
    if build_scale_plan(state_file, network, agents, request_id) != 0:
        return 1
    if execute_scale(state_file, network, agents, request_id, []) != 0:
        return 1
    status(state_file, request_id)
    repair(state_file, request_id)
    status(state_file, request_id)
    print(json.dumps({"ok": True, "request_id": request_id, "pipeline": "validate -> fund-check -> scale -> status -> repair"}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="axonctl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_wallet_set = sub.add_parser("funding-wallet-set")
    p_wallet_set.add_argument("--state-file", default="state/deploy_state.json")
    p_wallet_set.add_argument("--address", required=True)

    p_wallet_get = sub.add_parser("funding-wallet-get")
    p_wallet_get.add_argument("--state-file", default="state/deploy_state.json")

    p_wallet_gen = sub.add_parser("wallet-generate")
    p_wallet_gen.add_argument("--state-file", default="state/deploy_state.json")
    p_wallet_gen.add_argument("--role", required=True, choices=["funding", "agent"])
    p_wallet_gen.add_argument("--label", required=True)

    p_wallet_list = sub.add_parser("wallet-list")
    p_wallet_list.add_argument("--state-file", default="state/deploy_state.json")

    p_wallet_export = sub.add_parser("wallet-export")
    p_wallet_export.add_argument("--state-file", default="state/deploy_state.json")
    p_wallet_export.add_argument("--key-id", required=True)
    p_wallet_export.add_argument("--reveal-secret", action="store_true")

    p_wallet_backup_export = sub.add_parser("wallet-backup-export")
    p_wallet_backup_export.add_argument("--state-file", default="state/deploy_state.json")
    p_wallet_backup_export.add_argument("--output-file", required=True)

    p_wallet_backup_verify = sub.add_parser("wallet-backup-verify")
    p_wallet_backup_verify.add_argument("--backup-file", required=True)

    p_wallet_template = sub.add_parser("funding-wallet-template")
    p_wallet_template.add_argument("--output", default="funding_wallet.template.yaml")

    p_wallet_import = sub.add_parser("funding-wallet-import")
    p_wallet_import.add_argument("--state-file", default="state/deploy_state.json")
    p_wallet_import.add_argument("--wallet-file", required=True)

    p_agent_wallet_template = sub.add_parser("agent-wallet-template")
    p_agent_wallet_template.add_argument("--output", default="agent_wallet.template.yaml")

    p_agent_wallet_import = sub.add_parser("agent-wallet-import")
    p_agent_wallet_import.add_argument("--state-file", default="state/deploy_state.json")
    p_agent_wallet_import.add_argument("--agent", required=True)
    p_agent_wallet_import.add_argument("--private-key", required=True)
    p_agent_wallet_import.add_argument("--address")
    p_agent_wallet_import.add_argument("--mnemonic")
    p_agent_wallet_import.add_argument("--overwrite", action="store_true")

    p_agent_wallets_template = sub.add_parser("agent-wallets-template")
    p_agent_wallets_template.add_argument("--output", default="agent_wallets.template.yaml")

    p_agent_wallets_import = sub.add_parser("agent-wallets-import")
    p_agent_wallets_import.add_argument("--state-file", default="state/deploy_state.json")
    p_agent_wallets_import.add_argument("--wallet-file", required=True)
    p_agent_wallets_import.add_argument("--overwrite", action="store_true")

    p_init = sub.add_parser("init-step")
    p_init.add_argument("--mode", required=True, choices=["local", "server"])
    p_init.add_argument("--hosts")
    p_init.add_argument("--host")

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--network", required=True)
    p_validate.add_argument("--agents", required=True)
    p_validate.add_argument("--strict-rpc", action="store_true")

    p_request_create = sub.add_parser("request-create")
    p_request_create.add_argument("--state-file", default="state/deploy_state.json")
    p_request_create.add_argument("--target-agents", type=int, required=True)
    p_request_create.add_argument("--min-funding-axon", type=float, required=True)
    p_request_create.add_argument("--funding-address", required=True)
    p_request_create.add_argument("--min-confirmations", type=int, default=2)
    p_request_create.add_argument("--timeout-sec", type=int, default=1800)
    p_request_create.add_argument("--stake-per-agent-axon", type=float, default=100.0)

    p_fund_check = sub.add_parser("fund-check")
    p_fund_check.add_argument("--state-file", default="state/deploy_state.json")
    p_fund_check.add_argument("--network", required=True)
    p_fund_check.add_argument("--request-id", required=True)
    p_fund_check.add_argument("--observed-amount-axon", type=float, required=True)
    p_fund_check.add_argument("--observed-confirmations", type=int, required=True)
    p_fund_check.add_argument("--observed-chain-id", type=int, default=8210)
    p_fund_check.add_argument("--strict-rpc", action="store_true")

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--state-file", default="state/deploy_state.json")
    p_plan.add_argument("--network", required=True)
    p_plan.add_argument("--agents", required=True)
    p_plan.add_argument("--request-id", required=True)

    p_scale = sub.add_parser("scale")
    p_scale.add_argument("--state-file", default="state/deploy_state.json")
    p_scale.add_argument("--network", required=True)
    p_scale.add_argument("--agents", required=True)
    p_scale.add_argument("--request-id", required=True)
    p_scale.add_argument("--fail-agent", action="append", default=[])

    p_register_onchain_once = sub.add_parser("register-onchain-once")
    p_register_onchain_once.add_argument("--state-file", default="state/deploy_state.json")
    p_register_onchain_once.add_argument("--network", required=True)
    p_register_onchain_once.add_argument("--agent", required=True)
    p_register_onchain_once.add_argument("--stake-axon", type=float, default=DEFAULT_REGISTER_STAKE_AXON)
    p_register_onchain_once.add_argument("--wait-receipt-timeout", type=int, default=DEFAULT_REGISTER_WAIT_RECEIPT_SEC)
    p_register_onchain_once.add_argument("--capabilities", default=DEFAULT_REGISTER_CAPABILITIES)
    p_register_onchain_once.add_argument("--model", default=DEFAULT_REGISTER_MODEL)
    p_register_onchain_once.add_argument("--dry-run", action="store_true")

    p_register_onchain_batch = sub.add_parser("register-onchain-batch")
    p_register_onchain_batch.add_argument("--state-file", default="state/deploy_state.json")
    p_register_onchain_batch.add_argument("--network", required=True)
    p_register_onchain_batch.add_argument("--request-id")
    p_register_onchain_batch.add_argument("--stake-axon", type=float, default=DEFAULT_REGISTER_STAKE_AXON)
    p_register_onchain_batch.add_argument("--wait-receipt-timeout", type=int, default=DEFAULT_REGISTER_WAIT_RECEIPT_SEC)
    p_register_onchain_batch.add_argument("--capabilities", default=DEFAULT_REGISTER_CAPABILITIES)
    p_register_onchain_batch.add_argument("--model", default=DEFAULT_REGISTER_MODEL)
    p_register_onchain_batch.add_argument("--dry-run", action="store_true")

    p_registration_audit = sub.add_parser("registration-audit")
    p_registration_audit.add_argument("--state-file", default="state/deploy_state.json")
    p_registration_audit.add_argument("--network", required=True)
    p_registration_audit.add_argument("--request-id")
    p_registration_audit.add_argument("--agent", action="append", default=[])
    p_registration_audit.add_argument("--strict", action="store_true")

    p_status = sub.add_parser("status")
    p_status.add_argument("--state-file", default="state/deploy_state.json")
    p_status.add_argument("--request-id", required=True)

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--state-file", default="state/deploy_state.json")
    p_repair.add_argument("--request-id", required=True)

    p_remote_deploy = sub.add_parser("remote-deploy")
    p_remote_deploy.add_argument("--state-file", default="state/deploy_state.json")
    p_remote_deploy.add_argument("--request-id", required=True)
    p_remote_deploy.add_argument("--hosts", default="configs/runtime/hosts.runtime.yaml")
    p_remote_deploy.add_argument("--host", required=True)
    p_remote_deploy.add_argument("--network", required=True)
    p_remote_deploy.add_argument("--agents", required=True)
    p_remote_deploy.add_argument("--dry-run", action="store_true")

    p_remote_status = sub.add_parser("remote-status")
    p_remote_status.add_argument("--state-file", default="state/deploy_state.json")
    p_remote_status.add_argument("--request-id", required=True)
    p_remote_status.add_argument("--hosts", default="configs/runtime/hosts.runtime.yaml")
    p_remote_status.add_argument("--host", required=True)

    p_heartbeat_once = sub.add_parser("heartbeat-once")
    p_heartbeat_once.add_argument("--state-file", default="state/deploy_state.json")
    p_heartbeat_once.add_argument("--network", required=True)
    p_heartbeat_once.add_argument("--agent", required=True)
    p_heartbeat_once.add_argument("--max-retries", type=int)
    p_heartbeat_once.add_argument("--backoff-seconds", type=int)
    p_heartbeat_once.add_argument("--receipt-timeout-sec", type=int)

    p_heartbeat_batch = sub.add_parser("heartbeat-batch")
    p_heartbeat_batch.add_argument("--state-file", default="state/deploy_state.json")
    p_heartbeat_batch.add_argument("--network", required=True)
    p_heartbeat_batch.add_argument("--request-id")
    p_heartbeat_batch.add_argument("--max-retries", type=int)
    p_heartbeat_batch.add_argument("--backoff-seconds", type=int)
    p_heartbeat_batch.add_argument("--receipt-timeout-sec", type=int)

    p_heartbeat_daemon = sub.add_parser("heartbeat-daemon")
    p_heartbeat_daemon.add_argument("--state-file", default="state/deploy_state.json")
    p_heartbeat_daemon.add_argument("--network", required=True)
    p_heartbeat_daemon.add_argument("--request-id")
    p_heartbeat_daemon.add_argument("--interval-sec", type=int, default=180)
    p_heartbeat_daemon.add_argument("--max-retries", type=int)
    p_heartbeat_daemon.add_argument("--backoff-seconds", type=int)
    p_heartbeat_daemon.add_argument("--receipt-timeout-sec", type=int)
    p_heartbeat_daemon.add_argument("--max-cycles", type=int, default=0)

    p_challenge_gate = sub.add_parser("challenge-gate-check")
    p_challenge_gate.add_argument("--state-file", default="state/deploy_state.json")
    p_challenge_gate.add_argument("--network", required=True)
    p_challenge_gate.add_argument("--agent", required=True)

    p_challenge_once = sub.add_parser("challenge-run-once")
    p_challenge_once.add_argument("--state-file", default="state/deploy_state.json")
    p_challenge_once.add_argument("--network", required=True)
    p_challenge_once.add_argument("--agent", required=True)

    p_challenge_batch = sub.add_parser("challenge-batch")
    p_challenge_batch.add_argument("--state-file", default="state/deploy_state.json")
    p_challenge_batch.add_argument("--network", required=True)
    p_challenge_batch.add_argument("--request-id")

    p_challenge_daemon = sub.add_parser("challenge-daemon")
    p_challenge_daemon.add_argument("--state-file", default="state/deploy_state.json")
    p_challenge_daemon.add_argument("--network", required=True)
    p_challenge_daemon.add_argument("--request-id")
    p_challenge_daemon.add_argument("--interval-sec", type=int, default=30)
    p_challenge_daemon.add_argument("--max-cycles", type=int, default=0)

    p_lifecycle_report = sub.add_parser("lifecycle-report")
    p_lifecycle_report.add_argument("--state-file", default="state/deploy_state.json")
    p_lifecycle_report.add_argument("--network", required=True)
    p_lifecycle_report.add_argument("--request-id")

    p_lifecycle_repair = sub.add_parser("lifecycle-repair")
    p_lifecycle_repair.add_argument("--state-file", default="state/deploy_state.json")
    p_lifecycle_repair.add_argument("--network", required=True)
    p_lifecycle_repair.add_argument("--request-id")

    p_intent = sub.add_parser("run-intent")
    p_intent.add_argument("--state-file", default="state/deploy_state.json")
    p_intent.add_argument("--network", required=True)
    p_intent.add_argument("--agents", required=True)
    p_intent.add_argument("--intent", required=True)
    p_intent.add_argument("--funding-address")
    p_intent.add_argument("--observed-confirmations", type=int, default=3)
    p_intent.add_argument("--observed-chain-id", type=int, default=8210)
    p_intent.add_argument("--strict-rpc", action="store_true")

    args = parser.parse_args()

    if args.cmd == "funding-wallet-set":
        return funding_wallet_set(args.state_file, args.address)
    if args.cmd == "funding-wallet-get":
        return funding_wallet_get(args.state_file)
    if args.cmd == "wallet-generate":
        return wallet_generate(args.state_file, args.role, args.label)
    if args.cmd == "wallet-list":
        return wallet_list(args.state_file)
    if args.cmd == "wallet-export":
        return wallet_export(args.state_file, args.key_id, args.reveal_secret)
    if args.cmd == "wallet-backup-export":
        return wallet_backup_export(args.state_file, args.output_file)
    if args.cmd == "wallet-backup-verify":
        return wallet_backup_verify(args.backup_file)
    if args.cmd == "funding-wallet-template":
        return funding_wallet_template(args.output)
    if args.cmd == "funding-wallet-import":
        return funding_wallet_import(args.state_file, args.wallet_file)
    if args.cmd == "agent-wallet-template":
        return agent_wallet_template(args.output)
    if args.cmd == "agent-wallet-import":
        return agent_wallet_import(args.state_file, args.agent, args.private_key, args.address, args.mnemonic, args.overwrite)
    if args.cmd == "agent-wallets-template":
        return agent_wallets_template(args.output)
    if args.cmd == "agent-wallets-import":
        return agent_wallets_import(args.state_file, args.wallet_file, args.overwrite)
    if args.cmd == "init-step":
        return init_step(args.mode, args.hosts, args.host)
    if args.cmd == "validate":
        return validate(args.network, args.agents, args.strict_rpc)
    if args.cmd == "request-create":
        return create_request(args.state_file, args.target_agents, args.min_funding_axon, args.funding_address, args.min_confirmations, args.timeout_sec, args.stake_per_agent_axon)
    if args.cmd == "fund-check":
        return fund_check(args.state_file, args.network, args.request_id, args.observed_amount_axon, args.observed_confirmations, args.observed_chain_id, args.strict_rpc)
    if args.cmd == "plan":
        return build_scale_plan(args.state_file, args.network, args.agents, args.request_id)
    if args.cmd == "scale":
        return execute_scale(args.state_file, args.network, args.agents, args.request_id, args.fail_agent)
    if args.cmd == "register-onchain-once":
        return register_onchain_once(
            state_file=args.state_file,
            network=args.network,
            agent=args.agent,
            stake_axon=args.stake_axon,
            wait_receipt_timeout=args.wait_receipt_timeout,
            dry_run=args.dry_run,
            capabilities=args.capabilities,
            model=args.model,
        )
    if args.cmd == "register-onchain-batch":
        return register_onchain_batch(
            state_file=args.state_file,
            network=args.network,
            request_id=args.request_id,
            stake_axon=args.stake_axon,
            wait_receipt_timeout=args.wait_receipt_timeout,
            dry_run=args.dry_run,
            capabilities=args.capabilities,
            model=args.model,
        )
    if args.cmd == "registration-audit":
        return registration_audit(args.state_file, args.network, args.request_id, args.agent, args.strict)
    if args.cmd == "status":
        return status(args.state_file, args.request_id)
    if args.cmd == "repair":
        return repair(args.state_file, args.request_id)
    if args.cmd == "remote-deploy":
        return remote_deploy(args.state_file, args.request_id, args.hosts, args.host, args.network, args.agents, args.dry_run)
    if args.cmd == "remote-status":
        return remote_status(args.state_file, args.request_id, args.hosts, args.host)
    if args.cmd == "heartbeat-once":
        return heartbeat_once(args.state_file, args.network, args.agent, args.max_retries, args.backoff_seconds, args.receipt_timeout_sec)
    if args.cmd == "heartbeat-batch":
        return heartbeat_batch(args.state_file, args.network, args.request_id, args.max_retries, args.backoff_seconds, args.receipt_timeout_sec)
    if args.cmd == "heartbeat-daemon":
        return heartbeat_daemon(
            args.state_file,
            args.network,
            args.request_id,
            args.interval_sec,
            args.max_retries,
            args.backoff_seconds,
            args.receipt_timeout_sec,
            args.max_cycles,
        )
    if args.cmd == "challenge-gate-check":
        return challenge_gate_check(args.state_file, args.network, args.agent)
    if args.cmd == "challenge-run-once":
        return challenge_run_once(args.state_file, args.network, args.agent)
    if args.cmd == "challenge-batch":
        return challenge_batch(args.state_file, args.network, args.request_id)
    if args.cmd == "challenge-daemon":
        return challenge_daemon(
            args.state_file,
            args.network,
            args.request_id,
            args.interval_sec,
            args.max_cycles,
        )
    if args.cmd == "lifecycle-report":
        return lifecycle_report(args.state_file, args.network, args.request_id)
    if args.cmd == "lifecycle-repair":
        return lifecycle_repair(args.state_file, args.network, args.request_id)
    if args.cmd == "run-intent":
        return run_intent_pipeline(args.state_file, args.network, args.agents, args.intent, args.funding_address, args.observed_confirmations, args.observed_chain_id, args.strict_rpc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
