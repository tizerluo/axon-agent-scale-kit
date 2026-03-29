"""
scripts/axond_tx.py

axond CLI subprocess 封装，供 challenge_run_once() 的 command 模式调用。

消息结构（从 axon-chain/x/agent/types/tx.pb.go 确认）：
  MsgSubmitAIChallengeResponse  { Sender: bech32, Epoch: uint64, CommitHash: hex }
  MsgRevealAIChallengeResponse { Sender: bech32, Epoch: uint64, RevealData: string }

Commit hash 算法（从 axon-chain/x/agent/keeper/msg_server.go 确认）：
  keeper 在 reveal 时验证：
    commitInput = msg.Sender + ":" + msg.RevealData
    expected    = SHA256(commitInput)          ← 不做 normalize，直接原始 bytes
  所以 commit_hash = SHA256(bech32_addr + ":" + raw_answer)

这与 challengePool 中存储的 AnswerHash 不同（AnswerHash 是 normalize 后 SHA256），
但 keeper 在 reveal 时直接比对 commit_hash 和 SHA256(bech32:raw_answer)，
不需要关心 pool 中的 AnswerHash。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib import request

import yaml

import sys as _sys, os as _os
__scd = _os.path.dirname(_os.path.abspath(__file__))
if __scd not in _sys.path:
    _sys.path.insert(0, __scd)
del __scd, _sys, _os

import _shared_crypto
from _shared_crypto import (
    go_normalize,
    keeper_answer_hash,
    keeper_commit_hash,
)

# ─── 常量 ─────────────────────────────────────────────────────────────────────

AXOND_DEFAULT_TIMEOUT = 30          # 秒
AXOND_QUERY_TIMEOUT = 20           # 秒
COSMOS_BROADCAST_URL = "https://mainnet-api.axonchain.ai/axon/public/v1/txs/broadcast"

MAX_REVEAL_BYTES = 512


# ─── Hash 算法（来自 _shared_crypto，与 Axon keeper 源码保持一致）──────────────
# keeper_commit_hash, keeper_answer_hash, _go_normalize 均来自共享模块
# 各模块直接引用 _shared_crypto，避免重复实现导致分化

# 直接引用（供内部及外部调用方使用）
keeper_commit_hash = _shared_crypto.keeper_commit_hash
keeper_answer_hash = _shared_crypto.keeper_answer_hash
_go_normalize = _shared_crypto.go_normalize  # 向后兼容别名


# ─── axond subprocess 封装 ──────────────────────────────────────────────────

def _run_axond(args: list[str], timeout: int = AXOND_DEFAULT_TIMEOUT,
                capture: bool = True) -> tuple[int, str, str]:
    """运行 axond 命令，返回 (returncode, stdout, stderr)。"""
    try:
        kwargs = dict(text=True, timeout=timeout)
        if capture:
            kwargs["capture_output"] = True
        result = subprocess.run(["axond"] + args, **kwargs)
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def which_axond() -> str | None:
    """返回 axond 的绝对路径，找不到返回 None。"""
    r, out, _ = _run_axond(["version"], timeout=5)
    if r == 0:
        # 找 axond 在哪里
        r2, path, _ = _run_axond(["__show_path__"], timeout=5)
        return path.strip() or "axond"
    return None


def evm_to_bech32(evm_address: str) -> str | None:
    """
    调用 axond debug addr 将 EVM 地址转换为 Cosmos bech32 地址。
    返回 bech32 字符串，失败返回 None。
    """
    if not evm_address:
        return None
    # 确保是 checksummed 地址
    addr = evm_address.strip()
    r, stdout, stderr = _run_axond(["debug", "addr", addr], timeout=15)
    if r != 0:
        return None
    for line in stdout.splitlines():
        line = line.strip()
        if "Bech32 Acc" in line:
            # 格式：Bech32 Acc: axon1xxxx
            return line.split(":", 1)[1].strip()
        if "Bech32" in line and "axon1" in line:
            # 其他可能格式：Bech32 Acc axon1xxxx
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "Bech32" and i + 1 < len(parts):
                    return parts[i + 1]
    return None


def query_agent_cosmos(bech32_address: str) -> dict | None:
    """
    查询 Cosmos agent 记录。
    返回 agent dict 或 None（agent 不存在或 axond 不可用）。
    """
    r, stdout, stderr = _run_axond(
        ["query", "agent", "agent", bech32_address, "-o", "json"],
        timeout=AXOND_QUERY_TIMEOUT,
    )
    if r != 0:
        return None
    try:
        data = json.loads(stdout or "{}")
        agent = data.get("agent", {})
        if isinstance(agent, dict):
            return agent
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def ensure_axond_key(agent_name: str, private_key_hex: str,
                     keyring_dir: str = "~/.axond",
                     keyring_backend: str = "file") -> tuple[bool, str]:
    """
    确保密钥存在于 axond keyring。
    不存在则导入，已存在则跳过。

    返回 (success, message)。
    """
    # 先检查是否已存在
    r, stdout, _ = _run_axond(
        ["keys", "get", agent_name,
         "--keyring-dir", str(Path(keyring_dir).expanduser()),
         "--keyring-backend", keyring_backend],
        timeout=10,
    )
    if r == 0 and agent_name in stdout:
        return True, f"key already exists: {agent_name}"

    # 不存在，尝试导入
    # 注意：axond keys import 需要密钥的原始 hex（不带 0x 前缀）
    pk = private_key_hex.strip()
    if pk.startswith("0x"):
        pk = pk[2:]

    r, stdout, stderr = _run_axond(
        ["keys", "import", agent_name, pk,
         "--keyring-dir", str(Path(keyring_dir).expanduser()),
         "--keyring-backend", keyring_backend],
        timeout=20,
    )
    if r == 0:
        return True, f"key imported: {agent_name}"
    return False, f"key import failed for {agent_name}: {stderr.strip()}"


# ─── 链上 Challenge 查询 ──────────────────────────────────────────────────────

def query_current_challenge(rpc_url: str) -> dict | None:
    """
    从 EVM RPC 查询当前 AI Challenge 状态。
    通过解析链上合约事件（如果有），或通过 Cosmos REST API。
    目前用 Axon 自定义 REST API。

    返回：
      {
        "epoch": int,
        "deadline_block": int,
        "challenge_hash": str,
        "challenge_data": str,
        "category": str,
      }
    或 None（无活跃 challenge / 查询失败）。
    """
    rest_url = "https://mainnet-api.axonchain.ai"

    # 尝试 Cosmos SDK 标准 API
    url = f"{rest_url}/cosmos/agent/v1/challenges/current"
    try:
        req = request.Request(url, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_challenge_response(data)
    except Exception:
        pass

    # 尝试 Axon 自定义 API
    url2 = f"{rest_url}/axon/public/v1/agents/challenge/current"
    try:
        req = request.Request(url2, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_challenge_response(data)
    except Exception:
        pass

    return None


def _parse_challenge_response(data: dict) -> dict | None:
    """解析 challenge API 响应，提取 epoch/deadline_block。"""
    # 不同 API 格式兼容处理
    challenge = None

    # 格式 1：{"challenge": {...}}
    if "challenge" in data:
        challenge = data["challenge"]

    # 格式 2：直接 challenge 对象
    if isinstance(data, dict) and ("epoch" in data or "Epoch" in data):
        challenge = data

    if not challenge:
        return None

    def _int(d, k):
        v = d.get(k, d.get(k.title(), d.get(k.upper(), 0)))
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "epoch": _int(challenge, "epoch"),
        "deadline_block": _int(challenge, "deadline_block"),
        "challenge_hash": str(challenge.get("challenge_hash",
                                          challenge.get("ChallengeHash", ""))),
        "challenge_data": str(challenge.get("challenge_data",
                                             challenge.get("ChallengeData", ""))),
        "category": str(challenge.get("category",
                                       challenge.get("Category", ""))),
    }


def query_tx_status(tx_hash: str, rest_url: str = COSMOS_BROADCAST_URL) -> dict:
    """
    查询交易状态（通过 Cosmos SDK TX API）。
    返回 {"code": int, "raw_log": str, "txhash": str}。
    """
    url = f"{rest_url}/cosmos/tx/v1beta1/txs/{tx_hash}"
    try:
        req = request.Request(url, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tx_data = data.get("tx_response", data)
        return {
            "code": int(tx_data.get("code", 0)),
            "raw_log": str(tx_data.get("raw_log", "")),
            "txhash": str(tx_data.get("txhash", tx_hash)),
            "height": int(tx_data.get("height", 0)),
        }
    except Exception as e:
        return {"code": -1, "raw_log": str(e), "txhash": tx_hash, "height": 0}


# ─── 交易发送 ───────────────────────────────────────────────────────────────

def build_submit_tx(agent_name: str, epoch: int, commit_hash: str,
                    chain_id: str, keyring_dir: str,
                    broadcast_mode: str = "sync") -> list[str]:
    """
    构建 axond submit-ai-challenge-response 命令参数。
    """
    return [
        "tx", "agent", "submit-ai-challenge-response",
        agent_name,               # sender (axond key name)
        commit_hash,              # commit_hash (hex)
        "--epoch", str(epoch),
        "--chain-id", chain_id,
        "--keyring-dir", str(Path(keyring_dir).expanduser()),
        "--keyring-backend", "file",
        "--broadcast-mode", broadcast_mode,
        "--yes",
    ]


def build_reveal_tx(agent_name: str, epoch: int, reveal_data: str,
                    chain_id: str, keyring_dir: str,
                    broadcast_mode: str = "sync") -> list[str]:
    """
    构建 axond reveal-ai-challenge-response 命令参数。
    """
    return [
        "tx", "agent", "reveal-ai-challenge-response",
        agent_name,               # sender (axond key name)
        reveal_data,              # 原始答案（512 字节以内）
        "--epoch", str(epoch),
        "--chain-id", chain_id,
        "--keyring-dir", str(Path(keyring_dir).expanduser()),
        "--keyring-backend", "file",
        "--broadcast-mode", broadcast_mode,
        "--yes",
    ]


def submit_tx(args: list[str], dry_run: bool = False,
             timeout: int = AXOND_DEFAULT_TIMEOUT) -> tuple[bool, str, str]:
    """
    执行 axond tx 命令。

    dry_run=True 时只打印命令，不执行。

    返回 (success, tx_hash_or_error_msg, raw_output)。
    """
    if dry_run:
        cmd_str = "axond " + " ".join(args)
        return True, f"[dry-run] {cmd_str}", cmd_str

    r, stdout, stderr = _run_axond(args, timeout=timeout)
    output = (stdout + "\n" + stderr).strip()

    if r != 0:
        return False, _parse_tx_error(stderr or stdout), output

    # 解析 tx hash
    tx_hash = _extract_tx_hash(stdout)
    if not tx_hash:
        return False, f"tx submitted but could not extract txhash: {output}", output

    return True, tx_hash, output


def _extract_tx_hash(stdout: str) -> str | None:
    """从 axond tx 输出中提取 txhash。"""
    # 格式：{"txhash":"ABCDEF..."}
    try:
        data = json.loads(stdout)
        return data.get("txhash") or data.get("hash")
    except (json.JSONDecodeError, TypeError):
        pass

    # 格式：txhash: ABCDEF...
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("txhash:") or line.startswith("hash:"):
            return line.split(":", 1)[1].strip()
        if "txhash" in line.lower() and len(line) >= 64:
            # 可能直接是 hash
            parts = line.split()
            for p in parts:
                if re.fullmatch(r"[a-fA-F0-9]{64,}", p):
                    return p
    return None


def _parse_tx_error(stderr: str) -> str:
    """从 axond 错误输出中提取人类可读的错误信息。"""
    # 常见错误映射
    err_map = {
        "ErrAgentNotFound": "agent_not_found",
        "ErrAgentSuspended": "agent_suspended",
        "ErrValidatorRequired": "validator_required",
        "ErrChallengeNotActive": "challenge_not_active",
        "ErrChallengeWindowClosed": "challenge_window_closed",
        "ErrAlreadySubmitted": "already_submitted",
        "ErrRevealTooEarly": "reveal_too_early",
        "ErrRevealWindowClosed": "reveal_window_closed",
        "ErrInvalidReveal": "invalid_reveal_commit_hash_mismatch",
        "ErrAlreadyEvaluated": "already_evaluated",
        "ErrDeregisterCooldown": "deregister_in_cooldown",
    }
    stderr_lower = stderr.lower()
    for code, name in err_map.items():
        if code.lower() in stderr_lower or name in stderr_lower:
            return name
    # 返回最后几行非空错误
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    if lines:
        return lines[-1][:120]
    return "unknown_error"


# ─── 轮询确认 ────────────────────────────────────────────────────────────────

def wait_for_tx(tx_hash: str, rest_url: str = COSMOS_BROADCAST_URL,
                max_wait: int = 60, poll_interval: int = 3) -> tuple[bool, str]:
    """
    轮询交易确认。
    返回 (confirmed, status_msg)。
    """
    start = time.time()
    while time.time() - start < max_wait:
        status = query_tx_status(tx_hash, rest_url)
        code = status.get("code", -1)
        height = status.get("height", 0)
        if code == 0 and height > 0:
            # code==0 表示交易执行成功；height>0 表示进入了已提交的区块。
            # 两者都必须满足：code==0 的 Pending 交易在链分叉时可能被丢弃。
            return True, "confirmed"
        if code == -1:
            # 查询失败，继续等
            time.sleep(poll_interval)
            continue
        # code != 0 表示交易失败
        raw_log = status.get("raw_log", "")
        return False, f"tx_failed(code={code}): {raw_log[:120]}"
    return False, "tx_confirmation_timeout"


# ─── AxondClient ─────────────────────────────────────────────────────────────

class AxondClient:
    """
    axond CLI 封装，供 challenge_run_once() command 模式调用。

    私钥从 state_file 读取（与 heartbeat 共用 _state_wallet_for_agent 逻辑），
    不需要 agents.yaml 配置。

    使用方式：
        client = AxondClient(network_cfg, state_file)
        ok, tx_hash = client.submit_commit(agent_name, epoch, commit_hash)
        ok2, tx_hash2 = client.submit_reveal(agent_name, epoch, reveal_data)
    """

    def __init__(self, network_cfg: dict, state_file: str):
        self.rest_url = network_cfg.get("cosmos", {}).get(
            "rest_url", COSMOS_BROADCAST_URL)
        self.chain_id = network_cfg.get("cosmos", {}).get(
            "chain_id", "axon_8210-1")
        self.keyring_dir = network_cfg.get("cosmos", {}).get(
            "keyring_dir", "~/.axond")
        self.broadcast_mode = network_cfg.get("cosmos", {}).get(
            "broadcast_mode", "sync")
        self.state_file = state_file
        self._state: dict | None = None
        self._evm_addr_cache: dict[str, str] = {}

    # ── state 读取 ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if self._state is None:
            with open(self.state_file) as f:
                self._state = json.load(f)
        return self._state

    def _evm_address_for_agent(self, agent_name: str) -> str | None:
        """从 state 中获取 agent 的 EVM 地址。"""
        state = self._load_state()
        agent_state = state.get("agents", {}).get(agent_name, {})
        addr = agent_state.get("wallet_address", "")
        if addr:
            return addr
        # 备用：从 wallets 遍历找 label=agent:<name>
        for key_id, wallet in state.get("wallets", {}).items():
            if wallet.get("role") == "agent" and wallet.get("label") == f"agent:{agent_name}":
                return wallet.get("address", "")
        return None

    def _private_key_for_agent(self, agent_name: str) -> str | None:
        """从 state 中获取 agent 的私钥（hex，不带 0x）。"""
        state = self._load_state()
        for key_id, wallet in state.get("wallets", {}).items():
            if wallet.get("role") == "agent" and wallet.get("label") == f"agent:{agent_name}":
                pk = wallet.get("private_key", "")
                if pk:
                    return pk[2:] if pk.startswith("0x") else pk
        # 备用：通过 EVM 地址找
        evm_addr = self._evm_address_for_agent(agent_name)
        if not evm_addr:
            return None
        for key_id, wallet in state.get("wallets", {}).items():
            if wallet.get("address", "").lower() == evm_addr.lower():
                pk = wallet.get("private_key", "")
                if pk:
                    return pk[2:] if pk.startswith("0x") else pk
        return None

    def cosmos_address(self, agent_name: str) -> str | None:
        """
        获取 agent 的 Cosmos bech32 地址（从 EVM 地址转换，缓存结果）。
        """
        if agent_name in self._evm_addr_cache:
            return self._evm_addr_cache[agent_name]
        evm_addr = self._evm_address_for_agent(agent_name)
        if not evm_addr:
            return None
        bech32 = evm_to_bech32(evm_addr)
        if bech32:
            self._evm_addr_cache[agent_name] = bech32
        return bech32

    # ── 密钥管理 ────────────────────────────────────────────────────────────

    def ensure_key(self, agent_name: str) -> tuple[bool, str]:
        """
        确保 agent 的密钥在 axond keyring 中。
        首次调用时自动导入。

        返回 (success, message)。
        """
        pk = self._private_key_for_agent(agent_name)
        if not pk:
            return False, f"private key not found for agent: {agent_name}"
        return ensure_axond_key(
            agent_name, pk,
            keyring_dir=self.keyring_dir,
            keyring_backend="file",
        )

    # ── Challenge 查询 ──────────────────────────────────────────────────────

    def query_current_challenge(self) -> dict | None:
        """查询当前活跃的 AI Challenge。"""
        return query_current_challenge(self.rest_url)

    def query_tx(self, tx_hash: str) -> dict:
        """查询交易状态。"""
        return query_tx_status(tx_hash, self.rest_url)

    # ── Commit ─────────────────────────────────────────────────────────────

    def submit_commit(
        self,
        agent_name: str,
        epoch: int,
        commit_hash: str,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """
        发送 MsgSubmitAIChallengeResponse。

        返回 (success, tx_hash_or_error)。
        """
        # 确保密钥存在
        ok, msg = self.ensure_key(agent_name)
        if not ok:
            return False, f"key_setup_failed: {msg}"

        args = build_submit_tx(
            agent_name=agent_name,
            epoch=epoch,
            commit_hash=commit_hash,
            chain_id=self.chain_id,
            keyring_dir=self.keyring_dir,
            broadcast_mode=self.broadcast_mode,
        )

        ok, tx_or_err, raw = submit_tx(args, dry_run=dry_run)
        if not ok:
            return False, tx_or_err

        if dry_run:
            return True, tx_or_err

        # 轮询确认（sync 模式需要）
        confirmed, status = wait_for_tx(tx_or_err, self.rest_url)
        if not confirmed:
            return False, f"commit_tx_unconfirmed: {status}"
        return True, tx_or_err

    # ── Reveal ──────────────────────────────────────────────────────────────

    def submit_reveal(
        self,
        agent_name: str,
        epoch: int,
        answer: str,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        """
        发送 MsgRevealAIChallengeResponse。

        返回 (success, tx_hash_or_error)。
        注意：answer 必须是原始答案（512 字节以内），不做 normalize。
        """
        # 长度检查：答案超长时拒绝，不做静默截断。
        # 静默截断会导致 reveal 时传给链上的答案与 commit 阶段计算的 commit_hash 不匹配，
        # 链上验证必败。调用方应在发交易前检查答案长度。
        answer_bytes = answer.encode("utf-8")
        if len(answer_bytes) > MAX_REVEAL_BYTES:
            return False, f"answer_too_long:{ len(answer_bytes)} bytes (max {MAX_REVEAL_BYTES})"

        ok, msg = self.ensure_key(agent_name)
        if not ok:
            return False, f"key_setup_failed: {msg}"

        args = build_reveal_tx(
            agent_name=agent_name,
            epoch=epoch,
            reveal_data=answer,
            chain_id=self.chain_id,
            keyring_dir=self.keyring_dir,
            broadcast_mode=self.broadcast_mode,
        )

        ok, tx_or_err, raw = submit_tx(args, dry_run=dry_run)
        if not ok:
            return False, tx_or_err

        if dry_run:
            return True, tx_or_err

        confirmed, status = wait_for_tx(tx_or_err, self.rest_url)
        if not confirmed:
            return False, f"reveal_tx_unconfirmed: {status}"
        return True, tx_or_err

    # ── 工具 ───────────────────────────────────────────────────────────────

    def compute_commit_hash(self, cosmos_address: str, answer: str) -> str:
        """计算 Keeper 验证用的 commit hash。"""
        return keeper_commit_hash(cosmos_address, answer)

    def validate_answer(self, answer: str, expected_answer_hash: str) -> bool:
        """
        验证 answer 是否匹配 pool 中的 expected_answer_hash。
        使用 keeper_answer_hash（normalize 后 SHA256）。
        """
        return keeper_answer_hash(answer) == expected_answer_hash.lower()
