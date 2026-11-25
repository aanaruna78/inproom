"""The symbols@1 producer: a source file's definitions and imports, with exact lines (INPROOM-174 / -29).

Output (canonical JSON):
  {"schema": 1, "language", "method": "ast" | "scan", "definitions": [{kind, name, container, signature,
   line_start, line_end}], "imports": [{module, names, line}], "warnings": [...], "stats": {...}}

- Python: the standard `ast` module (exact). A syntax error is CODE_SYNTAX_ERROR with its line.
- JavaScript/TypeScript, Go, Java: declaration scanners over a masked copy of the source (comments and string
  contents blanked, line structure and braces kept), with brace depth giving containers and end lines. They
  are labelled method "scan": precise for ordinary code, not a full parser (a later symbols@2 may be).
Bounded: at most MAX_DEFINITIONS definitions; lines longer than MAX_LINE are not scanned (minified code) and
are reported as a warning.
"""

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from app.content.parsing.model import ParseError

MAX_DEFINITIONS = 20_000
MAX_LINE = 5_000
MEDIA_LANGUAGE = {
    "text/x-python": "python",
    "text/javascript": "javascript",
    "text/x-typescript": "typescript",
    "text/x-go": "go",
    "text/x-java": "java",
}


@dataclass
class Definition:
    kind: str
    name: str
    container: str | None
    signature: str
    line_start: int
    line_end: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "container": self.container,
            "signature": self.signature[:200],
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


def _limit(defs: list) -> None:
    if len(defs) > MAX_DEFINITIONS:
        raise ParseError("PARSE_LIMIT_EXCEEDED", f"more than {MAX_DEFINITIONS} definitions")


# ---------------------------------------------------------------- Python (exact)


