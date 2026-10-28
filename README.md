# Patroller

Patroller keeps order on shared, multi-user, multi-GPU machines by tracking GPU usage, mapping processes back to users, and exposing a small HTTP API for reserving available devices.

It is designed for research servers where many users launch Docker containers on the same NVIDIA GPU host. Patroller watches GPU activity through `nvidia-smi`, identifies Docker containers from Linux cgroup metadata, reads user identity from container labels, and hands out short-lived device reservations to clients.

## Features

- Monitors NVIDIA GPU statistics with `nvidia-smi dmon`.
- Detects GPU-using processes with `nvidia-smi pmon`.
- Resolves Docker containers from cgroup v1, cgroup v2, and systemd scope paths.
- Associates usage with users through configurable Docker labels.
- Exposes JSON endpoints for device inventory, live status, immediate reservations, and blocking reservations.
- Provides a small Python helper for clients that sets `CUDA_VISIBLE_DEVICES` after a successful claim.
- Includes a test mode with synthetic devices for local API development.

## How it works

Patroller runs one server process on a GPU host:

1. A device monitor discovers GPUs and periodically updates utilization statistics.
2. A process monitor observes GPU access and emits access events.
3. An identity resolver maps each GPU-using process to a Docker container and then to a user identity from container labels.
4. The reservation API grants free devices to callers and keeps a short lease while waiting for the claimed process to appear on the GPU.

A typical client asks Patroller for one or more GPUs before starting a workload. When a reservation is granted, the response contains device metadata; the helper in `example.py` converts the assigned GPU numbers into `CUDA_VISIBLE_DEVICES`.

## Requirements

- Python 3.8 or newer (required by the Docker SDK dependency).
- NVIDIA drivers and `nvidia-smi` available on the host.
- Docker Engine access from the Patroller process.
- Linux `/proc/<pid>/cgroup` visibility for GPU-using processes.

Python dependencies are listed in [`requirements.txt`](requirements.txt):

- Tornado
- Docker SDK for Python
- Blinker
- cachetools

## Installation

Clone the repository and install it into a Python environment:

```bash
git clone https://github.com/vicoslab/patroller.git
cd patroller
python -m pip install .
```

For development, install the package in editable mode and add test tooling if needed:

```bash
python -m pip install -e .
python -m pip install pytest
```

## Running the server

### Test mode

Test mode starts Patroller with eight synthetic devices and does not require Docker or NVIDIA hardware:

```bash
PATROLLER_TEST=1 python -m patroller
```

The server listens on port `80` by default. For local development use
`PATROLLER_ADDRESS=127.0.0.1 PATROLLER_PORT=8080 PATROLLER_TEST=1 python -m patroller`.
Synthetic UUIDs (`TEST-0` through `TEST-7`) are stable across restarts; this mode
never inspects or signals GPU processes, even with enforcement set to `kill`.

### GPU host mode

On an NVIDIA Docker host, run:

```bash
python -m patroller
```

The process must be able to:

- Execute `nvidia-smi`.
- Read `/proc/<pid>/cgroup` for observed processes.
- Connect to the Docker daemon.
- Bind to port `80` or run behind a port-forwarding/proxy setup.

## Configuration

Patroller is configured with environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `PATROLLER_TEST` | unset | Enables synthetic test devices instead of Docker/GPU monitoring. |
| `PATROLLER_ADDRESS` | existing all-interface bind | Set `127.0.0.1` for local development. |
| `PATROLLER_PORT` | `80` | HTTP listening port. |
| `PATROLLER_RESERVATIONS_DB` | unset (disabled) | Local SQLite file; parent directory must exist and be writable. |
| `PATROLLER_RESERVATION_TOKEN` | unset (writes denied) | Trusted management service credential. |
| `PATROLLER_RESERVATION_ENFORCEMENT` | `dry-run` | `off`, `dry-run`, or explicit `kill`; other values fail startup. |
| `PATROLLER_LEASE` | `10` | Number of seconds a reservation may remain unclaimed before it is released. |
| `PATROLLER_USER_LABELS` | `user.email,email,maintainer` | Comma-separated Docker labels searched for a user email address. |
| `PATROLLER_USER_INFO_LABELS` | unset | Extra comma-separated Docker labels copied into the returned user identity. |

Container labels are the primary way Patroller attributes GPU use. For example:

