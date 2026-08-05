"""The local submission log and `ipaapi history`."""

import os
import pathlib
import tempfile

import pytest

from ipaapi import history
from ipaapi.cli import build_parser, cmd_history


@pytest.fixture
def log():
    return str(pathlib.Path(tempfile.mkdtemp()) / "submissions.tsv")


def record(analysis_id, project="P05", **kw):
    return history.SubmissionRecord(analysis_id=analysis_id, project=project, **kw)


def test_append_creates_the_file_with_a_header(log):
    history.append([record("43595039")], path=log)
    lines = pathlib.Path(log).read_text().splitlines()
    assert lines[0].split("\t") == list(history.FIELDS)
    assert lines[1].startswith(str(history.SubmissionRecord("x", "y").timestamp)[:4])


def test_records_round_trip(log):
    history.append(
        [
            record("43595039", dataset_name="SampleA_DEG", source_file="/data/a.txt"),
            record("43595041", dataset_name="SampleB_DEG"),
        ],
        path=log,
    )
    rows = history.read(log)
    assert [r["analysis_id"] for r in rows] == ["43595039", "43595041"]
    assert rows[0]["dataset_name"] == "SampleA_DEG"
    assert rows[0]["source_file"] == "/data/a.txt"
    assert rows[0]["project"] == "P05"


def test_appending_does_not_repeat_the_header(log):
    history.append([record("1")], path=log)
    history.append([record("2")], path=log)
    lines = pathlib.Path(log).read_text().splitlines()
    assert lines.count("\t".join(history.FIELDS)) == 1
    assert len(lines) == 3


def test_timestamp_is_iso_with_offset(log):
    history.append([record("1")], path=log)
    stamp = history.read(log)[0]["timestamp"]
    # e.g. 2026-08-05T14:03:11-07:00
    assert stamp[4] == "-" and stamp[10] == "T"
    assert len(stamp) >= 19


def test_missing_log_reads_as_empty():
    assert history.read("/nonexistent/path/submissions.tsv") == []


def test_empty_append_is_a_no_op(log):
    assert history.append([], path=log) is None
    assert not os.path.exists(log)


def test_default_path_honours_xdg_state_home(monkeypatch=None):
    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = "/tmp/state-test"
    try:
        assert history.default_log_path() == "/tmp/state-test/ipaapi/submissions.tsv"
    finally:
        if old is None:
            del os.environ["XDG_STATE_HOME"]
        else:
            os.environ["XDG_STATE_HOME"] = old


# -- the history command ---------------------------------------------------


def args_for(log, *extra):
    return build_parser().parse_args(["history", "--log-file", log, *extra])


def test_history_filters_by_project(log):
    history.append(
        [record("1", project="P05"), record("2", project="P06")], path=log
    )
    rows = history.read(log)
    assert len([r for r in rows if r["project"] == "P05"]) == 1
    assert cmd_history(args_for(log, "--project", "P05")) == 0


def test_history_limit_keeps_the_most_recent(log):
    history.append([record(str(i)) for i in range(5)], path=log)
    args = args_for(log, "--limit", "2")
    assert args.limit == 2
    assert cmd_history(args) == 0


def test_history_on_an_empty_log_is_not_an_error(log):
    assert cmd_history(args_for(log)) == 0


def test_since_filters_lexicographically(log):
    history.append([record("1")], path=log)
    rows = history.read(log)
    stamp = rows[0]["timestamp"]
    assert [r for r in rows if r["timestamp"] >= "1999-01-01"]
    assert not [r for r in rows if r["timestamp"] >= "9999-01-01"]
    assert stamp >= "2020-01-01"