def _python(source: str) -> tuple[list[Definition], list[dict], list[dict]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ParseError("CODE_SYNTAX_ERROR", f"line {exc.lineno}: {exc.msg}") from exc
    except (RecursionError, MemoryError) as exc:
        raise ParseError("PARSE_LIMIT_EXCEEDED", "the file nests too deeply") from exc
    defs: list[Definition] = []
    imports: list[dict] = []

    def walk(nodes, container: str | None, in_class: bool, top: bool) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                sig = f"{prefix} {node.name}({ast.unparse(node.args)})"
                if node.returns is not None:
                    sig += f" -> {ast.unparse(node.returns)}"
                defs.append(
                    Definition(
                        "method" if in_class else "function",
                        node.name,
                        container,
                        sig,
                        node.lineno,
                        node.end_lineno or node.lineno,
                    )
                )
                walk(node.body, node.name if container is None else f"{container}.{node.name}", False, False)
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
                defs.append(
                    Definition(
                        "class", node.name, container, sig, node.lineno, node.end_lineno or node.lineno
                    )
                )
                walk(node.body, node.name if container is None else f"{container}.{node.name}", True, False)
            elif top and isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        defs.append(
                            Definition(
                                "constant",
                                t.id,
                                None,
                                ast.unparse(node).split("\n")[0],
                                node.lineno,
                                node.end_lineno or node.lineno,
                            )
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({"module": alias.name, "names": [], "line": node.lineno})
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                imports.append({"module": module, "names": [a.name for a in node.names], "line": node.lineno})
            elif isinstance(node, ast.If | ast.Try | ast.With | ast.For | ast.While):
                # definitions guarded by "if TYPE_CHECKING:", try/except imports, etc.
                for field in ("body", "orelse", "finalbody", "handlers"):
                    walk(getattr(node, field, []) or [], container, in_class, top)
            elif isinstance(node, ast.ExceptHandler):
                walk(node.body, container, in_class, top)
            _limit(defs)

    walk(tree.body, None, False, True)
    return defs, imports, []


# ---------------------------------------------------------------- C-like languages (scan)


def mask(source: str, raw_backtick: bool) -> str:
    """Comments and string contents replaced by spaces; newlines, quotes and braces outside strings kept."""
    out = []
    i, n = 0, len(source)
    state = None  # None | "line" | "block" | quote char
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"
                out.append("  ")
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block"
                out.append("  ")
                i += 2
                continue
            if c in "'\"`":
                state = c
            out.append(c)
        elif state == "line":
            if c == "\n":
                state = None
                out.append("\n")
            else:
                out.append(" ")
        elif state == "block":
            if c == "*" and nxt == "/":
                state = None
                out.append("  ")
                i += 2
                continue
            out.append("\n" if c == "\n" else " ")
        else:  # inside a string
            if c == "\\" and not (state == "`" and raw_backtick):
                out.append("  " if nxt != "\n" else " \n")
                i += 2
                continue
            if c == state:
                state = None
                out.append(c)
            elif c == "\n":
                out.append("\n")
                if state in "'\"":
                    state = None  # unterminated single-line string: recover at the line end
            else:
                out.append(" ")
        i += 1
    return "".join(out)


KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "function",
    "new",
    "else",
    "do",
    "try",
    "throw",
    "typeof",
    "await",
    "yield",
    "delete",
    "case",
    "super",
    "this",
    "synchronized",
    "assert",
}
JS_PATTERNS = [
    (
        "function",
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    ),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*=")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
            r"(?:function\b|(?:<[^>]*>\s*)?\([^)]*\)\s*(?::\s*[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"
        ),
    ),
    ("constant", re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=")),
]
JS_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*"
    r"\*?\s*(#?[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*$"
)
JS_IMPORT = re.compile(r"""^\s*import\s+(?:type\s+)?(?:(.+?)\s+from\s+)?['"]([^'"]+)['"]""")
JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
GO_FUNC = re.compile(r"^func\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\(")
GO_METHOD = re.compile(r"^func\s+\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)\s*\(")
GO_TYPE = re.compile(r"^(?:type\s+)?([A-Za-z_]\w*)\s+(?:\[[^\]]*\]\s*)?(struct|interface)?\b")
GO_TYPE_LINE = re.compile(r"^type\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(=?\s*)(struct|interface)?")
GO_CONST = re.compile(r"^const\s+([A-Za-z_]\w*)")
GO_IMPORT = re.compile(r"""^\s*(?:import\s+)?(?:([A-Za-z_.]\w*)\s+)?"([^"]+)"\s*$""")
JAVA_MODIFIERS = (
    "public|protected|private|abstract|static|final|sealed|non-sealed|strictfp|default|synchronized|native"
    "|transient|volatile"
)
JAVA_MODS = rf"(?:(?:{JAVA_MODIFIERS})\s+)*"
JAVA_TYPE = re.compile(
    rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*{JAVA_MODS}(class|interface|enum|record|@interface)\s+([A-Za-z_]\w*)"
)
JAVA_METHOD = re.compile(
    rf"^\s*{JAVA_MODS}(?:<[^>]+>\s+)?([\w\[\]<>?,.]+(?:\s*<[^>]*>)?)\s+([A-Za-z_]\w*)\s*\("
)
JAVA_CTOR = re.compile(rf"^\s*{JAVA_MODS}([A-Za-z_]\w*)\s*\(")
JAVA_CONST = re.compile(rf"^\s*{JAVA_MODS}[\w<>\[\],.]+\s+([A-Z][A-Z0-9_]*)\s*=")
JAVA_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")


