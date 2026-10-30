"""s07: skill catalog plus on-demand loading.

The system prompt only carries names and descriptions. The full ``SKILL.md``
body enters the context only when the model calls ``load_skill``. A bad or
escaping manifest is skipped instead of taking the harness down.
"""

from __future__ import annotations

from pathlib import Path

from tools.frontmatter import parse_frontmatter


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, dict] = {}
        self.scan()

    # Kept for code written against the original loader.
    def _load_all(self) -> None:
        self.scan()

    def scan(self) -> None:
        self.skills.clear()
        if not self.skills_dir.exists():
            return
        root = self.skills_dir.resolve()
        for manifest in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                if not manifest.is_file() or not manifest.resolve().is_relative_to(root):
                    continue
                text = manifest.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = self._parse_frontmatter(text)
            name = str(meta.get("name") or manifest.parent.name).strip() or manifest.parent.name
            description = str(meta.get("description") or "").strip()
            if not description:
                description = next(
                    (line.lstrip("# ").strip() for line in body.splitlines() if line.strip()),
                    "No description",
                )
            self.skills[name] = {
                "meta": {**meta, "name": name, "description": description},
                "body": body,
                "path": str(manifest),
            }

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, str], str]:
        return parse_frontmatter(text)

    def get_descriptions(self) -> str:
        """Layer 1: short catalog embedded in the system prompt."""
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """Layer 2: full body returned in a tool_result."""
        skill = self.skills.get(name)
        if not skill:
            available = ", ".join(self.skills) or "none"
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

    # Aliases used by newer harness code and docs.
    def catalog(self) -> str:
        return self.get_descriptions()

    def load(self, name: str) -> str:
        return self.get_content(name)
