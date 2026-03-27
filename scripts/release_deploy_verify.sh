#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/release_deploy_verify.sh [options]

Options:
  --remote-host <host>       Remote host (default: 43.165.195.71)
  --remote-user <user>       Remote user (default: ubuntu)
  --ssh-key <path>           SSH private key path
                             (default: ${HOME}/Downloads/QQClaw.pem)
  --remote-dir <path>         Remote workdir (default: /home/ubuntu/axon-agent-scale)
  --service <name>           systemd service to restart/verify
                             (default: axon-heartbeat-daemon.service)
  --skip-tests               Skip local unittest before push
  --allow-dirty              Allow dirty working tree
  --dry-run                  Print actions without mutating remote/local state
  -h, --help                 Show this help

Examples:
  scripts/release_deploy_verify.sh
  scripts/release_deploy_verify.sh --dry-run --allow-dirty --skip-tests
EOF
}

log() {
  printf '[release] %s\n' "$*"
}

die() {
  printf '[release][error] %s\n' "$*" >&2
  exit 1
}

REMOTE_HOST="43.165.195.71"
REMOTE_USER="ubuntu"
SSH_KEY="${HOME}/Downloads/QQClaw.pem"
REMOTE_DIR="/home/ubuntu/axon-agent-scale"
SERVICE_NAME="axon-heartbeat-daemon.service"
SKIP_TESTS=0
ALLOW_DIRTY=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote-host)
      [[ $# -ge 2 ]] || die "missing value for --remote-host"
      REMOTE_HOST="$2"
      shift 2
      ;;
    --remote-user)
      [[ $# -ge 2 ]] || die "missing value for --remote-user"
      REMOTE_USER="$2"
      shift 2
      ;;
    --ssh-key)
      [[ $# -ge 2 ]] || die "missing value for --ssh-key"
      SSH_KEY="$2"
      shift 2
      ;;
    --remote-dir)
      [[ $# -ge 2 ]] || die "missing value for --remote-dir"
      REMOTE_DIR="$2"
      shift 2
      ;;
    --service)
      [[ $# -ge 2 ]] || die "missing value for --service"
      SERVICE_NAME="$2"
      shift 2
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git not found"
command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"

[[ -f "$SSH_KEY" ]] || die "ssh key not found: $SSH_KEY"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "must run inside git repository"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if pgrep -f "axonctl.py heartbeat-daemon" >/dev/null 2>&1; then
  die "local heartbeat-daemon appears to be running; stop local daemon before release"
fi

if [[ "$ALLOW_DIRTY" -ne 1 ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    die "working tree is dirty; commit/stash changes or pass --allow-dirty"
  fi
fi

LOCAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
LOCAL_COMMIT="$(git rev-parse HEAD)"
LOCAL_SHORT="$(git rev-parse --short HEAD)"

log "local branch: $LOCAL_BRANCH"
log "local commit: $LOCAL_COMMIT"

if [[ "$LOCAL_BRANCH" == "main" ]]; then
  die "pushing directly to main is forbidden; all changes must go through a PR reviewed by 6tizer"
fi

log "pushing HEAD to origin/$LOCAL_BRANCH"

if [[ "$SKIP_TESTS" -ne 1 ]]; then
  log "running local regression: python3 -m unittest tests.test_axonctl -q"
  python3 -m unittest tests.test_axonctl -q
else
  log "skip tests enabled"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY RUN: would push HEAD to origin/$LOCAL_BRANCH"
else
  log "pushing HEAD to origin/$LOCAL_BRANCH"
  git push origin HEAD
fi

remote_head="$(git ls-remote --heads origin "$LOCAL_BRANCH" | awk '{print $1}')"
if [[ "$remote_head" != "$LOCAL_COMMIT" ]]; then
  die "origin/$LOCAL_BRANCH ($remote_head) does not match local commit ($LOCAL_COMMIT)"
fi

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")
SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY RUN: would ensure remote directory: $REMOTE_DIR"
  log "DRY RUN: would deploy commit archive (scripts, configs, README.md, requirements.txt)"
  log "DRY RUN: would write $REMOTE_DIR/.release_meta.json"
  log "DRY RUN: would restart service $SERVICE_NAME and run lifecycle verification"
  exit 0
fi

log "ensuring remote directories"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/scripts' '$REMOTE_DIR/configs'"

log "deploying tracked files from commit $LOCAL_SHORT"
git archive --format=tar "$LOCAL_COMMIT" scripts configs README.md requirements.txt \
  | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "tar -xf - -C '$REMOTE_DIR'"

deployed_at="$(date '+%Y-%m-%d %H:%M:%S %Z')"
cat <<EOF | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cat > '$REMOTE_DIR/.release_meta.json'"
{
  "commit": "$LOCAL_COMMIT",
  "short_commit": "$LOCAL_SHORT",
  "deployed_at": "$deployed_at",
  "deployed_by": "$(whoami)",
  "source_repo": "$(git remote get-url origin)"
}
EOF

log "restarting service: $SERVICE_NAME"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "sudo systemctl restart '$SERVICE_NAME'"

log "service status"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "systemctl is-active '$SERVICE_NAME'"

log "docker status snapshot"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}' | sort"

log "lifecycle verification"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
  "python3 '$REMOTE_DIR/scripts/axonctl.py' lifecycle-report --state-file '$REMOTE_DIR/state/deploy_state.json' --network '$REMOTE_DIR/configs/network.yaml'"

log "release flow completed for commit $LOCAL_SHORT"
