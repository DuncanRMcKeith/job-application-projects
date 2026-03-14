# Network Monitoring Tool

A Python-based network monitoring tool that concurrently pings multiple hosts, logs uptime metrics to SQLite, and displays a live terminal dashboard. Built with `asyncio`, `rich`, and `click`.

---

## Features

- **Async concurrent pinging** — all hosts are pinged simultaneously via `asyncio`, so monitoring 10 hosts takes no longer than monitoring 1
- **Live terminal dashboard** — real-time status table with uptime % and average latency per host, powered by `rich`
- **SQLite metrics storage** — every check is persisted with a UTC timestamp for historical reporting
- **Automated alerts** — detects when a host goes down or recovers and logs timestamped alerts
- **Click CLI** — fully configurable from the command line with sensible defaults
- **Headless mode** — run without the dashboard for background/service use

---

## Project Structure

```
job-application-projects/
└── python/
    └── network_monitor.py
requirements.txt
```

Output files (`metrics.db`, `network_monitor.log`) are written to `job-application-projects/python/` automatically, regardless of where you invoke the script from.

---

## Requirements

- Python 3.10+
- `rich >= 13.0`
- `click >= 8.0`

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Usage

### Run with defaults
Monitors `8.8.8.8` and `1.1.1.1` every 10 seconds with the live dashboard:

```bash
python job-application-projects/python/network_monitor.py
```

### Custom targets and interval
```bash
python job-application-projects/python/network_monitor.py \
    -t 8.8.8.8 -t 1.1.1.1 -t google.com \
    --interval 5
```

### Headless mode (no dashboard)
Useful for running as a background service or piping output to a log:

```bash
python job-application-projects/python/network_monitor.py --no-dashboard
```

### Debug logging
```bash
python job-application-projects/python/network_monitor.py --log-level DEBUG
```

### Print a historical report
Queries the SQLite database and prints an aggregated uptime/latency summary:

```bash
python job-application-projects/python/network_monitor.py report

# Filter to a single host
python job-application-projects/python/network_monitor.py report --host 8.8.8.8
```

---

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `-t`, `--target` | `8.8.8.8`, `1.1.1.1` | Host(s) to monitor — repeat flag for multiple |
| `--interval` | `10` | Seconds between checks |
| `--ping-count` | `3` | Pings per host per check |
| `--db` | `python/metrics.db` | SQLite database path |
| `--log-file` | `python/network_monitor.log` | Log file path |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, or `WARNING` |
| `--no-dashboard` | off | Disable live dashboard |

---

## How It Works

1. **Ping** — `async_ping()` spawns native OS ping subprocesses via `asyncio.create_subprocess_exec`, parses latency and packet loss from stdout
2. **Concurrent checks** — `asyncio.gather()` runs all host pings in parallel each interval
3. **State tracking** — `UptimeTracker` maintains in-memory uptime counts and latency history, detecting UP→DOWN and DOWN→UP transitions to fire alerts
4. **Persistence** — every check result is inserted into a SQLite `checks` table with a UTC ISO timestamp
5. **Dashboard** — `rich.Live` refreshes a layout of panels at 2 fps, independent of the ping interval
6. **Report** — the `report` subcommand aggregates the SQLite data with a single GROUP BY query

---

## Output Files

| File | Location | Contents |
|------|----------|----------|
| `metrics.db` | `python/` | SQLite database with all check results |
| `network_monitor.log` | `python/` | Timestamped log of checks and alerts |
