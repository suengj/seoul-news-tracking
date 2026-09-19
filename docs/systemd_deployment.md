# Linux systemd deployment contract

This repository's Linux contract is the example unit at
[`scripts/systemd/seoulnews-runlocal.service.example`](../scripts/systemd/seoulnews-runlocal.service.example).
It runs the same long-lived `python -u -m app.commands.run_local` entry point as
the observed Mac job. The example's `/path/to/...` values are deployment-owned
placeholders: replace them with the existing checkout and its `.venv`; do not
make a second checkout or a second application layout.

The service account is deliberately non-root (`User=seoulnews`,
`Group=seoulnews`). The account and its file permissions are provisioned by the
deployment authority. The unit does not contain a bot token, API key, or other
secret. `EnvironmentFile` must point outside the Git checkout to the
deployment-owned runtime configuration. That file should set the secret values,
the external `DATABASE_PATH` and `HISTORY_DATABASE_PATH`, and the explicit
operator-facing identity values:

- `DEPLOYMENT_LABEL` — a short non-secret label such as `linux-production`.
- `RUNTIME_MODE` — the explicit mode such as `systemd`.
- `CUTOVER_FENCE_PATH` — the shared, operator-provisioned fence directory.
- `CUTOVER_HOST_ID` — the explicit host label, such as `linux`.

`/status` uses only those two configured values for runtime identity. It does
not inspect the hostname, infer a mode from the environment, or print the
environment-file path. Keep the database paths outside the checkout as well;
the unit intentionally does not hard-code an application state directory.

`DEPLOYMENT_LABEL` and `RUNTIME_MODE` are display labels, not free-form
configuration echoes. They must be short labels made from letters, digits,
`_`, and `-`, starting with a letter. IP addresses and values containing
credential-shaped words such as `token`, `secret`, or `api_key` are rendered as
`unconfigured`.

## Unit directive rationale

The Mac job was observed live with these exact values: `ProgramArguments`
pointed at `<project>/.venv/bin/python -u -m app.commands.run_local`,
Python 3.13.5 came from that project virtual environment, `KeepAlive=true`,
`RunAtLoad=true`, `ThrottleInterval=60`, and both standard streams went to
`<project>/data/logs/launchd_run_local.log`. It had no
`StartInterval` or `StartCalendarInterval`.

The Linux mapping is:

| Unit directive | Contract and reason |
|---|---|
| `Description` | Names the long-running poller/Telegram supervisor for service inventory and journal queries. |
| `Wants=network-online.target` / `After=network-online.target` | Gives the long poll its network prerequisite at boot; this supplements the observed `RunAtLoad=true` startup intent without changing the process lifecycle. |
| `Type=simple` | `run_local` stays in the foreground as the long-lived supervisor; there is no daemon readiness protocol. |
| `User`/`Group` | Runs the service as the named non-root account. |
| `WorkingDirectory` | Uses the existing Git checkout as the process working directory. |
| `EnvironmentFile` | Keeps deployment configuration and secrets outside Git and outside the unit. |
| `ExecStart` | Preserves the observed project virtualenv interpreter, unbuffered mode, module, and supervisor entry point. |
| `Restart=always` | Carries the observed `KeepAlive=true` continuous-supervision intent: a terminated `run_local` is brought back, including after its crash-loop give-up exit. This is not a claim that the two supervisors have identical edge-case semantics. |
| `RestartSec=60` | Carries the observed `ThrottleInterval=60` restart-throttle intent. It is a 60-second delay before systemd's restart attempt. |
| `KillMode=control-group` | `run_local` supervises the poller and Telegram bot as child processes; stopping/restarting the unit must take the whole service cgroup with it. |
| `StandardOutput`/`StandardError=journal` | Sends both streams to journald, replacing launchd's one combined `StandardOutPath`/`StandardErrorPath` file. |
| `WantedBy=multi-user.target` | When the deployment authority enables the unit, it is attached to the normal multi-user boot target, providing reboot persistence. |

There is no systemd timer and no `Type=oneshot`: the observed Mac job is a
continuously supervised process, and tearing down the Telegram long poll after
each timer firing would be the wrong lifecycle.

## Restart and reboot behaviour

`run_local` already restarts an individual poller or Telegram child with its
own backoff. The unit's `Restart=always` is the outer layer for the supervisor
itself, with a 60-second delay matching the observed launchd throttle intent.
An enabled unit attached to `multi-user.target` is started again after reboot;
the source tree does not install, enable, start, or stop it.

## Logs

Linux uses journald as the service log sink. The application configures Python
logging to `sys.stderr` (`app/logging_config.py`), so the old launchd file is
not needed to preserve application logs. Operators who currently tail
`data/logs/launchd_run_local.log` by path must instead read the unit journal,
for example:

```text
journalctl -u seoulnews-runlocal.service -f
journalctl -u seoulnews-runlocal.service --since today
journalctl -u seoulnews-runlocal.service -b
```

The exact installed unit name follows the deployment copy of the example. The
unit sends both stdout and stderr to journald, and this repository does not
create the old `data/logs/launchd_run_local.log` on Linux.

The existing
[`scripts/launchd/com.user.seoulnews-runlocal.plist.example`](../scripts/launchd/com.user.seoulnews-runlocal.plist.example)
remains a macOS rollback reference. Its observed `KeepAlive`, `RunAtLoad`,
60-second `ThrottleInterval`, and single combined log file describe the Mac
contract; it is not the active Linux contract.

