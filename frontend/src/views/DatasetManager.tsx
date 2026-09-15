import React, { useCallback, useEffect, useState } from "react";
import {
  DatasetRecord,
  deleteDataset,
  getDatasets,
  registerDataset,
} from "../api/client";
import { useToast } from "../components/Toast";

const MODALITIES = [
  { value: "text", label: "文本" },
  { value: "image", label: "图像" },
  { value: "audio", label: "音频" },
];

const TASK_KINDS = [
  { value: "classification", label: "分类任务（图文音分类）" },
  { value: "llm_generation", label: "大模型回答生成（prompt → 回复）" },
];

const MODALITY_LABEL: Record<string, string> = {
  text: "文本",
  image: "图像",
  audio: "音频",
};

const KIND_LABEL: Record<string, string> = {
  classification: "分类",
  llm_generation: "回答生成",
};

const fmtBytes = (n?: number | null): string => {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
};

const emptyForm = {
  name: "",
  modality: "text",
  task_kind: "classification",
  data_path: "",
  label_file: "",
  label_field: "",
  content_field: "",
  notes: "",
};

/** 数据集管理页：注册/查看自主研究任务的初始数据集（图文音 · 分类 / 大模型回答生成）。 */
export function DatasetManager() {
  const { push } = useToast();
  const [datasets, setDatasets] = useState<DatasetRecord[]>([]);
  const [form, setForm] = useState({ ...emptyForm });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setDatasets(await getDatasets());
      setError("");
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const set = (k: keyof typeof emptyForm) => (e: any) =>
    setForm((prev) => ({ ...prev, [k]: e.target.value }));

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const rec = await registerDataset(form);
      push(`数据集「${rec.name}」注册成功：${rec.num_samples ?? "?"} 条样本`, "ok");
      setForm({ ...emptyForm });
      await load();
      setExpanded(rec.dataset_id);
    } catch (e: any) {
      const detail = e?.message || String(e);
      setError(detail.includes("detail") ? detail : `注册失败：${detail}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await deleteDataset(id);
      push("已删除该数据集登记（磁盘数据不受影响）", "info");
      if (expanded === id) setExpanded(null);
      await load();
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flywheel">
      <div className="card">
        <h2>🗂 数据集管理</h2>
        <p className="muted">
          注册自主研究任务的初始数据集（<b>路径引用，不复制数据</b>）：填入数据源路径与标签文件位置，
          平台自动统计样本量与类别分布。支持 <b>文本 / 图像 / 音频</b> 三类模态，任务类型先支持
          <b> 分类任务</b> 与 <b>大模型回答生成任务</b>。目录型图像/音频数据按子目录名统计类别。
        </p>

        {/* ------------------------- 注册表单 ------------------------- */}
        <h3 style={{ margin: "14px 0 8px", fontSize: 14 }}>注册新数据集</h3>
        <div className="row" style={{ gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="field" style={{ minWidth: 220 }}>
            <label>数据集名称 *</label>
            <input value={form.name} onChange={set("name")} placeholder="例如：中文影评情感二分类" />
          </div>
          <div className="field" style={{ width: 130 }}>
            <label>模态 *</label>
            <select value={form.modality} onChange={set("modality")}>
              {MODALITIES.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ minWidth: 230 }}>
            <label>任务类型 *</label>
            <select value={form.task_kind} onChange={set("task_kind")}>
              {TASK_KINDS.map((k) => (
                <option key={k.value} value={k.value}>{k.label}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="row" style={{ gap: 14, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
          <div className="field" style={{ flex: 1, minWidth: 320 }}>
            <label>数据源路径 *（文件或目录；支持历史注册的旧机器路径自动重映射）</label>
            <input
              className="mono"
              value={form.data_path}
              onChange={set("data_path")}
              placeholder="/abs/path/to/train.csv 或 /abs/path/to/image_dir"
            />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 260 }}>
            <label>标签文件（可选：独立标注文件 csv/jsonl）</label>
            <input className="mono" value={form.label_file} onChange={set("label_file")}
                   placeholder="/abs/path/to/labels.csv" />
          </div>
        </div>
        <div className="row" style={{ gap: 14, flexWrap: "wrap", alignItems: "flex-end", marginTop: 10 }}>
          <div className="field" style={{ width: 180 }}>
            <label>标签字段（默认 label）</label>
            <input value={form.label_field} onChange={set("label_field")} placeholder="label" />
          </div>
          <div className="field" style={{ width: 180 }}>
            <label>内容字段（可选）</label>
            <input
              value={form.content_field}
              onChange={set("content_field")}
              placeholder={form.task_kind === "llm_generation" ? "prompt / instruction" : "text / image_path / audio_path"}
            />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 200 }}>
            <label>备注</label>
            <input value={form.notes} onChange={set("notes")} placeholder="选填" />
          </div>
          <div className="field">
            <label>&nbsp;</label>
            <button className="btn primary" disabled={busy} onClick={submit}>
              {busy ? "注册中…" : "注册数据集"}
            </button>
          </div>
        </div>

        {error && <div className="card error" style={{ marginTop: 10 }}>{error}</div>}
      </div>

      {/* ------------------------- 数据集列表 ------------------------- */}
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>已注册数据集（{datasets.length}）</h2>
          <button className="btn tiny" onClick={load} disabled={loading}>刷新</button>
        </div>

        {datasets.length === 0 ? (
          <div className="muted" style={{ padding: 20, textAlign: "center" }}>
            {loading ? "加载中…" : "还没有注册数据集——用上方表单注册第一个数据集。"}
          </div>
        ) : (
          <table className="lb-table" style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th style={{ width: 36 }}>▸</th>
                <th>名称</th>
                <th style={{ width: 90 }}>模态</th>
                <th style={{ width: 110 }}>任务类型</th>
                <th style={{ width: 100 }}>样本数</th>
                <th style={{ width: 100 }}>体积</th>
                <th style={{ width: 90 }}>类别数</th>
                <th style={{ width: 90 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map((d) => {
                const isOpen = expanded === d.dataset_id;
                const classCount = d.label_stats ? Object.keys(d.label_stats).length : null;
                return (
                  <React.Fragment key={d.dataset_id}>
                    <tr
                      className="rec-row"
                      style={{ cursor: "pointer" }}
                      onClick={() => setExpanded(isOpen ? null : d.dataset_id)}
                      title="点击展开数据集详情"
                    >
                      <td><span className={`chev ${isOpen ? "collapsed" : ""}`}>▾</span></td>
                      <td>
                        <strong>{d.name}</strong>
                        <div className="muted mono small" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                          {d.data_path}
                        </div>
                      </td>
                      <td>{MODALITY_LABEL[d.modality] || d.modality}</td>
                      <td className="muted small">{KIND_LABEL[d.task_kind] || d.task_kind}</td>
                      <td className="mono">{d.num_samples ?? "—"}</td>
                      <td className="muted small">{fmtBytes(d.size_bytes)}</td>
                      <td className="mono small">{classCount ?? "—"}</td>
                      <td>
                        <button
                          className="btn tiny"
                          disabled={busy}
                          onClick={(e) => {
                            e.stopPropagation();
                            remove(d.dataset_id);
                          }}
                          title="仅移除登记，不删除磁盘数据"
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={8} style={{ background: "var(--panel-2)", padding: "12px 16px" }}>
                          <div className="kv">
                            <span className="muted">数据源路径</span>
                            <span className="mono small">{d.data_path}</span>
                          </div>
                          {d.label_file && (
                            <div className="kv">
                              <span className="muted">标签文件</span>
                              <span className="mono small">
                                {d.label_file}
                                {d.label_file_stats && `（${d.label_file_stats.rows} 行）`}
                              </span>
                            </div>
                          )}
                          {d.notes_list?.map((n, i) => (
                            <div key={i} className="muted small" style={{ margin: "4px 0" }}>· {n}</div>
                          ))}
                          {d.notes && <div className="muted small" style={{ margin: "4px 0" }}>备注：{d.notes}</div>}

                          {d.label_stats && (
                            <div style={{ marginTop: 8 }}>
                              <strong style={{ fontSize: 13 }}>类别分布</strong>
                              <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 6 }}>
                                {Object.entries(d.label_stats).map(([k, v]) => (
                                  <span key={k} className="pill gray">
                                    {k}: {v}
                                  </span>
                                ))}
                              </div>
                            </div>
                          )}

                          {d.samples && d.samples.length > 0 && (
                            <div style={{ marginTop: 10 }}>
                              <strong style={{ fontSize: 13 }}>数据样本（前 {d.samples.length} 条）</strong>
                              <table className="lb-table" style={{ marginTop: 4 }}>
                                {d.columns && d.columns.length > 0 && (
                                  <thead>
                                    <tr>{d.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                                  </thead>
                                )}
                                <tbody>
                                  {d.samples.map((row, i) => (
                                    <tr key={i}>
                                      {(row as any[]).map((cell, j) => (
                                        <td key={j} className="mono" style={{
                                          fontSize: 11, maxWidth: 260,
                                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                                        }}>
                                          {String(cell)}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