def _scan(source: str, language: str) -> tuple[list[Definition], list[dict], list[dict]]:
    original = source.splitlines()
    masked = mask(source, raw_backtick=language == "go").splitlines()
    defs: list[Definition] = []
    imports: list[dict] = []
    warnings: list[dict] = []
    open_blocks: list[tuple[int, Definition | None, bool]] = []  # (depth before it, definition, class-like)
    pending: tuple[Definition, bool, int] | None = None  # a definition whose "{" has not been seen yet
    depth = 0
    group: str | None = None  # Go "import (", "type (", "const (" blocks
    long_lines = []

    def container() -> str | None:
        names = [d.name for _, d, cls in open_blocks if d is not None and cls]
        return ".".join(names) if names else None

    def in_class_body() -> bool:
        return bool(open_blocks) and open_blocks[-1][2] and open_blocks[-1][0] + 1 == depth

    for number, (line, raw) in enumerate(zip(masked, original, strict=False), start=1):
        if len(line) > MAX_LINE:
            long_lines.append(number)
            depth += line.count("{") - line.count("}")
            continue
        found: Definition | None = None
        class_like = False
        stripped = line.strip()
        if language in ("javascript", "typescript"):
            m = JS_IMPORT.match(raw) if stripped.startswith("import") else None
            if m:
                names = []
                for part in re.split(r"[{},]", m.group(1) or ""):
                    part = re.sub(r"^\s*type\s+", "", part).strip()  # "type Request" -> "Request"
                    if part:
                        names.append(part.split(" as ")[-1].strip())  # "* as path" -> "path"
                imports.append({"module": m.group(2), "names": names, "line": number})
            elif "require(" in line:
                for r in JS_REQUIRE.finditer(raw):
                    imports.append({"module": r.group(1), "names": [], "line": number})
            if in_class_body():
                m = JS_METHOD.match(line)
                if m and m.group(1) not in KEYWORDS:
                    found = Definition(
                        "method", m.group(1), container(), raw.strip().rstrip("{").strip(), number, number
                    )
            if found is None:
                for kind, pattern in JS_PATTERNS:
                    m = pattern.match(line)
                    if m and (kind != "constant" or depth == 0):
                        found = Definition(
                            kind, m.group(1), container(), raw.strip().rstrip("{").strip(), number, number
                        )
                        class_like = kind in ("class", "interface")
                        break
        elif language == "go":
            if group == "import" and stripped != ")":
                m = GO_IMPORT.match(raw)
                if m:
                    imports.append(
                        {"module": m.group(2), "names": [m.group(1)] if m.group(1) else [], "line": number}
                    )
            elif group == "type" and depth == 0 and stripped and stripped != ")":
                m = GO_TYPE.match(stripped)
                if m:
                    found = Definition(m.group(2) or "type", m.group(1), None, raw.strip(), number, number)
            elif group == "const" and depth == 0 and stripped and stripped != ")":
                m = re.match(r"^([A-Za-z_]\w*)", stripped)
                if m:
                    found = Definition("constant", m.group(1), None, raw.strip(), number, number)
            if stripped == ")" and group:
                group = None
            elif depth == 0 and re.match(r"^(import|type|const|var)\s*\($", stripped):
                group = stripped.split()[0].rstrip("(")
            elif depth == 0 and stripped.startswith("import "):
                m = GO_IMPORT.match(raw)
                if m:
                    imports.append(
                        {"module": m.group(2), "names": [m.group(1)] if m.group(1) else [], "line": number}
                    )
            elif depth == 0 and group is None:
                m = GO_METHOD.match(line) or GO_FUNC.match(line)
                if m and line.startswith("func ("):
                    found = Definition(
                        "method", m.group(2), m.group(1), raw.strip().rstrip("{").strip(), number, number
                    )
                elif m:
                    found = Definition(
                        "function", m.group(1), None, raw.strip().rstrip("{").strip(), number, number
                    )
                else:
                    m = GO_TYPE_LINE.match(line)
                    if m:
                        kind = m.group(3) or ("type" if not m.group(2).strip() else "type")
                        found = Definition(
                            kind, m.group(1), None, raw.strip().rstrip("{").strip(), number, number
                        )
                    else:
                        m = GO_CONST.match(line)
                        if m:
                            found = Definition("constant", m.group(1), None, raw.strip(), number, number)
        elif language == "java":
            m = JAVA_IMPORT.match(line)
            if m:
                imports.append(
                    {"module": m.group(2), "names": ["static"] if m.group(1) else [], "line": number}
                )
            m = JAVA_TYPE.match(line)
            if m:
                found = Definition(
                    {"@interface": "annotation"}.get(m.group(1), m.group(1)),
                    m.group(2),
                    container(),
                    raw.strip().rstrip("{").strip(),
                    number,
                    number,
                )
                class_like = True
            elif in_class_body():
                owner = container()
                ctor = JAVA_CTOR.match(line)
                m = JAVA_METHOD.match(line)
                if ctor and owner and ctor.group(1) == owner.split(".")[-1]:
                    found = Definition(
                        "constructor", ctor.group(1), owner, raw.strip().rstrip("{").strip(), number, number
                    )
                elif (
                    m
                    and m.group(2) not in KEYWORDS
                    and m.group(1) not in KEYWORDS | {"new", "else", "return"}
                ):
                    found = Definition(
                        "method", m.group(2), owner, raw.strip().rstrip("{").strip(), number, number
                    )
                else:
                    m = JAVA_CONST.match(line)
                    if m and " static " in f" {line} " and " final " in f" {line} ":
                        found = Definition(
                            "constant", m.group(1), owner, raw.strip().rstrip(";"), number, number
                        )
        if found is not None:
            defs.append(found)
            _limit(defs)
            # only definitions that have a body wait for their "{" (a Go const or type alias has none)
            bodied = language != "go" or found.kind in ("function", "method", "struct", "interface")
            pending = (found, class_like, depth) if bodied else None
        # braces of this line: open blocks for a pending definition, close finished ones
        for ch in line:
            if ch == "{":
                if pending is not None:
                    open_blocks.append((pending[2], pending[0], pending[1]))
                    pending = None
                else:
                    open_blocks.append((depth, None, False))
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
                while open_blocks and open_blocks[-1][0] >= depth:
                    _, d, _ = open_blocks.pop()
                    if d is not None:
                        d.line_end = number
            elif (
                ch == ";"
                and pending is not None
                and pending[0].kind not in ("class", "interface", "enum", "record")
            ):
                pending = None  # e.g. an abstract method or a one-line alias: no body
    for _, d, _ in open_blocks:  # unbalanced input: close at the end
        if d is not None:
            d.line_end = len(masked)
    if long_lines:
        warnings.append(
            {"code": "LONG_LINES_NOT_SCANNED", "lines": long_lines[:20], "count": len(long_lines)}
        )
    return defs, imports, warnings


