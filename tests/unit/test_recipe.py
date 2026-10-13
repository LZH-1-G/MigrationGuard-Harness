from __future__ import annotations

from migrationguard.migrate.recipe import PydanticV2Recipe
from tests.conftest import write_tree

FILES = {
    "requirements.txt": "pydantic<2\n",
    "app/models.py": '''
        from pydantic import BaseModel

        class A(BaseModel):
            x: int

        def dump(a: A):
            return a.dict()
    ''',
    "tests/test_a.py": '''
        from app.models import A

        def test_a():
            assert A.parse_obj({"x": 1}).x == 1
    ''',
}


def test_apply_modes(tmp_path):
    recipe = PydanticV2Recipe()
    full = write_tree(tmp_path / "full", FILES)
    result = recipe.apply(full)
    assert {e.path for e in result.file_edits} == {"app/models.py", "tests/test_a.py"}
    assert "pydantic>=2.7,<3" in (full / "requirements.txt").read_text()

    tests_only = write_tree(tmp_path / "tests_only", FILES)
    result = recipe.apply(tests_only, tests_only=True)
    assert {e.path for e in result.file_edits} == {"tests/test_a.py"}
    assert "a.dict()" in (tests_only / "app/models.py").read_text()

    bump = write_tree(tmp_path / "bump", FILES)
    result = recipe.apply(bump, code=False)
    assert result.file_edits == [] and len(result.dependency_edits) == 1


def test_residual_and_expected_for(tmp_path):
    recipe = PydanticV2Recipe()
    root = write_tree(tmp_path / "r", FILES)
    assert {f.rule for f in recipe.residual(root)} >= {"PYD140", "PYD141", "DEP001"}
    expected = recipe.expected_for(root, "tests/test_a.py")
    assert "A.model_validate(" in expected
    assert recipe.expected_for(root, "missing.py") is None
    recipe.apply(root)
    assert recipe.residual(root) == []


def test_plan_lists_agent_steps(tmp_path):
    files = dict(FILES)
    files["app/root.py"] = '''
        from typing import List
        from pydantic import BaseModel

        class Tags(BaseModel):
            __root__: List[str]
    '''
    root = write_tree(tmp_path / "p", files)
    recipe = PydanticV2Recipe()
    plan = recipe.plan(recipe.analyze(root))
    kinds = [s.kind for s in plan.steps]
    assert kinds[0] == "dependency" and "codemod" in kinds
    agent_steps = [s for s in plan.steps if s.kind == "agent"]
    assert agent_steps and agent_steps[0].paths == ["app/root.py"] and "PYD204" in agent_steps[0].rules
