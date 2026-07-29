import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BenchmarkTask,
  FieldSpec,
  TaskTypeSpec,
  getTaskTypes,
  registerBenchmarkTask,
  uploadDataset,
  validateTask,
} from "../api/client";

const GROUP_LABELS: Record<string, string> = {
  basic: "基本信息",
  data: "数据集",
  model: "模型与方法",
  train: "训练配置",
  eval: "评测目标",
};
const GROUP_ORDER = ["basic", "data", "model", "train", "eval"];

/** Simple chip editor for `tags`-type fields. */
function TagsInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const v = draft.trim();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  };
  return (
    <div className="skill-tags">
      {value.map((s) => (
        <span key={s} className="chip">
          {s}
          <button type="button" className="chip-x" onClick={() => onChange(value.filter((x) => x !== s))}>
            ×
          </button>
        </span>
      ))}
      <input
        className="chip-input"
        value={draft}
        placeholder={placeholder || "输入后回车添加"}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
      />
    </div>
  );
}

/** Upload widget: uploads the file then writes the returned path into the linked path field. */
function UploadField({
  field,
  onUploaded,
}: {
  field: FieldSpec;
  onUploaded: (path: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div>
      <input
        ref={inputRef}
        type="file"
        accept={field.accept || undefined}
        disabled={busy}
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setBusy(true);
          setMsg("");
          try {
            const r = await uploadDataset(f);
            onUploaded(r.path);
            setMsg(
              `已上传 ${r.filename}（${(r.size_bytes / 1024 / 1024).toFixed(2)} MB${r.extracted ? "，已解压" : ""}）→ 路径已自动填入`,
            );
          } catch (err: any) {
            setMsg(`上传失败：${String(err?.message || err)}`);
          } finally {
            setBusy(false);
            if (inputRef.current) inputRef.current.value = "";
          }
        }}
      />
      {busy && <div className="muted small">上传中…</div>}
      {msg && <div className="muted small">{msg}</div>}
    </div>
  );
}