```bash
docker run --label user.email=alice@example.org --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## HTTP API

All endpoints return JSON.

### `GET /devices`

Returns static information about known devices.

```bash
curl http://localhost/devices
```

Example shape:

```json
{
  "GPU-...": {
    "name": "NVIDIA GPU",
    "brand": "NVIDIA",
    "arch": "...",
    "number": 0,
    "pci_address": "00000000:01:00.0",
    "total_mem": "24576 MiB",
    "group": "gpu"
  }
}
```

### `GET /status`

Returns live statistics and claim state for each device.

```bash
curl http://localhost/status
```

### `GET /request?<resource>=<count>`

Attempts to reserve devices immediately. If devices are unavailable, the endpoint returns an error response.

```bash
curl 'http://localhost/request?gpu=1'
```

### `GET /wait?<resource>=<count>`

Waits until the requested devices can be reserved or until the client disconnects.

```bash
curl 'http://localhost/wait?gpu=2'
```


## Scheduled reservation calendar

The calendar is independent of occupancy and short-lived `/request` leases. Unset
`PATROLLER_RESERVATIONS_DB` preserves legacy allocation behavior and returns JSON
503 from all calendar endpoints. Configuring a DB without a token enables read-only
calendar access. An inaccessible DB fails startup; runtime failures return 503 for
calendar operations and allocations, skip enforcement, and keep monitoring usable.

For a safe local synthetic server (create the private data directory first):

```bash
mkdir -p .local-data
chmod 700 .local-data
export PATROLLER_TEST=1 PATROLLER_ADDRESS=127.0.0.1 PATROLLER_PORT=8080
export PATROLLER_RESERVATIONS_DB="$PWD/.local-data/reservations.sqlite"
export PATROLLER_RESERVATION_TOKEN='replace-with-a-random-service-secret'
export PATROLLER_RESERVATION_ENFORCEMENT=dry-run
python -m patroller
```

Use a local persistent volume for the SQLite file and journal, retain them across
container replacement, and restrict directory access to the service account. Do
not place SQLite on a network filesystem or edit its records while the service is
running. Back up using SQLite's backup API or while the service is stopped. Keep
DB files and credentials out of Git. The service uses SQLite transactions for
atomic multi-GPU conflict checks and edits across connections.

All token holders may create, edit and cancel bookings for **any owner**. This is
trusted-lab management, not per-user authorization. Docker labels are advisory
identity data; a trusted launch workflow must control them. The token never
establishes human identity. Use `Authorization: Bearer <token>` for writes. If a
proxy requires Basic Auth on the same request, keep Basic Auth and send the token
in `X-Patroller-Reservation-Token` instead. The dedicated header takes precedence.
Avoid exposing tokens in shell history, logs or browser-visible configuration.

| Method/path | Body | Success |
| --- | --- | --- |
| `GET /reservations` | none | 200, array including past bookings |
| `POST /reserve` | all editable fields | 201, created record |
| `PATCH /reservations/<id>` | all editable fields | 200, updated record retaining id |
| `POST /unreserve` | `{"id":"..."}` | 200, `{"id":"...","cancelled":true}` |

Required editable fields are `gpu_uuids`, `owner_email`, `start_at`, `end_at`, and
`note`. The optional `recurrence` field adds a bounded series (see below).
The optional `owner_name` is display metadata: it is trimmed, limited to 200
characters, and must contain no ASCII control characters (including DEL). An
omitted or blank name is omitted from the record; a full PATCH clears an existing
name when omitted or blank. Names never affect permissions or enforcement, which
continue to use `owner_email`.
Submit opaque UUIDs from `/devices`, never GPU indices. Email addresses are trimmed
and compared case-insensitively. Use ISO8601 timestamps with seconds and an explicit
offset (`Z` or `+HH:MM`), with up to six fractional digits; responses use UTC `Z`.
Windows are half-open `[start_at,end_at)`; touching windows are permitted. Duplicate,
empty or unknown GPU lists, invalid emails/times, and end <= start return 400.
The request body is limited to 16 KiB and notes to 4096 characters. Overlaps on any
selected GPU return 409 without changing the original record. Missing IDs return
404, including repeat cancellation. Missing/incorrect credentials return 401/403;
an unconfigured token returns 403. Errors contain an `error` string.

For example, with the local synthetic server above:

```bash
curl http://127.0.0.1:8080/reservations
curl -X POST http://127.0.0.1:8080/reserve \
  -H "Authorization: Bearer $PATROLLER_RESERVATION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"gpu_uuids":["TEST-0"],"owner_email":"alice@example.org","start_at":"2030-01-01T10:00:00+01:00","end_at":"2030-01-01T11:00:00+01:00","note":"experiment"}'
