"""A cron turn that stops on a tool guardrail is a failed run.

``guardrail_halt`` (for example ``identical_call_streak_halt``) still returns a final
response with ``failed`` left false. Recording that as ``last_status`` ``ok`` hides the
stop. The job store, cron list, and doctor must show it as not ok, with the halt
reason in ``last_error``.
"""

from unittest.mock import MagicMock, patch

from cron.jobs import create_job, get_job
from cron.scheduler import _RunDelivery, _finish_completed_run, _guardrail_halt_reason, run_job
from hermes_cli.cron import _cron_doctor_issues_for_job, _last_run_display


def _halt_result() -> dict:
    return {
        "final_response": "I stopped retrying because I kept running terminal.",
        "failed": False,
        "completed": True,
        "turn_exit_reason": "guardrail_halt",
        "guardrail": {
            "action": "halt",
            "code": "identical_call_streak_halt",
            "message": "Stopped terminal: the same call returned the same result 8 times.",
            "tool_name": "terminal",
            "count": 8,
        },
        "messages": [],
        "api_calls": 4,
    }


def test_guardrail_halt_reason_names_the_code():
    assert "identical_call_streak_halt" in _guardrail_halt_reason(_halt_result())
    assert _guardrail_halt_reason({"turn_exit_reason": "text_response(finish_reason=stop)", "failed": False}) is None


def test_guardrail_halt_is_recorded_as_halted(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    job = create_job("check the deploy", "every 1h", name="deploy check")

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            return _halt_result()

    fake_db = MagicMock()
    with patch("cron.scheduler._hermes_home", home), \
         patch("cron.scheduler._preflight_job_config", return_value=None), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "***",
                 "base_url": "https://example.invalid/v1",
                 "provider": "openrouter",
                 "api_mode": "chat_completions",
             },
         ), \
         patch("run_agent.AIAgent", FakeAgent):
        success, _output, _final_response, error = run_job(job)

    assert success is False
    assert error and "guardrail_halt" in error and "identical_call_streak_halt" in error
    assert job.get("_terminal_status") == "halted"

    delivery = _RunDelivery(job=job, success=success, error=error)
    assert _finish_completed_run(delivery, None, "no-such-execution") is True

    saved = get_job(job["id"])
    assert saved["last_status"] == "halted"
    assert "identical_call_streak_halt" in (saved.get("last_error") or "")
    assert int(saved.get("failure_streak") or 0) >= 1
    # The cronjob tool treats only these two as a successful run.
    assert saved["last_status"] not in {"ok", "delivery_queued"}
    assert "_terminal_status" not in saved

    issues = _cron_doctor_issues_for_job(saved)
    assert any("identical_call_streak_halt" in issue for issue in issues)
    shown = _last_run_display(saved)
    assert "halted" in shown and "identical_call_streak_halt" in shown
