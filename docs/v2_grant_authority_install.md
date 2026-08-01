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
- Private runtime `.v2_real_executor_runtime.py`: executor owner/group,
  **0700**. It is an executable boundary, not a public Python API.

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

The unit must not receive the gate issuer-token path. The child connects to
the authority as `hermes-executor`; `SO_PEERCRED` checks that identity and the
authority atomically consumes the already-armed grant before returning the
canonical runtime arguments.

`/etc/hermes-grants/install.json` is exact-schema configuration and includes
`runtime_path`, `executor_launcher`, and `executor_unit_template`, in addition
to the three account IDs, socket group, state/socket paths, and token paths.
The runtime is executor-owned `0700`; the launcher and unit are root-owned.
The executor account must have a non-login shell. `verify-install` checks all
of these facts, including socket owner/group/mode and that every service account
belongs to the configured socket group.

Before enabling either unit, run `verify-install`; retain its JSON output with
the canary evidence. A failed check is a hard activation refusal.