def symbols_document(data: bytes, media_type: str) -> dict[str, Any]:
    language = MEDIA_LANGUAGE.get(media_type.split(";", 1)[0].strip())
    if language is None:
        raise ParseError("UNSUPPORTED_FORMAT", f"no symbol extractor for {media_type}")
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("PARSE_MALFORMED", "not UTF-8 text") from exc
    if language == "python":
        defs, imports, warnings = _python(source)
        method = "ast"
    else:
        defs, imports, warnings = _scan(source, language)
        method = "scan"
    definitions = [d.as_dict() for d in sorted(defs, key=lambda d: (d.line_start, d.name))]
    return {
        "schema": 1,
        "language": language,
        "method": method,
        "definitions": definitions,
        "imports": imports,
        "warnings": warnings,
        "stats": {"definitions": len(definitions), "imports": len(imports), "lines": source.count("\n") + 1},
    }


def build(data: bytes, media_type: str) -> bytes:
    try:
        doc = symbols_document(data, media_type)
    except ParseError:
        raise
    except RecursionError as exc:
        raise ParseError("PARSE_LIMIT_EXCEEDED", "the file nests too deeply") from exc
    except Exception as exc:  # noqa: BLE001 - untrusted input never crashes the derive job
        raise ParseError("PARSE_MALFORMED", f"unreadable source ({type(exc).__name__})") from exc
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
