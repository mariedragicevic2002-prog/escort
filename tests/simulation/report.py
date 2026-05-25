"""
report.py — Quality and analytics report generator for the simulation output.

Reads conversations.json produced by generator.py and produces:
  - summary.txt     : High-level stats (already done by generator)
  - quality_report.txt : Deeper per-scenario, per-persona-group, and
                         failure-mode quality analysis
  - fsm_violations.txt : Any conversations whose FSM trace breaks rules

Usage:
    python -m tests.simulation.report [--json-path PATH]
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OUTPUT_DIR = Path(__file__).parent / "output"
JSON_PATH  = OUTPUT_DIR / "conversations.json"

# Valid FSM transitions (mirrors core/state_machine.py)
ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    ("NEW",              "AWAITING_DETAILS"),
    ("NEW",              "ESCALATED"),        # adversarial: abuse/jailbreak/spam escalated immediately
    ("AWAITING_DETAILS", "CONFIRMED"),
    ("AWAITING_DETAILS", "CANCELLED"),
    ("AWAITING_DETAILS", "ESCALATED"),
    ("CONFIRMED",        "COMPLETED"),
    ("CONFIRMED",        "CANCELLED"),
    ("CONFIRMED",        "RESCHEDULED"),
    ("CONFIRMED",        "ESCALATED"),
    ("ESCALATED",        "NEW"),
    ("ESCALATED",        "CONFIRMED"),
    ("ESCALATED",        "CANCELLED"),
    ("RESCHEDULED",      "CONFIRMED"),
    ("RESCHEDULED",      "CANCELLED"),
    ("RESCHEDULED",      "ESCALATED"),
}

TERMINAL_STATES = {"COMPLETED", "CANCELLED", "ESCALATED"}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_conversations(path: Path = JSON_PATH) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"conversations.json not found at {path}. "
            "Run the generator first: python -m tests.simulation.runner"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# FSM validation
# ---------------------------------------------------------------------------

def check_fsm_trace(trace: list[str]) -> list[str]:
    """Return a list of violation descriptions for a single FSM trace."""
    violations: list[str] = []
    for i in range(len(trace) - 1):
        pair = (trace[i], trace[i + 1])
        if pair not in ALLOWED_TRANSITIONS:
            violations.append(
                f"Illegal transition: {trace[i]} → {trace[i + 1]}"
            )
    return violations


def validate_all_fsm(convs: list[dict]) -> list[dict]:
    """Return list of {conv_id, persona_id, scenario_id, violations} for bad traces."""
    bad: list[dict] = []
    for c in convs:
        trace = c.get("fsm_trace", [])
        violations = check_fsm_trace(trace)
        if violations:
            bad.append({
                "conv_id":     c["conversation_id"],
                "persona_id":  c["persona_id"],
                "scenario_id": c["scenario_id"],
                "trace":       " → ".join(trace),
                "violations":  violations,
            })
    return bad


# ---------------------------------------------------------------------------
# Metric collectors
# ---------------------------------------------------------------------------

def per_scenario_stats(convs: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "outcomes": Counter(), "avg_turns": 0.0,
        "failures": 0, "turns_total": 0,
    })
    for c in convs:
        sid = c["scenario_id"]
        stats[sid]["count"] += 1
        stats[sid]["outcomes"][c["outcome"]] += 1
        stats[sid]["turns_total"] += c["total_turns"]
        if c.get("failure_injected"):
            stats[sid]["failures"] += 1
    for sid, s in stats.items():
        s["avg_turns"] = s["turns_total"] / max(s["count"], 1)
    return dict(stats)


def per_persona_group_stats(convs: list[dict]) -> dict[str, dict]:
    """Group by first letter of persona_id's group field (loaded lazily)."""
    try:
        from tests.simulation.personas import PERSONA_BY_ID
        get_group = lambda pid: PERSONA_BY_ID[pid].group if pid in PERSONA_BY_ID else "?"
    except Exception:
        get_group = lambda pid: pid[0].upper()

    stats: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "outcomes": Counter(), "avg_turns": 0.0,
        "turns_total": 0, "failures": 0,
    })
    for c in convs:
        g = get_group(c["persona_id"])
        stats[g]["count"] += 1
        stats[g]["outcomes"][c["outcome"]] += 1
        stats[g]["turns_total"] += c["total_turns"]
        if c.get("failure_injected"):
            stats[g]["failures"] += 1
    for g, s in stats.items():
        s["avg_turns"] = s["turns_total"] / max(s["count"], 1)
    return dict(stats)


