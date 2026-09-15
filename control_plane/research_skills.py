"""Research-skill knowledge base: AREX-Skill 库的索引 / 检索 / 详情.

Indexes the locally cloned `VectorSpaceLab/AREX-Skill` repository
(``vendor/AREX-Skill/skills``, override with env ``RESEARCH_SKILLS_ROOT``):
6,000+ verified, executable research skills distilled from 1,000+ ML repos,
organised in a two-branch hierarchy::

    skills/
      repositories/repo-skills/<repo>/SKILL.md                     # repo-level skill
      repositories/repo-skills/<repo>/sub-skills/<skill>/SKILL.md  # repo sub-skill
      repositories/repo-skills-router/SKILL.md                     # router skill
      task-oriented/<benchmark>/skills/<source>/<groups>/<skill>/SKILL.md

Each ``SKILL.md`` carries YAML frontmatter (``name`` / ``description`` /
optional ``metadata.disco-role``). The index is built once per process (a
first pass over 6k files costs well under a second) and cached; search is
plain substring matching over name/description/path — good enough for a
selection UI, no external index service needed.

Skills become *effective* in research via the existing chain: selected skill
names are persisted into ``objective_snapshot.config.inner_loop.skills`` and
travel to the agent through ``StageTaskSpec.agent_config``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import lru_cache

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INDEX_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INDEX_TTL = 600.0
_LOCK = threading.Lock()


def skill_root() -> str:
    override = os.environ.get("RESEARCH_SKILLS_ROOT")
    if override:
        return override
    return os.path.join(_PKG_ROOT, "vendor", "AREX-Skill", "skills")


def _root_signature(root: str) -> float:
    try:
        return os.stat(root).st_mtime
    except OSError:
        return 0.0


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the leading ``---`` YAML frontmatter (subset) + return the body.

    Handles ``key: value``, quoted values, indented continuation lines and one
    level of nesting (``metadata:`` → ``disco-role:``). A hand-rolled subset
    keeps this dependency-free (PyYAML is not guaranteed in the runtime venv).
    """
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return meta, text
    last_key: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            if last_key and indented and last_key in meta:
                # continuation of a folded value
                meta[last_key] = (meta[last_key] + " " + line.strip()).strip()
            continue
        key, val = m.group(1), m.group(2).strip()
        if indented and last_key and key and not val:
            continue
        if not indented and key == "metadata" and not val:
            last_key = "metadata"
            continue
        val = val.strip("\"'")
        if indented and last_key == "metadata":
            meta[f"disco_role"] = val  # only disco-role matters today
            continue
        meta[key] = val
        last_key = key
    body = "\n".join(lines[end + 1:])
    return meta, body


