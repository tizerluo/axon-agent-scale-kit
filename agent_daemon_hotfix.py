#!/usr/bin/env python3
"""
QQClaw Daemon Hotfix Patch — AI Challenge Window Heartbeat Fix
==============================================================

File:   agent_daemon_hotfix.py
Target: /opt/axon-node/scripts/agent_daemon.py
Method: participate_ai_challenge()  (around line 235)

PROBLEM:
  The original code sends a heartbeat() transaction inside the AI Challenge window,
  violating the chain's HeartbeatInterval constraint (≥ 100 blocks between heartbeats).
  Each epoch ~50 detections × every 5 s = ~50 failed transactions per epoch with
  revert: "heartbeat sent too frequently".

FIX:
  Replace the _send_tx(heartbeat()) call with a pure-info log.
  AI Challenge participation is covered by heartbeat-daemon's normal heartbeat
  (~500 s interval) which triggers IncrementEpochActivity() on-chain.

USAGE:
  1. Read this file and the ORIGINAL agent_daemon.py
  2. Apply the REPLACEMENT below to agent_daemon.py
  3. Backup:  cp /opt/axon-node/scripts/agent_daemon.py /opt/axon-node/scripts/agent_daemon.py.bak
  4. Push:    scp agent_daemon_hotfix.py ubuntu@43.165.195.71:/tmp/
  5. SSH in and replace the method body
  6. Restart: sudo systemctl restart axon-agent-qqclaw.service
  7. Verify:  journalctl -u axon-agent-qqclaw.service -n 200 --no-pager | grep "heartbeat sent too frequent" | wc -l
     Expected: 0
"""

# ===========================================================================
# BEFORE (original — REMOVE this block)
# ===========================================================================
# def participate_ai_challenge(self, current_block: int):
#     block_in_epoch = current_block % EPOCH_BLOCKS
#     in_challenge_window = block_in_epoch < AI_CHALLENGE_WINDOW
#     if in_challenge_window:
#         blocks_since_last = current_block - self.last_heartbeat_block
#         if blocks_since_last > 10:
#             tx = self.registry.functions.heartbeat().build_transaction(self._tx_params())
#             tx_hash = self._send_tx(tx)   # ← REVERTS with "heartbeat sent too frequently"
#             logger.info(f"[AI Challenge] heartbeat tx={tx_hash}")

# ===========================================================================
# AFTER (fixed — REPLACE the entire method body)
# ===========================================================================
def participate_ai_challenge(self, current_block: int):
    block_in_epoch = current_block % EPOCH_BLOCKS
    in_challenge_window = block_in_epoch < AI_CHALLENGE_WINDOW
    if in_challenge_window:
        blocks_since_last = current_block - self.last_heartbeat_block
        # FIX: Do NOT send heartbeat here — it would revert "heartbeat sent too frequently".
        # AI Challenge participation is covered by heartbeat-daemon's normal heartbeat
        # (~500 s, HeartbeatInterval = 100 blocks), which triggers IncrementEpochActivity().
        # This daemon only needs to watch the window and log; heartbeats come from
        # the unified heartbeat-daemon, not from this validator daemon.
        logger.info(
            f"[AI Challenge Window] epoch_offset={block_in_epoch}/{AI_CHALLENGE_WINDOW}, "
            f"blocks_since_last_heartbeat={blocks_since_last}. "
            f"Heartbeat participation handled by heartbeat-daemon (not this daemon) "
            f"to avoid 'heartbeat sent too frequently' revert."
        )

# ===========================================================================
# NOTES
# ===========================================================================
#
# EPOCH_BLOCKS and AI_CHALLENGE_WINDOW constants are defined at the top of
# agent_daemon.py:
#   EPOCH_BLOCKS         = 720
#   AI_CHALLENGE_WINDOW  = 50   (commit window; reveal window = 50-100)
#
# The heartbeat-daemon (axon-heartbeat-daemon.service / axonctl.py heartbeat-daemon)
# manages heartbeat timing globally with interval_blocks = 100.
#
# After deploying this fix, the intended long-term migration is:
#   1. Import QQClaw validator private key into scale-kit state/deploy_state.json
#   2. heartbeat-daemon picks up qqclaw-validator automatically
#   3. Decommission axon-agent-qqclaw.service entirely
# ===========================================================================