function FieldRow({
  field,
  value,
  onChange,
  onUploaded,
}: {
  field: FieldSpec;
  value: any;
  onChange: (v: any) => void;
  onUploaded: (path: string) => void;
}) {
  return (
    <div className="field">
      <label>
        {field.label}
        {field.required && <span style={{ color: "var(--bad, #e5534b)" }}> *</span>}
      </label>
      {field.type === "text" || field.type === "path" ? (
        <input
          type="text"
          value={value ?? ""}
          placeholder={field.placeholder || ""}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : field.type === "number" ? (
        <input
          type="number"
          value={value ?? ""}
          min={field.min}
          max={field.max}
          step={field.step ?? "any"}
          placeholder={field.placeholder || ""}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
          style={{ width: 180 }}
        />
      ) : field.type === "textarea" ? (
        <textarea
          rows={3}
          value={value ?? ""}
          placeholder={field.placeholder || ""}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : field.type === "select" ? (
        <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
          {!field.required && <option value="">（未选择）</option>}
          {(field.options || []).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : field.type === "bool" ? (
        <label className="tool-item" style={{ display: "inline-flex", gap: 6 }}>
          <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
          <span className="muted small">启用</span>
        </label>
      ) : field.type === "tags" || field.type === "multiselect" ? (
        <TagsInput value={(value as string[]) || []} onChange={onChange} placeholder={field.placeholder} />
      ) : field.type === "upload" ? (
        <UploadField field={field} onUploaded={onUploaded} />
      ) : null}
      {field.help && <p className="muted small">{field.help}</p>}
    </div>
  );
}

export function RegisterTask({
  onDone,
  onCancel,
}: {
  onDone: (task: BenchmarkTask) => void;
  onCancel: () => void;
}) {
  const [specs, setSpecs] = useState<TaskTypeSpec[]>([]);
  const [spec, setSpec] = useState<TaskTypeSpec | null>(null);
  const [values, setValues] = useState<Record<string, any>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Live server-side validation (dataset format / metric match), debounced.
  const [validateErrors, setValidateErrors] = useState<string[]>([]);
  const [validating, setValidating] = useState(false);
  const [hasValidated, setHasValidated] = useState(false);

  useEffect(() => {
    getTaskTypes()
      .then(setSpecs)
      .catch((e) => setError(String(e?.message || e)));
  }, []);

  const allFields = useMemo(
    () => (spec ? [...spec.common_fields, ...spec.fields] : []),
    [spec],
  );

  const pickType = (s: TaskTypeSpec) => {
    setSpec(s);
    setError("");
    setValidateErrors([]);
    setHasValidated(false);
    const init: Record<string, any> = {
      eval_metric: s.default_metric.eval_metric,
      direction: s.default_metric.direction,
    };
    for (const f of [...s.common_fields, ...s.fields]) {
      if (f.default !== undefined && init[f.key] === undefined) init[f.key] = f.default;
    }
    setValues(init);
  };

  // Upload results land in the first path field of the same group (data).
  const uploadTarget = useMemo(() => {
    const p = allFields.find((f) => f.type === "path");
    return p?.key || "data_dir";
  }, [allFields]);

  const missingRequired = useMemo(
    () =>
      allFields.filter((f) => {
        if (!f.required || f.type === "upload") return false;
        const v = values[f.key];
        return v === undefined || v === null || (typeof v === "string" && !v.trim()) ||
          (Array.isArray(v) && v.length === 0);
      }),
    [allFields, values],
  );

  // Debounced live validation against the backend (dataset format + metric match).
  useEffect(() => {
    if (!spec) {
      setValidateErrors([]);
      return;
    }
    if (missingRequired.length > 0) {
      setValidateErrors([]);
      return;
    }
    const handle = setTimeout(async () => {
      setValidating(true);
      try {
        const res = await validateTask(spec.type_id, values);
        setValidateErrors(res.valid ? [] : res.errors);
        setHasValidated(true);
      } catch (e: any) {
        // Network/backend hiccup shouldn't block the form; surface softly.
        setValidateErrors([]);
        if (e) setError(String(e?.message || e));
      } finally {
        setValidating(false);
      }
    }, 400);
    return () => clearTimeout(handle);
  }, [spec, values, missingRequired.length]);

  const submit = async () => {
    if (!spec) return;
    setBusy(true);
    setError("");
    try {
      const task = await registerBenchmarkTask(spec.type_id, values);
      onDone(task);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="new-research">
      <div className="wizard-steps">
        <span className={`step ${!spec ? "active" : ""}`}>1 · 选择任务类型</span>
        <span className="step-arrow">→</span>
        <span className={`step ${spec ? "active" : ""}`}>2 · 填写注册信息</span>
      </div>

      {error && (
        <div className="card error" style={{ marginTop: 10 }}>
          {error}
        </div>
      )}

      {!spec && (
        <div className="card" style={{ marginTop: 12 }}>
          <p className="muted">
            选择要注册的研究任务类型。表单字段由后端任务类型规格驱动，注册后任务会出现在「新建研究」目录
            （分类：自定义注册任务）。<span className="pill ok">可实跑</span> 类型注册后可直接驱动双循环。
          </p>
          <div className="task-grid" style={{ marginTop: 12 }}>
            {specs.map((s) => (
              <button key={s.type_id} type="button" className="task-card" onClick={() => pickType(s)}>
                <div className="row">
                  <strong>
                    {s.icon} {s.label}
                  </strong>
                  {s.executable && <span className="pill ok">可实跑</span>}
                </div>
                <div className="muted mono small">{s.type_id}</div>
                <p className="desc">{s.summary}</p>
                <div className="metrics">
                  <span className="pill accent">默认指标 {s.default_metric.eval_metric}</span>
                  <span className="muted small">harness: {s.harness}</span>
                </div>
              </button>
            ))}
          </div>
          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
            <button className="btn" onClick={onCancel}>
              返回
            </button>
          </div>
        </div>
      )}

      {spec && (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <h2>
                {spec.icon} 注册 · {spec.label}
              </h2>
              <p className="muted" style={{ marginTop: 4 }}>
                {spec.summary}
              </p>
            </div>
            <button className="btn tiny" onClick={() => setSpec(null)}>
              ← 重选类型
            </button>
          </div>

          <div className="config-form" style={{ marginTop: 8 }}>
            {GROUP_ORDER.map((g) => {
              const fields = allFields.filter((f) => f.group === g);
              if (fields.length === 0) return null;
              return (
                <fieldset key={g} className="cfg-block">
                  <legend>{GROUP_LABELS[g]}</legend>
                  {fields.map((f) => (
                    <FieldRow
                      key={f.key}
                      field={f}
                      value={values[f.key]}
                      onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
                      onUploaded={(path) => setValues((prev) => ({ ...prev, [uploadTarget]: path }))}
                    />
                  ))}
                </fieldset>
              );
            })}
          </div>

          {missingRequired.length > 0 && (
            <p className="muted small" style={{ marginTop: 8 }}>
              仍缺必填项：{missingRequired.map((f) => f.label).join("、")}
            </p>
          )}

          {validating && (
            <p className="muted small" style={{ marginTop: 8 }}>正在校验数据集格式与评测指标…</p>
          )}
          {!validating && validateErrors.length > 0 && (
            <div className="card error" style={{ marginTop: 8 }}>
              <b>校验未通过：</b>
              <ul className="tight" style={{ marginTop: 6 }}>
                {validateErrors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </div>
          )}
          {hasValidated && !validating && validateErrors.length === 0 && missingRequired.length === 0 && (
            <p className="muted small ok-text" style={{ marginTop: 8 }}>
              ✓ 已通过数据集格式与评测指标校验
            </p>
          )}

          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end", gap: 10 }}>
            <button className="btn" onClick={onCancel}>
              取消
            </button>
            <button
              className="btn primary"
              disabled={busy || missingRequired.length > 0 || validating || validateErrors.length > 0}
              onClick={submit}
            >
              {busy ? "注册中…" : "注册任务 ✓"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