# Edit by PATCHing the full editable object to /reservations/<returned-id>.
# Cancel with POST /unreserve and {"id":"<returned-id>"} using the same credential.
```

### Bounded recurring reservations

`POST /reserve` and `PATCH /reservations/<id>` accept an optional `recurrence`
object. Omitting it or sending `null` creates a one-off with exactly the existing
record shape: no `recurrence` or `occurrences` fields are added. The backend still
requires `start_at` and `end_at`; clients offering start plus duration should
compute `end_at` by adding the duration to the UTC start. There is no API
`duration` field.

| Rule field | Required value |
| --- | --- |
| `frequency` | `daily`, `weekly`, `biweekly`, or `monthly` |
| `until` | Strict `YYYY-MM-DD`; inclusive local start-date limit, from the initial local date through 366 days later |
| `timezone` | Explicit valid IANA timezone name, for example `Europe/Ljubljana` or `UTC` |
| `weekdays` | Required only for `weekly`/`biweekly`: nonempty unique integer list, Monday=0 through Sunday=6, including the initial local weekday |

Unknown fields are rejected, including `weekdays` on daily/monthly rules. Boolean
and fractional weekday values are rejected. Returned weekdays are sorted.
Biweekly schedules use the Monday of the initial start's week as their anchor,
then repeat in every other week. Monthly schedules retain the original
day-of-month and skip months without it; January 31 never becomes February 28.
`until` limits occurrence starts; an occurrence may end after that date.

The explicit-offset input start identifies an instant. Converting it to the named
timezone establishes the local start date and clock; UTC inputs are supported.
Every occurrence retains that clock and the original **elapsed** duration
`end_utc - start_utc`. Ends are calculated in UTC, so a two-hour booking lasts two
actual hours across DST. If any recurrence start (including the initial one) is
ambiguous or nonexistent in its timezone, the entire request returns 400; starts
are never silently shifted. An end may land on a repeated clock hour. Deployments
need IANA timezone data; Python 3.8 installs `backports.zoneinfo` conditionally,
and Python 3.9+ uses the standard library.

For example, this creates two daily two-hour bookings across spring DST:

```json
{
  "gpu_uuids": ["TEST-0"],
  "owner_email": "alice@example.org",
  "start_at": "2030-03-30T09:00:00+01:00",
  "end_at": "2030-03-30T11:00:00+01:00",
  "note": "daily experiment",
  "recurrence": {
    "frequency": "daily",
    "until": "2030-03-31",
    "timezone": "Europe/Ljubljana"
  }
}
```

The create response (201), update response (200), and list records include one
series ID, the UTC template times, the normalized rule, and read-only windows:

```json
{
  "id": "<series-id>",
  "gpu_uuids": ["TEST-0"],
  "owner_email": "alice@example.org",
  "start_at": "2030-03-30T08:00:00Z",
  "end_at": "2030-03-30T10:00:00Z",
  "note": "daily experiment",
  "recurrence": {
    "frequency": "daily",
    "until": "2030-03-31",
    "timezone": "Europe/Ljubljana"
  },
  "occurrences": [
    {"start_at": "2030-03-30T08:00:00Z", "end_at": "2030-03-30T10:00:00Z"},
    {"start_at": "2030-03-31T07:00:00Z", "end_at": "2030-03-31T09:00:00Z"}
  ]
}
```

All windows are deterministic, sorted, and bounded to at most 367 occurrences.
The whole series is validated before writing: invalid rules, date ranges, DST
starts or self-overlap return 400; overlap with another booking on any selected
GPU returns 409. Touching windows are allowed. Conflict checks compare actual
windows, so free gaps between occurrences remain bookable. A later-occurrence
conflict rejects the entire multi-GPU create/edit and preserves existing records.

PATCH replaces the **entire series** while retaining its ID, owner/GPU/note fields
as supplied by the full editable payload. Omit `id` and `occurrences` from writes;
they are read-only. Omitting/nulling `recurrence` on PATCH converts the booking to
a one-off. `POST /unreserve` cancels all occurrences for that ID. Individual
occurrence edits, exceptions and unbounded schedules are unsupported.

The existing SQLite table stores recurring rules and windows in its JSON record;
the indexed start/end columns cover the full series envelope. Existing one-off
rows need no migration and are not rewritten. Restart uses the stored windows.
`Calendar.active_next` and `/status` return the actual active/next occurrence's
top-level `start_at`/`end_at`, retaining the series ID, owner, GPUs, note and rule
but omitting `occurrences`. This includes second/later windows and returns no
active booking in gaps or after the last end. Allocation and enforcement use
that same lookup; recurrence adds no scheduler or process signaling path.

`/status` retains the existing UUID-keyed response, GPU stats and `claim`, adding
`active_reservation`, `next_reservation`, `reservation_state` (`enabled`, `disabled`,
`unavailable`) and effective `reservation_enforcement` per GPU. A null reservation
with `unavailable` does not mean free. Legacy `/request` and `/wait` exclude GPUs
actively booked by other owners, including when enforcement is `off` or `dry-run`.
Future bookings do not block immediate allocation. Process observations expire
after five seconds without a refresh; idle events clear processes immediately.
When a PID changes owner, its obsolete owner entry is removed once that owner
has no other observed PIDs. Intentional legacy allocation leases remain intact.

### Enforcement safety

Start with `dry-run` and inspect logs. Only explicitly setting enforcement to
`kill` on a real monitor enables SIGKILL. Every observed PID is checked, including
work already running before the booking began and PIDs already present in claims.
Booking owners are exempt; unknown/unresolvable identities and system processes
(including PID 1, Patroller itself, and any root UID) are logged/skipped.
**An immediate booking in kill mode can terminate existing non-owner work.**

In kill mode, the monitor signal callback captures Linux process starttime and
all UIDs before queuing the event. The queued handler validates that identity
before updating claims; unresolved or changed observations are skipped without
recapturing an unresolved identity. Direct embedded calls capture at entry.
Immediately before signaling, the service re-resolves ownership, compares Linux
process starttime and all UIDs, and rechecks the active calendar with a fresh clock
while holding the same SQLite write lock used by edits/cancellation. Cancellation
and final signaling therefore serialize. Errors always skip signaling. A pidfd
pins process identity when Python/kernel support it; otherwise repeated checks
precede PID signaling. The fallback has an unavoidable narrow PID reuse race;
use pidfd-capable Python/Linux when kill mode is required. Permission failures
opening pidfds skip enforcement rather than downgrade protection. Correct host
PID namespace and `/proc` visibility are required; unresolved processes are skipped.

Enforcement is reactive to monitor events, not a job scheduler or GPU access
barrier. There is no container kill, GPU reset, grace timer, or HTTP PID kill API.
Expired/cancelled bookings cease authorizing enforcement. Synthetic CLI mode
always disables it. An embedded synthetic test may opt in only with an explicitly
injected process guard marked `safe_for_synthetic=True`; that fixture must restrict
all inspection/signaling to tracked test children. Never mark the production guard
safe or use actual GPU jobs as test fixtures.

## Client usage

The helper in `example.py` requests GPUs and sets `CUDA_VISIBLE_DEVICES` when a claim is granted:

```python
from example import claim

