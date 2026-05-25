"""
Test for rollout_guardrail_scheduler automation.
"""
def test_scheduler_runs_without_crash():
    from services.rollout_guardrail_scheduler import rollout_guardrail_job
    import threading
    import time
    # Run the job in a thread, but only let it loop once for test
    def single_run():
        # Patch time.sleep to break after first loop
        orig_sleep = time.sleep
        def fake_sleep(_):
            raise Exception("break")
        time.sleep = fake_sleep
        try:
            rollout_guardrail_job()
        except Exception as e:
            assert str(e) == "break"
        finally:
            time.sleep = orig_sleep
    t = threading.Thread(target=single_run)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
