"""
skills_loader.py — 技能系统：扫描目录、解析 frontmatter、注册表、按需加载
"""
from .config import SKILLS_DIR, build_system

# ── 技能注册表：{ 技能名: {name, description, content} } ─
SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(raw: str):
    """抠出开头 --- 与 --- 之间的 key: value，返回 (meta, body)。"""
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in raw[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            return meta, raw[end + 4:].lstrip("\n")
    return {}, raw


def _scan_skills():
    """扫描 SKILLS_DIR 下每个子目录的 SKILL.md，填充注册表。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        manifest = d / "SKILL.md"
        if d.is_dir() and manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


def list_skills() -> str:
    """返回所有已注册技能的简介列表。"""
    if not SKILL_REGISTRY:
        return "(无)"
    return "\n".join(
        f"- {s['name']}: {s['description']}" for s in SKILL_REGISTRY.values()
    )


def load_skill(name: str) -> str:
    """按名字查注册表，返回完整技能说明书。"""
    skill = SKILL_REGISTRY.get(name)
    return skill["content"] if skill else f"未找到技能: {name}"



