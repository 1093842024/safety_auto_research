import React, { useState } from "react";
import {
  ExecutableRubric,
  RUBRIC_DIMENSION_LABEL,
  RUBRIC_SEVERITY_CLASS,
  RUBRIC_SEVERITY_LABEL,
  RUBRIC_STATUS_CLASS,
  RUBRIC_STATUS_LABEL,
  RUBRIC_VERDICT_CLASS,
  RUBRIC_VERDICT_LABEL,
  RubricCriterion,
  RubricCriterionVerdict,
  RubricIteration,
  RubricPreview,
  RubricReview,
} from "../api/client";

const PRIORITY_LABEL: Record<string, string> = { high: "硬性", medium: "一般", low: "参考" };
const PRIORITY_CLASS: Record<string, string> = { high: "bad", medium: "warn", low: "accent" };

function pct(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(0)}%`;
}

/** A single 0–1 dimension score rendered as a labelled bar. */
function ScoreBar({ label, value }: { label: string; value: number | undefined }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const cls = v >= 0.85 ? "ok" : v >= 0.6 ? "warn" : "bad";
  return (
    <div style={{ flex: 1, minWidth: 120 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="muted small">{label}</span>
        <span className={`small ${cls}-text`}>{pct(v)}</span>
      </div>
      <div
        style={{
          height: 6,
          borderRadius: 3,
          background: "var(--border, #2c313a)",
          overflow: "hidden",
          marginTop: 3,
        }}
      >
        <div
          style={{
            width: `${v * 100}%`,
            height: "100%",
            background:
              cls === "ok"
                ? "var(--ok, #3fb950)"
                : cls === "warn"
                  ? "var(--warn, #d29922)"
                  : "var(--bad, #e5534b)",
          }}
        />
      </div>
    </div>
  );
}

/**
 * 三维评分标准审查卡：把「已提供标准的准确性/完整性/科学性」与缺陷清单显性化。
 * 当任务未提供可用标准时（standard_provided=false），说明将自动生成标准。
 */
export function RubricReviewCard({
  review,
  preview,
  compact,
}: {
  review: RubricReview | null | undefined;
  preview?: RubricPreview | null;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(!compact);
  if (!review) return null;
  const vClass = RUBRIC_VERDICT_CLASS[review.verdict] || "accent";
  const critical = review.findings.filter((f) => f.severity === "critical");

  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <strong>评分标准审查</strong>
          <span className={`pill ${vClass}`} style={{ marginLeft: 8 }}>
            {RUBRIC_VERDICT_LABEL[review.verdict] || review.verdict} · {pct(review.overall)}
          </span>
          <span className={`pill ${review.standard_provided ? "accent" : "warn"}`} style={{ marginLeft: 6 }}>
            {review.standard_provided ? "已提供标准 · 已审查" : "未提供标准 · 将自动生成"}
          </span>
        </div>
        <button className="btn tiny" onClick={() => setOpen(!open)}>
          {open ? "收起" : "展开"}
        </button>
      </div>

      <div className="row" style={{ gap: 14, marginTop: 10 }}>
        <ScoreBar label="准确性" value={review.accuracy} />
        <ScoreBar label="完整性" value={review.completeness} />
        <ScoreBar label="科学性" value={review.scientificity} />
      </div>

      <p className="muted small" style={{ marginTop: 8 }}>
        {review.summary}
      </p>

      {critical.length > 0 && (
        <div className="card error" style={{ marginTop: 8 }}>
          <b>存在 {critical.length} 项严重缺陷，会导致判定结果不可信：</b>
          <ul className="tight" style={{ marginTop: 6 }}>
            {critical.map((f) => (
              <li key={f.finding_id}>{f.message}</li>
            ))}
          </ul>
        </div>
      )}

      {open && review.findings.length > 0 && (
        <table className="tbl" style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th style={{ width: 60 }}>严重度</th>
              <th style={{ width: 72 }}>维度</th>
              <th>问题</th>
              <th>建议</th>
            </tr>
          </thead>
          <tbody>
            {review.findings.map((f) => (
              <tr key={f.finding_id}>
                <td>
                  <span className={`pill ${RUBRIC_SEVERITY_CLASS[f.severity] || "accent"}`}>
                    {RUBRIC_SEVERITY_LABEL[f.severity] || f.severity}
                  </span>
                </td>
                <td className="muted small">{RUBRIC_DIMENSION_LABEL[f.dimension] || f.dimension}</td>
                <td className="small">{f.message}</td>
                <td className="muted small">{f.suggestion || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {open && review.findings.length === 0 && (
        <p className="muted small ok-text" style={{ marginTop: 8 }}>
          ✓ 未发现评分标准缺陷
        </p>
      )}

      {open && Object.keys(review.suggested_fixes || {}).length > 0 && (
        <p className="muted small" style={{ marginTop: 6 }}>
          建议修正：
          <span className="mono">{JSON.stringify(review.suggested_fixes)}</span>
        </p>
      )}

      {open && preview && (
        <div style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong className="small">
              将按以下 {preview.criteria_count} 条可执行标准判定
              <span className="pill ok" style={{ marginLeft: 6 }}>
                {preview.machine_checkable_count} 条可程序化判定
              </span>
            </strong>
            <span className="muted mono small">{preview.rubric_id}</span>
          </div>
          <table className="tbl" style={{ marginTop: 6 }}>
            <thead>
              <tr>
                <th style={{ width: 48 }}>ID</th>
                <th style={{ width: 56 }}>强度</th>
                <th style={{ width: 72 }}>维度</th>
                <th>要求</th>
                <th>通过条件</th>
              </tr>
            </thead>
            <tbody>
              {preview.criteria.map((c) => (
                <tr key={c.criterion_id}>
                  <td className="mono small">{c.criterion_id}</td>
                  <td>
                    <span className={`pill ${PRIORITY_CLASS[c.priority] || "accent"}`}>
                      {PRIORITY_LABEL[c.priority] || c.priority}
                    </span>
                  </td>
                  <td className="muted small">{RUBRIC_DIMENSION_LABEL[c.dimension] || c.dimension}</td>
                  <td className="small">
                    {c.requirement}
                    {c.blocked_reason && (
                      <div className="muted small">⚠ 受阻：{c.blocked_reason}</div>
                    )}
                  </td>
                  <td className="muted small">
                    {c.satisfaction_condition}
                    <div className="muted mono small">
                      {c.check_kind === "judge" ? "judge 判定" : `程序化 · ${c.check_kind}`}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {preview.claims_to_avoid.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary className="muted small">禁止的断言（{preview.claims_to_avoid.length} 条）</summary>
              <ul className="tight" style={{ marginTop: 6 }}>
                {preview.claims_to_avoid.map((c, i) => (
                  <li key={i} className="muted small">
                    {c}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

/** The frozen rubric contract of a run (goals + criteria + provenance). */
export function RubricContractCard({ rubric }: { rubric: ExecutableRubric }) {
  const [open, setOpen] = useState(false);
  const machine = rubric.criteria.filter((c) => (c.check?.kind ?? "judge") !== "judge").length;
  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <strong>本次研究的评分标准（冻结）</strong>
          <span className={`pill ${rubric.source === "reviewed" ? "accent" : "warn"}`} style={{ marginLeft: 8 }}>
            {rubric.source === "reviewed" ? "任务已声明标准 · 已审查规范化" : "任务未声明标准 · 平台自动生成"}
          </span>
          <span className="pill ok" style={{ marginLeft: 6 }}>
            {rubric.criteria.length} 条 / {machine} 条可程序化
          </span>
          {rubric.hash_matches_run === false && (
            <span className="pill bad" style={{ marginLeft: 6 }}>
              与运行记录哈希不一致（任务定义可能已变更）
            </span>
          )}
        </div>
        <button className="btn tiny" onClick={() => setOpen(!open)}>
          {open ? "收起" : "展开"}
        </button>
      </div>
      <p className="muted small" style={{ marginTop: 6 }}>
        <span className="mono">{rubric.rubric_id}</span> · 生成方式 {rubric.generator} · 在内循环开始
        <b>之前</b>确立，运行期不可修改（因此不存在「按结果反推标准」的可能）。
      </p>
      {open && (
        <>
          <ul className="tight" style={{ marginTop: 8 }}>
            {rubric.goals.map((g) => (
              <li key={g.goal_id} className="small">
                <b>{g.goal_id}</b> {g.title} — <span className="muted">{g.requirement}</span>
              </li>
            ))}
          </ul>
          <RubricCriteriaTable criteria={rubric.criteria} />
        </>
      )}
    </div>
  );
}

function RubricCriteriaTable({ criteria }: { criteria: RubricCriterion[] }) {
  return (
    <table className="tbl" style={{ marginTop: 8 }}>
      <thead>
        <tr>
          <th style={{ width: 48 }}>ID</th>
          <th style={{ width: 56 }}>强度</th>
          <th style={{ width: 72 }}>维度</th>
          <th>要求</th>
          <th>通过条件</th>
        </tr>
      </thead>
      <tbody>
        {criteria.map((c) => (
          <tr key={c.criterion_id}>
            <td className="mono small">{c.criterion_id}</td>
            <td>
              <span className={`pill ${PRIORITY_CLASS[c.priority] || "accent"}`}>
                {PRIORITY_LABEL[c.priority] || c.priority}
              </span>
            </td>
            <td className="muted small">{RUBRIC_DIMENSION_LABEL[c.dimension] || c.dimension}</td>
            <td className="small">
              {c.requirement}
              {c.blocked_reason && <div className="muted small">⚠ 受阻：{c.blocked_reason}</div>}
            </td>
            <td className="muted small">{c.satisfaction_condition}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * 逐条判定视图：显示每一轮外审计对每条标准的通过 / 未通过与依据。
 * 这是 rubric 的核心价值 —— 报告「哪一条没达到、为什么」，而不是只给一个总分。
 */
export function RubricVerdictTable({ iteration }: { iteration: RubricIteration }) {
  const vetoed = iteration.criteria.filter(
    (c) => c.priority === "high" && c.status === "conflict",
  );
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong className="small">
          评分标准逐条判定 · 第 {iteration.iteration + 1} 轮
          <span className={`pill ${iteration.failed === 0 ? "ok" : "warn"}`} style={{ marginLeft: 6 }}>
            {iteration.passed}/{iteration.passed + iteration.failed} 通过
          </span>
        </strong>
        {vetoed.length > 0 && (
          <span className="pill bad">硬性条目未通过 {vetoed.length} 条 · 一票否决</span>
        )}
      </div>
      <table className="tbl" style={{ marginTop: 6 }}>
        <thead>
          <tr>
            <th style={{ width: 48 }}>ID</th>
            <th style={{ width: 72 }}>结果</th>
            <th style={{ width: 56 }}>强度</th>
            <th>要求</th>
            <th>判定依据</th>
          </tr>
        </thead>
        <tbody>
          {iteration.criteria.map((c) => (
            <RubricVerdictRow key={c.criterion_id} c={c} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RubricVerdictRow({ c }: { c: RubricCriterionVerdict }) {
  return (
    <tr>
      <td className="mono small">{c.criterion_id}</td>
      <td>
        <span className={`pill ${RUBRIC_STATUS_CLASS[c.status] || "accent"}`}>
          {RUBRIC_STATUS_LABEL[c.status] || c.status}
        </span>
      </td>
      <td>
        <span className={`pill ${PRIORITY_CLASS[c.priority || "medium"] || "accent"}`}>
          {PRIORITY_LABEL[c.priority || "medium"] || c.priority}
        </span>
      </td>
      <td className="small">
        {c.description}
        {c.satisfaction_condition && (
          <div className="muted small">通过条件：{c.satisfaction_condition}</div>
        )}
      </td>
      <td className="muted small">
        <span className="mono">{c.note || "—"}</span>
        <div className="muted small">
          {c.evaluated_by?.startsWith("programmatic")
            ? `程序化判定（${c.evaluated_by.replace("programmatic:", "")}）`
            : "judge 判定"}
          {c.blocked_reason ? " · 受阻" : ""}
        </div>
      </td>
    </tr>
  );
}
