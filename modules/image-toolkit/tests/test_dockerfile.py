"""Dockerfile parsing: continuations, flags, stages, and the two argv forms."""

from __future__ import annotations

import pytest

from imagekit import parse_dockerfile
from imagekit.dockerfile import parse_duration, parse_exec_form, parse_key_values


def test_single_stage_instructions_in_order():
    stages = parse_dockerfile("FROM scratch\nCOPY a b\nRUN true\n")
    assert len(stages) == 1
    assert stages[0].base == "scratch"
    assert [i.op for i in stages[0].instructions] == ["COPY", "RUN"]


def test_comments_and_blank_lines_are_dropped():
    stages = parse_dockerfile("# leading\n\nFROM scratch\n\n# mid\nCOPY a b\n")
    assert [i.op for i in stages[0].instructions] == ["COPY"]


def test_backslash_continuation_joins_into_one_instruction():
    stages = parse_dockerfile("FROM scratch\nRUN echo one && \\\n    echo two\n")
    instruction = stages[0].instructions[0]
    assert instruction.op == "RUN"
    assert instruction.args == "echo one && echo two"


def test_comment_inside_a_continuation_is_skipped():
    stages = parse_dockerfile("FROM scratch\nRUN echo one && \\\n# a note\n    echo two\n")
    assert stages[0].instructions[0].args == "echo one && echo two"


def test_named_stages_and_copy_from():
    stages = parse_dockerfile(
        "FROM base AS builder\nRUN make\nFROM scratch\nCOPY --from=builder /out /out\n"
    )
    assert [s.name for s in stages] == ["builder", None]
    assert stages[1].instructions[0].flags["from"] == "builder"


def test_flags_are_split_from_arguments():
    stages = parse_dockerfile("FROM scratch\nCOPY --chown=1000:1000 --from=b src dst\n")
    instruction = stages[0].instructions[0]
    assert instruction.flags == {"chown": "1000:1000", "from": "b"}
    assert instruction.args == "src dst"


def test_instruction_text_is_stable_regardless_of_flag_order():
    a = parse_dockerfile("FROM scratch\nCOPY --from=b --chown=1 s d\n")[0].instructions[0]
    b = parse_dockerfile("FROM scratch\nCOPY --chown=1 --from=b s d\n")[0].instructions[0]
    # The cache key is built from this string, so two spellings of the same
    # instruction must not produce two cache entries.
    assert a.text() == b.text()


def test_instruction_before_from_is_an_error():
    with pytest.raises(ValueError, match="before any FROM"):
        parse_dockerfile("RUN true\n")


def test_global_arg_before_from_is_allowed():
    assert parse_dockerfile("ARG VERSION=1\nFROM scratch\nRUN true\n")[0].base == "scratch"


def test_unsupported_instruction_is_rejected_not_ignored():
    with pytest.raises(ValueError, match="ADD"):
        parse_dockerfile("FROM scratch\nADD http://x/y /y\n")


def test_missing_from_is_an_error():
    with pytest.raises(ValueError, match="no FROM"):
        parse_dockerfile("# nothing here\n")


def test_exec_form_versus_shell_form():
    argv, was_json = parse_exec_form('["python3", "serve.py"]')
    assert (argv, was_json) == (["python3", "serve.py"], True)

    argv, was_json = parse_exec_form("python3 serve.py")
    assert (argv, was_json) == (["/bin/sh", "-c", "python3 serve.py"], False)


@pytest.mark.parametrize("text,expected", [("30s", 30.0), ("0s", 0.0), ("500ms", 0.5), ("1m30s", 90.0), ("2h", 7200.0)])
def test_durations(text, expected):
    assert parse_duration(text) == expected


def test_unparseable_duration_raises():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_key_value_forms():
    assert parse_key_values("A=1 B=2") == {"A": "1", "B": "2"}
    assert parse_key_values("A the rest of it") == {"A": "the rest of it"}
    assert parse_key_values('A="quoted value"') == {"A": "quoted value"}
