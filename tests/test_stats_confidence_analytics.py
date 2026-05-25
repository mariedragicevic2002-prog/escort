from __future__ import annotations


class _StatsDB:
    def execute_query(self, query, params=None, fetch=False, **_kwargs):
        _ = (params, fetch)
        q = str(query)
        if "JOIN conversation_states" in q and "event_type = 'turn_quality'" in q:
            return [
                {"flow_version": "v1", "total_turns": 12, "low_confidence_count": 5},
                {"flow_version": "v2", "total_turns": 20, "low_confidence_count": 4},
            ]
        if "FROM conversation_events" in q and "event_type = 'turn_quality'" in q:
            return [
                {
                    "funnel_step": "qualification",
                    "turn_count": 10,
                    "avg_confidence": 0.7,
                    "low_confidence_count": 2,
                },
                {
                    "funnel_step": "deposit",
                    "turn_count": 5,
                    "avg_confidence": 0.5,
                    "low_confidence_count": 3,
                },
            ]
        if "FROM conversation_events" in q and "event_type = 'action_tag'" in q:
            return [
                {"action_tag": "retrieval_policy_used", "count": 4},
                {"action_tag": "ai_fallback_used", "count": 3},
                {"action_tag": "fallback_template_used", "count": 2},
                {"action_tag": "fallback_template_low_confidence", "count": 1},
            ]
        if "FROM conversation_states" in q and "GROUP BY COALESCE(NULLIF(LOWER(flow_version), ''), 'v1')" in q:
            return [
                {
                    "flow_version": "v1",
                    "total_conversations": 10,
                    "qualified_count": 6,
                    "deposit_reached_count": 3,
                    "confirmed_count": 2,
                },
                {
                    "flow_version": "v2",
                    "total_conversations": 12,
                    "qualified_count": 9,
                    "deposit_reached_count": 5,
                    "confirmed_count": 4,
                },
            ]
        return []


def test_collect_all_stats_includes_confidence_and_fallback_metrics(monkeypatch):
    import admin.blueprints.stats as stats_mod

    monkeypatch.setattr(stats_mod, "get_shared_db", lambda _url: _StatsDB())
    monkeypatch.setattr(stats_mod, "_get_count", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        stats_mod,
        "_compute_earnings",
        lambda *_args, **_kwargs: (0.0, 0.0, 0.0, [], [], [], [], []),
    )
    monkeypatch.setattr(
        stats_mod,
        "get_setting",
        lambda key, default=None: ("0.45" if key == "ai_fallback_confidence_threshold" else default),
    )

    stats = stats_mod._collect_all_stats(days=30, location_filter="all", experience_filter="all")

    assert stats["ai_fallback_confidence_threshold"] == 0.45
    assert stats["total_turn_quality_events"] == 15
    assert stats["low_confidence_turns"] == 5
    assert stats["low_confidence_rate"] == 33.3
    assert stats["avg_turn_confidence"] == 63.3
    assert stats["confidence_by_step_labels"] == [
        "Qualification",
        "Availability",
        "Screening",
        "Deposit",
        "Confirmation",
        "Follow-up",
    ]
    assert stats["confidence_by_step_values"] == [70.0, 0.0, 0.0, 50.0, 0.0, 0.0]
    assert stats["fallback_path_counts"] == {"retrieval_policy": 4, "ai_fallback": 3, "template": 3}
    assert stats["fallback_path_percentages"] == {"retrieval_policy": 40.0, "ai_fallback": 30.0, "template": 30.0}
    assert stats["flow_version_comparison"]["labels"] == ["v1", "v2"]
    assert stats["flow_version_comparison"]["qualification_rate"] == [60.0, 75.0]
    assert stats["flow_version_comparison"]["deposit_reach_rate"] == [30.0, 41.7]
    assert stats["flow_version_comparison"]["confirmation_rate"] == [20.0, 33.3]
    assert stats["flow_version_comparison"]["low_confidence_rate"] == [41.7, 20.0]
    assert "threshold_optimizer" in stats
    assert "rollout_guardrail" in stats
    assert stats["threshold_optimizer"]["suggested_thresholds"]["global"] >= 0.0
    assert stats["rollout_guardrail"]["recommended_action"] in {"none", "reduce_v2", "force_v1"}
