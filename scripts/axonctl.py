import argparse
import json
from pathlib import Path

import yaml


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def validate(network: str, agents: str) -> int:
    network_cfg = load_yaml(network)
    agents_cfg = load_yaml(agents)
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
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "agents": len(entries)}, ensure_ascii=False, indent=2))
    return 0


def status(agents: str) -> int:
    agents_cfg = load_yaml(agents)
    entries = agents_cfg.get("agents", [])
    result = [{"name": item.get("name"), "state": "planned"} for item in entries]
    print(json.dumps({"ok": True, "items": result}, ensure_ascii=False, indent=2))
    return 0


def scale(state_file: str, agents: str, add: int) -> int:
    agents_cfg = load_yaml(agents)
    target = agents_cfg.get("agents", [])
    state_path = Path(state_file)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"deployed": []}
    deployed = set(state.get("deployed", []))
    candidates = [item.get("name") for item in target if item.get("name") and item.get("name") not in deployed]
    picked = candidates[: max(add, 0)]
    deployed.update(picked)
    state["deployed"] = sorted(deployed)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "added": picked, "total_deployed": len(state["deployed"])}, ensure_ascii=False, indent=2))
    return 0


def repair() -> int:
    print(json.dumps({"ok": True, "action": "no-op", "message": "repair flow placeholder"}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="axonctl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--network", required=True)
    p_validate.add_argument("--agents", required=True)

    p_scale = sub.add_parser("scale")
    p_scale.add_argument("--network", required=True)
    p_scale.add_argument("--agents", required=True)
    p_scale.add_argument("--state-file", default="state/deploy_state.json")
    p_scale.add_argument("--add", type=int, default=1)

    p_status = sub.add_parser("status")
    p_status.add_argument("--network", required=True)
    p_status.add_argument("--agents", required=True)

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--network", required=True)
    p_repair.add_argument("--agents", required=True)

    args = parser.parse_args()

    if args.cmd == "validate":
        return validate(args.network, args.agents)
    if args.cmd == "scale":
        return scale(args.state_file, args.agents, args.add)
    if args.cmd == "status":
        return status(args.agents)
    if args.cmd == "repair":
        return repair()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
