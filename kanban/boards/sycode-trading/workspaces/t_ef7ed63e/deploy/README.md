# DGX Network Hardening Package — t_ef7ed63e

Structural fixes for the 2026-07-09 DGX outage: r8127 NIC driver tx-timeout
took the box fully offline (~07:23–08:47, unreachable on LAN+Starlink+Tailscale
until Frank power-cycled), and at boot Docker created phantom root-owned dirs over
4 missing bind-mount config files (kong/prometheus/tempo), killing those services
(GAP1).

## Evidence (boot -1, journalctl)
- `Jul 09 07:22:48 r8127: enP7s7: link down`
- `Jul 09 07:23:54 r8127 ... NETDEV WATCHDOG: CPU: 6: transmit queue 0 timed out 6144 ms`
- `Jul 09 07:23:54 r8127 ... Transmit timeout reset Device!`
- Link never recovered; box unreachable until power-cycle at ~08:47.
- Class: r8127 tx-timeout (TSO/GSO/GRO offload on Realtek 2.5G NIC).

## Files in this package
| File | Purpose | Option |
|------|---------|--------|
| `r8127-offload-fix.sh` | Installer — NM dispatcher hook disabling TSO/GSO/GRO on enP7s7 up | (1) |
| `r8127-link-watchdog.sh` | Watchdog: bounce link if route gone + carrier down >120s; reload driver after 3 fails; escalate | (3) |
| `r8127-link-watchdog.service` | oneshot systemd unit | (3) |
| `r8127-link-watchdog.timer` | 60s timer, Persistent, After=network-online | (3) |
| `seed-sycode-config.sh` | Seed deploy-owned config dir from origin/main (task t_70eb2622) | (4) |
| `sycode-config-guard.sh` | Two-tier guard: verify deploy-owned dir + compose-relative paths; restore from origin/main | (4) |
| `sycode-config-guard.service` | oneshot, `Before=docker.service`, seeds + verifies config; fails loudly | (4) |
| `install.sh` | Root installer wiring all of the above | — |

Live bind-mount sources confirmed on this host:
- `supabase/kong.yml` → sycodetrading-supabase-kong
- `monitoring/prometheus/recording-rules.yml` → sycodetrading-prometheus
- `monitoring/prometheus/slo-rules.yml` → sycodetrading-prometheus
- `monitoring/tempo/tempo.yml` → sycodetrading-tempo

## Why these mechanisms
- enP7s7 is **NetworkManager-owned** (`nmcli device` shows connected/external), so a
  NetworkManager `dispatcher.d` hook is the correct persistent place for `ethtool -K`
  (survives reboots + re-dhcp; an ad-hoc `ethtool -K` is lost on next link event).
- `ethtool -K` requires CAP_NET_ADMIN → root. The watchdog/config-guard run as root
  via systemd.
- Option (2) — newer r8127 dkms driver — NOT taken: current driver is the latest
  vendor drop (11.014.00-NAPI, 2025); DKMS recompile on this aarch64 kernel is higher
  risk for marginal gain. Offload-disabling is the widely-documented, lowest-risk fix.

## INSTALL (requires root — sudo password-gated, so Frank runs this)
```
cd /home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_ef7ed63e/deploy
sudo bash install.sh
```

## Post-install verification
- `ethtool -k enP7s7 | grep -iE 'tcp-segmentation-offload:|generic-segmentation-offload:|generic-receive-offload:'`
  → expect all `off`.
- `systemctl is-enabled r8127-link-watchdog.timer` → `enabled`.
- `systemctl is-enabled sycode-config-guard.service` → `enabled`.
- `ls -la /home/frank/.hermes/deploy-state/sycode-config/supabase/kong.yml`
  → deploy-owned config directory populated and readable.
- After next reboot, config-guard runs `Before=docker.service`; no phantom dirs.
- Verify compose resolves deploy-owned paths:
  `SYCODE_CONFIG_DIR=/home/frank/.hermes/deploy-state/sycode-config docker compose config | grep -E '(kong|recording-rules|slo-rules|tempo)\.yml'`
  → paths show deploy-owned directory.

## Reversible
- `sudo systemctl disable --now r8127-link-watchdog.timer`
- `sudo systemctl disable sycode-config-guard.service`
- `sudo rm /etc/NetworkManager/dispatcher.d/r8127-offload-fix /usr/local/sbin/r8127-*.sh /usr/local/sbin/seed-sycode-config.sh /usr/local/sbin/sycode-config-guard.sh /etc/systemd/system/r8127-link-watchdog.* /etc/systemd/system/sycode-config-guard.service`
- `sudo rm -rf /home/frank/.hermes/deploy-state/sycode-config`
- then `sudo systemctl daemon-reload`.
- In docker-compose.yml, revert the 4 `SYCODE_CONFIG_DIR` env var prefixes back to `./` paths (or restore from git).
- In `sycode-deploy-pristine.sh`, remove the config-sync step (step 2c).

## Recovery-script rollback

Full operator rollback steps: `deploy/r8127-recover-rollback-guide.md`
Covers: disable watchdog timer, re-enable TSO/GSO/GRO, restore link,
remove NM dispatcher hook, stop config-guard, verify full rollback.
