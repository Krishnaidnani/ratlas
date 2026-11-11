from repo_atlas.parser import parse_python


def test_extracts_nested_symbols_and_references() -> None:
    result = parse_python(
        b'''class Store:\n    """Persistence layer."""\n    def save(self, item):\n        validate(item)\n\ndef validate(value):\n    return value\n'''
    )

    symbols = {symbol.qualname: symbol for symbol in result.symbols}
    assert result.status == "ok"
    assert symbols["Store"].kind == "class"
    assert symbols["Store"].doc == "Persistence layer."
    assert symbols["Store.save"].kind == "method"
    assert any(ref.text == "validate" and ref.kind == "calls" for ref in result.references)


def test_syntax_error_is_partial() -> None:
    result = parse_python(b"def valid():\n    return 1\n\ndef broken(:\n")
    assert result.status == "partial"
    assert any(symbol.qualname == "valid" for symbol in result.symbols)


def test_syntax_error_recovers_valid_declarations_after_it() -> None:
    result = parse_python(
        b"def before():\n    return 1\n\ndef broken(:\n    pass\n\ndef after(value: int) -> int:\n    return value\n"
    )
    symbols = {symbol.qualname: symbol for symbol in result.symbols}
    assert result.status == "partial"
    assert {"before", "after"}.issubset(symbols)
    assert "value: int" in symbols["after"].signature
    assert result.error_ranges == ((4, 6),)


def test_extracts_definitions_nested_in_control_flow() -> None:
    result = parse_python(
        b"if TYPE_CHECKING:\n    def imported_only():\n        return True\n"
        b"def outer():\n    if True:\n        def inner():\n            return False\n"
    )
    names = {symbol.qualname for symbol in result.symbols}
    assert "imported_only" in names
    assert "outer.inner" in names