import pytest

from aecom import jobs


@pytest.fixture(autouse=True)
def _restore_handlers():
    saved = dict(jobs._handlers)
    yield
    jobs._handlers.clear()
    jobs._handlers.update(saved)


def test_enqueue_run_and_result():
    jobs.register("backup", lambda params: {"path": "x", "label": params.get("label")})
    jid = jobs.enqueue("backup", "kaan", {"label": "manual"})
    assert jobs.get(jid)["status"] == "queued"
    assert jobs.run_pending() == [jid]
    j = jobs.get(jid)
    assert j["status"] == "ok" and j["result"] == {"path": "x", "label": "manual"} and j["finished_at"]


def test_only_one_job_per_kind_at_a_time():
    jobs.enqueue("verify", "kaan")
    with pytest.raises(ValueError, match="already queued"):
        jobs.enqueue("verify", "kaan")
    with pytest.raises(ValueError, match="unknown"):
        jobs.enqueue("rm-rf", "kaan")


def test_failing_handler_marks_error_and_goes_on():
    def boom(_):
        raise RuntimeError("disk full")

    jobs.register("restore", boom)
    jobs.register("verify", lambda p: {"ok": True})
    bad, good = jobs.enqueue("restore", "kaan"), jobs.enqueue("verify", "kaan")
    assert jobs.run_pending() == [bad, good]
    assert jobs.get(bad)["status"] == "error" and "disk full" in jobs.get(bad)["result"]["error"]
    assert jobs.get(good)["status"] == "ok"
    jobs.enqueue("restore", "kaan")  # a finished job no longer blocks the kind


def test_doctor_job_runs_the_checks():
    jid = jobs.enqueue("doctor", "kaan")
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok" and j["result"]["checks"] and {"name", "status"} <= set(j["result"]["checks"][0])
