"""Skills: composable markdown instruction modules that replace monolithic prompts.

A skill is a directory `skills/<name>/SKILL.md` with optional YAML frontmatter
(`name`, `description`) and a markdown body. An agent declares the skills it uses in
config; `compose_instructions` concatenates their bodies (in declared order) into the
single instruction string passed to `Agent(instructions=...)`.

Resolution order for the skills root:
  1. $CLEAN_FASTOMOP_SKILLS_DIR (tests / alternate layouts)
  2. <repo_root>/skills
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
_SEPARATOR = "\n\n---\n\n"


def skills_dir() -> Path:
    """The directory that holds skill folders."""
    override = os.getenv("CLEAN_FASTOMOP_SKILLS_DIR")
    return Path(override) if override else _DEFAULT_SKILLS_DIR


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---`-delimited YAML frontmatter block, if present."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            after = text.find("\n", end + 1)
            return text[after + 1 :].lstrip("\n") if after != -1 else ""
    return text


def load_skill(name: str) -> str:
    """Return the markdown body of `skills/<name>/SKILL.md` (frontmatter stripped)."""
    path = skills_dir() / name / "SKILL.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill '{name}' not found at {path}")
    return _strip_frontmatter(path.read_text(encoding="utf-8")).strip()


def compose_instructions(skill_names: List[str]) -> str:
    """Concatenate the named skills' bodies into one instruction string."""
    if not skill_names:
        raise ValueError("No skills declared for this agent")
    return _SEPARATOR.join(load_skill(name) for name in skill_names)
