"""
Adella Chatbot — Conversational Simulation & Stress-Test Framework
==================================================================

Generates 500 unique, production-grade end-to-end conversation simulations.

Coverage areas:
  - Booking success flows (incall, outcall, various durations & services)
  - Edge cases (invalid dates, impossible slots, timezone confusion)
  - Adversarial inputs (jailbreak, prompt injection, abuse, spam)
  - Diverse user personas (50 archetypes across 6 behavioural groups)
  - Failure injection (API timeouts, session loss, duplicate bookings)
  - Human behavioural realism (typos, slang, emojis, ghosting, rage-quits)
  - FSM state-machine compliance verification on every transition

Entry point::

    python -m tests.simulation.runner

Output::

    tests/simulation/output/conversations.txt   — Full readable transcripts
    tests/simulation/output/conversations.json  — Machine-readable records
    tests/simulation/output/summary.txt         — Aggregate statistics
"""
