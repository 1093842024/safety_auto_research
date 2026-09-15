/** 研究 Skill 视图：AREX-Skill 知识库的层级浏览 / 检索 / 详情 / 文件查看.

数据来自本地克隆的 VectorSpaceLab/AREX-Skill 仓库（6,000+ 技能）：
  - 层级：大类（仓库技能 / 任务型技能）→ 研究大类（官方 catalog：Computer
    Vision / Biomedical AI / …）→ 仓库或基准 → 子分组 → 技能
  - 检索：按名称 / 描述 / 路径的子串匹配（名称命中优先排序）
  - 详情：SKILL.md 正文 + 目录内脚本 / 参考资料等文件的内容查看
  - 生效：在「新建研究」的技能（skills）字段填入技能名，
    经 StageTaskSpec.agent_config 传递给 agent 使用。
*/

import React, { useEffect, useMemo, useState } from "react";
import {
  getSkillDetail,
  getSkillFile,
  getSkillTree,
  searchSkills,
  SkillDetail,
  SkillEntry,
  SkillFileContent,
  SkillTree,
} from "../api/client";

const CATEGORY_LABEL: Record<string, string> = {
  repositories: "仓库技能",
  task_oriented: "任务型技能",
  other: "其他",
};

const fmtSize = (n: number): string =>
  n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;

/** 简单的 SKILL.md 正文渲染：标题加粗、其余等宽保留格式。 */
function FileBody({ content }: { content: string }) {
  const lines = content.split("\n");
  return (
    <pre
      style={{
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        fontSize: 12,
        lineHeight: 1.55,
        margin: 0,
        maxHeight: 420,
        overflow: "auto",
      }}
    >
      {lines.map((ln, i) =>
        ln.startsWith("#") ? (
          <div key={i} style={{ fontWeight: 700, marginTop: i ? 10 : 0 }}>
            {ln}
          </div>
        ) : (
          ln + "\n"
        ),
      )}
    </pre>
  );
}

