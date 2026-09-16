"""The star-metric run: N, per-stage timings, parity, artifact.

``run_star`` plays the same scripted day as the timeline artifact, but
through the LLM call path (deterministic mock model offline) and a
Telegram sink (mock transport offline, live transport injectable), with
``perf_counter`` timings attached to every entry. These tests pin the
claims the README table makes: N, every stage measured, the mock
verdicts match the rules verdicts, and the phone-bound sink really
receives what routing decided.
"""

from __future__ import annotations

from ring_assistant.routing import Action
from ring_assistant.settings import Settings
from ring_assistant.star import ARTIFACT_NAME, main, run_star, summarize
from ring_assistant.telegram import RecordingTelegramTransport
from ring_assistant.timeline import run_day

SECRET = "star-test-secret"

EXPECTED_TITLES = [
    "Package deposited",
    "Vehicle at the door",
    "Person at the door",
    "Package deposited",
    "Package picked up",
    "Repeated motion",
    "Person at the door at night",
]


def star(tmp_path):
    return run_star(tmp_path / "star", secret=SECRET, settings=Settings())


def test_n_is_at_least_ten_and_every_stage_is_measured(tmp_path):
    run = star(tmp_path)
    assert len(run.entries) >= 10  # the sprint's N bar
    for entry in run.entries:
        timings = entry.timings
        assert timings is not None
        assert timings.edge_ms >= 0
        assert timings.classify_ms >= 0
        assert timings.state_ms >= 0
        assert timings.route_ms >= 0
        assert timings.deliver_ms >= 0
        assert timings.total_ms > 0


def test_star_run_agrees_with_the_rules_timeline(tmp_path):
    # same day, different classifier path (mock vision vs pixel rules):
    # intents, actions, and transitions must not drift
    other = run_day(tmp_path / "day", secret=SECRET, settings=Settings())
    outcomes = lambda entries: [  # noqa: E731
        (e.intent.intent, e.decision.action, e.apply_result.transition) for e in entries
    ]
    assert outcomes(star(tmp_path).entries) == outcomes(other.entries)


def test_every_routed_notification_reaches_the_telegram_sink(tmp_path):
    run = star(tmp_path)
    assert run.telegram is not None and not run.telegram_live
    assert [n.title for n in run.telegram.sent] == EXPECTED_TITLES
    # severity conditioning survives the whole trip: info silent, the
    # night escalation rings
    calls = run.telegram.transport.calls
    assert len(calls) == len(EXPECTED_TITLES)
    assert calls[-1].payload["disable_notification"] is False
    assert all(call.payload["disable_notification"] for call in calls[:-1])


def test_configured_env_wires_the_live_sink_with_the_injected_transport(tmp_path):
    settings = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "42"}
    )
    recording = RecordingTelegramTransport()
    run = run_star(
        tmp_path / "star", secret=SECRET, settings=settings, telegram_transport=recording
    )
    assert run.telegram_live
    assert len(recording.calls) == len(EXPECTED_TITLES)
    assert recording.calls[0].url.startswith("https://api.telegram.org/botsynthetic-token/")
    assert recording.calls[0].payload["chat_id"] == "42"


def test_offline_run_reports_the_mock_classifier_source(tmp_path):
    run = star(tmp_path)
    assert run.classifier_names == {"llm:mock-vision-1"}
    assert not run.llm_live


def test_summary_statistics_are_ordered_and_complete(tmp_path):
    summary = summarize(star(tmp_path).timings)
    assert summary["n"] == 11
    assert 0 <= summary["min_ms"] <= summary["median_ms"] <= summary["p90_ms"]
    assert summary["min_ms"] <= summary["mean_ms"]
    assert summary["classify_median_ms"] >= 0


def test_artifact_carries_the_table_summary_and_go_live_steps(tmp_path):
    run = star(tmp_path)
    markdown = (run.out_dir / ARTIFACT_NAME).read_text(encoding="utf-8")
    assert "| id | occurred | intent | action | edge ms |" in markdown
    assert "| s3-001 | 08:03:21 |" in markdown
    assert "N = 11 events" in markdown
    assert "mock vision transport" in markdown
    assert "TELEGRAM_BOT_TOKEN" in markdown  # the exact go-live pointer


def test_cli_main_writes_the_artifact(tmp_path, capsys):
    out = tmp_path / "cli"
    assert main(["--out", str(out)]) == 0
    assert (out / ARTIFACT_NAME).is_file()
    printed = capsys.readouterr().out
    assert "N = 11 events" in printed
    assert f"artifact: {out / ARTIFACT_NAME}" in printed
