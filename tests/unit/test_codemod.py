"""Unit tests for the deterministic Pydantic v1 → v2 codemod."""

from __future__ import annotations

from migrationguard.analysis.project import ProjectIndex
from migrationguard.migrate.pydantic_codemod import migrate_source
from tests.conftest import migrate


def rules(report) -> set[str]:
    return {c.rule for c in report.changes}


def unresolved(report) -> set[str]:
    return {f.rule for f in report.unresolved}


def test_config_class_becomes_config_dict(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from pydantic import BaseModel, Extra

        class A(BaseModel):
            x: int

            class Config:
                orm_mode = True
                allow_population_by_field_name = True
                extra = Extra.forbid
                allow_mutation = False
                anystr_strip_whitespace = True
                schema_extra = {"example": {"x": 1}}
    '''}, "m.py")
    assert "class Config" not in new
    assert ("model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra='forbid', frozen=True, "
            "str_strip_whitespace=True, json_schema_extra={\"example\": {\"x\": 1}})") in new
    assert "Extra" not in new.split("\n")[0]
    assert "PYD130" in rules(report)


def test_unknown_config_option_is_left_for_agent(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from pydantic import BaseModel

        class A(BaseModel):
            x: int

            class Config:
                orm_mode = True
                fields = {"x": "X"}
    '''}, "m.py")
    assert "class Config" in new  # never half-converted
    assert "PYD203" in unresolved(report)


def test_validators(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from typing import Optional
        from pydantic import BaseModel, validator

        class A(BaseModel):
            name: str
            nick: Optional[str]
            score: int = 0

            @validator("name", pre=True)
            def strip(cls, v):
                return v.strip()

            @validator("nick", always=True)
            def default_nick(cls, v, values):
                return v or values.get("name")

            @validator("score")
            def positive(cls, v, field):
                if v < 0:
                    raise ValueError(f"{field.name} must be positive")
                return v
    '''}, "m.py")
    assert '@field_validator("name", mode="before")\n    @classmethod' in new
    assert "def default_nick(cls, v, info: ValidationInfo):" in new
    assert "info.data.get(\"name\")" in new
    assert "nick: Optional[str] = Field(None, validate_default=True)" in new
    assert "info.field_name" in new
    assert "@validator" not in new
    assert {"PYD110", "PYD111", "PYD112", "PYD160"} <= rules(report)
    assert not report.unresolved


def test_values_attribute_and_keyword_are_not_renamed(tmp_path):
    new, _ = migrate(tmp_path, {"m.py": '''
        from pydantic import BaseModel, validator

        class A(BaseModel):
            a: int
            b: int

            @validator("b")
            def check(cls, v, values):
                other = dict(values=1)
                return v + values["a"] + len(other.values())
    '''}, "m.py")
    assert "dict(values=1)" in new
    assert "other.values()" in new
    assert 'info.data["a"]' in new


def test_each_item_validator_is_unresolved(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from typing import List
        from pydantic import BaseModel, validator

        class A(BaseModel):
            tags: List[str]

            @validator("tags", each_item=True)
            def lower(cls, v):
                return v.lower()
    '''}, "m.py")
    assert "@validator" in new
    assert "PYD201" in unresolved(report)


def test_root_validators(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from pydantic import BaseModel, root_validator

        class A(BaseModel):
            a: int
            b: int

            @root_validator(pre=True)
            def legacy(cls, values):
                values.setdefault("b", 0)
                return values

            @root_validator(skip_on_failure=True)
            def check(cls, values):
                if values.get("a") > values["b"]:
                    raise ValueError("a > b")
                return values

            @root_validator
            def mutate(cls, values):
                values["b"] = values["a"] * 2
                return values
    '''}, "m.py")
    assert '@model_validator(mode="before")\n    @classmethod\n    def legacy' in new
    assert '@model_validator(mode="after")\n    def check(self):' in new
    assert "if self.a > self.b:" in new and "return self" in new
    assert "@root_validator\n    def mutate" in new  # mutating post validator left for the agent
    assert "PYD202" in unresolved(report)


def test_methods_rewritten_only_on_known_models(tmp_path):
    new, report = migrate(tmp_path, {
        "app/models.py": '''
            from typing import List
            from pydantic import BaseModel

            class Item(BaseModel):
                id: int

            class Order(BaseModel):
                items: List[Item]
                main: Item
        ''',
        "app/use.py": '''
            from app.models import Item, Order

            def build(raw) -> Order:
                return Order.parse_obj(raw)

            def work(raw: dict, row, cfg):
                order = build(raw)
                data = order.dict(exclude_none=True)
                first = order.main.json()
                names = [i.dict() for i in order.items]
                clone = order.copy(update={"items": []})
                schema = Order.schema()
                parsed = Item.parse_raw("{}")
                orm = Item.from_orm(row)
                other = cfg.dict()
                return data, first, names, clone, schema, parsed, orm, other, Order.__fields__, order.__fields_set__
        ''',
    }, "app/use.py")
    assert "Order.model_validate(raw)" in new
    assert "order.model_dump(exclude_none=True)" in new
    assert "order.main.model_dump_json()" in new
    assert "[i.model_dump() for i in order.items]" in new
    assert "order.model_copy(update=" in new
    assert "Order.model_json_schema()" in new
    assert 'Item.model_validate_json("{}")' in new
    assert "Item.model_validate(row, from_attributes=True)" in new
    assert "cfg.dict()" in new  # unknown receiver: untouched, reported as info only
    assert "Order.model_fields" in new and "order.model_fields_set" in new
    assert {f.rule for f in report.infos} == {"PYD299"}


def test_v1_only_arguments_are_not_dropped(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from pydantic import BaseModel

        class A(BaseModel):
            x: int

        def f(a: A):
            return a.dict(skip_defaults=True)
    '''}, "m.py")
    assert "a.dict(skip_defaults=True)" in new
    assert "PYD205" in unresolved(report)