export function ResearchSkills() {
  const [tree, setTree] = useState<SkillTree | null>(null);
  const [category, setCategory] = useState<string>("");
  const [domain, setDomain] = useState<string>("");
  const [group, setGroup] = useState<string>("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [items, setItems] = useState<SkillEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailPath, setDetailPath] = useState("");
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [viewFile, setViewFile] = useState("");
  const [fileContent, setFileContent] = useState<SkillFileContent | null>(null);
  const [copied, setCopied] = useState("");
  const [limit, setLimit] = useState(50);

  useEffect(() => {
    getSkillTree()
      .then(setTree)
      .catch((e) => setError(String(e?.message || e)));
  }, []);

  useEffect(() => {
    setLoading(true);
    setError("");
    searchSkills({ q, category, domain, group, limit, offset: 0 })
      .then((r) => {
        setItems(r.items);
        setTotal(r.total);
      })
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
  }, [q, category, domain, group, limit]);

  const loadMore = () => {
    setLoading(true);
    searchSkills({ q, category, domain, group, limit, offset: items.length })
      .then((r) => setItems((prev) => [...prev, ...r.items]))
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
  };

  const openDetail = (p: string) => {
    if (detailPath === p) {
      setDetailPath("");
      return;
    }
    setDetailPath(p);
    setDetail(null);
    setViewFile("");
    setFileContent(null);
    getSkillDetail(p)
      .then(setDetail)
      .catch((e) => setError(String(e?.message || e)));
  };

  const openSkillFile = (skillPath: string, filePath: string) => {
    setViewFile(filePath);
    setFileContent(null);
    const base = skillPath.replace(/SKILL\.md$/, "");
    getSkillFile(base + filePath)
      .then(setFileContent)
      .catch((e) => setError(String(e?.message || e)));
  };

  const copyText = (text: string, what: string) => {
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(what);
        setTimeout(() => setCopied(""), 1500);
      })
      .catch(() => {});
  };

  /** 研究大类（domain）列表：当前 category 下按技能数降序。 */
  const domains = useMemo(() => {
    if (!tree) return [];
    const cat = category || "repositories";
    return Object.entries(tree.tree?.[cat] || {})
      .map(([d, groups]) => ({
        name: d,
        count: Object.values(groups).reduce((a, n) => a + n, 0),
        repos: Object.entries(groups).sort((a, b) => b[1] - a[1]),
      }))
      .sort((a, b) => b.count - a.count);
  }, [tree, category]);

  const toggleExpand = (d: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });

  return (
    <div>
      <div className="card" style={{ padding: "18px 20px" }}>
        <h2 style={{ margin: 0 }}>🧩 研究 Skill 知识库</h2>
        <p className="muted" style={{ marginTop: 6 }}>
          来源：<b>VectorSpaceLab/AREX-Skill</b>（本地克隆于{" "}
          <code>vendor/AREX-Skill</code>，共 {tree?.total ?? "…"} 个技能）。
          层级：大类 → 研究大类 → 仓库 / 基准 → 技能（含可查看的脚本与参考文件）。
          在「新建研究」的技能（skills）字段填入技能名即可生效：技能经{" "}
          <code>StageTaskSpec.agent_config</code> 传递给 agent。
        </p>

        <div className="row" style={{ gap: 8, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
          <button
            className={`chip ${category === "" ? "chip-on" : ""}`}
            onClick={() => {
              setCategory("");
              setDomain("");
              setGroup("");
            }}
          >
            全部（{tree?.total ?? "…"}）
          </button>
          {Object.keys(CATEGORY_LABEL)
            .filter((c) => c !== "other" && Object.keys(tree?.tree?.[c] || {}).length > 0)
            .map((c) => (
              <button
                key={c}
                className={`chip ${category === c ? "chip-on" : ""}`}
                onClick={() => {
                  setCategory(c);
                  setDomain("");
                  setGroup("");
                }}
              >
                {CATEGORY_LABEL[c]}（{tree?.categories?.[c] ?? 0}）
              </button>
            ))}
          <input
            className="search"
            style={{ flex: "1 1 260px", marginLeft: "auto" }}
            placeholder="搜索技能名 / 描述 / 路径…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      {error && (
        <div className="card error" style={{ marginTop: 10 }}>
          {error}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "280px 1fr", gap: 14, marginTop: 14, alignItems: "start" }}>
        {/* ---- 左侧：研究大类 → 仓库/基准 两级树 ---- */}
        <div className="card" style={{ padding: "12px 14px", maxHeight: 680, overflow: "auto" }}>
          <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>研究大类</h3>
          <button
            className={`btn tiny ${!domain && !group ? "primary" : ""}`}
            style={{ marginBottom: 8, display: "block", width: "100%", textAlign: "left", justifyContent: "flex-start" }}
            onClick={() => {
              setDomain("");
              setGroup("");
            }}
          >
            全部{category ? `（${tree?.categories?.[category] ?? "…"}）` : ""}
          </button>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {domains.map((d) => {
              const isOpen = expanded.has(d.name) || domain === d.name;
              return (
                <div key={d.name}>
                  <div className="row" style={{ gap: 4, alignItems: "center" }}>
                    <button
                      className="linklike"
                      style={{ width: 16 }}
                      onClick={() => toggleExpand(d.name)}
                      title={isOpen ? "收起仓库列表" : "展开仓库列表"}
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>
                    <button
                      className={`linklike ${domain === d.name ? "" : ""}`}
                      style={{
                        flex: 1,
                        textAlign: "left",
                        fontWeight: domain === d.name ? 700 : 400,
                        fontSize: 12,
                      }}
                      onClick={() => {
                        setDomain(domain === d.name ? "" : d.name);
                        setGroup("");
                      }}
                      title={`按研究大类「${d.name}」过滤`}
                    >
                      {d.name}
                    </button>
                    <span className="muted mono" style={{ fontSize: 11 }}>{d.count}</span>
                  </div>
                  {isOpen && (
                    <div style={{ marginLeft: 20, marginBottom: 4 }}>
                      {d.repos.map(([repo, n]) => (
                        <button
                          key={repo}
                          className={`btn tiny ${group === repo ? "primary" : ""}`}
                          style={{
                            display: "block",
                            width: "100%",
                            textAlign: "left",
                            justifyContent: "flex-start",
                            fontSize: 11,
                            padding: "2px 6px",
                          }}
                          onClick={() => {
                            setDomain(d.name);
                            setGroup(group === repo ? "" : repo);
                          }}
                          title={`${repo}（${n} 个技能）`}
                        >
                          {repo}
                          <span className="muted" style={{ marginLeft: 6 }}>{n}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            {domains.length === 0 && (
              <div className="muted small">加载中或当前分类无数据。</div>
            )}
          </div>
        </div>

        {/* ---- 右侧：技能列表 + 内联详情 ---- */}
        <div className="card" style={{ padding: "12px 14px" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3 style={{ margin: 0, fontSize: 14 }}>
              技能列表 <span className="muted">（{total} 个匹配）</span>
              {domain && <span className="pill accent" style={{ marginLeft: 8 }}>{domain}</span>}
              {group && <span className="pill gray" style={{ marginLeft: 8 }}>{group}</span>}
            </h3>
            {loading && <span className="muted small">加载中…</span>}
          </div>
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
            {items.map((s) => (
              <React.Fragment key={s.path}>
                <div className="card" style={{ padding: "10px 12px", margin: 0 }}>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <button
                      className="linklike"
                      onClick={() => openDetail(s.path)}
                      title={s.path}
                    >
                      <strong>{s.name}</strong>
                      <span style={{ marginLeft: 4 }}>{detailPath === s.path ? "▾" : "▸"}</span>
                    </button>
                    <span className="muted mono small">{s.group}</span>
                    {s.disco_role && <span className="pill accent">{s.disco_role}</span>}
                    <span style={{ flex: 1 }} />
                    <button
                      className="btn tiny"
                      title="复制技能名（粘贴到新建研究的技能字段即可生效）"
                      onClick={() => copyText(s.name, s.path + "#name")}
                    >
                      {copied === s.path + "#name" ? "已复制" : "复制名称"}
                    </button>
                  </div>
                  <div className="muted small" style={{ marginTop: 4 }}>
                    {s.description || "（无描述）"}
                  </div>
                </div>

                {/* ---- 详情：内联展开在技能名下方 ---- */}
                {detailPath === s.path && (
                  <div className="card" style={{ padding: "12px 14px", margin: "-4px 0 0 16px" }}>
                    <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                      <span className="muted mono small">{detailPath}</span>
                      <button className="btn tiny" onClick={() => setDetailPath("")}>
                        收起
                      </button>
                    </div>
                    {!detail && <div className="muted" style={{ marginTop: 8 }}>加载中…</div>}
                    {detail?.found && (
                      <>
                        {detail.files.length > 1 && (
                          <div className="row" style={{ gap: 4, marginTop: 8, flexWrap: "wrap" }}>
                            {detail.files.map((f) => (
                              <button
                                key={f.path}
                                className={`chip ${viewFile === f.path ? "chip-on" : ""}`}
                                style={{ fontSize: 11 }}
                                title={`${f.path}（${fmtSize(f.size)}）`}
                                onClick={() =>
                                  viewFile === f.path
                                    ? setViewFile("")
                                    : openSkillFile(detail.path, f.path)
                                }
                              >
                                {f.path === "SKILL.md" ? "📄 SKILL.md" : f.path}
                              </button>
                            ))}
                          </div>
                        )}
                        <div
                          style={{
                            marginTop: 8,
                            background: "var(--panel-2, rgba(0,0,0,0.06))",
                            padding: "10px 12px",
                            borderRadius: 8,
                          }}
                        >
                          {viewFile === "" ? (
                            <>
                              <FileBody content={detail.body} />
                              {detail.truncated && (
                                <div className="muted small" style={{ marginTop: 6 }}>
                                  内容过长已截断——完整内容见仓库文件。
                                </div>
                              )}
                            </>
                          ) : !fileContent ? (
                            <div className="muted small">加载文件内容…</div>
                          ) : fileContent.binary ? (
                            <div className="muted small">
                              二进制文件（{fmtSize(fileContent.size ?? 0)}），不支持预览——请到仓库目录查看。
                            </div>
                          ) : (
                            <>
                              <FileBody content={fileContent.content || ""} />
                              {fileContent.truncated && (
                                <div className="muted small" style={{ marginTop: 6 }}>
                                  文件过大已截断（{fmtSize(fileContent.size ?? 0)}）。
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      </>
                    )}
                    {detail && !detail.found && (
                      <div className="muted" style={{ marginTop: 8 }}>
                        加载失败：技能文件不存在或路径非法。
                      </div>
                    )}
                  </div>
                )}
              </React.Fragment>
            ))}
            {items.length === 0 && !loading && (
              <div className="muted">无匹配技能——试试更短的关键词或切换分组。</div>
            )}
          </div>
          {items.length < total && (
            <button className="btn" style={{ marginTop: 10 }} disabled={loading} onClick={loadMore}>
              {loading ? "加载中…" : `加载更多（已显示 ${items.length} / ${total}）`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
