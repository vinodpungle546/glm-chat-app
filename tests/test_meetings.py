#!/usr/bin/env python3
"""App-side client of the local transcriber (HTTP mocked)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import meetings


def resp(status=200, data=None, text=""):
    r = MagicMock(status_code=status, text=text)
    r.json.return_value = data if data is not None else {}
    return r


def test_calls_local_transcriber_only():
    with patch.object(meetings.requests, "request", return_value=resp(data={"ok": True})) as req:
        meetings.health()
        meetings.start("a.m4a")
    urls = [c.args[1] for c in req.call_args_list]
    assert all(u.startswith(meetings.TRANSCRIBER_URL) for u in urls)
    assert req.call_args.kwargs["json"] == {"path": "a.m4a"}


def test_unreachable_gives_start_hint():
    with patch.object(meetings.requests, "request", side_effect=requests.ConnectionError()):
        with pytest.raises(meetings.TranscriberUnavailable, match="start_local_ai.sh"):
            meetings.recordings()


def test_http_error_detail_surfaced():
    with patch.object(meetings.requests, "request",
                      return_value=resp(507, {"detail": "Not enough free disk space"})):
        with pytest.raises(RuntimeError, match="free disk"):
            meetings.start("big.mp4")


def test_start_with_speaker_count_and_new_endpoints():
    with patch.object(meetings.requests, "request", return_value=resp(data={"ok": True})) as req:
        meetings.start("/m/a.m4a", 4)
        assert req.call_args.kwargs["json"] == {"path": "/m/a.m4a", "speakers": {"num": 4}}
        meetings.start("/m/a.m4a", None)
        assert req.call_args.kwargs["json"] == {"path": "/m/a.m4a"}  # auto-detect
        meetings.set_watch_folder("~/Docs/Meetings", create=True)
        assert req.call_args.args[:2] == ("POST", f"{meetings.TRANSCRIBER_URL}/settings")
        assert req.call_args.kwargs["json"] == {"meetings_dir": "~/Docs/Meetings", "create": True}
        meetings.import_text("Budget", "Priya: hi", "b.txt")
        assert req.call_args.args[1].endswith("/transcripts/import")


def test_delete_transcript_call():
    with patch.object(meetings.requests, "request", return_value=resp(data={"ok": True})) as req:
        meetings.delete_transcript("0123456789abcdef", recording=True)
    assert req.call_args.args == ("DELETE", f"{meetings.TRANSCRIBER_URL}/transcripts/0123456789abcdef")
    assert req.call_args.kwargs["params"] == {"recording": "true"}




def test_format_seconds():
    assert meetings.fmt_seconds(9600) == "2 h 40 min"
    assert meetings.fmt_seconds(75) == "1 min 15 s"


def test_guessed_speakers_are_saved_with_the_model_name():
    with patch.object(meetings.requests, "request", return_value=resp(data={"ok": True})) as req:
        meetings.save_guessed_speakers("0123456789abcdef", [{"speaker": "SPEAKER_00"}], {}, "qwen3:8b")
    assert req.call_args.args[0] == "PUT" and req.call_args.args[1].endswith("/segments")
    assert req.call_args.kwargs["json"]["speaker_source"] == "qwen3:8b"
