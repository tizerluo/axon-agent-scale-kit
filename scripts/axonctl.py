import argparse
import json
import re
import time
import uuid
from pathlib import Path
from urllib import request

import yaml


def now_ts() -> int:
    return int(time.time())


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {"requests": {}, "agents": {}, "events": []}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if "requests" not in state:
        state["requests"] = {}
    if "agents" not in state:
        state["agents"] = {}
    if "events" not in state:
        state["events"] = []
    return state


def save_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def network_and_agent_checks(network_cfg: dict, agents_cfg: dict) -> list[str]:
    errors = []
    if network_cfg.get("evm_chain_id") != 8210:
        errors.append("evm_chain_id must be 8210")
    if network_cfg.get("cosmos_chain_id") != "axon_8210-1":
        errors.append("cosmos_chain_id must be axon_8210-1")
    if not network_cfg.get("rpc_url"):
        errors.append("rpc_url is required")
    entries = agents_cfg.get("agents", [])
    if not isinstance(entries, list) or not entries:
        errors.append("agents list is required")
    for idx, item in enumerate(entries):
        if not item.get("name"):
            errors.append(f"agents[{idx}].name is required")
        if not item.get("wallet_ref"):
            errors.append(f"agents[{idx}].wallet_ref is required")
    return errors


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


def create_request(
    state_file: str,
    target_agents: int,
    min_funding_axon: float,
    funding_address: str,
    min_confirmations: int,
    timeout_sec: int,
    stake_per_agent_axon: float,
) -> int:
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


def fund_check(
    state_file: str,
    network: str,
    request_id: str,
    observed_amount_axon: float,
    observed_confirmations: int,
    observed_chain_id: int,
    strict_rpc: bool,
) -> int:
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
    rpc_error_msg = None
    if not rpc_ok:
        rpc_error_msg = f"rpc unreachable: {rpc_error}"
    elif rpc_chain != 8210:
        rpc_error_msg = f"rpc chain id mismatch: {rpc_chain}"
    if strict_rpc and rpc_error_msg:
        req["status"] = "FAILED"
        req["failure_reason"] = rpc_error_msg
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
        req["funding"] = {
            "observed_amount_axon": observed_amount_axon,
            "observed_confirmations": observed_confirmations,
            "observed_chain_id": observed_chain_id,
            "rpc_ok": rpc_ok,
            "rpc_chain_id": rpc_chain,
        }
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
    stake_budget = req["target_agents"] * req["stake_per_agent_axon"]
    gas_budget = max(80.0, float(req["target_agents"]) * 5.0)
    retry_budget = max(40.0, float(req["target_agents"]) * 2.0)
    req["scale_plan"] = {
        "agents": planned_names,
        "batch_size": concurrency,
        "batches": batches,
        "budget": {
            "stake_axon": round(stake_budget, 4),
            "gas_buffer_axon": round(gas_budget, 4),
            "retry_buffer_axon": round(retry_budget, 4),
            "total_required_axon": round(stake_budget + gas_budget + retry_budget, 4),
        },
    }
    if req["status"] == "FUNDED":
        req["status"] = "PLANNED"
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "plan_built", "request_id": request_id})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "status": req["status"], "scale_plan": req["scale_plan"]}, ensure_ascii=False, indent=2))
    return 0


