import { invoke } from "@tauri-apps/api/core";
import {
  AlertTriangle,
  Database,
  FileSpreadsheet,
  History,
  Loader2,
  Lock,
  Play,
  RefreshCw,
  Settings,
  Table2,
  UploadCloud,
} from "lucide-react";
import { useMemo, useState } from "react";

type WorkerResponse =
  | { ok: true; result: any }
  | { ok: false; error: { code: string; message: string } };

type PreviewRow = Record<string, string | number | null>;

const tabs = [
  { id: "tasks", label: "Tasks", icon: FileSpreadsheet },
  { id: "data", label: "Data", icon: Database },
  { id: "history", label: "Job History", icon: History },
  { id: "settings", label: "Settings", icon: Settings },
];

function requestId(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.round(Math.random() * 100000)}`;
}

async function engine(method: string, params: Record<string, unknown> = {}) {
  const response = (await invoke("engine_request", {
    request: { protocolVersion: 1, id: requestId(method), method, params },
  })) as WorkerResponse;
  if (!response.ok) {
    throw new Error(response.error.message);
  }
  return response.result;
}

export default function App() {
  const [activeTab, setActiveTab] = useState("tasks");
  const [sourcePath, setSourcePath] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [filter, setFilter] = useState("");
  const [sortKey, setSortKey] = useState("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [importResult, setImportResult] = useState<any>(null);
  const [preview, setPreview] = useState<{ totalRows: number; rows: PreviewRow[] } | null>(null);
  const [jobs, setJobs] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const columns = useMemo(() => {
    const row = preview?.rows?.[0];
    return row ? Object.keys(row) : [];
  }, [preview]);

  async function runImport() {
    setBusy(true);
    setError("");
    try {
      const result = await engine("importSyntheticCsv", { path: sourcePath });
      setImportResult(result);
      setDatasetId(result.datasetId);
      const page = await engine("previewDataset", { datasetId: result.datasetId, pageSize: 50 });
      setPreview(page);
      setActiveTab("data");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setBusy(false);
    }
  }

  async function refreshPreview() {
    if (!datasetId) return;
    setBusy(true);
    setError("");
    try {
      setPreview(await engine("previewDataset", { datasetId, pageSize: 50, filter, sortKey, sortDir }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setBusy(false);
    }
  }

  async function refreshHistory() {
    setBusy(true);
    setError("");
    try {
      const result = await engine("jobHistory");
      setJobs(result.jobs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load job history");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brandMark">T</span>
          <div>
            <strong>TACOS</strong>
            <span>Installed prototype</span>
          </div>
        </div>
        <nav>
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                className={activeTab === tab.id ? "active" : ""}
                onClick={() => {
                  setActiveTab(tab.id);
                  if (tab.id === "history") void refreshHistory();
                }}
              >
                <Icon size={18} />
                {tab.label}
              </button>
            );
          })}
        </nav>
      </aside>

      <section className="workspace">
        <header>
          <div>
            <h1>Inventory Import</h1>
            <p>Local CSV pilot with private Python worker, SQLite ledger, and DuckDB paging.</p>
          </div>
          <span className="account"><Lock size={14} /> Local only</span>
        </header>

        {error && (
          <div className="notice error">
            <AlertTriangle size={18} />
            {error}
          </div>
        )}

        {activeTab === "tasks" && (
          <div className="taskFlow">
            {["Source", "Mapping", "Validation", "Preview", "Run", "Results"].map((step, index) => (
              <section className="step" key={step}>
                <div className="stepIndex">{index + 1}</div>
                <h2>{step}</h2>
                {index === 0 && (
                  <>
                    <label htmlFor="sourcePath">Local synthetic CSV path</label>
                    <div className="sourceRow">
                      <input
                        id="sourcePath"
                        value={sourcePath}
                        onChange={(event) => setSourcePath(event.target.value)}
                        placeholder="C:\\data\\synthetic-inventory.csv"
                      />
                      <button onClick={runImport} disabled={busy || !sourcePath.trim()}>
                        {busy ? <Loader2 className="spin" size={18} /> : <Play size={18} />}
                        Import
                      </button>
                    </div>
                  </>
                )}
                {index === 1 && <p>Saved mapping controls are stubbed for the phase-1 prototype.</p>}
                {index === 2 && <p>Malformed CSV rows are retained as source warnings, not discarded.</p>}
                {index === 3 && <p>{datasetId ? `Dataset ${datasetId} is ready for local preview.` : "Import a source file to enable preview."}</p>}
                {index === 4 && (
                  <button className="disabledAction" disabled>
                    <UploadCloud size={18} />
                    Brightpearl writes disabled
                  </button>
                )}
                {index === 5 && importResult && (
                  <dl>
                    <dt>Rows read</dt>
                    <dd>{importResult.rowsRead}</dd>
                    <dt>Warnings</dt>
                    <dd>{importResult.warningCount}</dd>
                  </dl>
                )}
              </section>
            ))}
          </div>
        )}

        {activeTab === "data" && (
          <section className="panel">
            <div className="toolbar">
              <Table2 size={18} />
              <input value={datasetId} onChange={(event) => setDatasetId(event.target.value)} placeholder="Dataset id" />
              <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter rows" />
              <input value={sortKey} onChange={(event) => setSortKey(event.target.value)} placeholder="Sort column" />
              <select value={sortDir} onChange={(event) => setSortDir(event.target.value as "asc" | "desc")}>
                <option value="asc">Ascending</option>
                <option value="desc">Descending</option>
              </select>
              <button onClick={refreshPreview} disabled={busy || !datasetId}>
                <RefreshCw size={18} />
                Refresh
              </button>
            </div>
            <div className="tableWrap">
              <table>
                <thead>
                  <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
                </thead>
                <tbody>
                  {preview?.rows.map((row, index) => (
                    <tr key={`${row.rowNumber}-${index}`}>
                      {columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
              {!preview && <p className="empty">Import a synthetic CSV to preview data.</p>}
            </div>
            {preview && <p className="meta">{preview.totalRows} matching rows, first page shown.</p>}
          </section>
        )}

        {activeTab === "history" && (
          <section className="panel">
            <div className="toolbar">
              <History size={18} />
              <button onClick={refreshHistory} disabled={busy}>
                <RefreshCw size={18} />
                Refresh
              </button>
            </div>
            {jobs.map((job) => (
              <article className="job" key={job.id}>
                <strong>{job.kind}</strong>
                <span>{job.state}</span>
                <code>{job.dataset_id || job.source_path}</code>
              </article>
            ))}
            {!jobs.length && <p className="empty">No local jobs recorded yet.</p>}
          </section>
        )}

        {activeTab === "settings" && (
          <section className="panel settingsGrid">
            <div>
              <h2>Storage</h2>
              <p>Runtime databases, logs, temp files and credentials stay in local application storage.</p>
            </div>
            <div>
              <h2>Updates</h2>
              <p>SharePoint distribution is manual download/run only; the app performs no Graph or hosted update checks.</p>
            </div>
            <div>
              <h2>WebView2</h2>
              <p>The installer must detect WebView2 and document Microsoft’s offline prerequisite option.</p>
            </div>
          </section>
        )}
      </section>
    </main>
  );
}

