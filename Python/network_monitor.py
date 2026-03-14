"""
Network Monitoring Tool — Advanced Edition
==========================================
Async, concurrent pinging with a live Rich dashboard, SQLite metrics
storage, and a Click CLI.

Dependencies:
    pip install rich click

Usage:
    python network_monitor.py                          # defaults
    python network_monitor.py -t 8.8.8.8 -t 1.1.1.1  # custom targets
    python network_monitor.py --interval 5 --db metrics.db
    python network_monitor.py --no-dashboard           # headless / log-only
    python network_monitor.py report                   # print DB summary
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import sqlite3
import statistics
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import click
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ──────────────────────────────────────────────────────────────────────────────
# Ping
# ──────────────────────────────────────────────────────────────────────────────

async def async_ping(host: str, count: int = 3) -> dict:
    """
    Non-blocking ping via asyncio.create_subprocess_exec.

    Returns:
        host, reachable, avg_ms, packet_loss_pct
    """
    is_windows = platform.system().lower() == "windows"
    cmd = (
        ["ping", "-n", str(count), host]
        if is_windows
        else ["ping", "-c", str(count), "-W", "2", host]
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=count * 3 + 2
        )
        output = stdout.decode() + stderr.decode()
        reachable = proc.returncode == 0

        avg_ms = _parse_latency(output, is_windows)
        loss = _parse_packet_loss(output)

        return {"host": host, "reachable": reachable, "avg_ms": avg_ms, "packet_loss_pct": loss}

    except (asyncio.TimeoutError, FileNotFoundError):
        return {"host": host, "reachable": False, "avg_ms": None, "packet_loss_pct": 100.0}


def _parse_latency(output: str, is_windows: bool) -> float | None:
    if is_windows:
        m = re.search(r"Average\s*=\s*([\d.]+)ms", output)
    else:
        m = re.search(r"[\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", output)
    return float(m.group(1)) if m else None


def _parse_packet_loss(output: str) -> float:
    m = re.search(r"([\d.]+)%\s*(packet\s*)?loss", output, re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# SQLite Storage
# ──────────────────────────────────────────────────────────────────────────────

class MetricsDB:
    """Thread-safe SQLite wrapper for ping results."""

    DDL = """
    CREATE TABLE IF NOT EXISTS checks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT    NOT NULL,
        host         TEXT    NOT NULL,
        reachable    INTEGER NOT NULL,
        avg_ms       REAL,
        packet_loss  REAL    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_checks_host ON checks(host);
    CREATE INDEX IF NOT EXISTS idx_checks_ts   ON checks(ts);
    """

    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(self.DDL)
        self._conn.commit()

    def insert(self, ts: str, host: str, reachable: bool, avg_ms: float | None, loss: float) -> None:
        self._conn.execute(
            "INSERT INTO checks (ts, host, reachable, avg_ms, packet_loss) VALUES (?,?,?,?,?)",
            (ts, host, int(reachable), avg_ms, loss),
        )
        self._conn.commit()

    def summary(self, host: str | None = None) -> list[dict]:
        """Aggregate uptime % and latency stats, optionally filtered by host."""
        where = "WHERE host = ?" if host else ""
        params = (host,) if host else ()
        rows = self._conn.execute(
            f"""
            SELECT host,
                   COUNT(*)                                         AS total,
                   SUM(reachable)                                   AS up_count,
                   ROUND(AVG(CASE WHEN reachable THEN avg_ms END),1) AS avg_ms,
                   ROUND(MIN(CASE WHEN reachable THEN avg_ms END),1) AS min_ms,
                   ROUND(MAX(CASE WHEN reachable THEN avg_ms END),1) AS max_ms,
                   ROUND(AVG(packet_loss),1)                        AS avg_loss
            FROM checks {where}
            GROUP BY host
            ORDER BY host
            """,
            params,
        ).fetchall()
        cols = ["host", "total", "up_count", "avg_ms", "min_ms", "max_ms", "avg_loss"]
        return [dict(zip(cols, r)) for r in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, host, reachable, avg_ms, packet_loss FROM checks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = ["ts", "host", "reachable", "avg_ms", "packet_loss"]
        return [dict(zip(cols, r)) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Uptime Tracker (in-memory)
# ──────────────────────────────────────────────────────────────────────────────

class UptimeTracker:
    def __init__(self, hosts: list[str]):
        self._data: dict[str, dict] = {
            h: {"total": 0, "up": 0, "latencies": [], "last_status": None, "down_since": None}
            for h in hosts
        }

    def record(self, r: dict, now: datetime) -> str | None:
        """Update state; return alert message if status changed, else None."""
        host, reachable = r["host"], r["reachable"]
        d = self._data[host]
        d["total"] += 1
        if reachable:
            d["up"] += 1
        if r["avg_ms"] is not None:
            d["latencies"].append(r["avg_ms"])

        alert = None
        if d["last_status"] is True and not reachable:
            d["down_since"] = now
            alert = f"[red]⚠  {host} went DOWN at {now.strftime('%H:%M:%S')}[/red]"
        elif d["last_status"] is False and reachable:
            duration = (now - d["down_since"]).seconds if d["down_since"] else 0
            d["down_since"] = None
            alert = f"[green]✓  {host} RECOVERED at {now.strftime('%H:%M:%S')} (down {duration}s)[/green]"
        d["last_status"] = reachable
        return alert

    def uptime_pct(self, host: str) -> float:
        d = self._data[host]
        return round(d["up"] / d["total"] * 100, 1) if d["total"] else 0.0

    def avg_latency(self, host: str) -> float | None:
        lats = self._data[host]["latencies"]
        return round(statistics.mean(lats), 1) if lats else None

    def all_up(self) -> bool:
        return all(d["last_status"] for d in self._data.values())


# ──────────────────────────────────────────────────────────────────────────────
# Rich Dashboard
# ──────────────────────────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, hosts: list[str], tracker: UptimeTracker):
        self._hosts = hosts
        self._tracker = tracker
        self._alerts: list[str] = []
        self._check = 0

    def tick(self, results: list[dict], alerts: list[str]) -> None:
        self._check += 1
        self._alerts = (alerts + self._alerts)[:12]  # keep last 12

    def _status_table(self) -> Table:
        t = Table(
            box=box.SIMPLE_HEAVY,
            expand=True,
            show_header=True,
            header_style="bold cyan",
        )
        t.add_column("Host", style="bold")
        t.add_column("Status", justify="center")
        t.add_column("Uptime %", justify="right")
        t.add_column("Avg Latency", justify="right")

        for host in self._hosts:
            up = self._tracker._data[host]["last_status"]
            status = Text("● UP", style="bold green") if up else Text("● DOWN", style="bold red")
            uptime = f"{self._tracker.uptime_pct(host)}%"
            lat = self._tracker.avg_latency(host)
            latency = f"{lat} ms" if lat else "—"
            t.add_row(host, status, uptime, latency)

        return t

    def _alert_panel(self) -> Panel:
        body = "\n".join(self._alerts) if self._alerts else "[dim]No alerts[/dim]"
        return Panel(body, title="Alerts", border_style="yellow", padding=(0, 1))

    def build(self) -> Layout:
        overall = "[bold green]ONLINE[/bold green]" if self._tracker.all_up() else "[bold red]DEGRADED[/bold red]"
        header = Panel(
            f"[bold]Network Monitor[/bold]  |  Check #{self._check}  |  {overall}  "
            f"|  [dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
            box=box.HORIZONTALS,
            border_style="dim",
        )
        layout = Layout()
        layout.split_column(
            Layout(header, size=3),
            Layout(self._status_table(), size=len(self._hosts) + 4),
            Layout(self._alert_panel()),
        )
        return layout


# ──────────────────────────────────────────────────────────────────────────────
# Monitor Core
# ──────────────────────────────────────────────────────────────────────────────

async def monitor_loop(
    targets: list[str],
    interval: int,
    ping_count: int,
    db: MetricsDB,
    tracker: UptimeTracker,
    dashboard: Dashboard | None,
    logger: logging.Logger,
) -> None:
    async def run_checks() -> tuple[list[dict], list[str]]:
        now = datetime.utcnow()
        ts = now.isoformat()
        results = await asyncio.gather(*[async_ping(h, ping_count) for h in targets])
        alerts: list[str] = []
        for r in results:
            alert = tracker.record(r, datetime.now())
            db.insert(ts, r["host"], r["reachable"], r["avg_ms"], r["packet_loss_pct"])
            if alert:
                alerts.append(alert)
                logger.warning(re.sub(r"\[.*?\]", "", alert))  # strip Rich markup for log
            logger.debug(
                f"{r['host']}  {'UP  ' if r['reachable'] else 'DOWN'}  "
                f"latency={r['avg_ms']}ms  loss={r['packet_loss_pct']}%"
            )
        return list(results), alerts

    if dashboard:
        with Live(dashboard.build(), refresh_per_second=2, screen=True) as live:
            while True:
                results, alerts = await run_checks()
                dashboard.tick(results, alerts)
                live.update(dashboard.build())
                await asyncio.sleep(interval)
    else:
        while True:
            results, alerts = await run_checks()
            for alert in alerts:
                print(re.sub(r"\[.*?\]", "", alert))
            await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.option("-t", "--target", "targets", multiple=True, default=("8.8.8.8", "1.1.1.1"),
              show_default=True, help="Host(s) to monitor. Repeat flag for multiple.")
@click.option("--interval", default=10, show_default=True, help="Seconds between checks.")
@click.option("--ping-count", default=3, show_default=True, help="Pings per host per check.")
@click.option("--db", "db_path", default="metrics.db", show_default=True, help="SQLite database path.")
@click.option("--log-file", default="network_monitor.log", show_default=True)
@click.option("--log-level", default="INFO",
              type=click.Choice(["DEBUG", "INFO", "WARNING"], case_sensitive=False))
@click.option("--no-dashboard", is_flag=True, help="Disable live dashboard (headless mode).")
@click.pass_context
def cli(ctx, targets, interval, ping_count, db_path, log_file, log_level, no_dashboard):
    """Network connectivity monitor with async pinging, SQLite storage, and live dashboard."""
    if ctx.invoked_subcommand:
        ctx.ensure_object(dict)
        ctx.obj["db_path"] = db_path
        return

    # Logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s  [%(levelname)-8s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("NetMon")

    db = MetricsDB(db_path)
    tracker = UptimeTracker(list(targets))
    dash = None if no_dashboard else Dashboard(list(targets), tracker)

    logger.info(f"Starting monitor | targets={list(targets)} | interval={interval}s | db={db_path}")

    try:
        asyncio.run(
            monitor_loop(list(targets), interval, ping_count, db, tracker, dash, logger)
        )
    except KeyboardInterrupt:
        logger.info("Monitor stopped.")
    finally:
        db.close()


@cli.command()
@click.option("--db", "db_path", default="metrics.db", show_default=True)
@click.option("--host", default=None, help="Filter report to a single host.")
def report(db_path, host):
    """Print an uptime/latency summary from the SQLite database."""
    if not Path(db_path).exists():
        click.echo(f"Database not found: {db_path}")
        raise SystemExit(1)

    db = MetricsDB(db_path)
    rows = db.summary(host)
    db.close()

    if not rows:
        click.echo("No data in database.")
        return

    console = Console()
    t = Table(title=f"Network Monitor Report — {db_path}", box=box.SIMPLE_HEAVY, header_style="bold cyan")
    for col in ["Host", "Total Checks", "Uptime %", "Avg Ms", "Min Ms", "Max Ms", "Avg Loss %"]:
        t.add_column(col, justify="right" if col != "Host" else "left")

    for r in rows:
        uptime = round(r["up_count"] / r["total"] * 100, 1) if r["total"] else 0.0
        t.add_row(
            r["host"],
            str(r["total"]),
            f"{uptime}%",
            str(r["avg_ms"] or "—"),
            str(r["min_ms"] or "—"),
            str(r["max_ms"] or "—"),
            f"{r['avg_loss']}%",
        )

    console.print(t)


if __name__ == "__main__":
    cli()