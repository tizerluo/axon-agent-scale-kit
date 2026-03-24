import argparse
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--agents", required=True)
    args = parser.parse_args()
    while True:
        print(f"heartbeat agent={args.agent} network={args.network} agents={args.agents}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
