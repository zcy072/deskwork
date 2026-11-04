"""SKILL.md frontmatter 解析与 XML 生成。"""

import pytest

from app.agents import skills_helper
from app.agents.skills_helper import (
    _escape_xml,
    _parse_frontmatter,
    _skill_to_xml,
    get_skill_path,
    get_skills_xml,
)


def _write_skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def test_get_skill_path():
    assert get_skill_path("pdf") == "/workspace/skills/pdf"


def test_escape_xml():
    assert _escape_xml("a<b>&\"'") == "a&lt;b&gt;&amp;&quot;&apos;"


def test_parse_frontmatter_ok(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: pdf\ndescription: do pdf\n---\nbody", encoding="utf-8")
    assert _parse_frontmatter(p) == {"name": "pdf", "description": "do pdf"}


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter",
        "---\nname: x\nno closing",
        "---\n- just\n- a list\n---\n",
        "---\n: bad: [yaml\n---\n",
    ],
)
def test_parse_frontmatter_invalid_returns_none(tmp_path, text):
    p = tmp_path / "SKILL.md"
    p.write_text(text, encoding="utf-8")
    assert _parse_frontmatter(p) is None


def test_skill_to_xml_requires_name_and_description():
    assert _skill_to_xml({"name": "x"}, "/p") is None
    assert _skill_to_xml({"description": "d"}, "/p") is None


def test_skill_to_xml_contains_optional_fields():
    xml = _skill_to_xml(
        {"name": "x", "description": "d<>", "license": "MIT", "metadata": {"k": "v"}},
        "/workspace/skills/x",
    )
    assert "<name>x</name>" in xml
    assert "<description>d&lt;&gt;</description>" in xml
    assert "<license>MIT</license>" in xml
    assert "<k>v</k>" in xml


def test_get_skills_xml_skips_invalid_skills(tmp_path, monkeypatch):
    _write_skill(tmp_path, "good", "---\nname: good\ndescription: ok\n---\n")
    _write_skill(tmp_path, "bad", "no frontmatter")
    (tmp_path / "stray.txt").write_text("not a dir")
    monkeypatch.setattr(skills_helper, "DEFAULT_SKILLS_DIR", tmp_path)

    xml = get_skills_xml()
    assert "<name>good</name>" in xml
    assert "bad" not in xml


def test_get_skills_xml_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_helper, "DEFAULT_SKILLS_DIR", tmp_path / "missing")
    assert get_skills_xml() == ""


def test_bundled_skills_all_parse():
    """仓库自带的技能必须都能被解析出来。"""
    xml = get_skills_xml()
    for name in ("pdf", "docx", "pptx", "xlsx", "theme-factory", "doc-coauthoring"):
        assert f"<name>{name}</name>" in xml


def test_get_skills_xml_is_cached_and_refreshes_when_a_skill_changes(tmp_path, monkeypatch):
    import os

    _write_skill(tmp_path, "a", "---\nname: a\ndescription: one\n---\n")
    monkeypatch.setattr(skills_helper, "DEFAULT_SKILLS_DIR", tmp_path)
    parsed = []
    real = skills_helper._parse_frontmatter
    monkeypatch.setattr(skills_helper, "_parse_frontmatter", lambda p: parsed.append(p) or real(p))

    assert "<description>one</description>" in get_skills_xml()
    get_skills_xml()
    assert len(parsed) == 1  # 第二次命中缓存,没有重新解析

    manifest = tmp_path / "a" / "SKILL.md"
    manifest.write_text("---\nname: a\ndescription: two\n---\n")
    stat = manifest.stat()
    os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert "<description>two</description>" in get_skills_xml()
