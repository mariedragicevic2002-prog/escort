"""
Generates all 500 simulated conversations and writes output files.

Allocation strategy
-------------------
- 50 personas × 30 scenarios = 1,500 possible (persona, scenario) pairs.
- We deterministically select 500 unique pairs (using a seeded shuffle).
- Each pair gets a unique integer seed for reproducibility.
- ~15 % of conversations receive failure injection.

Output files (written to tests/simulation/output/)
----------------------------------------------------
conversations.txt  — Human-readable transcripts, one per conversation
conversations.json — Machine-readable JSON array
summary.txt        — Aggregate statistics
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# Allow running as `python -m tests.simulation.generator` from repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.simulation.personas import ALL_PERSONAS, PERSONA_BY_ID, Persona
from tests.simulation.scenarios import SCENARIOS, SCENARIO_BY_ID, Scenario
from tests.simulation.engine import ConversationEngine, ConversationLog
from tests.simulation.failure_modes import FailureInjector

OUTPUT_DIR = Path(__file__).parent / "output"
TARGET_COUNT = 500
GLOBAL_SEED = 42


# ---------------------------------------------------------------------------
# Pair selection
# ---------------------------------------------------------------------------

def _all_pairs() -> list[tuple[Persona, Scenario]]:
    """Return all 1,500 (persona, scenario) pairs."""
    return [
        (p, s)
        for p in ALL_PERSONAS
        for s in SCENARIOS
    ]


def _select_500(pairs: list[tuple[Persona, Scenario]],
                seed: int = GLOBAL_SEED) -> list[tuple[Persona, Scenario]]:
    """Deterministically select 500 unique pairs."""
    rng = random.Random(seed)
    shuffled = list(pairs)
    rng.shuffle(shuffled)
    return shuffled[:TARGET_COUNT]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_conversations(verbose: bool = True) -> list[ConversationLog]:
    pairs = _select_500(_all_pairs())
    injector = FailureInjector(injection_rate=0.15, rng=random.Random(GLOBAL_SEED + 1))
    engine = ConversationEngine(failure_injector=injector)

    logs: list[ConversationLog] = []
    for idx, (persona, scenario) in enumerate(pairs):
        conv_id = f"SIM-{idx + 1:04d}"
        seed = GLOBAL_SEED + idx + 1
        log = engine.generate(persona, scenario, seed, conv_id)
        logs.append(log)
        if verbose and (idx + 1) % 50 == 0:
            print(f"  Generated {idx + 1}/{TARGET_COUNT} conversations…")

    return logs


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_transcripts(logs: list[ConversationLog], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for log in logs:
            fh.write(log.as_transcript())
            fh.write("\n\n")


def _write_json(logs: list[ConversationLog], path: Path) -> None:
    data = [log.as_dict() for log in logs]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _write_summary(logs: list[ConversationLog], path: Path) -> None:
    from collections import Counter

    outcomes = Counter(log.outcome for log in logs)
    failures = Counter(log.failure_injected for log in logs if log.failure_injected)
    persona_groups: Counter = Counter()
    from tests.simulation.personas import PERSONA_BY_ID
    for log in logs:
        p = PERSONA_BY_ID.get(log.persona_id)
        if p:
            persona_groups[p.group] += 1
    scenario_cats: Counter = Counter()
    for log in logs:
        s = SCENARIO_BY_ID.get(log.scenario_id)
        if s:
            scenario_cats[s.category.value] += 1

    avg_turns = sum(log.total_turns for log in logs) / max(len(logs), 1)
    total_injected = sum(1 for log in logs if log.failure_injected)

    lines = [
        "=" * 60,
        "SIMULATION SUMMARY",
        f"Total conversations : {len(logs)}",
        f"Average turns/conv  : {avg_turns:.1f}",
        f"Failures injected   : {total_injected} ({100*total_injected/len(logs):.1f}%)",
        "",
        "OUTCOMES",
        *[f"  {k:<25} {v}" for k, v in outcomes.most_common()],
        "",
        "FAILURE MODES INJECTED",
        *[f"  {k:<35} {v}" for k, v in failures.most_common()],
        "",
        "PERSONA GROUPS",
        *[f"  {k:<30} {v}" for k, v in persona_groups.most_common()],
        "",
        "SCENARIO CATEGORIES",
        *[f"  {k:<30} {v}" for k, v in scenario_cats.most_common()],
        "=" * 60,
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(verbose: bool = True) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"Generating {TARGET_COUNT} conversations…")

    logs = generate_conversations(verbose=verbose)

    txt_path = OUTPUT_DIR / "conversations.txt"
    json_path = OUTPUT_DIR / "conversations.json"
    summary_path = OUTPUT_DIR / "summary.txt"

    if verbose:
        print("Writing transcripts…")
    _write_transcripts(logs, txt_path)

    if verbose:
        print("Writing JSON…")
    _write_json(logs, json_path)

    if verbose:
        print("Writing summary…")
    _write_summary(logs, summary_path)

    if verbose:
        print(f"\nDone! Files written to {OUTPUT_DIR}:")
        print(f"  {txt_path.name}")
        print(f"  {json_path.name}")
        print(f"  {summary_path.name}")


if __name__ == "__main__":
    run()
