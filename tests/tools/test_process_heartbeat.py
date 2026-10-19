"""Heartbeat notifications for long background processes.

A ``heartbeat`` on a background process emits a periodic "still running + output since the last
heartbeat" event on the completion queue so the agent stays current on a long bounded job (merge
train, full suite, deploy) without polling. Invariants: each heartbeat carries only NEW output,
heartbeats stop at exit, and the normal completion notice still fires.
"""
import json
import queue
import time

import pytest

import tools.process_registry as pr
from tools.process_registry import ProcessRegistry


def _drain(q: "queue.Queue") -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _wait_until(pred, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.mark.platforms("linux")
def test_heartbeat_carries_only_new_output_and_stops_at_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    registry = ProcessRegistry()
    session = registry.spawn_local("echo first; sleep 2.5; echo second; sleep 2.5", cwd=str(tmp_path))
    session.notify_on_complete = True
    assert registry.arm_heartbeat(session, 1) == 1

    assert _wait_until(lambda: registry.poll(session.id)["status"] != "running", timeout=20)
    # Give the completion event a moment to be enqueued after the reader observes EOF.
    assert _wait_until(lambda: any(e.get("type") == "completion" for e in list(registry.completion_queue.queue)),
                       timeout=5)
    events = _drain(registry.completion_queue)
    beats = [e for e in events if e["type"] == "heartbeat"]
    completion = [e for e in events if e["type"] == "completion"]

    assert len(beats) >= 2, events
    assert [b["seq"] for b in beats] == list(range(1, len(beats) + 1))
    assert all(b["session_id"] == session.id and b["interval"] == 1 for b in beats)
    # Output is a delta: every produced line appears in exactly one heartbeat, never twice.
    joined = "".join(b["output"] for b in beats)
    assert joined.count("first") == 1 and joined.count("second") == 1, [b["output"] for b in beats]
    assert len(completion) == 1
    # Heartbeats never outlive the process: nothing after the completion notice.
    assert events.index(completion[0]) > events.index(beats[-1])
    assert not _wait_until(lambda: any(e.get("type") == "heartbeat" for e in list(registry.completion_queue.queue)),
                           timeout=2.5)


def test_foreground_call_ignores_filled_in_heartbeat_and_notify(monkeypatch):
    """Models that fill optional fields send heartbeat:60 and notify:false on every
    terminal call. A foreground command still runs; those fields are background-only."""
    from tools import terminal_tool as tt

    captured = {}

    def fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "ok", "exit_code": 0})

    monkeypatch.setattr(tt, "terminal_tool", fake_terminal_tool)
    result = json.loads(tt._handle_terminal({
        "command": "echo hi",
        "heartbeat": 60,
        "notify": False,
        "pty": True,
        "persist_on_release": True,
    }))
    assert not result.get("error")
    assert captured["background"] is False
    assert captured["heartbeat"] == 0
    assert captured["notify_on_complete"] is False
    assert captured["pty"] is False
    assert captured["persist_on_release"] is False
    # 0 is a valid "off" value, so a provider that enforces the schema accepts it.
    assert tt.TERMINAL_SCHEMA["parameters"]["properties"]["heartbeat"]["minimum"] == 0


def test_background_heartbeat_still_implies_notify(monkeypatch):
    from tools import terminal_tool as tt

    captured = {}

    def fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "Background process started", "session_id": "proc_x", "exit_code": 0})

    monkeypatch.setattr(tt, "terminal_tool", fake_terminal_tool)
    bg = json.loads(tt._handle_terminal({"command": "sleep 1", "background": True, "heartbeat": 120}))
    assert not bg.get("error")
    assert captured["heartbeat"] == 120 and captured["notify_on_complete"] is True
    assert captured["background"] is True