## Cross-host duplicate execution: the cutover fence

One bot token permits only one Telegram `getUpdates` consumer. The local
`SingleInstanceLock` still protects two processes on one filesystem, and HTTP
409 remains a reactive diagnostic. Neither is the cross-host admission
decision.

This repository uses the smallest mechanism needed for this two-host,
operator-driven, one-directional move: an operator-provisioned shared fence
directory, configured with `CUTOVER_FENCE_PATH`, plus an explicit
`CUTOVER_HOST_ID` on each host. It is not a lease service, leader election, or
general cross-host locking framework. The directory contains an atomic
`authority.json`, an advisory `authority.lock`, a fence-identity consumer lock,
and separate `hosts/<host>.json` observations.

The shared directory is a hard deployment prerequisite, not merely any
directory visible from both machines. Its filesystem must provide all of the
following to both clients: globally coherent POSIX `flock` on the same lock
file, current close-to-open reads while that lock is held, atomic same-directory
`os.replace`, and durable directory metadata after `fsync`. NFS/SMB mounts with
client-side stale caching, local-only locks, or unsupported directory `fsync`
are not supported. `run_telegram_bot` runs a lock/write/read/replace/fsync
probe before claim and refuses closed if that local probe fails. Operators must
run `python -m app.commands.cutover_fence verify-filesystem` from both hosts
against the same path and verify the mount/export options provide the
cross-client properties above; the application cannot prove another client's
cache or lock implementation from one process.

The outgoing host creates a request, is physically stopped, and then records a
positive OFF fact with its host identity, request id, and UTC timestamp. The
`confirm-off` command must also acquire the fence-identity consumer lock; a
repaired local consumer holding that lock makes OFF recording fail. The
incoming `run_telegram_bot` process freshly reads both the authority record and
the outgoing host's separate state record before its first Telegram request.
The two records must bind the same bot-token fingerprint, request id, host,
and matching fresh timestamp. The complete transition chronology is
`issued_at <= outgoing OFF observed_at == outgoing host read-back observed_at <= active_since == incoming ACTIVE read-back observed_at <= validation time < expires_at`,
with a lifetime no longer than the fence TTL. This separate host-state read is
the independent read-back: a successful write of `authority.json` alone cannot
authorize a consumer. A same-host restart also requires the complete durable
ACTIVE artifact: valid OFF and ACTIVE host-state records, matching timestamps
and identities, the original bounded lifetime fields, and the positive OFF fact.
A missing, stale, malformed, expired, wrong-host, ACTIVE, or in-progress
record refuses with a stable `CUTOVER_FENCE_REFUSED[...]` reason.

The fence is fail-closed. It never turns peer silence, an unreachable path, or
expiry into OFF. After a valid claim, the authority becomes durable `ACTIVE`
for the incoming host; another host cannot claim it. The consumer lock is
derived from the token, shared fence identity, and host id, and is held for the
whole bot process lifetime, so different database paths cannot create two
local consumers. A misconfigured duplicate host id is also serialized when
both machines use the same coherent shared fence filesystem. The `run_local`
supervisor does not respawn a Telegram child that exits with the fence-refusal
code. A systemd `Restart=always` therefore repeats a refusal, never creates
authority, and never starts a Telegram network call without an existing
hand-off.

### Operator protocol

On the outgoing host, with its `CUTOVER_FENCE_PATH` pointing at the shared
authority directory:

```bash
python -m app.commands.cutover_fence request \
  --outgoing-host mac --incoming-host linux --request-id mac-to-linux-20260919
```

Stop the outgoing service using the deployment's normal service procedure and
verify that its `getUpdates` consumer is gone. Only after that positive local
check, record OFF:

```bash
python -m app.commands.cutover_fence confirm-off \
  --request-id mac-to-linux-20260919 --confirmed-local-off
```

Transfer the final SQLite state (including its WAL/SHM handling) and the
deployment-owned runtime configuration to Linux. Do not copy secrets into Git
or the fence record. Configure Linux with the same bot token, the shared fence
path, and `CUTOVER_HOST_ID=linux`, then start the normal service. Its Telegram
child claims the authority and only then may call `getUpdates`.

Rollback is the same protocol in reverse: Linux issues a new request with
`--outgoing-host linux --incoming-host mac`, Linux is stopped and positively
confirmed OFF, and only then may Mac's gated consumer claim it. A rollback
attempt before the Linux OFF read-back is refused.

The lock check is a system-verifiable absence check for this application
version; it does not inspect or stop an independently launched old binary or
an unrelated Telegram consumer. The operator must still stop and verify the
outgoing service before confirming OFF. The implementation therefore provides
single admission for repaired consumers under the shared-filesystem contract
and honest, lock-checked cutover operation. It does not authenticate
operator-written files or detect an out-of-band consumer that does not use the
fence identity lock.

### Failure paths

- No fence path/host id, absent evidence, malformed evidence, stale evidence,
  wrong token/host, or an uncompleted request: startup exits loudly before any
  Telegram HTTP call.
- If cutover is interrupted after the request or after only one of the two
  evidence writes, no host can claim the request. The operator repairs the
  sequence with a new, explicit request; neither host infers safety from the
  missing half.
- If the authority is already `ACTIVE` for the other host, a duplicate or
  replayed request is refused. The active owner remains the only claimant
  until it completes the reverse OFF/read-back sequence.
- HTTP 409 may still be logged as a secondary symptom, but it cannot grant or
  revoke authority.
