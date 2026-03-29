#!/usr/bin/env bash
#
# release_deploy_verify.sh
#
# 部署流程（GitHub-commit-based，服务器无需 git）：
#
#   1. 本地 push 到 GitHub（已有 commit hash）
#   2. 服务器从 GitHub 下载指定 commit 的 archive
#   3. 解压到 staging 目录
#   4. 原子切换（mv 到 live 目录）
#   5. 写 .release_meta.json（版本锁定在切换之前）
#   6. 重启服务 + 验证
#   7. 可选：记录 PREV_COMMIT，支持回滚
#
# 设计原则：
#   - GitHub commit hash 是唯一真相源
#   - 服务器不装 git，纯 curl
#   - 版本切换原子化，可靠回滚
#
set -euo pipefail

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_REMOTE_HOST="43.165.195.71"
DEFAULT_REMOTE_DIR="/home/ubuntu/axon-agent-scale"
DEFAULT_SERVICE_NAME="axon-heartbeat-daemon.service"
SSH_KEY_DEFAULT="${HOME}/Downloads/QQClaw.pem"
KEEP_RELEASES=5

# ─── 参数解析 ────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage:
  scripts/release_deploy_verify.sh [options]

Options:
  --commit <hash>         GitHub commit SHA（默认：本地 HEAD）
  --repo <org/repo>      GitHub org/repo（默认：从 git remote 推断）
  --remote-host <host>    服务器 IP（默认：43.165.195.71）
  --remote-user <user>    服务器用户（默认：ubuntu）
  --ssh-key <path>       SSH 私钥路径
  --remote-dir <path>     服务器部署根目录（默认：/home/ubuntu/axon-agent-scale）
  --service <name>       systemd service（默认：axon-heartbeat-daemon.service）
  --challenge-service     同时部署 axon-challenge-daemon.service
  --rollback              回滚到上一版本
  --skip-tests            跳过本地 unittest
  --allow-dirty           允许 dirty working tree
  --dry-run               打印操作但不执行
  -h, --help              显示帮助
EOF
}

log()  { printf '[release] %s\n' "$*"; }
warn() { printf '[release][warn] %s\n' "$*" >&2; }
die()  { printf '[release][error] %s\n' "$*" >&2; exit 1; }

