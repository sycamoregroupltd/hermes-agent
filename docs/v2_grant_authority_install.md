# V2 real-executor grant authority provisioning contract

This is a deployment plan, not an installer.  `v2_grant_authority.py
verify-install --config …` is deliberately non-mutating and refuses until all
of these controls already exist.

## Required identities

Create three distinct non-login system accounts: `hermes-grant-authority`,
`hermes-gate`, and `hermes-executor`.  Create `hermes-grant-socket`; all three
are members.  Do not use the interactive DGX/Claude account for any of them.

- Authority state directory: authority owner/group, **0700**.
- Gate issuer token: gate owner/group, **0600**.
- Authority copy of the gate-token digest: authority owner/group, **0600**.
  The authority must compare the request token to the stored digest; it must
  never read the gate token itself.
- Socket parent: authority-owned and not group/world writable.
- Socket: authority owner, `hermes-grant-socket` group, **0660**.
- Runtime, authority module and executor child: **root-owned 0755** regular
  files. The executor may read/execute them but may never modify an authority
  boundary. `install.json` pins each installed file by SHA-256 and pins the
  reviewed source Git head; a mismatch is an activation refusal.

The grant service must use `SO_PEERCRED`: `issue` only accepts the gate UID;
`consume` only accepts the executor UID.  A unit must start the executor as
the executor account, not as the gate process.

## Unit templates

`hermes-grant-authority.service`:

```ini
[Service]
User=hermes-grant-authority
Group=hermes-grant-authority
SupplementaryGroups=hermes-grant-socket
ExecStart=/usr/bin/python3 /opt/hermes/bin_verify/v2_grant_authority.py serve --socket /run/hermes-grants/authority.sock --state-dir /var/lib/hermes-grants --issuer-secret-file /etc/hermes-grants/authority-token-digest --install-config /etc/hermes-grants/install.json
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/hermes-executor/workspaces
ReadWritePaths=/run/hermes-grants /var/lib/hermes-grants
```

`hermes-real-executor@.service` must use `User=hermes-executor` and be the
*only* route that can start the child. The instance identifier is the opaque
grant ID; it is not an issuer secret. The gate first asks the authority to
issue and arm the grant, then invokes a small root-owned compiled launcher
which is hard-coded to `systemctl start hermes-real-executor@<64-hex>.service`.
The launcher rejects every other unit name and argument. A shell script cannot
be used here: Linux ignores set-id bits on scripts.

```ini
# /etc/systemd/system/hermes-real-executor@.service
[Service]
User=hermes-executor
Group=hermes-executor
SupplementaryGroups=hermes-grant-socket
ExecStart=/usr/bin/python3 /opt/hermes/bin_verify/v2_real_executor_child.py --grant-id %i --grant-authority-socket /run/hermes-grants/authority.sock --install-config /etc/hermes-grants/install.json
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
```

The unit must not receive the gate issuer-token path. Its complete `[Service]`
contract is closed: `User`, `Group`, `SupplementaryGroups`, `ExecStart`,
`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, and one exact
`ReadWritePaths=/var/lib/hermes-executor/workspaces` are the only accepted
directives. `ExecStart` is tokenized and must exactly equal the shown Python,
child, grant-id, authority-socket, and install-config arguments; any injected
environment, pre/post command, alternate path, or extra directive refuses.
The child connects to
the authority as `hermes-executor`; `SO_PEERCRED` checks that identity and the
authority atomically consumes the already-armed grant before returning the
canonical runtime arguments.

`/etc/hermes-grants/install.json` is exact-schema configuration and includes
`runtime_path`, `authority_path`, `executor_child_path`, `executor_launcher`,
`executor_unit_template`, `authority_unit_template`, `python_path`, canonical
64-hex `source_head`, canonical `source_root`, `effective_unit_name`,
`effective_authority_unit_name`, and an exhaustive
`provider_import_closure` map of relative source files to SHA-256 pins, plus
SHA-256 pins for the runtime, authority, child, launcher, executor `unit_sha256`
and `authority_unit_sha256`, the `authority_issuer_digest_sha256` and
`authority_token_sha256` secret file pins, and the pinned `authority_readwrite_paths`
list, in addition to the three account IDs, socket group, state/socket paths,
and token paths. The runtime is root-owned `0755`; the compiled launcher is
root-owned `4750` and the units are root-owned `0644`. The launcher source is
itself a mandatory activation-packet manifest pin. The launcher is compiled
from `bin_verify/v2_executor_launcher.c`; it accepts precisely one lower-case
64-hex grant ID, clears its environment, and executes only the fixed
`/usr/bin/systemctl start hermes-real-executor@<grant>.service` argv.
The executor account must have a non-login shell. `verify-install` checks all
of these facts, including socket owner/group/mode and that every service account
belongs to the configured socket group.

Before enabling either unit, run `verify-install`; retain its JSON output with
the install evidence. `verify-install` resolves the effective
`hermes-real-executor@.service` through systemd, requires its FragmentPath to
be the pinned template, rejects all drop-ins/overrides, and refuses while
systemd reports a pending daemon reload. The install config itself must be a
root-owned regular `0644` non-secret file below a root-owned, non-writable path chain;
the source root and every closure member must be root-owned and non-writable.
the canary evidence. The executor-visible install config contains only
immutable paths, identities and digest pins, never a secret; it is root-owned
regular **0644** so the non-login executor can read it. The gate token and
authority digest remain separate owner-only **0600** files. Under
`ProtectSystem=strict`, the dedicated workspace root above is the sole
writable path; broad, relative, repeated, or injected write paths are refused.
A failed check is a hard activation refusal.
