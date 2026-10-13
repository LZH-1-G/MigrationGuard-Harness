"""Pure helpers of the in-sandbox probe runner and pytest recorder (importable on the host)."""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import re
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[2] / "src" / "migrationguard" / "sandbox" / "runtime"
sys.path.insert(0, str(RUNTIME))
import mg_probe_runner as runner  # noqa: E402

sys.path.remove(str(RUNTIME))


@pytest.mark.parametrize("pattern", [r"^[A-Z]{3}-\d{4}$", r"^[a-z0-9-]+$", r"(foo|bar)\w+@x\.com", r"[^abc]+", r"^\S{2,}$"])
def test_sample_regex_matches(pattern):
    value = runner.sample_regex(pattern)
    assert value is not None and re.search(pattern, value)


def test_jsonable_is_stable():
    class Color(enum.Enum):
        RED = "r"

    value = {"b": {1, 2}, "a": (decimal.Decimal("1.10"), dt.date(2024, 1, 2), Color.RED), "__x": float("nan")}
    out = runner.jsonable(value)
    assert list(out) == ["__x", "a", "b"]
    assert out["a"] == {"__tuple__": [{"__decimal__": "1.10"}, {"__date__": "2024-01-02"}, {"__enum__": "Color", "value": "r"}]}
    assert out["b"] == {"__set__": [1, 2]}
    assert runner.jsonable({"__root__": [1]}) == [1]


def test_generate_inputs_for_v2_model():
    from pydantic import BaseModel, Field

    class Item(BaseModel):
        sku: str = Field(pattern=r"^[A-Z]{2}\d$")
        qty: int = Field(1, ge=1)
        price: float
        note: str | None = None
        tags: list[str] = []
        code: str = Field("x", alias="Code")

    cases = {c["kind"]: c["payload"] for c in runner.generate_inputs(Item, 30)}
    assert runner.run_model_probe(Item, cases["full"])["status"] == "ok"
    assert cases["coerce-int:qty"]["qty"] == "42"
    assert cases["truncate-int:qty"]["qty"] == 42.7
    assert cases["none:note"]["note"] is None
    assert "code" in cases["by-name:code"] and "Code" not in cases["by-name:code"]
    assert "sku" not in cases["missing:sku"]
    assert runner.run_model_probe(Item, cases["missing:sku"]) == {"status": "error", "error_type": "ValidationError",
                                                                   "error_locs": ["sku"]}


def test_optional_detection_supports_both_union_spellings():
    from typing import Optional, Union

    from pydantic import BaseModel

    class M(BaseModel):
        a: Optional[int] = None  # typing.Union origin
        b: Union[str, None] = None  # typing.Union origin
        c: str | None = None  # types.UnionType origin (PEP 604)

    kinds = {c["kind"] for c in runner.generate_inputs(M, 50)}
    assert {"none:a", "none:b", "none:c", "coerce-int:a", "coerce-str:b", "coerce-str:c"} <= kinds
