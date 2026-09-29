from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable


class MoraError(Exception):
    pass


FORBIDDEN_APP_FORMS = [
    re.compile(r"\bfn\s+"),
    re.compile(r"\bdef\s+"),
    re.compile(r"\bclass\s+"),
    re.compile(r"\blambda\b"),
    re.compile(r"\bstate\."),
    re.compile(r"\.connect\s*\("),
]

TOP_LEVEL = {
    "app", "belief", "meaning", "pattern", "offer", "withdraw", "faculty",
    "law", "preference", "desire", "scene", "concept", "scenario", "when",
}


@dataclass
class Node:
    header: str
    source: pathlib.Path
    line: int
    children: list["Node"] = field(default_factory=list)
    statements: list[tuple[int, str]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.header.split(None, 1)[0] if self.header else ""

    @property
    def name(self) -> str:
        parts = self.header.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    def walk(self) -> Iterable["Node"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self) -> str:
        chunks = [self.header]
        chunks.extend(stmt for _, stmt in self.statements)
        for child in self.children:
            chunks.append(child.text())
        return "\n".join(chunks)

    def child(self, kind: str, name: str | None = None) -> "Node | None":
        for node in self.children:
            if node.kind != kind:
                continue
            if name is None or node.name == name:
                return node
        return None


@dataclass
class Program:
    nodes: list[Node]
    files: list[pathlib.Path]
    language: str | None = None

    def walk(self) -> Iterable[Node]:
        for node in self.nodes:
            yield from node.walk()

    def declarations(self, kind: str) -> list[Node]:
        return [n for n in self.walk() if n.kind == kind]

    def declaration(self, kind: str, name: str) -> Node | None:
        for n in self.declarations(kind):
            if n.name == name:
                return n
        return None


BRING_RE = re.compile(r'^bring\s+["\'](.+?)["\']\s*$')
LANG_RE = re.compile(r'^language\s+mora\s+([0-9]+(?:\.[0-9]+)*)\s*$')


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {'"', "'"}:
            out.append(ch)
            quote = ch
            continue
        if ch == '#':
            break
        out.append(ch)
    return ''.join(out).rstrip()


def _check_forbidden(path: pathlib.Path, lineno: int, text: str) -> None:
    if path.suffix.lower() != ".mora":
        return
    for rx in FORBIDDEN_APP_FORMS:
        if rx.search(text):
            raise MoraError(
                f"{path}:{lineno}: host-language-shaped construct is not Mora 0.3: {text.strip()}"
            )


def parse_file(path: pathlib.Path, seen: set[pathlib.Path] | None = None) -> Program:
    path = path.resolve()
    seen = set() if seen is None else set(seen)
    if path in seen:
        raise MoraError(f"recursive bring: {path}")
    if not path.exists():
        raise MoraError(f"file not found: {path}")
    seen.add(path)

    nodes: list[Node] = []
    files = [path]
    language = None
    stack: list[Node] = []

    lines = path.read_text(encoding="utf-8").splitlines()
    for lineno, raw in enumerate(lines, 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue

        bring = BRING_RE.match(line)
        if bring and not stack:
            child = parse_file(path.parent / bring.group(1), seen)
            nodes.extend(child.nodes)
            files.extend(child.files)
            if language is None and child.language is not None:
                language = child.language
            continue

        lang = LANG_RE.match(line)
        if lang and not stack:
            language = lang.group(1)
            continue

        _check_forbidden(path, lineno, line)

        if line == "}":
            if not stack:
                raise MoraError(f"{path}:{lineno}: unexpected }}")
            stack.pop()
            continue

        if line.endswith("{"):
            header = line[:-1].strip()
            if not header:
                raise MoraError(f"{path}:{lineno}: empty block header")
            node = Node(header=header, source=path, line=lineno)
            if stack:
                stack[-1].children.append(node)
            else:
                first = header.split(None, 1)[0]
                if first not in TOP_LEVEL:
                    raise MoraError(f"{path}:{lineno}: unknown top-level form '{first}'")
                nodes.append(node)
            stack.append(node)
            continue

        if "{" in line or "}" in line:
            raise MoraError(f"{path}:{lineno}: braces must delimit blocks on their own boundaries")

        if stack:
            stack[-1].statements.append((lineno, line))
        else:
            raise MoraError(f"{path}:{lineno}: statement outside a declaration: {line}")

    if stack:
        opened = stack[-1]
        raise MoraError(f"{opened.source}:{opened.line}: unclosed block '{opened.header}'")

    uniq = []
    known = set()
    for f in files:
        if f not in known:
            known.add(f)
            uniq.append(f)
    return Program(nodes=nodes, files=uniq, language=language)


INTENSITY = {
    "faintly": 0.025,
    "gently": 0.055,
    "moderately": 0.11,
    "strongly": 0.20,
}


@dataclass
class Belief:
    name: str
    values: dict[str, float]
    initial: dict[str, float]
    history: list[tuple[float, str, dict[str, float]]] = field(default_factory=list)

    def apply(self, event: str, deltas: dict[str, float]):
        for key, delta in deltas.items():
            self.values[key] = max(0.0, min(1.0, self.values.get(key, 0.0) + delta))
        self.history.append((time.monotonic(), event, dict(self.values)))


@dataclass
class MeaningRule:
    pattern: str
    effects: dict[str, float]


class AffectiveRuntime:
    """Evidence engine shared by CLI simulation and graphical backends."""

    def __init__(self, program: Program):
        self.program = program
        self.beliefs: dict[str, Belief] = {}
        self.meanings: list[MeaningRule] = []
        self.event_counts: dict[str, int] = {}
        self.event_history: list[tuple[float, str]] = []
        self.offers: set[str] = set()
        self.last_perception: str | None = None
        self.on_desire: Callable[[str], None] | None = None
        self.on_change: Callable[[], None] | None = None
        self._load_beliefs()
        self._load_meanings()
        self._flow_since: float | None = None

    def _load_beliefs(self):
        for node in self.program.declarations("belief"):
            m = re.match(r"belief\s+([A-Za-z_][\w]*)\s+about\s+([A-Za-z_][\w]*)$", node.header)
            if not m:
                raise MoraError(f"{node.source}:{node.line}: invalid belief header")
            name = m.group(1)
            values: dict[str, float] = {}
            for _, stmt in node.statements:
                parts = stmt.split()
                if len(parts) == 2:
                    try:
                        values[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
            self.beliefs[name] = Belief(name, dict(values), dict(values))

    def _load_meanings(self):
        for node in self.program.declarations("meaning"):
            pattern = node.header[len("meaning "):].strip()
            effects: dict[str, float] = {}
            for _, stmt in node.statements:
                m = re.match(
                    r"(suggests|weakens)\s+([A-Za-z_][\w]*)\s+(faintly|gently|moderately|strongly)$",
                    stmt,
                )
                if not m:
                    continue
                sign = 1.0 if m.group(1) == "suggests" else -1.0
                effects[m.group(2)] = effects.get(m.group(2), 0.0) + sign * INTENSITY[m.group(3)]
            self.meanings.append(MeaningRule(pattern, effects))

    @staticmethod
    def _canonical_event(event: str) -> str:
        e = re.sub(r"\([^)]*\)", "(frame)", event.strip())
        return re.sub(r"\s+", " ", e)

    def perceive(self, event: str, belief_name: str = "user"):
        self.last_perception = event
        now = time.monotonic()
        canon = self._canonical_event(event)
        self.event_counts[canon] = self.event_counts.get(canon, 0) + 1
        self.event_history.append((now, canon))

        belief = self.beliefs.get(belief_name)
        if belief is not None:
            deltas: dict[str, float] = {}
            for rule in self.meanings:
                pat = rule.pattern
                match = False
                if pat.startswith("undo soon after correction"):
                    match = canon.startswith("undo") and any(
                        e == "correction(frame)" and now - t <= 12
                        for t, e in self.event_history[:-1]
                    )
                elif pat.startswith("repeated "):
                    rest = pat[len("repeated "):]
                    base, _, within = rest.partition(" within ")
                    base = self._canonical_event(base.strip())
                    recent = [t for t, e in self.event_history if e == canon and t >= now - _duration_seconds(within)] if within else []
                    match = base == canon and len(recent) >= 3
                elif " and " in pat:
                    match = self._composite_meaning_holds(pat, canon, now)
                else:
                    match = self._canonical_event(pat) == canon
                if match:
                    for key, delta in rule.effects.items():
                        deltas[key] = deltas.get(key, 0.0) + delta
            belief.apply(event, deltas)

        self._evaluate_journeys(event)
        if self.on_change:
            self.on_change()
        return dict(belief.values) if belief is not None else {}

    def _composite_meaning_holds(self, pattern: str, canon: str, now: float) -> bool:
        if pattern.startswith("undo soon after correction") and canon.startswith("undo"):
            return any(e == "correction(frame)" and now - t <= 12 for t, e in reversed(self.event_history[:-1]))
        if pattern.startswith("detection succeeded") and canon.startswith("detection succeeded"):
            return True
        if pattern.startswith("export succeeded") and canon.startswith("export succeeded"):
            return True
        return pattern == canon

    def patterns(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        now = time.monotonic()
        for node in self.program.declarations("pattern"):
            name = node.header[len("pattern "):]
            normalized = name.replace("current ", "")
            holds = False
            if "struggles with frame" in normalized:
                recent_corrections = sum(
                    1 for t, e in self.event_history if e == "correction(frame)" and now - t <= 12
                )
                recent_undo = any(e.startswith("undo") and now - t <= 12 for t, e in self.event_history)
                holds = recent_corrections >= 3 or (recent_corrections >= 1 and recent_undo)
            elif normalized == "user flows" and "user" in self.beliefs:
                b = self.beliefs["user"].values
                c = b.get("confident", 0.0)
                dominates = c > b.get("uncertain", 0.0) and c > b.get("frustrated", 0.0)
                if dominates:
                    if self._flow_since is None:
                        self._flow_since = now
                    holds = now - self._flow_since >= 8.0
                else:
                    self._flow_since = None
                    holds = False
            out[name] = holds
        return out

    def _condition_holds(self, condition: str, event: str) -> bool:
        condition = condition.strip().replace("current ", "")
        event_norm = event.strip().replace("current ", "")
        if condition == event_norm:
            return True
        if condition == "scan arrives" and event_norm == "scan arrived":
            return True
        pats = self.patterns()
        for name, value in pats.items():
            if value and name.replace("current ", "") == condition:
                return True
        return False

    def _evaluate_journeys(self, event: str):
        for journey in self.program.declarations("journey"):
            for child in journey.children:
                if child.header.startswith("when "):
                    condition = child.header[len("when "):].strip()
                    if self._condition_holds(condition, event):
                        self._execute_journey_statements(child)

    def _execute_journey_statements(self, node: Node):
        for _, stmt in node.statements:
            if stmt.startswith("offer "):
                self.offers.add(stmt[len("offer "):].strip())
            elif stmt.startswith("withdraw "):
                self.offers.discard(stmt[len("withdraw "):].strip())
            elif stmt.startswith("desire ") and self.on_desire:
                self.on_desire(stmt[len("desire "):].strip())


def _duration_seconds(text: str) -> float:
    text = text.strip()
    m = re.match(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|m|h)?", text)
    if not m:
        return 12.0
    value = float(m.group(1))
    unit = m.group(2) or "s"
    return value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def inspect(program: Program) -> str:
    lines = [f"Mora {program.language or '?'} — {len(program.files)} source file(s)"]
    for node in program.nodes:
        lines.append(f"{node.kind:10} {node.header[len(node.kind):].strip()}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mora", description="Mora 0.3 affective language runtime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("check", "inspect"):
        p = sub.add_parser(cmd)
        p.add_argument("file")
    sim = sub.add_parser("simulate")
    sim.add_argument("file")
    sim.add_argument("events", nargs="+")
    run = sub.add_parser("run")
    run.add_argument("file")
    args = ap.parse_args(argv)

    try:
        program = parse_file(pathlib.Path(args.file))
        if program.language and program.language != "0.3":
            raise MoraError(f"this runtime implements Mora 0.3, source requests {program.language}")

        if args.cmd == "check":
            print(f"OK: {args.file} (Mora {program.language or '0.3'}, {len(program.files)} files)")
            return 0
        if args.cmd == "inspect":
            print(inspect(program))
            return 0
        if args.cmd == "simulate":
            rt = AffectiveRuntime(program)
            if "user" not in rt.beliefs:
                raise MoraError("program has no 'user' belief")
            print("start", rt.beliefs["user"].values)
            for ev in args.events:
                print(ev, "=>", rt.perceive(ev))
            print("patterns", rt.patterns())
            print("offers", sorted(rt.offers))
            return 0
        if args.cmd == "run":
            from .gtk_backend import GtkBackend
            GtkBackend(program).run()
            return 0
        return 0
    except MoraError as exc:
        print(f"Mora error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
