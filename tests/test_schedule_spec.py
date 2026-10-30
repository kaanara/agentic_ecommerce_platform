import pytest

from aecom.schedule_spec import add_job, is_valid, parse_schedule


def test_intervals():
    assert parse_schedule("30s") == ("interval", {"seconds": 30})
    assert parse_schedule(" 15M ") == ("interval", {"minutes": 15})
    assert parse_schedule("6h") == ("interval", {"hours": 6})


def test_cron():
    assert parse_schedule("cron:15 3 * * *") == (
        "cron", {"minute": "15", "hour": "3", "day": "*", "month": "*", "day_of_week": "*"})
    assert parse_schedule("cron:0 8 * * 1,4")[1]["day_of_week"] == "mon,thu"  # cron 1 = Monday (APScheduler's 1 would be Tuesday)


@pytest.mark.parametrize("bad", ["", "0s", "2s", "abc", "cron:", "cron:1 2 3", "cron:61 * * * *", "cron:* 24 * * *",
                                 "cron:*/5 * * * *", "every 5 minutes"])
def test_rejects_bad_specs(bad):
    assert not is_valid(bad)
    with pytest.raises(ValueError):
        parse_schedule(bad)


def test_add_job_falls_back_on_garbage():
    calls = []

    class Sched:
        def add_job(self, func, kind, **kw):
            calls.append((kind, kw))

    add_job(Sched(), print, "nonsense", "x", "15m")
    assert calls == [("interval", {"id": "x", "replace_existing": True, "minutes": 15})]
