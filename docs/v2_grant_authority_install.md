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

`hermes-real-executor@.service` must use `User=hermes-executor`, only receive
the consumed opaque grant descriptor from the gate hand-off, and execute the
private runtime. It must not receive the gate issuer-token path.

Before enabling either unit, run `verify-install`; retain its JSON output with
the canary evidence. A failed check is a hard activation refusal.