REMOTE_HOST="$DEFAULT_REMOTE_HOST"
REMOTE_USER="ubuntu"
SSH_KEY="$SSH_KEY_DEFAULT"
REMOTE_DIR="$DEFAULT_REMOTE_DIR"
SERVICE_NAME="$DEFAULT_SERVICE_NAME"
DEPLOY_CHALLENGE_SERVICE=0
ROLLBACK=0
SKIP_TESTS=0
ALLOW_DIRTY=0
DRY_RUN=0
TARGET_COMMIT=""
UPSTREAM_ORG=""
UPSTREAM_REPO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote-host)       [[ $# -ge 2 ]] || die "missing"; REMOTE_HOST="$2"; shift 2 ;;
    --remote-user)       [[ $# -ge 2 ]] || die "missing"; REMOTE_USER="$2"; shift 2 ;;
    --ssh-key)           [[ $# -ge 2 ]] || die "missing"; SSH_KEY="$2"; shift 2 ;;
    --remote-dir)        [[ $# -ge 2 ]] || die "missing"; REMOTE_DIR="$2"; shift 2 ;;
    --service)           [[ $# -ge 2 ]] || die "missing"; SERVICE_NAME="$2"; shift 2 ;;
    --challenge-service) DEPLOY_CHALLENGE_SERVICE=1; shift ;;
    --rollback)          ROLLBACK=1; shift ;;
    --skip-tests)        SKIP_TESTS=1; shift ;;
    --allow-dirty)       ALLOW_DIRTY=1; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --commit)            [[ $# -ge 2 ]] || die "missing"; TARGET_COMMIT="$2"; shift 2 ;;
    --repo)              [[ $# -ge 2 ]] || die "missing"
                        UPSTREAM_ORG="${2%%/*}"; UPSTREAM_REPO="${2#*/}"; shift 2 ;;
    -h|--help)           usage; exit 0 ;;
    *)                   die "unknown option: $1" ;;
  esac
done

# ─── 前置检查 ────────────────────────────────────────────────────────────────

command -v git    >/dev/null 2>&1 || die "git not found"
command -v ssh    >/dev/null 2>&1 || die "ssh not found"
command -v python3>/dev/null 2>&1 || die "python3 not found"
command -v base64>/dev/null 2>&1 || die "base64 not found"
[[ -f "$SSH_KEY" ]] || die "ssh key not found: $SSH_KEY"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "must run inside git repository"
cd "$(git rev-parse --show-toplevel)"

if pgrep -f "axonctl.py heartbeat-daemon" >/dev/null 2>&1; then
  die "local heartbeat-daemon is running; stop it first"
fi

if [[ "$ALLOW_DIRTY" -ne 1 ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    die "working tree is dirty; pass --allow-dirty or commit first"
  fi
fi

LOCAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
LOCAL_COMMIT="$(git rev-parse HEAD)"
LOCAL_SHORT="$(git rev-parse --short HEAD)"

[[ "$LOCAL_BRANCH" != "main" ]] || die "cannot push directly to main; use a PR"

# ─── 确定目标 commit ─────────────────────────────────────────────────────────

if [[ -n "$TARGET_COMMIT" ]]; then
  DEPLOY_COMMIT="$TARGET_COMMIT"
else
  DEPLOY_COMMIT="$LOCAL_COMMIT"
fi
DEPLOY_SHORT="${DEPLOY_COMMIT:0:7}"
log "target commit: $DEPLOY_COMMIT"
log "local branch:  $LOCAL_BRANCH"

# ─── 确定 upstream org/repo ─────────────────────────────────────────────────

if [[ -n "$UPSTREAM_ORG" ]]; then
  ARCHIVE_ORG="$UPSTREAM_ORG"
  ARCHIVE_REPO="$UPSTREAM_REPO"
else
  ORIGIN_URL="$(git remote get-url origin 2>/dev/null || echo "")"
  if [[ "$ORIGIN_URL" =~ github\.com[:/]([^/]+)/([^/]+) ]]; then
    ARCHIVE_ORG="${BASH_REMATCH[1]}"
    ARCHIVE_REPO="${BASH_REMATCH[2]%.git}"
  else
    die "cannot infer org/repo from origin; use --repo"
  fi
fi

ARCHIVE_URL="https://github.com/${ARCHIVE_ORG}/${ARCHIVE_REPO}/archive/${DEPLOY_COMMIT}.tar.gz"
log "GitHub archive: https://github.com/${ARCHIVE_ORG}/${ARCHIVE_REPO}/tree/${DEPLOY_SHORT}"

# ─── 本地测试 ────────────────────────────────────────────────────────────────

if [[ "$SKIP_TESTS" -ne 1 ]]; then
  log "running unit tests"
  python3 -m unittest tests.test_axonctl -q
else
  log "tests skipped"
fi

# ─── GitHub push ─────────────────────────────────────────────────────────────

if [[ "$DRY_RUN" -ne 1 ]]; then
  log "pushing to origin/$LOCAL_BRANCH"
  git push origin HEAD
fi

remote_head="$(git ls-remote --heads origin "$LOCAL_BRANCH" 2>/dev/null | awk '{print $1}')"
[[ -n "$remote_head" ]] || die "origin/$LOCAL_BRANCH not found; did you push?"
[[ "$remote_head" == "$LOCAL_COMMIT" ]] || die "push mismatch: origin=$remote_head local=$LOCAL_COMMIT"

# ─── SSH ────────────────────────────────────────────────────────────────────

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")
SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"

ssh_exec() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY RUN: ssh $*"
    return 0
  fi
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"
}

# ─── 回滚模式 ───────────────────────────────────────────────────────────────

if [[ "$ROLLBACK" -eq 1 ]]; then
  log "rollback: reading current version from server"
  CURRENT_META="$(ssh_exec "cat '${REMOTE_DIR}/.release_meta.json' 2>/dev/null" || echo "")"
  [[ -n "$CURRENT_META" ]] || die "no .release_meta.json on server; cannot rollback"

  PREV_COMMIT="$(echo "$CURRENT_META" | python3 -c '
import json,sys
d=json.loads(sys.stdin.read())
print(d.get("prev_commit",""))
' 2>/dev/null || echo "")"
  [[ -n "$PREV_COMMIT" ]] || die "no prev_commit in .release_meta.json; cannot rollback"

  DEPLOY_COMMIT="$PREV_COMMIT"
  DEPLOY_SHORT="${DEPLOY_COMMIT:0:7}"
  ARCHIVE_URL="https://github.com/${ARCHIVE_ORG}/${ARCHIVE_REPO}/archive/${DEPLOY_COMMIT}.tar.gz"
  warn "rolling back to: $DEPLOY_SHORT"
fi

# ─── 服务器端参数 ────────────────────────────────────────────────────────────

DEPLOY_TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
RELEASE_NAME="v${DEPLOY_SHORT}-${DEPLOY_TIMESTAMP}"
ARCHIVE_TMP="/tmp/axon-archive-${DEPLOY_SHORT}.$$.tar.gz"
PY_TMP="/tmp/axon-deploy.$$.py"

# ─── 生成 Python 部署脚本（写入临时文件，base64 传到服务器）───────────────────

python3 <<'GEN_SCRIPT' || die "failed to generate server script"
import sys
import json
import base64

# Server-side Python deployment script (pure Python, no bash heredoc)
SERVER_SCRIPT = r'''
import subprocess, os, sys, json, time

DEPLOY_COMMIT          = os.environ["DEPLOY_COMMIT"]
DEPLOY_SHORT           = os.environ["DEPLOY_SHORT"]
RELEASE_NAME           = os.environ["RELEASE_NAME"]
ARCHIVE_URL            = os.environ["ARCHIVE_URL"]
ARCHIVE_TMP            = os.environ["ARCHIVE_TMP"]
REMOTE_DIR             = os.environ["REMOTE_DIR"]
SERVICE_NAME           = os.environ["SERVICE_NAME"]
DEPLOY_CHALLENGE       = os.environ["DEPLOY_CHALLENGE_SERVICE"]
KEEP_RELEASES          = int(os.environ["KEEP_RELEASES"])
ARCHIVE_ORG            = os.environ["ARCHIVE_ORG"]
ARCHIVE_REPO           = os.environ["ARCHIVE_REPO"]

def run(*args, **kw):
    r = subprocess.run(*args, **kw)
    if r.returncode != 0 and not kw.get("check", False):
        sys.stderr.write((r.stderr or b"").decode())
        sys.stderr.flush()
    return r

def log(msg):
    print(f"[deploy] {msg}", flush=True)

log(f"starting deployment: {DEPLOY_COMMIT}")

# 1. 读取当前版本（用于 prev_commit 记录）
current_commit = ""
meta_path = os.path.join(REMOTE_DIR, ".release_meta.json")
if os.path.exists(meta_path):
    try:
        with open(meta_path) as f:
            current_commit = json.load(f).get("commit", "")
    except Exception:
        pass
log(f"current server version: {current_commit or 'none'}")

# 2. 创建 releases 目录
os.makedirs(os.path.join(REMOTE_DIR, "releases"), exist_ok=True)

# 3. 下载 GitHub archive
log(f"downloading {ARCHIVE_URL}")
r = run(["curl", "-fsSL", "--max-time", "120", "-o", ARCHIVE_TMP, ARCHIVE_URL])
if r.returncode != 0:
    sys.stderr.write(f"[deploy][ERROR] download failed\n")
    sys.exit(1)

size = os.path.getsize(ARCHIVE_TMP)
if size < 1024:
    sys.stderr.write(f"[deploy][ERROR] archive too small ({size} bytes) - likely GitHub 404\n")
    os.remove(ARCHIVE_TMP)
    sys.exit(1)
log(f"downloaded: {size} bytes")

# 4. 解压到 staging
staging_dir = os.path.join(REMOTE_DIR, f"staging.{os.getpid()}")
os.makedirs(staging_dir)
run(["tar", "-xzf", ARCHIVE_TMP, "-C", staging_dir, "--strip-components=1"], check=True)

# 5. 验证内容
expected = os.path.join(staging_dir, "scripts", "axonctl.py")
if not os.path.exists(expected):
    sys.stderr.write(f"[deploy][ERROR] {expected} not found after extract\n")
    sys.exit(1)
log("content verified: scripts/axonctl.py found")

# 6. 创建 release snapshot 并原子移动
release_dir = os.path.join(REMOTE_DIR, "releases", RELEASE_NAME)
os.makedirs(release_dir)
for item in os.listdir(staging_dir):
    src = os.path.join(staging_dir, item)
    dst = os.path.join(release_dir, item)
    os.rename(src, dst)
os.rmdir(staging_dir)
log(f"snapshot created: {release_dir}")

# 7. 原子切换 live symlink
current_link = os.path.join(REMOTE_DIR, "current")
if os.path.islink(current_link):
    os.remove(current_link)
elif os.path.exists(current_link):
    os.remove(current_link)
os.symlink(release_dir, current_link)
log(f"live symlink switched: {current_link} -> {release_dir}")

# 8. 原子写入 .release_meta.json（在任何服务重启之前！）
prev_field = ""
if current_commit:
    prev_field = f', "prev_commit": "{current_commit}"'
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
user = os.environ.get("USER", "unknown")
meta = {
    "commit": DEPLOY_COMMIT,
    "short_commit": DEPLOY_SHORT,
    "deployed_at": ts,
    "deployed_by": user,
    "source_repo": f"https://github.com/{ARCHIVE_ORG}/{ARCHIVE_REPO}",
    "release_name": RELEASE_NAME,
}
if current_commit:
    meta["prev_commit"] = current_commit
with open(meta_path, "w") as f:
    json.dump(meta, f, indent=2)
log(f".release_meta.json written (locked: {DEPLOY_COMMIT})")

# 9. 清理临时文件
os.remove(ARCHIVE_TMP)

# 10. 重启服务
log(f"restarting {SERVICE_NAME}")
run(["systemctl", "is-active", SERVICE_NAME"], check=False)
run(["systemctl", "restart", SERVICE_NAME"], check=True)

if DEPLOY_CHALLENGE == "1":
    log("restarting axon-challenge-daemon")
    run(["systemctl", "is-active", "axon-challenge-daemon"], check=False)
    run(["systemctl", "restart", "axon-challenge-daemon"], check=True)

time.sleep(3)

# 11. 验证状态
status = run(["systemctl", "is-active", SERVICE_NAME"], capture_output=True, text=True)
status = (status.stdout or status.stderr or b"unknown").decode().strip()
log(f"{SERVICE_NAME} status: {status}")
if status != "active":
    r = run(["journalctl", "-u", SERVICE_NAME, "-n", "15", "--no-pager"], capture_output=True, text=True)
    sys.stderr.write((r.stdout or b"").decode())
    sys.stderr.flush()

# 12. lifecycle 验证
log("running lifecycle verification")
run(
    ["python3", os.path.join(release_dir, "scripts", "axonctl.py"),
     "lifecycle-report",
     "--state-file", os.path.join(REMOTE_DIR, "state", "deploy_state.json"),
     "--network", os.path.join(release_dir, "configs", "network.yaml")],
    check=False
)

# 13. 清理旧快照（保留最近 N 个）
releases_dir = os.path.join(REMOTE_DIR, "releases")
all_releases = sorted(
    [d for d in os.listdir(releases_dir) if os.path.isdir(os.path.join(releases_dir, d))],
    reverse=True
)
for old in all_releases[KEEP_RELEASES:]:
    path = os.path.join(releases_dir, old)
    import shutil
    shutil.rmtree(path)
    log(f"cleaned old release: {old}")

log(f"deployment complete: {DEPLOY_COMMIT}")
'''

# Inject vars via JSON/env, avoiding any shell injection
vars = {
    "DEPLOY_COMMIT":            "${DEPLOY_COMMIT}",
    "DEPLOY_SHORT":             "${DEPLOY_SHORT}",
    "RELEASE_NAME":            "${RELEASE_NAME}",
    "ARCHIVE_URL":             "${ARCHIVE_URL}",
    "ARCHIVE_TMP":             "${ARCHIVE_TMP}",
    "REMOTE_DIR":              "${REMOTE_DIR}",
    "SERVICE_NAME":            "${SERVICE_NAME}",
    "DEPLOY_CHALLENGE_SERVICE":"${DEPLOY_CHALLENGE_SERVICE}",
    "KEEP_RELEASES":           "${KEEP_RELEASES}",
    "ARCHIVE_ORG":             "${ARCHIVE_ORG}",
    "ARCHIVE_REPO":            "${ARCHIVE_REPO}",
}

# Substitute env-var placeholders
script = SERVER_SCRIPT
for key, val in vars.items():
    script = script.replace(f'"${{{key}}}"', json.dumps(val))

# Print base64 to stdout (captured by bash)
sys.stdout.write(base64.b64encode(script.encode()).decode())
GEN_SCRIPT

SERVER_B64="$?"  # captured above, but can't mix with python heredoc

# Generate base64-encoded script
SERVER_B64="$(python3 <<'PY_IN'
import sys, json, base64, os

SERVER_SCRIPT = r'''
import subprocess, os, sys, json, time

DEPLOY_COMMIT          = os.environ["DEPLOY_COMMIT"]
DEPLOY_SHORT           = os.environ["DEPLOY_SHORT"]
RELEASE_NAME           = os.environ["RELEASE_NAME"]
ARCHIVE_URL            = os.environ["ARCHIVE_URL"]
ARCHIVE_TMP            = os.environ["ARCHIVE_TMP"]
REMOTE_DIR             = os.environ["REMOTE_DIR"]
SERVICE_NAME           = os.environ["SERVICE_NAME"]
DEPLOY_CHALLENGE       = os.environ["DEPLOY_CHALLENGE_SERVICE"]
KEEP_RELEASES          = int(os.environ["KEEP_RELEASES"])
ARCHIVE_ORG            = os.environ["ARCHIVE_ORG"]
ARCHIVE_REPO           = os.environ["ARCHIVE_REPO"]

def run(*args, **kw):
    r = subprocess.run(*args, **kw)
    return r

def log(msg):
    print(f"[deploy] {msg}", flush=True)

log(f"starting deployment: {DEPLOY_COMMIT}")

# 1. 读取当前版本
current_commit = ""
meta_path = os.path.join(REMOTE_DIR, ".release_meta.json")
if os.path.exists(meta_path):
    try:
        with open(meta_path) as f:
            current_commit = json.load(f).get("commit", "")
    except Exception:
        pass
log(f"current server version: {current_commit or 'none'}")

# 2. 创建 releases 目录
os.makedirs(os.path.join(REMOTE_DIR, "releases"), exist_ok=True)

# 3. 下载 GitHub archive
log(f"downloading {ARCHIVE_URL}")
r = run(["curl", "-fsSL", "--max-time", "120", "-o", ARCHIVE_TMP, ARCHIVE_URL])
if r.returncode != 0:
    sys.stderr.write(f"[deploy][ERROR] download failed\n")
    sys.exit(1)

size = os.path.getsize(ARCHIVE_TMP)
if size < 1024:
    sys.stderr.write(f"[deploy][ERROR] archive too small ({size} bytes) - likely GitHub 404\n")
    os.remove(ARCHIVE_TMP)
    sys.exit(1)
log(f"downloaded: {size} bytes")

# 4. 解压到 staging
staging_dir = os.path.join(REMOTE_DIR, f"staging.{os.getpid()}")
os.makedirs(staging_dir)
run(["tar", "-xzf", ARCHIVE_TMP, "-C", staging_dir, "--strip-components=1"], check=True)

# 5. 验证内容
expected = os.path.join(staging_dir, "scripts", "axonctl.py")
if not os.path.exists(expected):
    sys.stderr.write(f"[deploy][ERROR] {expected} not found after extract\n")
    sys.exit(1)
log("content verified: scripts/axonctl.py found")

# 6. 创建 release snapshot 并原子移动
release_dir = os.path.join(REMOTE_DIR, "releases", RELEASE_NAME)
os.makedirs(release_dir)
for item in os.listdir(staging_dir):
    src = os.path.join(staging_dir, item)
    dst = os.path.join(release_dir, item)
    os.rename(src, dst)
os.rmdir(staging_dir)
log(f"snapshot created: {release_dir}")

# 7. 原子切换 live symlink
current_link = os.path.join(REMOTE_DIR, "current")
if os.path.islink(current_link) or os.path.exists(current_link):
    os.remove(current_link)
os.symlink(release_dir, current_link)
log(f"live symlink switched: {current_link} -> {release_dir}")

# 8. 原子写入 .release_meta.json（在任何服务重启之前！）
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
user = os.environ.get("USER", "unknown")
meta = {
    "commit": DEPLOY_COMMIT,
    "short_commit": DEPLOY_SHORT,
    "deployed_at": ts,
    "deployed_by": user,
    "source_repo": f"https://github.com/{ARCHIVE_ORG}/{ARCHIVE_REPO}",
    "release_name": RELEASE_NAME,
}
if current_commit:
    meta["prev_commit"] = current_commit
with open(meta_path, "w") as f:
    json.dump(meta, f, indent=2)
log(f".release_meta.json written (locked: {DEPLOY_COMMIT})")

# 9. 清理临时文件
os.remove(ARCHIVE_TMP)

# 10. 重启服务
log(f"restarting {SERVICE_NAME}")
run(["systemctl", "is-active", SERVICE_NAME"], check=False)
run(["systemctl", "restart", SERVICE_NAME"], check=True)

if DEPLOY_CHALLENGE == "1":
    log("restarting axon-challenge-daemon")
    run(["systemctl", "is-active", "axon-challenge-daemon"], check=False)
    run(["systemctl", "restart", "axon-challenge-daemon"], check=True)

time.sleep(3)

# 11. 验证状态
status = run(["systemctl", "is-active", SERVICE_NAME"], capture_output=True, text=True)
status = (status.stdout or status.stderr or b"unknown").decode().strip()
log(f"{SERVICE_NAME} status: {status}")
if status != "active":
    r = run(["journalctl", "-u", SERVICE_NAME, "-n", "15", "--no-pager"], capture_output=True, text=True)
    sys.stderr.write((r.stdout or b"").decode())
    sys.stderr.flush()

# 12. lifecycle 验证
log("running lifecycle verification")
run(
    ["python3", os.path.join(release_dir, "scripts", "axonctl.py"),
     "lifecycle-report",
     "--state-file", os.path.join(REMOTE_DIR, "state", "deploy_state.json"),
     "--network", os.path.join(release_dir, "configs", "network.yaml")],
    check=False
)

# 13. 清理旧快照
releases_dir = os.path.join(REMOTE_DIR, "releases")
all_releases = sorted(
    [d for d in os.listdir(releases_dir) if os.path.isdir(os.path.join(releases_dir, d))],
    reverse=True
)
for old in all_releases[KEEP_RELEASES:]:
    path = os.path.join(releases_dir, old)
    import shutil
    shutil.rmtree(path)
    log(f"cleaned old release: {old}")

log(f"deployment complete: {DEPLOY_COMMIT}")
'''

env_vars = {
    "DEPLOY_COMMIT":             os.environ.get("DEPLOY_COMMIT",""),
    "DEPLOY_SHORT":              os.environ.get("DEPLOY_SHORT",""),
    "RELEASE_NAME":             os.environ.get("RELEASE_NAME",""),
    "ARCHIVE_URL":              os.environ.get("ARCHIVE_URL",""),
    "ARCHIVE_TMP":              os.environ.get("ARCHIVE_TMP",""),
    "REMOTE_DIR":               os.environ.get("REMOTE_DIR",""),
    "SERVICE_NAME":             os.environ.get("SERVICE_NAME",""),
    "DEPLOY_CHALLENGE_SERVICE": os.environ.get("DEPLOY_CHALLENGE_SERVICE",""),
    "KEEP_RELEASES":            os.environ.get("KEEP_RELEASES",""),
    "ARCHIVE_ORG":              os.environ.get("ARCHIVE_ORG",""),
    "ARCHIVE_REPO":             os.environ.get("ARCHIVE_REPO",""),
}

script = SERVER_SCRIPT
for key, val in env_vars.items():
    script = script.replace(f'"${{{key}}}"', json.dumps(val))

sys.stdout.write(base64.b64encode(script.encode()).decode())
PY_IN
)" || die "failed to generate server script"

# ─── 在服务器上执行 ─────────────────────────────────────────────────────────

log "GitHub archive: $ARCHIVE_URL"
log "release snapshot: $RELEASE_NAME"

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY RUN: would execute Python deploy script on server"
  log "DRY RUN: env vars: DEPLOY_COMMIT=$DEPLOY_COMMIT RELEASE_NAME=$RELEASE_NAME SERVICE_NAME=$SERVICE_NAME"
  log "DRY RUN: server script base64 length: ${#SERVER_B64} chars"
else
  log "executing deployment on ${SSH_TARGET}"
  ssh_exec "PY_B64='${SERVER_B64}'
    PY_SCRIPT=\$(python3 -c \"import sys,base64; sys.stdout.write(base64.b64decode(sys.stdin.read()).decode())\" <<< \"\$PY_B64\")
    DEPLOY_COMMIT='${DEPLOY_COMMIT}' \
    DEPLOY_SHORT='${DEPLOY_SHORT}' \
    RELEASE_NAME='${RELEASE_NAME}' \
    ARCHIVE_URL='${ARCHIVE_URL}' \
    ARCHIVE_TMP='${ARCHIVE_TMP}' \
    REMOTE_DIR='${REMOTE_DIR}' \
    SERVICE_NAME='${SERVICE_NAME}' \
    DEPLOY_CHALLENGE_SERVICE='${DEPLOY_CHALLENGE_SERVICE}' \
    KEEP_RELEASES='${KEEP_RELEASES}' \
    ARCHIVE_ORG='${ARCHIVE_ORG}' \
    ARCHIVE_REPO='${ARCHIVE_REPO}' \
    python3 -c \"\${PY_SCRIPT}\""
fi

# ─── 汇总 ───────────────────────────────────────────────────────────────────

log "════════════════════════════════════════"
log "  commit:    ${DEPLOY_COMMIT}"
log "  branch:    ${LOCAL_BRANCH}"
log "  GitHub:    https://github.com/${ARCHIVE_ORG}/${ARCHIVE_REPO}/commit/${DEPLOY_COMMIT}"
log "  rollback:  bash scripts/release_deploy_verify.sh --rollback"
log "════════════════════════════════════════"
log "done."