def failure_recovery_stats(convs: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(lambda: {
        "injected": 0, "recovered": 0, "failed": 0,
    })
    for c in convs:
        fm = c.get("failure_injected")
        if not fm:
            continue
        stats[fm]["injected"] += 1
        if c["outcome"] in ("confirmed", "inquiry_only"):
            stats[fm]["recovered"] += 1
        else:
            stats[fm]["failed"] += 1
    return dict(stats)


def turn_length_stats(convs: list[dict]) -> dict:
    user_lengths: list[int] = []
    bot_lengths:  list[int] = []
    for c in convs:
        for t in c["turns"]:
            length = len(t["text"])
            if t["speaker"] == "USER":
                user_lengths.append(length)
            else:
                bot_lengths.append(length)
    def _stats(lengths: list[int]) -> dict:
        if not lengths:
            return {}
        lengths_sorted = sorted(lengths)
        n = len(lengths_sorted)
        return {
            "count": n,
            "avg": round(sum(lengths) / n, 1),
            "min": lengths_sorted[0],
            "max": lengths_sorted[-1],
            "p50": lengths_sorted[n // 2],
            "p90": lengths_sorted[int(n * 0.9)],
        }
    return {"user": _stats(user_lengths), "bot": _stats(bot_lengths)}


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def _hr(width: int = 62) -> str:
    return "─" * width


def write_quality_report(convs: list[dict], path: Path) -> None:
    scenario_s  = per_scenario_stats(convs)
    group_s     = per_persona_group_stats(convs)
    failure_s   = failure_recovery_stats(convs)
    turn_s      = turn_length_stats(convs)
    fsm_bad     = validate_all_fsm(convs)
    total       = len(convs)
    confirmed   = sum(1 for c in convs if c["outcome"] == "confirmed")

    lines: list[str] = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║         ADELLA CHATBOT  —  SIMULATION QUALITY REPORT        ║",
        "╚══════════════════════════════════════════════════════════════╝",
        "",
        f"  Total conversations  : {total}",
        f"  Successful bookings  : {confirmed}  ({100*confirmed/total:.1f}%)",
        f"  FSM violations       : {len(fsm_bad)}",
        f"  Failures injected    : {sum(s['injected'] for s in failure_s.values())}",
        "",
        _hr(),
        "PER-SCENARIO BREAKDOWN",
        _hr(),
        f"  {'Scenario':<35} {'N':>4}  {'Avg turns':>9}  {'Top outcome':<20}  {'Fails':>5}",
        f"  {'-'*35} {'-'*4}  {'-'*9}  {'-'*20}  {'-'*5}",
    ]

    for sid in sorted(scenario_s.keys()):
        s = scenario_s[sid]
        top_outcome = s["outcomes"].most_common(1)[0][0] if s["outcomes"] else "—"
        lines.append(
            f"  {sid:<35} {s['count']:>4}  {s['avg_turns']:>9.1f}  {top_outcome:<20}  {s['failures']:>5}"
        )

    lines += [
        "",
        _hr(),
        "PER-PERSONA-GROUP BREAKDOWN",
        _hr(),
        f"  {'Group':<6} {'N':>5}  {'Avg turns':>9}  {'Confirmed':>9}  {'Abandoned':>9}  {'Blocked':>7}",
        f"  {'-'*6} {'-'*5}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*7}",
    ]

    GROUP_LABELS = {
        "A": "A — Friendly/Cooperative",
        "B": "B — Difficult/Demanding",
        "C": "C — Confused/Vague",
        "D": "D — Human-quirk Realists",
        "E": "E — Adversarial",
        "F": "F — Edge-case Specialists",
    }
    for g in sorted(group_s.keys()):
        s = group_s[g]
        label = GROUP_LABELS.get(g, f"Group {g}")
        confirmed_n  = s["outcomes"].get("confirmed",    0)
        abandoned_n  = s["outcomes"].get("abandoned",    0)
        blocked_n    = s["outcomes"].get("blocked",      0)
        lines.append(
            f"  {label:<26} {s['count']:>5}  {s['avg_turns']:>9.1f}"
            f"  {confirmed_n:>9}  {abandoned_n:>9}  {blocked_n:>7}"
        )

    lines += [
        "",
        _hr(),
        "FAILURE-INJECTION RECOVERY ANALYSIS",
        _hr(),
        f"  {'Failure mode':<35} {'Injected':>8}  {'Recovered':>9}  {'Unrecovered':>11}  {'Recovery %':>10}",
        f"  {'-'*35} {'-'*8}  {'-'*9}  {'-'*11}  {'-'*10}",
    ]

    for fm in sorted(failure_s.keys()):
        s = failure_s[fm]
        pct = 100 * s["recovered"] / max(s["injected"], 1)
        lines.append(
            f"  {fm:<35} {s['injected']:>8}  {s['recovered']:>9}  {s['failed']:>11}  {pct:>9.1f}%"
        )

    lines += [
        "",
        _hr(),
        "MESSAGE LENGTH ANALYSIS",
        _hr(),
        f"  {'Speaker':<8} {'Count':>7}  {'Avg chars':>9}  {'Min':>6}  {'p50':>6}  {'p90':>6}  {'Max':>6}",
        f"  {'-'*8} {'-'*7}  {'-'*9}  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*6}",
    ]
    for speaker in ("user", "bot"):
        s = turn_s.get(speaker, {})
        if s:
            lines.append(
                f"  {speaker.upper():<8} {s['count']:>7}  {s['avg']:>9}  "
                f"{s['min']:>6}  {s['p50']:>6}  {s['p90']:>6}  {s['max']:>6}"
            )

    lines += [
        "",
        _hr(),
        f"FSM COMPLIANCE  ({total - len(fsm_bad)}/{total} clean traces)",
        _hr(),
    ]
    if not fsm_bad:
        lines.append("  ✓ All 500 conversation FSM traces are valid.")
    else:
        for bad in fsm_bad[:20]:
            lines.append(f"  ✗ {bad['conv_id']}  ({bad['scenario_id']})")
            lines.append(f"      Trace: {bad['trace']}")
            for v in bad['violations']:
                lines.append(f"      → {v}")
        if len(fsm_bad) > 20:
            lines.append(f"  … and {len(fsm_bad)-20} more.")

    lines += ["", "═" * 62]

    path.write_text("\n".join(lines), encoding="utf-8")


def write_fsm_violations(convs: list[dict], path: Path) -> None:
    bad = validate_all_fsm(convs)
    if not bad:
        path.write_text("No FSM violations detected across all 500 conversations.\n",
                        encoding="utf-8")
        return
    rows = [f"FSM VIOLATIONS ({len(bad)} conversations)\n", "=" * 60]
    for b in bad:
        rows.append(f"\n{b['conv_id']}  persona={b['persona_id']}  scenario={b['scenario_id']}")
        rows.append(f"  Trace: {b['trace']}")
        for v in b["violations"]:
            rows.append(f"  ✗ {v}")
    path.write_text("\n".join(rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(json_path: Path = JSON_PATH, verbose: bool = True) -> None:
    if verbose:
        print(f"Loading conversations from {json_path}…")
    convs = load_conversations(json_path)

    quality_path    = OUTPUT_DIR / "quality_report.txt"
    violations_path = OUTPUT_DIR / "fsm_violations.txt"

    if verbose:
        print("Generating quality report…")
    write_quality_report(convs, quality_path)

    if verbose:
        print("Generating FSM violation report…")
    write_fsm_violations(convs, violations_path)

    if verbose:
        print(f"\nReports written:")
        print(f"  {quality_path}")
        print(f"  {violations_path}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Generate simulation quality report.")
    parser.add_argument("--json-path", type=Path, default=JSON_PATH,
                        help="Path to conversations.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    run(json_path=args.json_path, verbose=not args.quiet)


if __name__ == "__main__":
    main()
