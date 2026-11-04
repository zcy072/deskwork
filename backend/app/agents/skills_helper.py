from pathlib import Path

import yaml

from app.sandbox.constants import DEFAULT_SKILLS_DIR, VIRTUAL_WORKSPACE
from app.utils import logger

SKILL_MANIFEST = "SKILL.md"


def get_skill_path(skill_name: str) -> str:
    """Return the agent-facing virtual path for a skill."""
    return f"{VIRTUAL_WORKSPACE}/skills/{skill_name}"


def _parse_frontmatter(skill_md_path: Path) -> dict | None:
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", skill_md_path, exc)
        return None

    text = text.strip()
    if not text.startswith("---"):
        logger.warning("No frontmatter found in %s", skill_md_path)
        return None

    end_idx = text.find("---", 3)
    if end_idx == -1:
        logger.warning("Unclosed frontmatter in %s", skill_md_path)
        return None

    yaml_block = text[3:end_idx]
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        logger.warning("Invalid YAML in %s: %s", skill_md_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Frontmatter is not a mapping in %s", skill_md_path)
        return None

    return data


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _skill_to_xml(frontmatter: dict, container_path: str) -> str | None:
    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not description:
        logger.warning(
            "Skill missing required field(s) – name: %s, description: %s",
            name,
            bool(description),
        )
        return None

    lines = [
        "<skill>",
        f"  <name>{_escape_xml(str(name))}</name>",
        f"  <description>{_escape_xml(str(description))}</description>",
        f"  <path>{_escape_xml(container_path)}</path>",
    ]

    license_val = frontmatter.get("license")
    if license_val:
        lines.append(f"  <license>{_escape_xml(str(license_val))}</license>")

    compatibility = frontmatter.get("compatibility")
    if compatibility:
        lines.append(f"  <compatibility>{_escape_xml(str(compatibility))}</compatibility>")

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools:
        lines.append(f"  <allowed-tools>{_escape_xml(str(allowed_tools))}</allowed-tools>")

    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict) and metadata:
        lines.append("  <metadata>")
        for key, value in metadata.items():
            safe_key = _escape_xml(str(key))
            safe_val = _escape_xml(str(value))
            lines.append(f"    <{safe_key}>{safe_val}</{safe_key}>")
        lines.append("  </metadata>")

    lines.append("</skill>")
    return "\n".join(lines)


_skills_cache: tuple[tuple, str] | None = None


def _skills_signature(skills_dir: Path) -> tuple:
    """目录路径加各 SKILL.md 的 mtime:技能被增删改后签名变化,缓存自动失效。"""
    return (
        str(skills_dir),
        tuple(
            (c.name, (c / SKILL_MANIFEST).stat().st_mtime_ns)
            for c in sorted(skills_dir.iterdir())
            if c.is_dir() and (c / SKILL_MANIFEST).is_file()
        ),
    )


def get_skills_xml() -> str:
    """每次模型调用都会用到,所以按目录签名缓存,避免反复读盘解析 YAML。"""
    global _skills_cache
    skills_dir = DEFAULT_SKILLS_DIR
    if not skills_dir.is_dir():
        logger.warning("Skills directory does not exist: %s", skills_dir)
        return ""

    signature = _skills_signature(skills_dir)
    if _skills_cache and _skills_cache[0] == signature:
        return _skills_cache[1]

    xml_blocks: list[str] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        frontmatter = (
            _parse_frontmatter(child / SKILL_MANIFEST)
            if (child / SKILL_MANIFEST).is_file()
            else None
        )
        if frontmatter is None:
            continue
        xml = _skill_to_xml(frontmatter, get_skill_path(child.name))
        if xml is not None:
            xml_blocks.append(xml)

    logger.info("Loaded %d skills from %s", len(xml_blocks), skills_dir)
    result = "\n".join(xml_blocks)
    _skills_cache = (signature, result)
    return result
