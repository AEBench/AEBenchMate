from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


_console = Console(force_terminal=True, color_system="256", width=120)

_COLORS = {
    "yellow": "#FFD300",
    "cyan": "#00FFFF",
    "green": "#39FF14",
    "orange": "#FF782F",
    "violet": "#EDE1FF",
}


def _stage_style(stage_name: str) -> str:
    return {
        "extractor": "#EDE1FF",
        "generator": "#FFD300",
        "validation (extractor)": "#FF782F",
        "validation (generator)": "#FF782F",
        "docker": "#00FFFF",
        "pipeline": "#39FF14",
    }.get(stage_name, "#F0F8FF")


def print_banner() -> None:
    banner = r"""
░█▀█░█▀▀░█▀▄░█▀▀░█▀█░█▀▀░█░█░█▄█░█▀█░▀█▀░█▀▀
░█▀█░█▀▀░█▀▄░█▀▀░█░█░█░░░█▀█░█░█░█▀█░░█░░█▀▀
░▀░▀░▀▀▀░▀▀░░▀▀▀░▀░▀░▀▀▀░▀░▀░▀░▀░▀░▀░░▀░░▀▀▀
    """
    text = Text(banner.strip("\n"), style="bold #00FFFF")
    panel = Panel(text, title="AEBenchMate", border_style="bold #00FFFF", box=ROUNDED)
    _console.print(panel)


def print_stage_header(stage_name: str, artifact_label: str | None = None) -> None:
    style = _stage_style(stage_name)

    title = f"Stage: {stage_name}"
    if artifact_label:
        title = f"{title} | Artifact: {artifact_label}"

    content = Text(title, style=f"bold {style}")
    panel = Panel(content, border_style=style, box=ROUNDED)
    _console.print(panel)


def print_agent_line(line: str, *, stage: str = "agent") -> None:
    stage_style = _stage_style(stage)
    prefix = {
        "extractor": "🔎 [extractor]: ",
        "generator": "⚙️ [generator]: ",
        "validator": "🛡️ [validator]: ",
        "agent": "🤖 [agent]: ",
    }.get(stage, f"🤖 [{stage}]: ")
    _console.print(f"{prefix}{line}", style=stage_style, markup=False)


def print_info(message: str) -> None:
    _console.print(f"[{_COLORS['cyan']}]🛈[/{_COLORS['cyan']}] {message}")


def print_warning(message: str) -> None:
    _console.print(f"[bold {_COLORS['yellow']}]⚠️[/bold {_COLORS['yellow']}] {message}")


def print_success(message: str) -> None:
    _console.print(f"[bold {_COLORS['green']}]🗹[/bold {_COLORS['green']}] {message}")


def print_retry(attempt: int, max_repairs: int) -> None:
    _console.print(
        f"[bold {_COLORS['yellow']}]🔍[/bold {_COLORS['yellow']}] repair attempt {attempt} "
        f"(max repairs: {max_repairs})"
    )


def print_cost(call_cost_usd: float | None, total_spent_usd: float) -> None:
    if call_cost_usd is None:
        _console.print(f"[{_COLORS['green']}]💰 total spent[/{_COLORS['green']}]: ${total_spent_usd:.6f}")
        return

    _console.print(
        f"[{_COLORS['green']}]💰 call[/{_COLORS['green']}]: ${call_cost_usd:.6f}    "
        f"[{_COLORS['green']}]💰 total[/{_COLORS['green']}]: ${total_spent_usd:.6f}"
    )


def print_validator_summary(report: Any, *, title: str = "Validation") -> None:
    table = Table(box=ROUNDED, show_header=True, header_style="bold")
    table.add_column("Severity", style="white", no_wrap=True)
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Path", style="magenta")
    table.add_column("Message", style="white")

    issue_count = 0
    for issue in getattr(report, "issues", ()):
        severity = str(getattr(issue, "severity", ""))
        code = str(getattr(issue, "code", ""))
        path = str(getattr(issue, "path", ""))
        message = str(getattr(issue, "message", ""))
        table.add_row(severity, code, path, message)
        issue_count += 1

    if issue_count == 0:
        panel = Panel(f"{title}: no issues", border_style="green", box=ROUNDED)
        _console.print(panel)
        return

    ok_value = bool(getattr(report, "ok", False))
    border_style = f"{_COLORS['green']}" if ok_value else "red"
    panel = Panel(table, title=title, border_style=border_style, box=ROUNDED)
    _console.print(panel)


def print_footer_mode(mode_text: str) -> None:
    style = _stage_style(mode_text)
    panel = Panel(mode_text, border_style=style, box=ROUNDED)
    _console.print(panel)


def print_paths_summary(
    *,
    clean_agent_eval_dir: Path | None = None,
    internals_dir: Path | None = None,
    report_path: Path | None = None,
) -> None:
    table = Table(box=ROUNDED, show_header=False)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    if clean_agent_eval_dir is not None:
        table.add_row("_agent_eval", clean_agent_eval_dir.as_posix())
    if internals_dir is not None:
        table.add_row("_agent_internals", internals_dir.as_posix())
    if report_path is not None:
        table.add_row("report", report_path.as_posix())

    panel = Panel(table, title="Outputs", border_style="cyan", box=ROUNDED)
    _console.print(panel)