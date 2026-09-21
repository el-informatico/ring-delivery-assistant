"""``star-metric`` CLI: ding -> classification -> routed notification, measured.

The sprint's one number: for each scripted event of THE_DAY, how long
does the whole trip take — the v1.1 edge (HMAC verify + normalize),
classification (snapshot fetch + verdict), the state machine, the
routing decision, and delivery to the sinks including Telegram. N is
the day itself (11 events, comfortably over the required 10), and the
latencies are real ``perf_counter`` measurements of this pipeline on
this machine — only the NETWORK legs are mocked offline (see the
live-vs-mock section of the artifact).

Composition (the same building blocks every other entry uses):

* classifier: ``build_classifier`` — the configured LLM endpoint when
  ``RING_LLM_*`` is set, otherwise the full multimodal call path with
  the deterministic ``mock_vision_transport`` standing in for the
  model. Either way the rules fallback wraps the LLM, so endpoint
  friction degrades per event instead of failing the run.
* Telegram: live ``TelegramSink`` when ``TELEGRAM_BOT_TOKEN`` +
  ``TELEGRAM_CHAT_ID`` are set; otherwise ``mock_telegram_sink`` —
  identical formatting, payload, and response parsing, no socket.

Output directory (default ``.star``, gitignored — timings are this
machine's, not a contract): ``star-metric.md`` plus the snapshots,
manifest, and ``state.db`` the run consumed.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from .classify import IntentClassifier
from .demo import DEMO_SECRET
from .llm import build_classifier, mock_vision_transport
from .replay import Pipeline, StageTimings, TimelineEntry
from .routing import Action, LogSink, Router
from .settings import Settings, load_env_file
from .snapshots import ManifestSnapshotSource
from .state import StateStore
from .telegram import TelegramSink, mock_telegram_sink, telegram_sink_from_env
from .timeline import DAY, THE_DAY, deliver_day, write_snapshots

ARTIFACT_NAME = "star-metric.md"
OUT_DEFAULT = ".star"

# Offline stand-in for the LLM environment: a clearly-not-real endpoint
# plus the deterministic mock vision transport, so the FULL multimodal
# call path (request assembly, base64 data URI, response parse) runs
# without a socket. Going live is configuration, not code.
MOCK_LLM_ENV = {
    "RING_LLM_ENDPOINT": "mock://vision",
    "RING_LLM_MODEL": "mock-vision-1",
    "RING_LLM_API_KEY": "mock-key",
}


@dataclass
class StarRun:
    entries: list[TimelineEntry] = field(default_factory=list)
    telegram: TelegramSink | None = None
    telegram_live: bool = False
    llm_live: bool = False
    classifier_names: set[str] = field(default_factory=set)
    out_dir: Path = Path(OUT_DEFAULT)

    @property
    def timings(self) -> list[StageTimings]:
        return [e.timings for e in self.entries if e.timings is not None]


def _star_classifier(
    settings: Settings, source: ManifestSnapshotSource
) -> tuple[IntentClassifier, bool]:
    """The classifier for a star run: live LLM if configured, mock call path otherwise."""
    if settings.llm_endpoint and settings.llm_model:
        return build_classifier(settings, snapshot_source=source), True
    offline = Settings.from_env(MOCK_LLM_ENV)
    return (
        build_classifier(offline, snapshot_source=source, transport=mock_vision_transport),
        False,
    )


def run_star(
    out_dir: Path,
    *,
    secret: str,
    settings: Settings | None = None,
    telegram_transport=None,
) -> StarRun:
    """Play THE_DAY with stage timings on; write the run under ``out_dir``."""
    settings = settings or Settings()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    manifest_path = write_snapshots(out_dir)
    source = ManifestSnapshotSource(root=out_dir, manifest_path=manifest_path)

    classifier, llm_live = _star_classifier(settings, source)

    # Telegram: the live sink when .env configures one, the mock sink
    # otherwise. Router.from_settings already wires a live sink when it
    # sees credentials — swap it for ours so ``telegram_transport`` (the
    # test recorder) is the only network leg this run can take; offline,
    # add the mock so delivery still executes end to end
    router = Router.from_settings(settings)
    telegram = telegram_sink_from_env(settings, transport=telegram_transport)
    telegram_live = telegram is not None
    if telegram_live:
        router = replace(
            router,
            primary=tuple(s for s in router.primary if s.name != "telegram") + (telegram,),
            secondary=tuple(s for s in router.secondary if s.name != "telegram")
            + (telegram,),
        )
    else:
        telegram = mock_telegram_sink()
        router = replace(router, primary=router.primary + (telegram,), secondary=router.secondary + (telegram,))

    pipeline = Pipeline(
        secret=secret,
        classifier=classifier,
        store=StateStore(out_dir / "state.db"),
        router=router,
        signature_header="x-signature",
    )
    deliveries = deliver_day(pipeline)
    pipeline.store.close()

    run = StarRun(
        entries=[d.entry for d in deliveries],
        telegram=telegram,
        telegram_live=telegram_live,
        llm_live=llm_live,
        classifier_names={d.entry.intent.source for d in deliveries},
        out_dir=out_dir,
    )
    (out_dir / ARTIFACT_NAME).write_text(render_star_markdown(run), encoding="utf-8")
    return run


def summarize(timings: list[StageTimings]) -> dict[str, float]:
    """The summary statistics the README table carries (milliseconds)."""
    totals = sorted(t.total_ms for t in timings)
    p90 = totals[min(len(totals) - 1, int(round(0.9 * (len(totals) - 1))))]
    return {
        "n": float(len(totals)),
        "min_ms": totals[0],
        "median_ms": statistics.median(totals),
        "mean_ms": statistics.fmean(totals),
        "p90_ms": p90,
        "classify_median_ms": statistics.median(t.classify_ms for t in timings),
    }


def _mode(run: StarRun) -> str:
    llm = "live LLM endpoint" if run.llm_live else "mock vision transport (no socket)"
    telegram = "live Telegram API" if run.telegram_live else "mock Telegram transport (no socket)"
    return f"classifier: {llm} · sink: {telegram}"


def render_star_markdown(run: StarRun) -> str:
    """The star-metric artifact: per-event table, summary, live-vs-mock."""
    s = summarize(run.timings)
    out = [f"# Star metric — ding → classification → routed notification"]
    out.append("")
    out.append(
        f"One scripted day ({DAY:%Y-%m-%d}, {len(run.entries)} wire deliveries, all "
        "through the live v1.1 edge) with per-stage latency measured on this "
        "machine. The number the sprint asks for is the **total** column: "
        "signed bytes in at the edge, routed notification out of the sinks."
    )
    out.append("")
    out.append(f"Run mode — {_mode(run)}; classifier sources seen: "
        + ", ".join(f"`{name}`" for name in sorted(run.classifier_names)) + ".")
    out.append("")
    out.append("## Measured latencies")
    out.append("")
    out.append(
        "| id | occurred | intent | action | edge ms | classify ms | state ms "
        "| route ms | deliver ms | total ms |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for beat, entry in zip(THE_DAY, run.entries, strict=True):
        t = entry.timings
        assert t is not None  # every star entry went through the timed edge
        notified = "★" if entry.decision.action in (Action.NOTIFY, Action.ESCALATE) else "—"
        out.append(
            "| {id} | {occurred} | {intent} {notified} | {action} | {edge:.3f} | {cls:.3f} "
            "| {state:.3f} | {route:.3f} | {deliver:.3f} | {total:.3f} |".format(
                id=beat.beat_id,
                occurred=entry.event.occurred_at.strftime("%H:%M:%S"),
                intent=entry.intent.intent.value,
                notified=notified,
                action=entry.decision.action.value,
                edge=t.edge_ms,
                cls=t.classify_ms,
                state=t.state_ms,
                route=t.route_ms,
                deliver=t.deliver_ms,
                total=t.total_ms,
            )
        )
    out.append("")
    out.append("★ = a routed notification left the sinks for this event.")
    out.append("")
    out.append("## Summary")
    out.append("")
    notified = sum(
        1 for e in run.entries if e.decision.action in (Action.NOTIFY, Action.ESCALATE)
    )
    out.append(
        f"- **N = {int(s['n'])} events** (sprint requires ≥ 10), {notified} routed "
        "notifications, "
        f"{len(run.telegram.sent) if run.telegram else 0} Telegram sendMessage calls"
    )
    out.append(
        f"- ding → routed notification (total): min {s['min_ms']:.3f} ms · "
        f"median {s['median_ms']:.3f} ms · mean {s['mean_ms']:.3f} ms · p90 {s['p90_ms']:.3f} ms"
    )
    out.append(f"- classification stage alone: median {s['classify_median_ms']:.3f} ms")
    out.append("")
    out.append("## Live vs mock (this run)")
    out.append("")
    out.append("| stage | mode this run | how to go live |")
    out.append("|---|---|---|")
    out.append(
        "| classification | "
        + (
            "live LLM endpoint" if run.llm_live
            else "full multimodal call path, deterministic mock model"
        )
        + " | set `RING_LLM_ENDPOINT`, `RING_LLM_MODEL`, `RING_LLM_API_KEY` in `.env` |"
    )
    out.append(
        "| delivery | "
        + (
            "live Telegram API (real sendMessage POSTs, RTT included in `deliver`)"
            if run.telegram_live
            else "Telegram sink with recording transport (no POST)"
        )
        + " | set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env` (see README) |"
    )
    out.append("")
    if run.llm_live or run.telegram_live:
        legs = ", ".join(
            leg
            for leg, live in (("LLM", run.llm_live), ("Telegram", run.telegram_live))
            if live
        )
        # name only the columns whose stage actually went over the network:
        # deliver is live iff the Telegram sink is, classify iff the LLM is —
        # with a live sink but a mocked classifier, "deliver/classify" claimed
        # network round trips the classify column never made
        cols = [c for c, live in (("deliver", run.telegram_live), ("classify", run.llm_live)) if live]
        noun = "columns" if len(cols) > 1 else "column"
        out.append(
            f"This run went live on {legs} — the `{'`/`'.join(cols)}` {noun} and the "
            "totals include those real network round trips. Stages that stayed mocked "
            "measure the pipeline's own work only."
        )
    else:
        out.append(
            "Offline latencies therefore measure the pipeline's own work; the live "
            "network legs (LLM RTT, Telegram RTT) are not included."
        )
    out.append(
        "The artifact is "
        "regenerated by `uv run star-metric` and gitignored on purpose — "
        "timings belong to the machine that measured them."
    )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="star-metric",
        description="Measure ding -> classification -> routed notification over "
        "the scripted day (offline unless .env configures live legs).",
    )
    parser.add_argument("--out", default=OUT_DEFAULT, help=f"output directory (default: {OUT_DEFAULT})")
    args = parser.parse_args(argv)

    load_env_file()  # best-effort .env; real env vars always win
    settings = Settings.from_env()
    secret = settings.webhook_secret or DEMO_SECRET
    run = run_star(Path(args.out), secret=secret, settings=settings)

    s = summarize(run.timings)
    print(_mode(run))
    print()
    print(
        f"N = {int(s['n'])} events · ding -> routed notification: "
        f"min {s['min_ms']:.3f} ms · median {s['median_ms']:.3f} ms · "
        f"mean {s['mean_ms']:.3f} ms · p90 {s['p90_ms']:.3f} ms"
    )
    print(f"classification stage: median {s['classify_median_ms']:.3f} ms")
    print(f"artifact: {run.out_dir / ARTIFACT_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
