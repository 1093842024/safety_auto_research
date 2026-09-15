"""Research-skill knowledge base routes (研究 Skill tab).

* ``GET /research-skills/tree``    — hierarchical category counts
* ``GET /research-skills``         — search / filter the 6k+ skill index
* ``GET /research-skills/detail``  — full SKILL.md content for one skill
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import status

from .. import research_skills


def build_research_skills_router(deps) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/research-skills/tree",
        summary="Hierarchical research-skill categories (大类 → 研究大类 → 仓库/基准)",
    )
    def get_skill_tree() -> dict:
        return research_skills.skill_tree()

    @router.get(
        "/research-skills",
        summary="Search the research-skill index (q / category / domain / group / 分页)",
    )
    def search(
        q: str = "",
        category: str = "",
        group: str = "",
        domain: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return research_skills.search_skills(
            q=q, category=category, group=group, domain=domain, limit=limit, offset=offset
        )

    @router.get(
        "/research-skills/detail",
        summary="SKILL.md content + file manifest of one research skill (by its index path)",
    )
    def detail(path: str) -> dict:
        result = research_skills.skill_detail(path)
        if not result.get("found"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=result.get("reason"))
        return result

    @router.get(
        "/research-skills/file",
        summary="Read one file inside the skill library (scripts / references / …)",
    )
    def file_content(path: str) -> dict:
        result = research_skills._skill_file_text(path, max_bytes=64_000)
        if not result.get("found"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=result.get("reason"))
        return result

    return router
