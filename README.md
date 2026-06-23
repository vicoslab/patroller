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

- Python 3.6 or newer.
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

The server listens on port `80`.

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

The existing tests focus on Docker cgroup parsing across legacy and modern cgroup layouts.

## Security notes

Patroller is intended for trusted internal infrastructure. Before exposing it broadly:

- Put it behind network access controls; the API does not implement authentication.
- Avoid exposing Docker daemon access beyond the Patroller process.
- Treat user identity labels as advisory metadata unless your container launch workflow enforces them.
- Review whether binding to port `80` is appropriate for your deployment.

## License

This project is distributed under the GNU General Public License v3.0, as declared in the package metadata.

## Contributing

Issues and pull requests are welcome. Please include tests for behavioral changes where practical and describe the GPU/Docker environment used to validate changes.
