import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Download,
  FileSearch,
  FileSpreadsheet,
  FolderOpen,
  History,
  KeyRound,
  Loader2,
  Lock,
  RefreshCw,
  Settings,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

type WorkerResponse =
  | { ok: true; result: any }
  | { ok: false; error: { code: string; message: string } };

type ReferenceCounts = {
  products: number;
  warehouses: number;
  locations: number;
  priceLists: number;
  priceListValues: number;
  validatedInventory: number;
};

type Account = {
  accountName: string;
  region: "euw1" | "use1";
  baseCurrency?: string;
  credentialStatus: string;
  lastValidatedAt?: number;
  lastReferenceSyncAt?: number;
  dataDbPath: string;
  referenceCounts: ReferenceCounts;
};

type PreviewRow = Record<string, unknown>;

const tabs = [
  { id: "accounts", label: "Accounts", icon: KeyRound },
  { id: "tasks", label: "Inventory", icon: FileSpreadsheet },
  { id: "data", label: "Data", icon: Database },
  { id: "history", label: "Job History", icon: History },
  { id: "settings", label: "Settings", icon: Settings },
];

const inventoryHeaders = ["sku", "quantity", "locationName", "costprice", "warehouseId"];

function requestId(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.round(Math.random() * 100000)}`;
}

async function engine(method: string, params: Record<string, unknown> = {}) {
  const response = (await invoke("engine_request", {
    request: { protocolVersion: 1, id: requestId(method), method, params },
  })) as WorkerResponse;
  if (!response.ok) {
    throw new Error(response.error?.message || "Worker request failed");
  }
  return response.result;
}

function errorMessage(error: unknown) {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) {
    return String(error.message);
  }
  return "Action failed";
}

function formatTime(value?: number) {
  return value ? new Date(value * 1000).toLocaleString() : "Not yet";
}

function emptyCounts(): ReferenceCounts {
  return {
    products: 0,
    warehouses: 0,
    locations: 0,
    priceLists: 0,
    priceListValues: 0,
    validatedInventory: 0,
  };
}

export default function App() {
  const [activeTab, setActiveTab] = useState("accounts");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [activeAccountName, setActiveAccountName] = useState("");
  const [form, setForm] = useState({
    accountName: "",
    appRef: "",
    token: "",
    region: "euw1" as "euw1" | "use1",
  });
  const [sourcePath, setSourcePath] = useState("");
  const [allowZeroBlanks, setAllowZeroBlanks] = useState(false);
  const [priceListId, setPriceListId] = useState("");
  const [priceLists, setPriceLists] = useState<{ id: number; name: string }[]>([]);
  const [validation, setValidation] = useState<any>(null);
  const [runPreview, setRunPreview] = useState<any>(null);
  const [liveConfirm, setLiveConfirm] = useState("");
  const [liveProgress, setLiveProgress] = useState<{ completed: number; total: number; done: boolean } | null>(null);
  const [liveBatches, setLiveBatches] = useState<any[]>([]);
  const stopLive = useRef(false);
  const [resultView, setResultView] = useState<"accepted" | "rejected">("accepted");
  const [jobs, setJobs] = useState<any[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const activeAccount = accounts.find((account) => account.accountName === activeAccountName) || null;
  const counts = activeAccount?.referenceCounts || emptyCounts();
  const hasReferences = counts.products > 0 && counts.warehouses > 0 && counts.locations > 0;

  const tableRows = (
    resultView === "accepted" ? validation?.validatedPreview : validation?.rejectedPreview
  ) as PreviewRow[] || [];
  const columns = useMemo(() => {
    const row = tableRows[0];
    return row ? Object.keys(row) : [];
  }, [tableRows]);

  useEffect(() => {
    void refreshAccounts();
  }, []);

  useEffect(() => {
    setPriceListId("");
    setValidation(null);
    setRunPreview(null);
    setLiveConfirm("");
    setLiveProgress(null);
    setLiveBatches([]);
    if (!activeAccountName) {
      setPriceLists([]);
      return;
    }
    let current = true;
    void engine("inventoryPriceLists", { accountName: activeAccountName })
      .then((result) => { if (current) setPriceLists(result.priceLists); })
      .catch((err) => {
        if (current) {
          setPriceLists([]);
          setError(`Could not load price lists: ${errorMessage(err)}`);
        }
      });
    void engine("inventoryLiveStatus", { accountName: activeAccountName })
      .then((result) => { if (current) setLiveBatches(result.batches); })
      .catch(() => { if (current) setLiveBatches([]); });
    void engine("inventoryRunResume", { accountName: activeAccountName })
      .then((result) => {
        if (current && result.preview) {
          setRunPreview(result.preview);
          setLiveProgress({ completed: result.completedBatches, total: result.preview.batches, done: false });
        }
      })
      .catch((err) => { if (current) setError(`Could not restore live run: ${errorMessage(err)}`); });
    return () => { current = false; };
  }, [activeAccountName]);

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function refreshAccounts(selectName?: string) {
    const result = await engine("listAccounts");
    const loaded = result.accounts as Account[];
    setAccounts(loaded);
    const next = selectName || activeAccountName || loaded[0]?.accountName || "";
    setActiveAccountName(next);
  }

  async function saveAccount() {
    await run("saveAccount", async () => {
      const result = await engine("saveAccount", form);
      await refreshAccounts(result.account.accountName);
      setMessage("Account saved locally. Credentials are stored outside the repository.");
    });
  }

  async function validateAccount() {
    if (!activeAccountName) return;
    await run("validateAccount", async () => {
      const result = await engine("validateAccount", { accountName: activeAccountName });
      await refreshAccounts(result.account.accountName);
      setMessage(`Credentials verified. Base currency: ${result.baseCurrency || "not returned"}.`);
    });
  }

  async function syncReferences() {
    if (!activeAccountName) return;
    await run("syncReferences", async () => {
      const result = await engine("syncInventoryReferences", { accountName: activeAccountName });
      setValidation(null);
      setRunPreview(null);
      await refreshAccounts(result.account.accountName);
      const lists = await engine("inventoryPriceLists", { accountName: activeAccountName });
      setPriceLists(lists.priceLists);
      setMessage(
        `Synced products ${result.results.products}, warehouses ${result.results.warehouses}, locations ${result.results.locations}, price values ${result.results.priceListValues}.`
      );
    });
  }

  async function chooseSource() {
    const selected = await open({
      multiple: false,
      filters: [{ name: "Inventory source", extensions: ["csv", "xlsx"] }],
    });
    if (typeof selected === "string") {
      setSourcePath(selected);
      setValidation(null);
      setRunPreview(null);
    }
  }

  async function validateSource() {
    if (!activeAccountName || !sourcePath) return;
    await run("validateSource", async () => {
      const result = await engine("validateInventoryFile", {
        accountName: activeAccountName,
        path: sourcePath,
        allowZeroBlanks,
        priceListId: priceListId ? Number(priceListId) : null,
      });
      setValidation(result);
      setRunPreview(null);
      setResultView(result.inserted > 0 ? "accepted" : "rejected");
      await refreshAccounts(result.account.accountName);
      setActiveTab("data");
      setMessage(`Validation complete: ${result.inserted} accepted, ${result.rejected} rejected for ${activeAccountName}.`);
    });
  }

  async function previewRun() {
    if (!activeAccountName || !validation?.validationJobId) return;
    await run("previewRun", async () => {
      const result = await engine("previewInventoryRun", {
        accountName: activeAccountName,
        validationJobId: validation.validationJobId,
      });
      setRunPreview(result);
      setLiveConfirm("");
      setLiveProgress(null);
      setActiveTab("data");
      setMessage(`Dry run prepared ${result.corrections} corrections in ${result.batches} batches. Nothing was sent to Brightpearl.`);
    });
  }

  async function saveRunPreview() {
    if (!runPreview?.reportPath || !activeAccountName) return;
    const destination = await save({
      defaultPath: runPreview.reportFileName || "inventory-run-preview.jsonl",
      filters: [{ name: "JSONL payload report", extensions: ["jsonl"] }],
    });
    if (typeof destination !== "string") return;
    await run("saveRunPreview", async () => {
      const result = await engine("saveInventoryRunPreview", {
        accountName: activeAccountName,
        reportPath: runPreview.reportPath,
        reportSha256: runPreview.reportSha256,
        destination,
      });
      setMessage(`Dry-run payload report saved to ${result.path}.`);
    });
  }

  async function runLive() {
    if (!runPreview || !activeAccountName || liveConfirm !== activeAccountName) return;
    const accountName = activeAccountName;
    const preview = runPreview;
    stopLive.current = false;
    await run("runLive", async () => {
      try {
        for (;;) {
          if (stopLive.current) {
            setMessage(`Live run paused for ${accountName}. Resume to send remaining batches.`);
            break;
          }
          const result = await engine("runInventoryBatch", {
            accountName,
            confirmAccountName: liveConfirm,
            validationJobId: preview.validationJobId,
            previewJobId: preview.previewJobId,
            reportSha256: preview.reportSha256,
          });
          setLiveProgress({ completed: result.completedBatches, total: result.totalBatches, done: result.done });
          if (result.done) {
            setMessage(`Live run complete for ${accountName}: ${result.completedBatches} batches confirmed by Brightpearl.`);
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, result.waitMs));
        }
      } finally {
        const status = await engine("inventoryLiveStatus", { accountName });
        setLiveBatches(status.batches);
      }
    });
  }

  async function saveExceptionReport() {
    if (!validation?.exceptionReportPath || !activeAccountName) return;
    const destination = await save({
      defaultPath: validation.exceptionReportFileName || "inventory-exceptions.csv",
      filters: [{ name: "CSV report", extensions: ["csv"] }],
    });
    if (typeof destination !== "string") return;
    await run("saveExceptionReport", async () => {
      const result = await engine("saveInventoryExceptionReport", {
        accountName: activeAccountName,
        reportPath: validation.exceptionReportPath,
        destination,
      });
      setMessage(`Exception report saved to ${result.path}.`);
    });
  }

  async function refreshHistory() {
    await run("history", async () => {
      const result = await engine("jobHistory");
      setJobs(result.jobs);
    });
  }

  return (
    <main className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brandMark">T</span>
          <div>
            <strong>TACOS</strong>
            <span>Inventory prototype</span>
          </div>
        </div>
        <label className="sideLabel" htmlFor="activeAccount">Active account</label>
        <select id="activeAccount" value={activeAccountName} disabled={busy === "runLive"} onChange={(event) => setActiveAccountName(event.target.value)}>
          <option value="">No account</option>
          {accounts.map((account) => (
            <option key={account.accountName} value={account.accountName}>{account.accountName}</option>
          ))}
        </select>
        <nav>
          {tabs.map((tab) => {
            const Icon = tab.icon;
            return (
              <button
                key={tab.id}
                className={activeTab === tab.id ? "active" : ""}
                disabled={busy === "runLive"}
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
            <p>Account-bound Brightpearl sync, local validation and confirmed stock corrections.</p>
          </div>
          <span className="account"><Lock size={14} /> {activeAccountName || "No active account"}</span>
        </header>

        {error && <div className="notice error"><AlertTriangle size={18} />{error}</div>}
        {message && <div className="notice"><CheckCircle2 size={18} />{message}</div>}

        {activeTab === "accounts" && (
          <section className="panel accountPanel">
            <div>
              <h2>Register Brightpearl Account</h2>
              <div className="formGrid">
                <label>
                  Account name
                  <input value={form.accountName} onChange={(event) => setForm({ ...form, accountName: event.target.value })} />
                </label>
                <label>
                  Region
                  <select value={form.region} onChange={(event) => setForm({ ...form, region: event.target.value as "euw1" | "use1" })}>
                    <option value="euw1">euw1</option>
                    <option value="use1">use1</option>
                  </select>
                </label>
                <label>
                  App ref
                  <input value={form.appRef} onChange={(event) => setForm({ ...form, appRef: event.target.value })} />
                </label>
                <label>
                  Account token
                  <input type="password" value={form.token} onChange={(event) => setForm({ ...form, token: event.target.value })} />
                </label>
              </div>
              <div className="toolbar">
                <button onClick={saveAccount} disabled={!!busy}>
                  {busy === "saveAccount" ? <Loader2 className="spin" size={18} /> : <KeyRound size={18} />}
                  Save account
                </button>
                <button onClick={validateAccount} disabled={!!busy || !activeAccountName}>
                  {busy === "validateAccount" ? <Loader2 className="spin" size={18} /> : <CheckCircle2 size={18} />}
                  Check credentials
                </button>
              </div>
            </div>
            <div className="accountSnapshot">
              <h2>Active Account Status</h2>
              <dl>
                <dt>Account</dt><dd>{activeAccount?.accountName || "None"}</dd>
                <dt>Region</dt><dd>{activeAccount?.region || "-"}</dd>
                <dt>Credential</dt><dd>{activeAccount?.credentialStatus || "-"}</dd>
                <dt>Base currency</dt><dd>{activeAccount?.baseCurrency || "-"}</dd>
                <dt>Credential check</dt><dd>{formatTime(activeAccount?.lastValidatedAt)}</dd>
                <dt>Reference sync</dt><dd>{formatTime(activeAccount?.lastReferenceSyncAt)}</dd>
              </dl>
            </div>
          </section>
        )}

        {activeTab === "tasks" && (
          <div className="taskFlow">
            <section className="step stepAccount">
              <div className="stepHeading"><span className="stepIndex">1</span><h2>Account</h2></div>
              <p>{activeAccount ? `${activeAccount.accountName} is selected.` : "Register and select an account first."}</p>
            </section>
            <section className="step stepRefs">
              <div className="stepHeading"><span className="stepIndex">2</span><h2>References</h2></div>
              <div className="counts">
                {Object.entries(counts).map(([key, value]) => (
                  <span key={key}>{key}: <strong>{value}</strong></span>
                ))}
              </div>
              <button onClick={syncReferences} disabled={!!busy || !activeAccountName}>
                {busy === "syncReferences" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
                Sync required data
              </button>
            </section>
            <section className="step stepSource">
              <div className="stepHeading"><span className="stepIndex">3</span><h2>Source</h2></div>
              <p>Expected columns: {priceListId ? "sku, quantity, locationName, warehouseId" : inventoryHeaders.join(", ")}. Costprice is optional when using a synced price list.</p>
              <div className="sourceRow">
                <input value={sourcePath} onChange={(event) => { setSourcePath(event.target.value); setValidation(null); setRunPreview(null); }} placeholder="Choose CSV/XLSX source" />
                <button onClick={chooseSource}>
                  <FolderOpen size={18} />
                  Select
                </button>
              </div>
              <div className="formGrid">
                <label>
                  Cost source
                  <select value={priceListId} onChange={(event) => { setPriceListId(event.target.value); setValidation(null); setRunPreview(null); }}>
                    <option value="">Import file (costprice column)</option>
                    {priceLists.map((list) => <option key={list.id} value={list.id}>{list.name} ({list.id})</option>)}
                  </select>
                </label>
                <label className="checkboxLabel">
                  <input type="checkbox" checked={allowZeroBlanks} onChange={(event) => { setAllowZeroBlanks(event.target.checked); setValidation(null); setRunPreview(null); }} />
                  Allow zero and blank quantity/cost
                </label>
              </div>
              <p>{priceListId ? "Cost comes from the selected account's synced price list; costprice in the file is ignored. Missing or blank list values become zero when allowed." : "Cost comes from the import file. Blank quantity or cost becomes zero when allowed."}</p>
            </section>
            <section className="step stepValidate">
              <div className="stepHeading"><span className="stepIndex">4</span><h2>Validate</h2></div>
              <p>{hasReferences ? "References are available for local enrichment." : "Sync products, warehouses and locations before validation."}</p>
              <button onClick={validateSource} disabled={!!busy || !activeAccountName || !sourcePath || !hasReferences}>
                {busy === "validateSource" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Validate source
              </button>
            </section>
            <section className="step stepPreview">
              <div className="stepHeading"><span className="stepIndex">5</span><h2>Preview</h2></div>
              <p>{validation ? `${validation.inserted} row(s) staged in validated_inventory.` : "Validation preview appears after source validation."}</p>
            </section>
            <section className="step stepRun">
              <div className="stepHeading"><span className="stepIndex">6</span><h2>Run</h2></div>
              <button onClick={previewRun} disabled={!!busy || !validation?.inserted}>
                {busy === "previewRun" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Dry run
              </button>
              <p>Review the dry-run payload in Data before confirming a live run.</p>
            </section>
          </div>
        )}

        {activeTab === "data" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>Inventory validation results</h2>
                <p>{validation ? `${validation.totalRows} source row(s) checked against ${activeAccountName}'s synced references.` : "Validate an inventory source to see accepted and rejected rows."}</p>
              </div>
              {validation && (
                <div className="resultCounts">
                  <span className="acceptedCount">Accepted <strong>{validation.inserted}</strong></span>
                  <span className="rejectedCount">Rejected <strong>{validation.rejected}</strong></span>
                  {validation.rejected > 0 && (
                    <button onClick={saveExceptionReport} disabled={!!busy}>
                      {busy === "saveExceptionReport" ? <Loader2 className="spin" size={18} /> : <Download size={18} />}
                      Save exception report
                    </button>
                  )}
                </div>
              )}
            </div>
            {validation && (
              <div className="resultTabs" role="tablist" aria-label="Validation result rows">
                <button className={resultView === "accepted" ? "active" : ""} onClick={() => setResultView("accepted")}>Accepted</button>
                <button className={resultView === "rejected" ? "active" : ""} onClick={() => setResultView("rejected")}>Rejected</button>
              </div>
            )}
            <div className="tableWrap">
              <table>
                <thead>
                  <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
                </thead>
                <tbody>
                  {tableRows.map((row, index) => (
                    <tr key={`${row.rowNumber || row.sku}-${index}`}>
                      {columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
              {!validation && <p className="empty">No validation results yet.</p>}
              {validation && !tableRows.length && <p className="empty">No {resultView} rows.</p>}
            </div>
            {validation && <p className="meta">Showing up to 50 accepted rows and 100 rejected rows.</p>}
            {runPreview && (
              <div className="runPreview">
                <div className="resultHeader">
                  <div>
                    <h2>Dry-run payload</h2>
                    <p>{runPreview.corrections} corrections in {runPreview.batches} batches across {runPreview.warehouses} warehouses. Currency: {runPreview.currency}.</p>
                  </div>
                  <button onClick={saveRunPreview} disabled={!!busy}>
                    <Download size={18} /> Save all payloads
                  </button>
                </div>
                <p className="meta">First {runPreview.samplePayload?.corrections?.length || 0} corrections from the first batch. The saved JSONL contains every full payload. SHA-256: <code>{runPreview.reportSha256}</code></p>
                <div className="liveControls">
                  <p>Quantities are <strong>additive adjustments</strong>, not target stock levels. Confirm <strong>{activeAccountName}</strong> to send all {runPreview.corrections} corrections. An uncertain batch stops the run for reconciliation.</p>
                  <input aria-label="Confirm account name for live run" placeholder="Type account name" value={liveConfirm} disabled={!!busy || !!liveProgress?.done} onChange={(event) => setLiveConfirm(event.target.value)} />
                  <button onClick={runLive} disabled={!!busy || !!liveProgress?.done || liveConfirm !== activeAccountName}>
                    {busy === "runLive" ? <Loader2 className="spin" size={18} /> : <CheckCircle2 size={18} />}
                    {liveProgress?.completed ? "Resume live run" : "Run live"}
                  </button>
                  {busy === "runLive" && <button onClick={() => { stopLive.current = true; }}><AlertTriangle size={18} /> Stop after batch</button>}
                  {liveProgress && <span>{liveProgress.completed} of {liveProgress.total} batches confirmed</span>}
                </div>
                {liveBatches.length > 0 && (
                  <div className="liveBatchStatus">
                    <strong>Recent live batches</strong>
                    {liveBatches.slice(0, 10).map((batch) => (
                      <div key={`${batch.batchIndex}-${batch.updatedAt}`}>
                        Batch {batch.batchIndex}, warehouse {batch.warehouseId}: {batch.state}, {batch.rowCount} rows
                        {batch.goodsNoteIds.length > 0 && `, notes ${batch.goodsNoteIds.join(", ")}`}
                      </div>
                    ))}
                  </div>
                )}
                <pre>{JSON.stringify(runPreview.samplePayload, null, 2)}</pre>
              </div>
            )}
          </section>
        )}

        {activeTab === "history" && (
          <section className="panel">
            <div className="toolbar">
              <History size={18} />
              <button onClick={refreshHistory} disabled={!!busy}>
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
              <p>Each account has its own local data database, bound by account name.</p>
            </div>
            <div>
              <h2>Credentials</h2>
              <p>Windows builds store API credentials outside Git and outside synced folders.</p>
            </div>
            <div>
              <h2>Safety</h2>
              <p>Reference sync uses Brightpearl reads; stock-correction writes remain disabled.</p>
            </div>
          </section>
        )}
      </section>
    </main>
  );
}
