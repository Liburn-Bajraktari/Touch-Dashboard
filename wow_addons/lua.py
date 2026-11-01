"""
Reads a SavedVariables file: the Lua WoW writes at /reload, logout and exit.

Only what WoW writes is understood: top-level `Name = value` assignments whose
values are tables, strings, numbers, booleans and nil, with `-- comments`.
Nothing is evaluated. A table whose keys are 1..n becomes a list, any other a
dict (number keys stay numbers). Strings are UTF-8 bytes in the file; the core
reads files as latin-1, so each string is turned back into its bytes and
decoded.
"""
from __future__ import annotations

import math
from typing import Any

MAX_DEPTH = 64
MAX_NODES = 2_000_000
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
            "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
_NAME_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME = _NAME_START | set("0123456789")
_NUMBER = set("0123456789+-.eExXabcdefABCDEF")


class LuaError(ValueError):
    pass


class _Reader:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)
        self.nodes = 0

    def fail(self, what: str):
        line = self.s.count("\n", 0, self.i) + 1
        raise LuaError(f"{what} at line {line}")

    def skip(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n\f\v;":
                self.i += 1
            elif s.startswith("--", self.i):
                if s.startswith("--[[", self.i):
                    end = s.find("]]", self.i + 4)
                    self.i = n if end < 0 else end + 2
                else:
                    end = s.find("\n", self.i)
                    self.i = n if end < 0 else end + 1
            else:
                return

    def name(self) -> str:
        start = self.i
        if self.i >= self.n or self.s[self.i] not in _NAME_START:
            self.fail("expected a name")
        while self.i < self.n and self.s[self.i] in _NAME:
            self.i += 1
        return self.s[start:self.i]

    def value(self, depth: int) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            self.fail("too many values")
        self.skip()
        if self.i >= self.n:
            self.fail("expected a value")
        c = self.s[self.i]
        if c == "{":
            if depth >= MAX_DEPTH:
                self.fail("tables nested too deep")
            return self.table(depth + 1)
        if c in "\"'":
            return self.string()
        if c == "[" and self.s.startswith("[[", self.i):
            return self.long_string()
        if c.isdigit() or c in "-+.":
            return self.number()
        word = self.name()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "nil":
            return None
        if word in ("inf", "nan"):
            return math.inf if word == "inf" else math.nan
        self.fail(f"unexpected {word!r}")

    def number(self) -> float | int:
        start = self.i
        if self.s[self.i] in "-+":
            self.i += 1
            if self.s.startswith("inf", self.i):
                self.i += 3
                return -math.inf if self.s[start] == "-" else math.inf
            if self.s.startswith("nan", self.i):
                self.i += 3
                return math.nan
        while self.i < self.n and self.s[self.i] in _NUMBER:
            # a sign belongs to the number only right after an exponent
            if self.s[self.i] in "+-" and self.s[self.i - 1] not in "eE":
                break
            self.i += 1
        text = self.s[start:self.i]
        low = text.lower()
        try:
            if low.lstrip("+-").startswith("0x"):
                return int(text, 16)
            return float(text) if "." in low or "e" in low else int(text)
        except ValueError:
            self.fail(f"bad number {text!r}")

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        s, n = self.s, self.n
        while True:
            if self.i >= n:
                self.fail("unfinished string")
            c = s[self.i]
            if c == quote:
                self.i += 1
                break
            if c == "\n":
                self.fail("line break in a string")
            if c == "\\":
                self.i += 1
                if self.i >= n:
                    self.fail("unfinished string")
                e = s[self.i]
                if e in _ESCAPES:
                    out.append(_ESCAPES[e])
                    self.i += 1
                elif e.isdigit():
                    j = self.i
                    while j < n and j < self.i + 3 and s[j].isdigit():
                        j += 1
                    code = int(s[self.i:j])
                    if code > 255:
                        self.fail("bad escape")
                    out.append(chr(code))
                    self.i = j
                elif e == "x" and self.i + 2 < n:
                    try:
                        out.append(chr(int(s[self.i + 1:self.i + 3], 16)))
                    except ValueError:
                        self.fail("bad escape")
                    self.i += 3
                else:
                    self.fail("bad escape")
                continue
            out.append(c)
            self.i += 1
        return _utf8("".join(out))

    def long_string(self) -> str:
        end = self.s.find("]]", self.i + 2)
        if end < 0:
            self.fail("unfinished string")
        text = self.s[self.i + 2:end]
        self.i = end + 2
        return _utf8(text[1:] if text.startswith("\n") else text)

    def table(self, depth: int) -> list | dict:
        self.i += 1   # {
        out: dict = {}
        index = 1
        while True:
            self.skip()
            if self.i >= self.n:
                self.fail("unfinished table")
            c = self.s[self.i]
            if c == "}":
                self.i += 1
                break
            if c == "[" and not self.s.startswith("[[", self.i):
                self.i += 1
                key = self.value(depth)
                self.skip()
                if not self.s.startswith("]", self.i):
                    self.fail("expected ]")
                self.i += 1
                self.expect("=")
                val = self.value(depth)
                if isinstance(key, float) and key.is_integer():
                    key = int(key)
                if key is not None and not (isinstance(key, float) and math.isnan(key)):
                    if val is not None:
                        out[key] = val
            elif c in _NAME_START and self._is_field():
                key = self.name()
                self.expect("=")
                val = self.value(depth)
                if val is not None:
                    out[key] = val
            else:
                val = self.value(depth)
                if val is not None:
                    out[index] = val
                index += 1
            self.skip()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
        if out and all(isinstance(k, int) and not isinstance(k, bool) for k in out) \
                and sorted(out) == list(range(1, len(out) + 1)):
            return [out[k] for k in range(1, len(out) + 1)]
        return out

    def _is_field(self) -> bool:
        """`name = value` rather than a bare true/false/nil list item."""
        j = self.i
        while j < self.n and self.s[j] in _NAME:
            j += 1
        while j < self.n and self.s[j] in " \t":
            j += 1
        return j < self.n and self.s[j] == "=" and not self.s.startswith("==", j)

    def expect(self, ch: str) -> None:
        self.skip()
        if not self.s.startswith(ch, self.i):
            self.fail(f"expected {ch}")
        self.i += len(ch)


def _utf8(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8", errors="replace")
    except UnicodeEncodeError:   # already text (the caller did not read latin-1)
        return text


def parse_saved_variables(text: str) -> dict[str, Any]:
    """{variable name: value} for every top-level assignment. Raises LuaError."""
    r = _Reader(text)
    out: dict[str, Any] = {}
    while True:
        r.skip()
        if r.i >= r.n:
            return out
        name = r.name()
        r.expect("=")
        out[name] = r.value(0)


def table(value: Any) -> dict:
    """A parsed table as a dict whatever its keys (a list becomes {1: …, 2: …})."""
    if isinstance(value, list):
        return {i + 1: v for i, v in enumerate(value)}
    return value if isinstance(value, dict) else {}


def items(value: Any) -> list:
    """A parsed table's values in key order: a list as it is, a dict's int keys sorted."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[k] for k in sorted(k for k in value if isinstance(k, int))]
    return []