def test_imports_settings_generics_and_helpers(tmp_path):
    new, report = migrate(tmp_path, {"m.py": '''
        from typing import Generic, List, TypeVar
        from pydantic import BaseSettings, Field, parse_obj_as
        from pydantic.generics import GenericModel

        T = TypeVar("T")

        class Settings(BaseSettings):
            url: str = Field("x", env="APP_URL")

            class Config:
                env_prefix = "APP_"

        class Page(GenericModel, Generic[T]):
            items: List[T]

        def load(rows):
            return parse_obj_as(List[int], rows)
    '''}, "m.py")
    assert "from pydantic_settings import BaseSettings, SettingsConfigDict" in new
    assert 'model_config = SettingsConfigDict(env_prefix="APP_")' in new
    assert 'Field("x", validation_alias="APP_URL")' in new
    assert "class Page(BaseModel, Generic[T]):" in new
    assert "pydantic.generics" not in new
    assert "TypeAdapter(List[int]).validate_python(rows)" in new
    assert report.needs_settings_dependency


def test_field_and_constrained_type_kwargs(tmp_path):
    new, _ = migrate(tmp_path, {"m.py": '''
        from typing import List, Optional
        from pydantic import BaseModel, Field, conlist, constr

        class A(BaseModel):
            code: constr(regex=r"^[A-Z]+$")
            tags: conlist(str, min_items=1, max_items=3)
            sku: str = Field(..., regex=r"\\d+", example="1")
            note: Optional[str] = Field(description="free text")
            locked: int = Field(0, allow_mutation=False)
    '''}, "m.py")
    assert 'constr(pattern=r"^[A-Z]+$")' in new
    assert "conlist(str, min_length=1, max_length=3)" in new
    assert "json_schema_extra={'example': \"1\"}" in new and "pattern=" in new
    assert 'note: Optional[str] = Field(None, description="free text")' in new
    assert "frozen=True" in new


def test_custom_root_type_is_unresolved(tmp_path):
    _, report = migrate(tmp_path, {"m.py": '''
        from typing import List
        from pydantic import BaseModel

        class Tags(BaseModel):
            __root__: List[str]
    '''}, "m.py")
    assert "PYD204" in unresolved(report)


def test_pydantic_v1_shim_and_removed_modules_are_flagged(tmp_path):
    _, report = migrate(tmp_path, {"m.py": '''
        from pydantic.v1 import BaseModel
        from pydantic.json import pydantic_encoder

        class A(BaseModel):
            x: int
    '''}, "m.py")
    assert {"PYD209", "PYD208"} <= unresolved(report)


def test_models_through_project_base_classes(tmp_path):
    new, _ = migrate(tmp_path, {
        "base.py": '''
            from pydantic import BaseModel

            class Base(BaseModel):
                class Config:
                    orm_mode = True
        ''',
        "child.py": '''
            from typing import Optional
            from base import Base

            class Child(Base):
                x: Optional[int]

            def f(c: Child):
                return c.dict()
        ''',
    }, "child.py")
    assert "x: Optional[int] = None" in new
    assert "c.model_dump()" in new


def test_idempotent(tmp_path):
    files = {"m.py": '''
        from typing import List, Optional
        from pydantic import BaseModel, Field, validator, root_validator

        class A(BaseModel):
            a: int
            b: Optional[str]
            c: List[int] = Field(default_factory=list, min_items=1)

            class Config:
                orm_mode = True

            @validator("a")
            def v(cls, x, values):
                return x

            @root_validator(skip_on_failure=True)
            def r(cls, values):
                return values

        def f(x: A):
            return x.dict(), A.parse_obj({}), x.copy()
    '''}
    first, _ = migrate(tmp_path, files, "m.py")
    (tmp_path / "m.py").write_text(first)
    second, report = migrate_source(first, "m.py", ProjectIndex.build(tmp_path))
    assert second == first
    assert not report.changes and not report.unresolved


def test_non_pydantic_code_untouched(tmp_path):
    source = '''
        import json

        class Config:
            orm_mode = True

        def f(d: dict):
            return d.copy(), json.dumps(d), d.values()
    '''
    new, report = migrate(tmp_path, {"m.py": source}, "m.py")
    assert new == (tmp_path / "m.py").read_text()
    assert not report.changes