def execute_scale(
    state_file: str,
    network: str,
    agents: str,
    request_id: str,
    fail_agents: list[str],
) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    if req["status"] not in {"PLANNED", "SCALING", "PARTIAL", "FAILED"}:
        print(json.dumps({"ok": False, "error": "request must be PLANNED before scale"}, ensure_ascii=False, indent=2))
        return 1
    network_cfg = load_yaml(network)
    agents_cfg = load_yaml(agents)
    retry_times = int(network_cfg.get("deploy", {}).get("retry_times", 2))
    target_names = req.get("scale_plan", {}).get("agents", [])
    wallet_map = {x.get("name"): x.get("wallet_ref") for x in agents_cfg.get("agents", [])}
    req["status"] = "SCALING"
    completed = set(req["execution"].get("completed_agents", []))
    failed = dict(req["execution"].get("failed_agents", {}))
    attempts = dict(req["execution"].get("attempts", {}))
    for name in target_names:
        if name in completed:
            continue
        last_error = None
        for _ in range(retry_times + 1):
            attempts[name] = int(attempts.get(name, 0)) + 1
            should_fail = name in fail_agents or (wallet_map.get(name) or "").startswith("FAIL_")
            if should_fail:
                last_error = "simulated execution failure"
                continue
            state["agents"][name] = {
                "registered": True,
                "staked": True,
                "service_active": True,
                "heartbeat_at": now_ts(),
                "last_error": "",
            }
            completed.add(name)
            if name in failed:
                failed.pop(name)
            last_error = None
            break
        if last_error:
            failed[name] = {"error": last_error, "retryable": True}
    req["execution"] = {
        "completed_agents": sorted(completed),
        "failed_agents": failed,
        "attempts": attempts,
    }
    if failed:
        req["status"] = "PARTIAL"
    else:
        req["status"] = "SCALED"
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "scale_executed", "request_id": request_id, "status": req["status"]})
    save_state(state_file, state)
    print(
        json.dumps(
            {
                "ok": True,
                "request_id": request_id,
                "status": req["status"],
                "completed": sorted(completed),
                "failed": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


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
        service_ok = bool(item.get("service_active")) and bool(item.get("heartbeat_at"))
        if chain_ok and service_ok:
            status_value = "ready"
            success_count += 1
        elif chain_ok or service_ok:
            status_value = "partial"
        else:
            status_value = "failed"
        items.append(
            {
                "name": name,
                "chain_registered": bool(item.get("registered")),
                "chain_staked": bool(item.get("staked")),
                "service_active": bool(item.get("service_active")),
                "heartbeat_at": item.get("heartbeat_at"),
                "status": status_value,
                "last_error": item.get("last_error", ""),
            }
        )
    if success_count == len(target_names) and len(target_names) > 0:
        final_status = "SUCCESS"
    elif success_count > 0:
        final_status = "PARTIAL"
    else:
        final_status = "FAILED"
    report = {
        "ok": True,
        "request_id": request_id,
        "target": len(target_names),
        "success": success_count,
        "failed": len(target_names) - success_count,
        "report_status": final_status,
        "items": items,
        "repair_suggestion": "run repair command for failed or partial items",
    }
    state["events"].append({"ts": now_ts(), "type": "status_reported", "request_id": request_id, "report_status": final_status})
    if final_status == "SUCCESS":
        req["status"] = "SUCCESS"
    save_state(state_file, state)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def repair(state_file: str, request_id: str) -> int:
    state = load_state(state_file)
    req = state["requests"].get(request_id)
    if not req:
        print(json.dumps({"ok": False, "error": "request not found"}, ensure_ascii=False, indent=2))
        return 1
    target_names = req.get("scale_plan", {}).get("agents", [])
    repaired = []
    skipped = []
    for name in target_names:
        item = state["agents"].get(name, {})
        if item.get("registered") and item.get("staked") and item.get("service_active") and item.get("heartbeat_at"):
            skipped.append(name)
            continue
        state["agents"][name] = {
            "registered": True,
            "staked": True,
            "service_active": True,
            "heartbeat_at": now_ts(),
            "last_error": "",
        }
        repaired.append(name)
        if name in req["execution"].get("failed_agents", {}):
            req["execution"]["failed_agents"].pop(name)
        completed_agents = set(req["execution"].get("completed_agents", []))
        completed_agents.add(name)
        req["execution"]["completed_agents"] = sorted(completed_agents)
    req["status"] = "SCALED" if not req["execution"].get("failed_agents", {}) else "PARTIAL"
    req["updated_at"] = now_ts()
    state["events"].append({"ts": now_ts(), "type": "repair_run", "request_id": request_id, "repaired": repaired})
    save_state(state_file, state)
    print(json.dumps({"ok": True, "request_id": request_id, "repaired": repaired, "skipped": skipped, "status": req["status"]}, ensure_ascii=False, indent=2))
    return 0


def parse_intent(intent: str) -> dict:
    amount_match = re.search(r"(\d+(?:\.\d+)?)\s*AXON", intent, flags=re.IGNORECASE)
    target_match = re.search(r"扩容\s*(\d+)\s*个?\s*agents?", intent, flags=re.IGNORECASE)
    if not amount_match or not target_match:
        return {"ok": False, "error": "intent parse failed"}
    return {"ok": True, "amount_axon": float(amount_match.group(1)), "target_agents": int(target_match.group(1))}


def run_intent_pipeline(
    state_file: str,
    network: str,
    agents: str,
    intent: str,
    funding_address: str,
    observed_confirmations: int,
    observed_chain_id: int,
    strict_rpc: bool,
) -> int:
    if validate(network=network, agents=agents, strict_rpc=strict_rpc) != 0:
        return 1
    parsed = parse_intent(intent)
    if not parsed["ok"]:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return 1
    create_code = create_request(
        state_file=state_file,
        target_agents=parsed["target_agents"],
        min_funding_axon=parsed["amount_axon"],
        funding_address=funding_address,
        min_confirmations=2,
        timeout_sec=1800,
        stake_per_agent_axon=100.0,
    )
    if create_code != 0:
        return create_code
    state = load_state(state_file)
    request_id = sorted(state["requests"].keys())[-1]
    if fund_check(
        state_file=state_file,
        network=network,
        request_id=request_id,
        observed_amount_axon=parsed["amount_axon"],
        observed_confirmations=observed_confirmations,
        observed_chain_id=observed_chain_id,
        strict_rpc=strict_rpc,
    ) != 0:
        return 1
    if build_scale_plan(state_file=state_file, network=network, agents=agents, request_id=request_id) != 0:
        return 1
    if execute_scale(state_file=state_file, network=network, agents=agents, request_id=request_id, fail_agents=[]) != 0:
        return 1
    status(state_file=state_file, request_id=request_id)
    repair(state_file=state_file, request_id=request_id)
    status(state_file=state_file, request_id=request_id)
    print(json.dumps({"ok": True, "request_id": request_id, "pipeline": "validate -> fund-check -> scale -> status -> repair"}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="axonctl")
    sub = parser.add_subparsers(dest="cmd", required=True)

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

    p_status = sub.add_parser("status")
    p_status.add_argument("--state-file", default="state/deploy_state.json")
    p_status.add_argument("--request-id", required=True)

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--state-file", default="state/deploy_state.json")
    p_repair.add_argument("--request-id", required=True)

    p_intent = sub.add_parser("run-intent")
    p_intent.add_argument("--state-file", default="state/deploy_state.json")
    p_intent.add_argument("--network", required=True)
    p_intent.add_argument("--agents", required=True)
    p_intent.add_argument("--intent", required=True)
    p_intent.add_argument("--funding-address", default="0xFUNDINGADDRESS")
    p_intent.add_argument("--observed-confirmations", type=int, default=3)
    p_intent.add_argument("--observed-chain-id", type=int, default=8210)
    p_intent.add_argument("--strict-rpc", action="store_true")

    args = parser.parse_args()

    if args.cmd == "validate":
        return validate(args.network, args.agents, args.strict_rpc)
    if args.cmd == "request-create":
        return create_request(
            state_file=args.state_file,
            target_agents=args.target_agents,
            min_funding_axon=args.min_funding_axon,
            funding_address=args.funding_address,
            min_confirmations=args.min_confirmations,
            timeout_sec=args.timeout_sec,
            stake_per_agent_axon=args.stake_per_agent_axon,
        )
    if args.cmd == "fund-check":
        return fund_check(
            state_file=args.state_file,
            network=args.network,
            request_id=args.request_id,
            observed_amount_axon=args.observed_amount_axon,
            observed_confirmations=args.observed_confirmations,
            observed_chain_id=args.observed_chain_id,
            strict_rpc=args.strict_rpc,
        )
    if args.cmd == "plan":
        return build_scale_plan(args.state_file, args.network, args.agents, args.request_id)
    if args.cmd == "scale":
        return execute_scale(args.state_file, args.network, args.agents, args.request_id, args.fail_agent)
    if args.cmd == "status":
        return status(args.state_file, args.request_id)
    if args.cmd == "repair":
        return repair(args.state_file, args.request_id)
    if args.cmd == "run-intent":
        return run_intent_pipeline(
            state_file=args.state_file,
            network=args.network,
            agents=args.agents,
            intent=args.intent,
            funding_address=args.funding_address,
            observed_confirmations=args.observed_confirmations,
            observed_chain_id=args.observed_chain_id,
            strict_rpc=args.strict_rpc,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
