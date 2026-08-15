"""A Dockerfile parser, reduced to the instructions that matter for packaging.

Supported: FROM (with AS), COPY (with --from and --chown), RUN, ENV, ARG,
WORKDIR, USER, ENTRYPOINT, CMD, EXPOSE, LABEL, HEALTHCHECK, STOPSIGNAL.

Deliberately absent: ADD (a COPY that also does network fetches and tar
auto-extraction -- two behaviours nobody wants implicitly), ONBUILD, SHELL.

Multi-stage is not a syntax feature, it is the reason the format is worth
implementing: a stage whose output is consumed by `COPY --from` contributes
*nothing* to the final image, so build tools can be fat and runtime images thin.
The parser therefore keeps stages as first-class objects rather than flattening.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field


@dataclass
class Instruction:
    op: str
    args: str
    flags: dict[str, str] = field(default_factory=dict)
    line: int = 0

    def text(self) -> str:
        """Canonical single-line form -- this string is part of the cache key."""
        flags = "".join(f" --{k}={v}" for k, v in sorted(self.flags.items()))
        return f"{self.op}{flags} {self.args}".strip()

    def __str__(self) -> str:
        return self.text()


@dataclass
class Stage:
    base: str
    name: str | None = None
    index: int = 0
    instructions: list[Instruction] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or f"stage-{self.index}"


_FLAG = re.compile(r"^--([A-Za-z0-9_-]+)(?:=(.*))?$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")

_SUFFIX = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """Docker durations: `30s`, `1m30s`, `500ms`."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text)
    if not parts:
        # `0s` is a perfectly good duration, so emptiness -- not a zero total --
        # is what makes a string unparseable.
        raise ValueError(f"unparseable duration: {text!r}")
    return sum(float(value) * _SUFFIX[unit] for value, unit in parts)


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """Join backslash continuations, drop comments and blanks.

    Comments are stripped *before* joining, matching Docker: a `#` line inside a
    continuation ends the comment, not the instruction.
    """
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    for number, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            if not buffer:
                start = number
            buffer.append(stripped[:-1].strip())
            continue
        if buffer:
            buffer.append(stripped)
            out.append((start, " ".join(p for p in buffer if p)))
            buffer = []
        else:
            out.append((number, stripped))
    if buffer:
        out.append((start, " ".join(p for p in buffer if p)))
    return out


def _split_flags(args: str) -> tuple[dict[str, str], str]:
    flags: dict[str, str] = {}
    tokens = args.split()
    consumed = 0
    for token in tokens:
        match = _FLAG.match(token)
        if not match:
            break
        flags[match.group(1)] = match.group(2) if match.group(2) is not None else "true"
        consumed += 1
    return flags, " ".join(tokens[consumed:])


def parse(source: str) -> list[Stage]:
    stages: list[Stage] = []
    for number, line in _logical_lines(source):
        op, _, rest = line.partition(" ")
        op = op.upper()
        rest = rest.strip()
        flags, args = _split_flags(rest)

        if op == "FROM":
            parts = args.split()
            base = parts[0]
            name = None
            if len(parts) >= 3 and parts[1].upper() == "AS":
                name = parts[2]
            elif len(parts) != 1:
                raise ValueError(f"line {number}: malformed FROM: {args!r}")
            stages.append(Stage(base=base, name=name, index=len(stages)))
            continue

        if not stages:
            if op == "ARG":
                # ARG before the first FROM is legal and global; treated as a
                # build-arg default rather than an instruction.
                continue
            raise ValueError(f"line {number}: {op} before any FROM")

        if op not in _KNOWN:
            raise ValueError(f"line {number}: unsupported instruction {op!r}")
        stages[-1].instructions.append(Instruction(op=op, args=args, flags=flags, line=number))

    if not stages:
        raise ValueError("Dockerfile contains no FROM instruction")
    return stages


_KNOWN = {
    "COPY", "RUN", "ENV", "ARG", "WORKDIR", "USER", "ENTRYPOINT",
    "CMD", "EXPOSE", "LABEL", "HEALTHCHECK", "STOPSIGNAL",
}


def parse_exec_form(args: str) -> tuple[list[str], bool]:
    """Return (argv, was_json).

    ``ENTRYPOINT ["python", "serve.py"]`` execs directly; ``ENTRYPOINT python
    serve.py`` wraps in ``/bin/sh -c``, which is how a container ends up with a
    shell as PID 1 that swallows SIGTERM. Distinguishing the two is the whole
    reason to record ``was_json``.
    """
    text = args.strip()
    if text.startswith("["):
        import json

        return [str(v) for v in json.loads(text)], True
    return ["/bin/sh", "-c", text], False


def parse_key_values(args: str) -> dict[str, str]:
    """Handle both `ENV K=V K2=V2` and the legacy `ENV K rest of line`."""
    text = args.strip()
    if "=" not in text.split(" ", 1)[0]:
        key, _, value = text.partition(" ")
        return {key: value.strip()}
    out: dict[str, str] = {}
    for token in shlex.split(text):
        key, _, value = token.partition("=")
        out[key] = value
    return out
