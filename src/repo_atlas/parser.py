"""Python symbol and reference extraction using the standard-library AST."""

import ast
import hashlib
import re

from repo_atlas.models import ParseResult, Reference, Symbol

def _hash(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8", "replace"), digest_size=12).hexdigest()


def _body_hash(statements: list[ast.stmt], exclude_definitions: bool = False) -> str:
    body = [
        statement for statement in statements
        if not (exclude_definitions and isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    ]
    return _hash(ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False))


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = ast.unparse(node.args)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        signature = f"{prefix} {node.name}({arguments})"
        if node.returns is not None:
            signature += f" -> {ast.unparse(node.returns)}"
        return signature
    bases = [ast.unparse(base) for base in node.bases]
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def parse_python(source: bytes) -> ParseResult:
    text = source.decode("utf-8", "replace")
    lines = text.splitlines()
    status = "ok"
    roots: list[ast.Module] = []
    error_ranges: list[tuple[int, int]] = []
    try:
        roots.append(ast.parse(text))
    except SyntaxError as error:
        status = "partial"
        error_line = error.lineno or 1
        declarations = [
            index for index, line in enumerate(lines, start=1)
            if re.match(r"^(?:async\s+def\b|def\b|class\b|@)", line)
        ]
        enclosing = max((line for line in declarations if line <= error_line), default=None)
        prefix_end = (enclosing - 1) if enclosing is not None else (error_line - 1)
        if prefix_end > 0:
            try:
                roots.append(ast.parse("\n".join(lines[:prefix_end])))
            except SyntaxError:
                pass
        if enclosing is not None:
            next_declaration = min((line for line in declarations if line > enclosing), default=len(lines) + 1)
            error_ranges.append((enclosing, next_declaration - 1))
            suffix_starts = [line for line in declarations if line >= next_declaration]
        else:
            error_ranges.append((error_line, error_line))
            suffix_starts = [line for line in declarations if line > error_line]
        for position, start_line in enumerate(suffix_starts):
            end_line = suffix_starts[position + 1] - 1 if position + 1 < len(suffix_starts) else len(lines)
            snippet = "\n".join(lines[start_line - 1:end_line])
            try:
                suffix_root = ast.parse(snippet)
            except SyntaxError:
                error_ranges.append((start_line, end_line))
                continue
            ast.increment_lineno(suffix_root, start_line - 1)
            roots.append(suffix_root)

    if not roots:
        return ParseResult((), (), status, tuple(error_ranges))
    module_body = [node for root in roots for node in root.body]
    symbols: list[Symbol] = [
        Symbol("<module>", "<module>", "module", "", "", 1,
               len(lines) or 1, None, True, _hash(""), _body_hash(module_body, True))
    ]
    references: list[Reference] = []

    class ReferenceVisitor(ast.NodeVisitor):
        def __init__(self, owner: str) -> None:
            self.owner = owner

        def visit_Call(self, node: ast.Call) -> None:
            references.append(Reference(self.owner, ast.unparse(node.func), "calls", node.lineno))
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            references.append(Reference(self.owner, ast.unparse(node), "imports", node.lineno))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            references.append(Reference(self.owner, ast.unparse(node), "imports", node.lineno))

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    def visit_scope(statements: list[ast.stmt], scope: tuple[str, ...], in_class: bool, owner: str) -> None:
        def visit_nested_definitions(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit_scope([child], scope, in_class, owner)
                else:
                    visit_nested_definitions(child)

        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = ".".join((*scope, node.name))
                is_class = isinstance(node, ast.ClassDef)
                signature = _signature(node)
                doc = ast.get_docstring(node) or ""
                symbols.append(Symbol(
                    qualname, node.name, "class" if is_class else ("method" if in_class else "function"),
                    signature, doc.splitlines()[0] if doc else "", node.lineno, node.end_lineno or node.lineno,
                    ".".join(scope) or None, not node.name.startswith("_"), _hash(signature),
                    _body_hash(node.body, is_class),
                ))
                if is_class:
                    for base in node.bases:
                        references.append(Reference(qualname, ast.unparse(base), "extends", base.lineno))
                visitor = ReferenceVisitor(qualname)
                for statement in node.body:
                    visitor.visit(statement)
                visit_scope(node.body, (*scope, node.name), is_class, qualname)
            else:
                ReferenceVisitor(owner).visit(node)
                visit_nested_definitions(node)

    visit_scope(module_body, (), False, "<module>")
    return ParseResult(tuple(symbols), tuple(references), status, tuple(error_ranges))


_STRUCTURAL_DECLARATION = re.compile(
    r"(?m)^[ \t]*(?P<prefix>(?:(?:export|default|abstract|public|protected|private|static|final|sealed|async|declare)\s+)*)"
    r"(?P<kind>class|interface|enum|record|function|type|const|let|var)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_JAVA_METHOD = re.compile(
    r"(?m)(?:^|[{};])[ \t]*(?P<prefix>(?:(?:public|protected|private|static|final|abstract|synchronized|native|default)\s+)*"
    r"[\w$<>?,.\[\]]+\s+)(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^;{}]*)\)\s*(?:throws [^{]+)?\{",
)
_TS_METHOD = re.compile(
    r"(?m)(?:^|[{};])[ \t]*(?P<prefix>(?:(?:public|protected|private|static|async|readonly|override)\s+)*)"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^;{}]*)\)\s*(?::[^\n{]+)?\{",
)
_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:\s*(?:\.\s*[A-Za-z_$][\w$]*))*)\s*\(")
_CONTROL_CALLS = {"if", "for", "while", "switch", "catch", "synchronized", "function", "new", "return"}


def _mask_noncode(text: str) -> str:
    output = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and following == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char in {"'", '"', "`"}:
                quote = char
                output[index] = " "
                state = "string"
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block_comment":
            if char == "*" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "code"
                continue
            if char != "\n":
                output[index] = " "
        else:
            if char == "\\":
                if char != "\n":
                    output[index] = " "
                if following and following != "\n":
                    output[index + 1] = " "
                index += 2
                continue
            if char == quote:
                state = "code"
            if char != "\n":
                output[index] = " "
        index += 1
    return "".join(output)


def _matching_brace(masked: str, start: int) -> int | None:
    opening = masked.find("{", start)
    if opening < 0:
        return None
    depth = 0
    for position in range(opening, len(masked)):
        if masked[position] == "{":
            depth += 1
        elif masked[position] == "}":
            depth -= 1
            if depth == 0:
                return position
    return None


def parse_structural(source: bytes, language: str) -> ParseResult:
    """Extract declarations and call references from TS/Java source without executing it."""
    text = source.decode("utf-8", "replace")
    masked = _mask_noncode(text)
    declarations: list[tuple[int, int, str, str, str, str, bool]] = []
    for match in _STRUCTURAL_DECLARATION.finditer(masked):
        kind = match.group("kind")
        if language == "java" and kind in {"function", "type", "const", "let", "var"}:
            continue
        if language == "typescript" and kind == "record":
            continue
        name = match.group("name")
        start = match.start()
        opening = masked.find("{", match.end())
        semicolon = masked.find(";", match.end())
        if kind in {"type", "const", "let", "var"} and language == "typescript":
            end = semicolon if semicolon >= 0 and (opening < 0 or semicolon < opening) else masked.find("\n", match.end())
            end = len(text) - 1 if end < 0 else end
        else:
            close = _matching_brace(masked, match.end())
            end = close if close is not None else len(text) - 1
        prefix = match.group("prefix")
        header_end = opening if opening >= 0 and opening < end else end
        signature = " ".join(text[start:header_end].strip().rstrip("{").split())
        symbol_kind = "class" if kind in {"class", "interface", "enum", "record"} else (
            "type" if kind == "type" else "const" if kind in {"const", "let", "var"} else "function"
        )
        public = "private" not in prefix.split() and not name.startswith("_")
        declarations.append((start, end, name, symbol_kind, signature, prefix, public))

    if language in {"java", "typescript"}:
        class_ranges = [(start, end) for start, end, _, kind, *_ in declarations if kind == "class"]
        method_pattern = _JAVA_METHOD if language == "java" else _TS_METHOD
        for match in method_pattern.finditer(masked):
            start = match.start()
            if not any(class_start <= start <= class_end for class_start, class_end in class_ranges):
                continue
            name = match.group("name")
            if name in _CONTROL_CALLS:
                continue
            close = _matching_brace(masked, match.end() - 1)
            end = close if close is not None else len(text) - 1
            brace = masked.rfind("{", start, match.end())
            signature = " ".join(text[start:brace].strip().split())
            prefix = match.group("prefix")
            declarations.append((start, end, name, "method", signature, prefix, "private" not in prefix.split()))

    declarations.sort(key=lambda item: (item[0], -item[1], item[2]))
    symbols: list[Symbol] = [
        Symbol("<module>", "<module>", "module", "", "", 1, len(text.splitlines()) or 1,
               None, True, _hash(""), _hash(masked))
    ]
    qualnames: dict[int, str] = {}
    for item in declarations:
        start, end, name, kind, signature, _, public = item
        parents = [parent for parent in declarations if parent[0] < start < parent[1] and parent[3] in {"class", "function", "method"}]
        parent = min(parents, key=lambda candidate: candidate[1] - candidate[0]) if parents else None
        parent_name = qualnames.get(parent[0]) if parent else None
        qualname = f"{parent_name}.{name}" if parent_name else name
        qualnames[start] = qualname
        start_line = text.count("\n", 0, start) + 1
        end_line = text.count("\n", 0, end) + 1
        body_start = masked.find("{", start, end + 1)
        body = text[body_start:end + 1] if body_start >= 0 else text[start:end + 1]
        doc = ""
        if language == "typescript" and kind in {"function", "class"}:
            preceding = text[:start].rstrip().splitlines()
            if preceding and preceding[-1].strip().startswith("*"):
                doc = preceding[-1].strip(" */")
        symbols.append(Symbol(qualname, name, kind, signature, doc, start_line, end_line,
                              parent_name, public, _hash(signature), _hash(_mask_noncode(body))))

    references: list[Reference] = []
    for line_match in re.finditer(r"(?m)^\s*(?:import|export)\b[^\n]*", text):
        owner = _owner_for_offset(declarations, qualnames, line_match.start())
        references.append(Reference(owner, line_match.group(0).strip(), "imports", text.count("\n", 0, line_match.start()) + 1))
    declared_spans = [(start, masked.find("(", start, end + 1)) for start, end, _, kind, *_ in declarations if kind in {"function", "method"}]
    for match in _CALL.finditer(masked):
        name = match.group(1).replace(" ", "")
        if name.rsplit(".", 1)[-1] in _CONTROL_CALLS or any(start <= match.start() <= opening for start, opening in declared_spans if opening >= 0):
            continue
        owner = _owner_for_offset(declarations, qualnames, match.start())
        references.append(Reference(owner, name, "calls", text.count("\n", 0, match.start()) + 1))
    return ParseResult(tuple(symbols), tuple(references), "ok")


def _owner_for_offset(declarations: list[tuple[int, int, str, str, str, str, bool]],
                      qualnames: dict[int, str], offset: int) -> str:
    owners = [item for item in declarations if item[0] <= offset <= item[1] and item[3] in {"class", "function", "method"}]
    if not owners:
        return "<module>"
    owner = min(owners, key=lambda item: item[1] - item[0])
    return qualnames.get(owner[0], "<module>")


def parse_source(source: bytes, language: str) -> ParseResult:
    if language == "python":
        return parse_python(source)
    if language in {"typescript", "java"}:
        return parse_structural(source, language)
    raise ValueError(f"unsupported language: {language}")