def _load_domain_catalog(root: str) -> dict[str, tuple[str, str]]:
    """Parse ``docs/repository-catalog.md`` → skill_id → (domain, subdomain).

    The AREX-Skill repo ships an official research-domain catalog (20 大类 /
    178 子领域). Table rows look like::

        | `owner/repo` | `skill_id` | [`repo-skills/skill_id`](...) | ... |
    """
    catalog_path = os.path.join(
        os.path.dirname(root.rstrip(os.sep)), "docs", "repository-catalog.md"
    )
    mapping: dict[str, tuple[str, str]] = {}
    try:
        with open(catalog_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return mapping
    domain = ""
    subdomain = ""
    h_domain = re.compile(r'^## <a id="[^"]*"></a>(.+?)\s*\(\d+ repos\)\s*$')
    h_sub = re.compile(r'^### <a id="[^"]*"></a>(.+?)\s*\(\d+ repos\)\s*$')
    row = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|")
    for line in text.splitlines():
        m = h_domain.match(line)
        if m:
            domain = m.group(1).strip()
            subdomain = ""
            continue
        m = h_sub.match(line)
        if m:
            subdomain = m.group(1).strip()
            continue
        m = row.match(line)
        if m and domain:
            mapping[m.group(2).strip()] = (domain, subdomain)
    return mapping


def _classify(rel_dir: str) -> tuple[str, str, str]:
    """Map a SKILL.md's directory to (category, group, sub_path)."""
    parts = rel_dir.split(os.sep)
    if parts[0] == "repositories":
        # repositories/repo-skills/<repo>[/<...>]
        if len(parts) >= 3:
            group = parts[2]
            sub = "/".join(parts[3:])
            return "repositories", group, sub
        return "repositories", parts[1] if len(parts) > 1 else "", ""
    if parts[0] == "task-oriented" and len(parts) >= 2:
        group = parts[1]
        sub = "/".join(parts[2:])
        return "task_oriented", group, sub
    return "other", parts[0] if parts else "", "/".join(parts[1:])


def _build_index(root: str) -> list[dict]:
    domain_catalog = _load_domain_catalog(root)
    index: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "SKILL.md" not in filenames:
            continue
        rel_dir = os.path.relpath(dirpath, root)
        category, group, sub = _classify(rel_dir)
        skill_file = os.path.join(dirpath, "SKILL.md")
        name = os.path.basename(rel_dir)
        description = ""
        disco_role = ""
        try:
            with open(skill_file, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
            meta, _ = _parse_frontmatter(head)
            name = meta.get("name") or name
            description = meta.get("description") or ""
            disco_role = meta.get("disco_role", "")
        except OSError:
            pass
        # 研究大类：优先取官方 catalog 映射（repo 目录名 → domain/subdomain）。
        domain, subdomain = domain_catalog.get(group, ("通用", ""))
        if category == "task_oriented":
            # 任务型技能以基准名（PaperBench / FrontierCS / PassNet）为大类。
            domain, subdomain = group, sub
        index.append({
            "name": name,
            "description": description,
            "category": category,
            "group": group,
            "sub": sub,
            "domain": domain,
            "subdomain": subdomain,
            "disco_role": disco_role,
            # relative path of the SKILL.md (posix style) — the detail lookup key
            "path": os.path.join(rel_dir, "SKILL.md").replace(os.sep, "/"),
        })
    index.sort(key=lambda s: (s["category"], s["domain"], s["group"], s["name"]))
    return index


def get_index() -> list[dict]:
    root = skill_root()
    sig = _root_signature(root)
    with _LOCK:
        cached = _INDEX_CACHE.get(root)
        if cached and time.monotonic() - cached[0] < _INDEX_TTL and cached[2] == sig:
            return cached[1]
    if not os.path.isdir(root):
        return []
    index = _build_index(root)
    with _LOCK:
        _INDEX_CACHE[root] = (time.monotonic(), index, sig)
    return index


def _match(s: dict, q: str) -> bool:
    if not q:
        return True
    ql = q.lower()
    return (
        ql in s["name"].lower()
        or ql in s["description"].lower()
        or ql in s["group"].lower()
        or ql in s["path"].lower()
    )


def search_skills(
    q: str = "",
    category: str = "",
    group: str = "",
    domain: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    items = [
        s for s in get_index()
        if (not category or s["category"] == category)
        and (not group or s["group"] == group)
        and (not domain or s.get("domain") == domain)
        and _match(s, q.strip())
    ]
    # name-prefix / name-contains matches first (simple relevance).
    ql = q.strip().lower()
    if ql:
        items.sort(
            key=lambda s: (
                0 if s["name"].lower().startswith(ql)
                else 1 if ql in s["name"].lower()
                else 2
            )
        )
    total = len(items)
    return {
        "total": total,
        "items": items[offset: offset + max(1, min(limit, 200))],
    }


def skill_tree() -> dict:
    """Hierarchical counts: 大类 → 研究大类（domain）→ 仓库/基准 → 数量。

    repositories 类的 domain 来自官方 ``docs/repository-catalog.md``
    （Computer Vision / Biomedical AI / … 20 个研究大类）；
    task_oriented 类的 domain 即基准名。
    """
    tree: dict[str, dict] = {}
    totals: dict[str, int] = {}
    for s in get_index():
        cat = s["category"]
        branch = tree.setdefault(cat, {})
        node = branch.setdefault(s.get("domain") or "通用", {})
        node[s["group"]] = node.get(s["group"], 0) + 1
        totals[cat] = totals.get(cat, 0) + 1
    return {"total": sum(totals.values()), "categories": totals, "tree": tree}


def _skill_file_text(rel_path: str, max_bytes: int) -> dict:
    """Read one text file under the skill root (validated, size-capped).

    Binary payloads (NUL byte in the head) are reported as ``binary`` instead
    of being garbled.
    """
    root = skill_root()
    rel = (rel_path or "").replace("\\", "/").strip("/")
    if not rel or ".." in rel.split("/"):
        return {"found": False, "reason": "非法路径"}
    full = os.path.join(root, *rel.split("/"))
    root_abs = os.path.abspath(root)
    if not os.path.abspath(full).startswith(root_abs):
        return {"found": False, "reason": "非法路径"}
    if not os.path.isfile(full):
        return {"found": False, "reason": "文件不存在"}
    try:
        with open(full, "rb") as f:
            raw = f.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if b"\x00" in raw[:8192]:
            return {"found": True, "binary": True, "size": os.path.getsize(full)}
        text = raw.decode("utf-8", "replace")
        return {
            "found": True,
            "binary": False,
            "path": rel,
            "content": text[:max_bytes],
            "truncated": truncated,
            "size": os.path.getsize(full),
        }
    except OSError as exc:
        return {"found": False, "reason": str(exc)}


def skill_detail(path: str, max_bytes: int = 32_000) -> dict:
    """SKILL.md content + the skill directory's full file manifest."""
    root = skill_root()
    rel = (path or "").replace("\\", "/").strip("/")
    if not rel.endswith("SKILL.md") or ".." in rel.split("/"):
        return {"found": False, "reason": "非法路径"}
    full = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(full):
        return {"found": False, "reason": "技能文件不存在"}
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read(max_bytes + 1)
        truncated = len(text) > max_bytes
        meta, body = _parse_frontmatter(text)
        siblings: list[str] = []
        sub_dir = os.path.dirname(full)
        for entry in sorted(os.listdir(sub_dir)):
            if entry == "SKILL.md":
                continue
            p = os.path.join(sub_dir, entry)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
                siblings.append(entry)
        # 递归文件清单（相对技能目录；供前端查看脚本 / 参考资料等文件）。
        files: list[dict] = []
        skill_dir = os.path.dirname(full)
        for walk_dir, dirnames, filenames in os.walk(skill_dir):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fn in sorted(filenames):
                fp = os.path.join(walk_dir, fn)
                rel_file = os.path.relpath(fp, skill_dir).replace(os.sep, "/")
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    size = 0
                files.append({"path": rel_file, "size": size})
                if len(files) >= 200:
                    break
            if len(files) >= 200:
                break
        return {
            "found": True,
            "path": rel,
            "meta": meta,
            "body": body[:max_bytes],
            "truncated": truncated,
            "sub_skills": siblings,
            "files": files,
        }
    except OSError as exc:
        return {"found": False, "reason": str(exc)}
