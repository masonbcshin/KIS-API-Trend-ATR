from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.fast_eval_replay import build_replay_report, load_quote_replay_events, main


def _write_quote_replay_fixture(path: Path, *, duration_sec: int = 180) -> list[str]:
    symbols = [f"{idx:06d}" for idx in range(1, 9)]
    holding_symbols = symbols[:2]
    start_at = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)

    with path.open("w", encoding="utf-8") as handle:
        for second in range(duration_sec + 1):
            event_at = start_at + timedelta(seconds=second)
            received_at = event_at - timedelta(milliseconds=200)
            for symbol in symbols:
                handle.write(
                    json.dumps(
                        {
                            "ts": event_at.isoformat(),
                            "received_at": received_at.isoformat(),
                            "symbol": symbol,
                            "has_position": symbol in holding_symbols,
                            "ws_connected": True,
                            "current_price": 10000 + second,
                        }
                    )
                    + "\n"
                )
    return holding_symbols


def test_fast_eval_replay_builds_log_file_based_comparison(tmp_path: Path):
    replay_path = tmp_path / "quote_replay.jsonl"
    holding_symbols = _write_quote_replay_fixture(replay_path)

    events = load_quote_replay_events(replay_path)
    report = build_replay_report(
        events,
        holding_symbols=holding_symbols,
        source_path=str(replay_path),
    )

    assert report["input"]["events"] == 8 * 181
    assert report["input"]["source_path"] == str(replay_path)
    assert report["comparison"]["legacy_entry_p50_sec"] >= 40.0
    assert 10.0 <= report["comparison"]["fast_entry_p50_sec"] <= 15.0
    assert report["comparison"]["fast_exit_p50_sec"] <= 6.0
    assert report["comparison"]["entry_speedup_ratio"] >= 3.0
    assert report["fast"]["global"]["quote_age_p50_sec"] <= 1.0


def test_fast_eval_replay_cli_writes_json_report(tmp_path: Path):
    replay_path = tmp_path / "quote_replay.jsonl"
    output_path = tmp_path / "report.json"
    holding_symbols = _write_quote_replay_fixture(replay_path)

    rc = main(
        [
            "--input",
            str(replay_path),
            "--holding-symbol",
            holding_symbols[0],
            "--holding-symbol",
            holding_symbols[1],
            "--output",
            str(output_path),
            "--pretty",
        ]
    )

    assert rc == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["comparison"]["legacy_entry_p50_sec"] >= 40.0
    assert 10.0 <= payload["comparison"]["fast_entry_p50_sec"] <= 15.0
    assert payload["comparison"]["fast_exit_p50_sec"] <= 6.0