if claim(count=1, server="patroller-host", timeout=30):
    # Start GPU workload here. CUDA_VISIBLE_DEVICES is now set.
    pass
```

The `server` argument is the Patroller host name or address. Use `timeout=0` for an immediate `/request` call or a positive timeout for `/wait`.

## Docker image

A `Dockerfile` is included for containerized deployment. Build it with:

```bash
docker build -t patroller .
```

When running Patroller in a container, make sure it has access to the Docker daemon, NVIDIA utilities, and any host paths needed to inspect process cgroups.

## Development

Run the test suite with:

```bash
python -m pytest
```

Tests cover calendar validation, concurrent/atomic writes, persistence, HTTP/auth,
status, legacy allocation filtering, configuration and process enforcement, plus
existing cgroup/claim behavior. Recurrence tests cover all four frequencies,
weekday subsets, month skipping, bounds/types, DST, actual-window conflicts and
gaps, later-window status/enforcement, whole-series rollback/cancellation and old
DB compatibility. Tests block non-loopback networking, Unix/Docker
socket connections, arbitrary subprocesses and signals to unowned PIDs. The only
real termination test spawns and tracks its own sleeping Python child with a fake
resolver/inspector. No GPU or Docker daemon is needed. On sandboxes forbidding
AF_INET sockets, loopback API tests explicitly skip; identical scenarios still run
through Tornado routing/handlers using an in-memory connection. The parent
integration demo should run the loopback tests where sockets are permitted.

In the shared development workspace the configured interpreter is:

```bash
PYTHONPATH=. ../.venv/bin/python -m pytest -q --basetemp="$(mktemp -d /tmp/patroller-backend-tests-XXXXXX)"
```

## Security notes

Patroller is intended for trusted internal infrastructure. Before exposing it broadly:

- Put it behind network access controls and TLS. Reservation writes require a service token; monitoring reads and the legacy allocation API retain existing access behavior.
- Avoid exposing Docker daemon access beyond the Patroller process.
- Treat user identity labels as advisory metadata unless your container launch workflow enforces them.
- Review whether binding to port `80` is appropriate for your deployment.

## License

This project is distributed under the GNU General Public License v3.0, as declared in the package metadata.

## Contributing

Issues and pull requests are welcome. Please include tests for behavioral changes where practical and describe the GPU/Docker environment used to validate changes.
