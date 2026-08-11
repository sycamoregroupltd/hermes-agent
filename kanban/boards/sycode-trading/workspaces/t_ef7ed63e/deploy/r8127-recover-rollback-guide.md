---
title: "r8127-recover rollback guide — Task t_c1693188"
type: runbook
status: active
created: 2026-08-11
updated: 2026-08-11
confidence: high
tags:
  - r8127
  - dgx
  - network
  - rollback
  - runbook
sources:
  - "/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy/r8127-recover.sh"
  - "/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy/r8127-link-watchdog.sh"
  - "/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy/README.md"
  - "/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy/r8127-offload-fix.sh"
project: sycode-trading
owners:
  - trading-devops
review_after: 2026-09-11
---

# r8127-recover rollback guide — revert offload settings + restore previous network state

> **Root-gated.** All steps below require `sudo` and CAP_NET_ADMIN. Run as Frank
> or another authorized operator. This guide is for on-call use during an
> active incident — do not test casually.
>
> **Companion to:** `deploy/r8127-recover.sh`, `deploy/r8127-link-watchdog.sh`,
> `deploy/r8127-offload-fix.sh`.

## Trigger conditions for this rollback

Use this guide when:

- The recovery script (`r8127-recover.sh`) fails with exit code 3, 4, or 5
  AND the on-call team needs to manually revert the network to the pre-watchdog
  state (offloads back on, link reset, timer disabled).
- A fleet dashboard alert (`[r8127-recover] recovery failure exit N`) has fired.
- The watchdog has escalated and the operator wants to disable it before
  manual investigation.

Rollback does **not** revert the throttle or last_recover timestamp; those
are tmpfs and lost on reboot anyway.

---

## Step 1 — Disable the watchdog timer (stop further auto-recoveries)

```bash
sudo systemctl disable --now r8127-link-watchdog.timer
```

Verify stopped:

```bash
systemctl is-active r8127-link-watchdog.timer   # expect: inactive
```

---

## Step 2 — Re-enable TSO/GSO/GRO offload (undo the mitigation)

```bash
# Verify current offload state:
ethtool -k enP7s7 | grep -iE 'tcp-segmentation-offload:|generic-segmentation-offload:|generic-receive-offload:'

# Re-enable offloads (restore pre-watchdog state):
sudo ethtool -K enP7s7 tso on gso on gro on

# Confirm:
ethtool -k enP7s7 | grep -iE 'tcp-segmentation-offload:|generic-segmentation-offload:|generic-receive-offload:'
# expect all: on
```

---

## Step 3 — Restore the previous network link state

If the recovery script left the interface down or in an unknown state:

```bash
# Check current state:
ip link show enP7s7
cat /sys/class/net/enP7s7/operstate 2>/dev/null || echo "missing"

# Bring up (if down):
sudo ip link set enP7s7 up

# If default route is missing, restore via NetworkManager or dhcpcd:
sudo nmcli connection reload 2>/dev/null || true
sudo nmcli device connect enP7s7 2>/dev/null || true
# fallback: restart NetworkManager (will re-activate all connections)
sudo systemctl restart NetworkManager 2>/dev/null || true
```

Verify:

```bash
ip route show default | grep -q "dev enP7s7" && echo "default route OK" || echo "default route MISSING"
cat /sys/class/net/enP7s7/operstate 2>/dev/null
```

---

## Step 4 — Remove the NetworkManager dispatcher hook (anti-offload persistence)

```bash
sudo rm -f /etc/NetworkManager/dispatcher.d/r8127-offload-fix
sudo systemctl reload NetworkManager 2>/dev/null || true
```

This stops the NM dispatcher from re-disabling offloads on every link event.

---

## Step 5 — Stop the config-guard service (if it was enabled)

```bash
sudo systemctl disable --now sycode-config-guard.service 2>/dev/null || true
```

Only necessary if the full `install.sh` package was applied. The recovery
script itself does not touch config-guard, so this step is usually skipped
when rolling back a recovery-only intervention.

---

## Step 6 — Restart the watchdog (optional: roll back to observing only)

If the goal is to keep the watchdog in **passive observation mode** without
auto-recovery actions:

1. Leave `r8127-link-watchdog.timer` disabled (from Step 1).
2. The probe script (`r8127-watchdog.sh`) reports health but does not mutate
   state when `DO_RECOVER=0` (the default). To re-enable passive monitoring
   without recovery actions:

```bash
sudo systemctl enable --now r8127-link-watchdog.timer
# Edit the unit to pass DO_RECOVER=0 explicitly if needed:
sudo systemctl edit r8127-link-watchdog.service
# Add:
# [Service]
# Environment=DO_RECOVER=0
sudo systemctl daemon-reload
sudo systemctl restart r8127-link-watchdog.timer
```

---

## Step 7 — Verify full rollback

```bash
echo "=== Offload state (expect tso/gso/gro: on) ==="
ethtool -k enP7s7 | grep -iE 'tcp-segmentation-offload:|generic-segmentation-offload:|generic-receive-offload:'

echo "=== NM dispatcher hook (expect absent) ==="
ls -la /etc/NetworkManager/dispatcher.d/r8127-offload-fix 2>/dev/null || echo "absent (OK)"

echo "=== Watchdog timer (expect inactive) ==="
systemctl is-active r8127-link-watchdog.timer

echo "=== Link state ==="
ip link show enP7s7
cat /sys/class/net/enP7s7/operstate 2>/dev/null

echo "=== Default route ==="
ip route show default | grep "dev enP7s7" || echo "no default via enP7s7"
```

---

## Full re-install (if rollback was incorrect)

If the rollback was performed in error and the protection needs to be
re-applied:

```bash
cd /home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy
sudo bash install.sh
```

The installer re-applies offload disable, re-creates the dispatcher hook,
re-enables the timer, and re-enables the config-guard.

---

## Notes

- **Tempfs state dir** `/run/r8127-watchdog` is lost on reboot — expected,
  the watchdog starts fresh after reboot without stale throttle state.
- **NM dispatcher hook** is the persistent part of the mitigation. Removing
  it means offloads will stay ON until manually toggled or the host reboots
  and the hook is re-created by re-install.
- **Driver reload (`modprobe -r)`** is only ever attempted when
  `WATCHDOG_DRIVER_RELOAD=1` is set AND the module is in `lsmod`. The rollback
  does NOT reload the driver — if the recovery partially reloaded the driver
  and things are unstable, a manual `sudo modprobe -r r8127 && sudo modprobe r8127`
  followed by re-applying offloads can be used as a separate corrective step.
- **Logger / syslog**: recovery events are tagged `r8127-recover[enP7s7]` in
  syslog (facility user.crit for errors, user.warning for warnings). Journal
  entries show under `systemd` unit output if run as a timer-triggered service.
