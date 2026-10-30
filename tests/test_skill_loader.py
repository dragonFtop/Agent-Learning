from tools.skill_loader import SkillLoader

SKILL_TEXT = """---
name: demo
description: A demo skill
tags: test, demo
---

# Demo

Body content.
"""


def _make_loader(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    return SkillLoader(tmp_path / "skills")


def test_parses_frontmatter_and_body(tmp_path):
    loader = _make_loader(tmp_path)
    assert "demo" in loader.skills
    descriptions = loader.get_descriptions()
    assert "A demo skill" in descriptions
    assert "[test, demo]" in descriptions
    assert "Body content." in loader.get_content("demo")


def test_no_frontmatter_falls_back_to_dir_name(tmp_path):
    skill_dir = tmp_path / "skills" / "plain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    loader = SkillLoader(tmp_path / "skills")
    assert "plain" in loader.skills


def test_unknown_skill_and_missing_dir(tmp_path):
    loader = SkillLoader(tmp_path / "missing")
    assert loader.get_descriptions() == "(no skills available)"
    assert "Unknown skill" in loader.get_content("nope")
