from __future__ import annotations

from packaging.specifiers import SpecifierSet

from migrationguard.migrate.deps import (
    collect_requirements,
    declared_pydantic_specs,
    rewrite_dependencies,
    spec_allows_v1,
    spec_allows_v2,
)
from tests.conftest import write_tree


def test_collect_requirements_from_all_manifest_kinds(tmp_path):
    write_tree(tmp_path, {
        "requirements.txt": "fastapi==0.95  # api\npydantic[email]>=1.8,<2.0\n-r requirements-dev.txt\n-e .\n",
        "requirements-dev.txt": "pytest\n",
        "pyproject.toml": '''
            [project]
            name = "x"
            dependencies = ["httpx>=0.27"]
            [project.optional-dependencies]
            test = ["pytest-mock"]
            docs = ["mkdocs"]
            [tool.poetry.dependencies]
            python = "^3.10"
            attrs = "^23.1"
        ''',
    })
    reqs = collect_requirements(tmp_path)
    assert "pydantic[email]>=1.8,<2.0" in reqs
    assert "pytest" in reqs and "httpx>=0.27" in reqs and "pytest-mock" in reqs
    assert "mkdocs" not in reqs
    assert "attrs>=23.1,<24" in reqs
    assert not any(r.startswith("-") or r == "." for r in reqs)


def test_rewrite_preserves_layout(tmp_path):
    write_tree(tmp_path, {
        "requirements.txt": "# deps\nfastapi==0.95\npydantic[email]>=1.8,<2.0  # pinned\n",
        "pyproject.toml": '[project]\nname="x"\ndependencies = [\n  "pydantic>=1.10,<2",\n  "httpx",\n]\n',
        "setup.cfg": "[options]\ninstall_requires =\n    pydantic>=1.9,<2.0\n    attrs\n",
    })
    report = rewrite_dependencies(tmp_path, "pydantic>=2.7,<3", "pydantic-settings>=2.2,<3")
    assert {e.path for e in report.edits} == {"requirements.txt", "pyproject.toml", "setup.cfg"}
    req = (tmp_path / "requirements.txt").read_text()
    assert req == "# deps\nfastapi==0.95\npydantic[email]>=2.7,<3  # pinned\npydantic-settings>=2.2,<3\n"
    assert '"pydantic>=2.7,<3", "pydantic-settings>=2.2,<3",' in (tmp_path / "pyproject.toml").read_text()
    assert "    pydantic>=2.7,<3\n    pydantic-settings>=2.2,<3\n    attrs\n" in (tmp_path / "setup.cfg").read_text()
    for _, spec in declared_pydantic_specs(tmp_path):
        assert "2.7" in spec


def test_does_not_touch_similarly_named_packages(tmp_path):
    write_tree(tmp_path, {"requirements.txt": "pydantic-extra-types==2.0\npydantic==1.10.2\n"})
    rewrite_dependencies(tmp_path, "pydantic>=2.7,<3", None)
    assert (tmp_path / "requirements.txt").read_text() == "pydantic-extra-types==2.0\npydantic>=2.7,<3\n"


def test_spec_helpers():
    assert spec_allows_v1(SpecifierSet("<2")) and not spec_allows_v2(SpecifierSet("<2"))
    assert spec_allows_v2(SpecifierSet(">=2.7,<3")) and not spec_allows_v1(SpecifierSet(">=2.7,<3"))
    assert spec_allows_v1(SpecifierSet("")) and spec_allows_v2(SpecifierSet(""))
