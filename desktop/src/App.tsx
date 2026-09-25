import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open, save } from "@tauri-apps/plugin-dialog";
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Download,
  FileText,
  FileSearch,
  FileSpreadsheet,
  FolderOpen,
  History,
  Info,
  KeyRound,
  Loader2,
  Lock,
  RefreshCw,
  Settings,
  ShoppingCart,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

type WorkerResponse =
  | { ok: true; result: any }
  | { ok: false; error: { code: string; message: string; guidance?: string } };

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

type AppSettings = Record<string, string | number>;
type ProgressState = { percent: number; completed?: number; total?: number; message?: string };

type LegacyOperation = {
  id: string;
  label: string;
  category: string;
  legacyView: string;
  status: "active" | "foundation";
  legacyModules: string[];
  apiFamilies: string[];
  workflow: string[];
};

const navGroups = [
  {
    label: "File",
    tabs: [
      { id: "accounts", label: "Accounts", icon: KeyRound },
    ],
  },
  {
    label: "Go Live Tools",
    tabs: [
      { id: "tasks", label: "Inventory Import", icon: FileSpreadsheet },
      { id: "openSales", label: "Open Sales", icon: ShoppingCart },
      { id: "openPurchases", label: "Open Purchases", icon: ShoppingCart },
    ],
  },
  {
    label: "Review",
    tabs: [
      { id: "data", label: "Data", icon: Database },
      { id: "history", label: "Job History", icon: History },
      { id: "logs", label: "Logs", icon: FileText },
      { id: "legacy", label: "Legacy Workbench", icon: CheckCircle2 },
    ],
  },
  {
    label: "Settings",
    tabs: [
      { id: "settings", label: "Global Settings", icon: Settings },
      { id: "help", label: "Help", icon: Info },
    ],
  },
];

const headerDescriptions: Record<string, string> = {
  File: "Account registration, credential checks and account removal.",
  "Go Live Tools": "Migration workflows for reference sync, validation, previews and confirmed Brightpearl writes.",
  Review: "Validation results, job history, logs and migration parity status.",
  Settings: "Application settings, environment details and support information.",
};

const inventoryHeaders = ["sku", "quantity", "locationName", "costprice", "warehouseId"];

function requestId(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.round(Math.random() * 100000)}`;
}

async function engine(method: string, params: Record<string, unknown> = {}, explicitRequestId?: string) {
  const response = (await invoke("engine_request", {
    request: { protocolVersion: 1, id: explicitRequestId || requestId(method), method, params },
  })) as WorkerResponse;
  if (!response.ok) {
    const detail = response.error?.message || "Worker request failed";
    throw new Error(response.error?.guidance ? `${detail} ${response.error.guidance}` : detail);
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

function settingText(settings: AppSettings | null, key: string, fallback: string) {
  const value = settings?.[key];
  return typeof value === "string" && value ? value : fallback;
}

function appearanceClass(settings: AppSettings | null) {
  const mode = settingText(settings, "appearance_mode", "System").toLowerCase();
  const theme = settingText(settings, "appearance_theme", "Sage").toLowerCase();
  const safeMode = ["system", "light", "dark"].includes(mode) ? mode : "system";
  const safeTheme = theme === "brightpearl" ? "brightpearl" : "sage";
  return `app appearance-${safeMode} theme-${safeTheme}`;
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
  const [syncProgress, setSyncProgress] = useState<ProgressState | null>(null);
  const [inventoryReferenceActiveRequestId, setInventoryReferenceActiveRequestId] = useState("");
  const inventoryReferenceRequestId = useRef<string | null>(null);
  const [legacyOperations, setLegacyOperations] = useState<LegacyOperation[]>([]);
  const [legacyCategories, setLegacyCategories] = useState<string[]>([]);
  const [appSettings, setAppSettings] = useState<AppSettings | null>(null);
  const [settingsPath, setSettingsPath] = useState("");
  const [logs, setLogs] = useState<any[]>([]);
  const [selectedLogId, setSelectedLogId] = useState("sync");
  const [logContent, setLogContent] = useState("");
  const [logPath, setLogPath] = useState("");
  const [logTruncated, setLogTruncated] = useState(false);
  const [environment, setEnvironment] = useState<any>(null);
  const [openSalesPath, setOpenSalesPath] = useState("");
  const [openSalesValidation, setOpenSalesValidation] = useState<any>(null);
  const [openSalesPreview, setOpenSalesPreview] = useState<any>(null);
  const [openSalesConfirm, setOpenSalesConfirm] = useState("");
  const [openSalesLiveStatus, setOpenSalesLiveStatus] = useState<any>(null);
  const [openSalesProgress, setOpenSalesProgress] = useState<ProgressState | null>(null);
  const [openSalesActiveRequestId, setOpenSalesActiveRequestId] = useState("");
  const openSalesRequestId = useRef<string | null>(null);
  const cancelOpenSalesRequested = useRef(false);
  const [openPurchasesPath, setOpenPurchasesPath] = useState("");
  const [openPurchasesValidation, setOpenPurchasesValidation] = useState<any>(null);
  const [openPurchasesPreview, setOpenPurchasesPreview] = useState<any>(null);
  const [openPurchasesConfirm, setOpenPurchasesConfirm] = useState("");
  const [openPurchasesLiveStatus, setOpenPurchasesLiveStatus] = useState<any>(null);
  const [openPurchasesProgress, setOpenPurchasesProgress] = useState<ProgressState | null>(null);
  const [openPurchasesActiveRequestId, setOpenPurchasesActiveRequestId] = useState("");
  const openPurchasesRequestId = useRef<string | null>(null);
  const cancelOpenPurchasesRequested = useRef(false);

  const activeAccount = accounts.find((account) => account.accountName === activeAccountName) || null;
  const counts = activeAccount?.referenceCounts || emptyCounts();
  const hasReferences = counts.products > 0 && counts.warehouses > 0 && counts.locations > 0;
  const activeNavGroup = navGroups.find((group) => group.tabs.some((tab) => tab.id === activeTab));
  const activeHeaderTitle = activeNavGroup?.label || "TACOS";
  const activeHeaderDescription = headerDescriptions[activeHeaderTitle] || "";

  const tableRows = (
    resultView === "accepted" ? validation?.validatedPreview : validation?.rejectedPreview
  ) as PreviewRow[] || [];
  const columns = useMemo(() => {
    const row = tableRows[0];
    return row ? Object.keys(row) : [];
  }, [tableRows]);
  const openSalesProgressMessage = String(openSalesProgress?.message || "");
  const showOpenSalesReferenceProgress = !!openSalesProgress && (
    busy.startsWith("syncOpenSales")
    || /Open Sales reference|contact references|product references|reference data/i.test(openSalesProgressMessage)
  );
  const showOpenSalesLiveProgress = !!openSalesProgress && !showOpenSalesReferenceProgress && (
    busy === "runOpenSales"
    || /Open Sales sync|Confirmed|Stopped at first|Cancellation|cancelled/i.test(openSalesProgressMessage)
  );
  const openPurchasesProgressMessage = String(openPurchasesProgress?.message || "");
  const showOpenPurchasesReferenceProgress = !!openPurchasesProgress && (
    busy.startsWith("syncOpenPurchases")
    || /Open Purchases reference|supplier contacts|product references|reference data/i.test(openPurchasesProgressMessage)
  );
  const showOpenPurchasesLiveProgress = !!openPurchasesProgress && !showOpenPurchasesReferenceProgress && (
    busy === "runOpenPurchases"
    || /Open Purchases sync|Confirmed|Stopped at first|Cancellation|cancelled/i.test(openPurchasesProgressMessage)
  );

  useEffect(() => {
    void refreshAccounts();
    void engine("legacyOperations")
      .then((result) => {
        setLegacyOperations(result.operations || []);
        setLegacyCategories(result.categories || []);
      })
      .catch((err) => setError(`Could not load legacy tool registry: ${errorMessage(err)}`));
    void loadAppSettings();
    void loadEnvironment();
    const unlisten = listen<any>("reference-sync-progress", ({ payload: data }) => {
      if (data?.operation !== "reference_sync") return;
      setSyncProgress((current) => ({
        percent: typeof data.percent === "number" ? data.percent : current?.percent || 0,
        completed: data.completed ?? current?.completed,
        total: data.total ?? current?.total,
        message: data.message || current?.message,
      }));
    });
    const unlistenEngine = listen<any>("engine-event", ({ payload }) => {
      const data = payload?.data;
      const operation = String(data?.operation || "");
      if (operation.startsWith("open_sales_")) {
        setOpenSalesProgress((current) => ({
          percent: typeof data.percent === "number" ? data.percent : current?.percent || 0,
          completed: data.completed ?? current?.completed,
          total: data.total ?? current?.total,
          message: data.message || current?.message,
        }));
      }
      if (operation.startsWith("open_purchases_")) {
        setOpenPurchasesProgress((current) => ({
        percent: typeof data.percent === "number" ? data.percent : current?.percent || 0,
        completed: data.completed ?? current?.completed,
        total: data.total ?? current?.total,
        message: data.message || current?.message,
        }));
      }
    });
    return () => {
      void unlisten.then((stop) => stop());
      void unlistenEngine.then((stop) => stop());
    };
  }, []);

  useEffect(() => {
    setPriceListId("");
    setValidation(null);
    setRunPreview(null);
    setOpenSalesValidation(null);
    setOpenSalesPreview(null);
    setOpenSalesConfirm("");
    setOpenSalesLiveStatus(null);
    setOpenSalesProgress(null);
    setInventoryReferenceActiveRequestId("");
    setOpenPurchasesValidation(null);
    setOpenPurchasesPreview(null);
    setOpenPurchasesConfirm("");
    setOpenPurchasesLiveStatus(null);
    setOpenPurchasesProgress(null);
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
    void engine("openSalesLiveStatus", { accountName: activeAccountName })
      .then((result) => { if (current) setOpenSalesLiveStatus(result); })
      .catch(() => { if (current) setOpenSalesLiveStatus(null); });
    void engine("openPurchasesLiveStatus", { accountName: activeAccountName })
      .then((result) => { if (current) setOpenPurchasesLiveStatus(result); })
      .catch(() => { if (current) setOpenPurchasesLiveStatus(null); });
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

  async function removeAccount() {
    if (!activeAccountName) return;
    const accountName = activeAccountName;
    const confirmation = window.prompt(
      `Type ${accountName} to permanently remove it. Credentials and the local data file will be deleted.`,
    );
    if (confirmation === null) return;
    await run("removeAccount", async () => {
      await engine("removeAccount", { accountName, confirmAccountName: confirmation });
      setActiveAccountName("");
      setForm({ accountName: "", appRef: "", token: "", region: "euw1" });
      await refreshAccounts();
      setMessage(`${accountName} removed. Credentials and local data file were deleted.`);
    });
  }

  async function syncReferences(mode: "all" | "products" | "warehouses" | "locations" | "priceLists" = "all") {
    if (!activeAccountName) return;
    const labels = {
      all: "all required data",
      products: "products",
      warehouses: "warehouses",
      locations: "locations",
      priceLists: "price lists",
    };
    setSyncProgress({ percent: 0, message: `Starting ${labels[mode]} sync...` });
    await run(`syncReferences-${mode}`, async () => {
      const activeId = requestId(`syncInventoryReferences-${mode}`);
      inventoryReferenceRequestId.current = activeId;
      setInventoryReferenceActiveRequestId(activeId);
      let result: any;
      try {
        result = await engine("syncInventoryReferences", { accountName: activeAccountName, mode }, activeId);
      } finally {
        inventoryReferenceRequestId.current = null;
        setInventoryReferenceActiveRequestId("");
      }
      setValidation(null);
      setRunPreview(null);
      await refreshAccounts(result.account.accountName);
      const lists = await engine("inventoryPriceLists", { accountName: activeAccountName });
      setPriceLists(lists.priceLists);
      setMessage(
        `Synced products ${result.results.products}, warehouses ${result.results.warehouses}, locations ${result.results.locations}, price values ${result.results.priceListValues}.`
      );
      const completed = mode === "all" || mode === "products" ? result.results.products : undefined;
      setSyncProgress({ percent: 100, completed, total: completed, message: "Reference sync complete." });
    });
  }

  async function cancelInventoryReferences() {
    const activeId = inventoryReferenceActiveRequestId || inventoryReferenceRequestId.current;
    setSyncProgress((current) => current ? { ...current, message: "Cancellation requested. The current safe checkpoint will finish first." } : current);
    if (activeId) await engine("cancel", { id: activeId });
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

  async function chooseOpenSalesSource() {
    const selected = await open({
      multiple: false,
      filters: [{ name: "Open Sales source", extensions: ["csv", "xlsx"] }],
    });
    if (typeof selected === "string") {
      setOpenSalesPath(selected);
      setOpenSalesValidation(null);
      setOpenSalesPreview(null);
    }
  }

  async function validateOpenSalesSource() {
    if (!activeAccountName || !openSalesPath) return;
    setOpenSalesProgress({ percent: 0, message: "Starting Open Sales validation..." });
    await run("validateOpenSales", async () => {
      const activeId = requestId("validateOpenSales");
      openSalesRequestId.current = activeId;
      setOpenSalesActiveRequestId(activeId);
      let result: any;
      try {
        result = await engine("validateOpenSalesFile", {
          accountName: activeAccountName,
          path: openSalesPath,
        }, activeId);
      } finally {
        openSalesRequestId.current = null;
        setOpenSalesActiveRequestId("");
      }
      setOpenSalesValidation(result);
      setOpenSalesPreview(result.preview);
      setOpenSalesProgress({ percent: 100, completed: result.orders, total: result.orders, message: "Open Sales validation complete." });
      setMessage(`Open Sales validation complete: ${result.orders} order(s), ${result.rows} row(s) staged.`);
    });
  }

  async function saveOpenSalesTemplate() {
    const destination = await save({
      defaultPath: "bp_sales_import.csv",
      filters: [{ name: "Spreadsheet files", extensions: ["csv", "xlsx"] }],
    });
    if (typeof destination !== "string") return;
    await run("saveOpenSalesTemplate", async () => {
      const result = await engine("saveOpenSalesTemplate", { destination });
      setMessage(`Open Sales template saved to ${result.path}.`);
    });
  }

  async function syncOpenSalesReferences(mode: "all" | "reference" | "contacts" | "products") {
    if (!activeAccountName) return;
    setOpenSalesProgress({ percent: 0, message: `Starting Open Sales reference sync: ${mode === "all" ? "all" : mode}.` });
    await run(`syncOpenSales-${mode}`, async () => {
      const activeId = requestId(`syncOpenSales-${mode}`);
      openSalesRequestId.current = activeId;
      setOpenSalesActiveRequestId(activeId);
      let result: any;
      try {
        result = await engine("syncOpenSalesReferences", { accountName: activeAccountName, mode }, activeId);
      } finally {
        openSalesRequestId.current = null;
        setOpenSalesActiveRequestId("");
      }
      setOpenSalesValidation((current: any) => current ? { ...current, referenceCounts: result.referenceCounts, logs: result.logs } : { referenceCounts: result.referenceCounts, logs: result.logs });
      await refreshAccounts(activeAccountName);
      setOpenSalesProgress({ percent: 100, message: "Open Sales reference sync complete." });
      setMessage(`Open Sales ${mode === "all" ? "sync all" : mode} complete.`);
    });
  }

  async function refreshOpenSalesPreview() {
    if (!activeAccountName) return;
    await run("previewOpenSales", async () => {
      const result = await engine("previewOpenSalesRun", { accountName: activeAccountName });
      setOpenSalesPreview(result);
      setMessage(`Open Sales preview loaded: ${result.orders} staged order(s).`);
    });
  }

  async function runOpenSalesLive() {
    if (!activeAccountName || openSalesConfirm !== activeAccountName) return;
    const accountName = activeAccountName;
    cancelOpenSalesRequested.current = false;
    setOpenSalesProgress({ percent: 0, message: "Starting Open Sales sync..." });
    await run("runOpenSales", async () => {
      for (;;) {
        if (cancelOpenSalesRequested.current) {
          setOpenSalesProgress((current) => current ? { ...current, message: "Sync cancelled. Remaining staged orders were not sent." } : current);
          break;
        }
        let result: any;
        const activeId = requestId("runOpenSalesOrder");
        openSalesRequestId.current = activeId;
        setOpenSalesActiveRequestId(activeId);
        try {
          result = await engine("runOpenSalesOrder", {
            accountName,
            confirmAccountName: openSalesConfirm,
          }, activeId);
          openSalesRequestId.current = null;
          setOpenSalesActiveRequestId("");
        } catch (err) {
          openSalesRequestId.current = null;
          setOpenSalesActiveRequestId("");
          const status = await engine("openSalesLiveStatus", { accountName });
          setOpenSalesLiveStatus(status);
          setOpenSalesProgress((current) => current ? {
            ...current,
            message: `Stopped at first write error. ${status.remaining ?? 0} order(s) remain unprocessed; fix or reconcile the failed order before retrying.`,
          } : current);
          throw err;
        }
        const status = await engine("openSalesLiveStatus", { accountName });
        setOpenSalesLiveStatus(status);
        const completed = Math.max(0, (result.total || 0) - (result.remaining || 0));
        setOpenSalesProgress({
          percent: result.total ? Math.round((completed / result.total) * 100) : 100,
          completed,
          total: result.total,
          message: `Confirmed ${result.orderRef}.`,
        });
        setMessage(`Open Sales order ${result.orderRef} confirmed as Brightpearl order ${result.orderId}.`);
        if (result.done) break;
        await new Promise((resolve) => setTimeout(resolve, result.waitMs || 500));
      }
    });
  }

  async function cancelOpenSales() {
    const activeId = openSalesActiveRequestId || openSalesRequestId.current;
    cancelOpenSalesRequested.current = true;
    setOpenSalesProgress((current) => current ? { ...current, message: "Cancellation requested. The current safe checkpoint will finish first." } : current);
    if (activeId) await engine("cancel", { id: activeId });
  }

  async function chooseOpenPurchasesSource() {
    const selected = await open({
      multiple: false,
      filters: [{ name: "Open Purchases source", extensions: ["csv", "xlsx"] }],
    });
    if (typeof selected === "string") {
      setOpenPurchasesPath(selected);
      setOpenPurchasesValidation(null);
      setOpenPurchasesPreview(null);
    }
  }

  async function validateOpenPurchasesSource() {
    if (!activeAccountName || !openPurchasesPath) return;
    setOpenPurchasesProgress({ percent: 0, message: "Starting Open Purchases validation..." });
    await run("validateOpenPurchases", async () => {
      const activeId = requestId("validateOpenPurchases");
      openPurchasesRequestId.current = activeId;
      setOpenPurchasesActiveRequestId(activeId);
      let result: any;
      try {
        result = await engine("validateOpenPurchasesFile", {
          accountName: activeAccountName,
          path: openPurchasesPath,
        }, activeId);
      } finally {
        openPurchasesRequestId.current = null;
        setOpenPurchasesActiveRequestId("");
      }
      setOpenPurchasesValidation(result);
      setOpenPurchasesPreview(result.preview);
      setOpenPurchasesProgress({ percent: 100, completed: result.orders, total: result.orders, message: "Open Purchases validation complete." });
      setMessage(`Open Purchases validation complete: ${result.orders} purchase order(s), ${result.rows} row(s) staged.`);
    });
  }

  async function saveOpenPurchasesTemplate() {
    const destination = await save({
      defaultPath: "bp_purchases_import.csv",
      filters: [{ name: "Spreadsheet files", extensions: ["csv", "xlsx"] }],
    });
    if (typeof destination !== "string") return;
    await run("saveOpenPurchasesTemplate", async () => {
      const result = await engine("saveOpenPurchasesTemplate", { destination });
      setMessage(`Open Purchases template saved to ${result.path}.`);
    });
  }

  async function syncOpenPurchasesReferences(mode: "all" | "reference" | "contacts" | "products") {
    if (!activeAccountName) return;
    setOpenPurchasesProgress({ percent: 0, message: `Starting Open Purchases reference sync: ${mode === "all" ? "all" : mode}.` });
    await run(`syncOpenPurchases-${mode}`, async () => {
      const activeId = requestId(`syncOpenPurchases-${mode}`);
      openPurchasesRequestId.current = activeId;
      setOpenPurchasesActiveRequestId(activeId);
      let result: any;
      try {
        result = await engine("syncOpenPurchasesReferences", { accountName: activeAccountName, mode }, activeId);
      } finally {
        openPurchasesRequestId.current = null;
        setOpenPurchasesActiveRequestId("");
      }
      setOpenPurchasesValidation((current: any) => current ? { ...current, referenceCounts: result.referenceCounts, logs: result.logs } : { referenceCounts: result.referenceCounts, logs: result.logs });
      await refreshAccounts(activeAccountName);
      setOpenPurchasesProgress({ percent: 100, message: "Open Purchases reference sync complete." });
      setMessage(`Open Purchases ${mode === "all" ? "sync all" : mode} complete.`);
    });
  }

  async function refreshOpenPurchasesPreview() {
    if (!activeAccountName) return;
    await run("previewOpenPurchases", async () => {
      const result = await engine("previewOpenPurchasesRun", { accountName: activeAccountName });
      setOpenPurchasesPreview(result);
      setMessage(`Open Purchases preview loaded: ${result.orders} staged purchase order(s).`);
    });
  }

  async function runOpenPurchasesLive() {
    if (!activeAccountName || openPurchasesConfirm !== activeAccountName) return;
    const accountName = activeAccountName;
    cancelOpenPurchasesRequested.current = false;
    setOpenPurchasesProgress({ percent: 0, message: "Starting Open Purchases sync..." });
    await run("runOpenPurchases", async () => {
      for (;;) {
        if (cancelOpenPurchasesRequested.current) {
          setOpenPurchasesProgress((current) => current ? { ...current, message: "Sync cancelled. Remaining staged orders were not sent." } : current);
          break;
        }
        let result: any;
        const activeId = requestId("runOpenPurchasesOrder");
        openPurchasesRequestId.current = activeId;
        setOpenPurchasesActiveRequestId(activeId);
        try {
          result = await engine("runOpenPurchasesOrder", {
            accountName,
            confirmAccountName: openPurchasesConfirm,
          }, activeId);
          openPurchasesRequestId.current = null;
          setOpenPurchasesActiveRequestId("");
        } catch (err) {
          openPurchasesRequestId.current = null;
          setOpenPurchasesActiveRequestId("");
          const status = await engine("openPurchasesLiveStatus", { accountName });
          setOpenPurchasesLiveStatus(status);
          throw err;
        }
        const status = await engine("openPurchasesLiveStatus", { accountName });
        setOpenPurchasesLiveStatus(status);
        const completed = Math.max(0, (result.total || 0) - (result.remaining || 0));
        setOpenPurchasesProgress({
          percent: result.total ? Math.round((completed / result.total) * 100) : 100,
          completed,
          total: result.total,
          message: `Confirmed ${result.orderRef}.`,
        });
        setMessage(`Open Purchases order ${result.orderRef} confirmed as Brightpearl order ${result.orderId}.`);
        if (result.done) break;
        await new Promise((resolve) => setTimeout(resolve, result.waitMs || 500));
      }
    });
  }

  async function cancelOpenPurchases() {
    const activeId = openPurchasesActiveRequestId || openPurchasesRequestId.current;
    cancelOpenPurchasesRequested.current = true;
    setOpenPurchasesProgress((current) => current ? { ...current, message: "Cancellation requested. The current safe checkpoint will finish first." } : current);
    if (activeId) await engine("cancel", { id: activeId });
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

  async function loadLogs(logId = selectedLogId) {
    const listing = await engine("appLogs");
    setLogs(listing.logs || []);
    const nextId = (listing.logs || []).some((log: any) => log.id === logId) ? logId : (listing.logs?.[0]?.id || "sync");
    setSelectedLogId(nextId);
    const result = await engine("readAppLog", { id: nextId, maxChars: 90000 });
    setLogContent(result.content || "");
    setLogPath(result.path || "");
    setLogTruncated(!!result.truncated);
  }

  async function exportLog() {
    const selected = logs.find((log) => log.id === selectedLogId);
    const destination = await save({
      defaultPath: selected?.path?.split(/[\\/]/).pop() || "tacos-log.txt",
      filters: [{ name: "Log file", extensions: ["log", "txt"] }],
    });
    if (typeof destination !== "string") return;
    await run("exportLog", async () => {
      const result = await engine("exportAppLog", { id: selectedLogId, destination });
      setMessage(`Log exported to ${result.path}.`);
    });
  }

  async function loadAppSettings() {
    const result = await engine("appSettings");
    setAppSettings(result.settings);
    setSettingsPath(result.settingsPath || "");
  }

  async function saveAppSettings() {
    if (!appSettings) return;
    await run("saveSettings", async () => {
      const result = await engine("saveAppSettings", { settings: appSettings });
      setAppSettings(result.settings);
      setSettingsPath(result.settingsPath || settingsPath);
      setMessage("Settings saved.");
    });
  }

  function updateSetting(key: string, value: string) {
    setAppSettings((current) => current ? { ...current, [key]: value } : current);
  }

  async function loadEnvironment() {
    const result = await engine("appEnvironment");
    setEnvironment(result);
  }

  function renderProgress(
    progress: ProgressState | null,
    unit: string,
    options: { canCancel?: boolean; onCancel?: () => void; cancelBusy?: boolean } = {},
  ) {
    if (!progress) return null;
    const percent = Math.max(0, Math.min(100, Math.round(progress.percent || 0)));
    const total = progress.total || 0;
    const completed = progress.completed || 0;
    return (
      <div className="syncProgress" aria-live="polite">
        <div className="progressTrack"><span style={{ width: `${percent}%` }} /></div>
        <div className="progressMeta">
          <span>
            <strong>{percent}%</strong>
            {total ? ` (${completed.toLocaleString()} of ${total.toLocaleString()} ${unit})` : ""}
          </span>
          {options.canCancel && options.onCancel && (
            <button className="progressCancel" onClick={options.onCancel} disabled={!!options.cancelBusy}>
              <AlertTriangle size={16} />
              Cancel
            </button>
          )}
        </div>
        {progress.message && <small>{progress.message}</small>}
      </div>
    );
  }

  return (
    <main className={appearanceClass(appSettings)}>
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
          {navGroups.map((group) => (
            <div className="navGroup" key={group.label}>
              <span className="navGroupLabel">{group.label}</span>
              {group.tabs.map((tab) => {
                const Icon = tab.icon;
                return (
                  <button
                    key={tab.id}
                    className={activeTab === tab.id ? "active" : ""}
                    disabled={busy === "runLive"}
                    onClick={() => {
                      setActiveTab(tab.id);
                      if (tab.id === "history") void refreshHistory();
                      if (tab.id === "logs") void loadLogs();
                      if (tab.id === "help") void loadEnvironment();
                    }}
                  >
                    <Icon size={18} />
                    {tab.label}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
      </aside>

      <section className="workspace">
        <header>
          <div>
            <h1>{activeHeaderTitle}</h1>
            <p>{activeHeaderDescription}</p>
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
                <button className="dangerButton" onClick={removeAccount} disabled={!!busy || !activeAccountName}>
                  {busy === "removeAccount" ? <Loader2 className="spin" size={18} /> : <Trash2 size={18} />}
                  Disconnect account
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
            <section className="step stepRefs">
              <div className="stepHeading"><span className="stepIndex">1</span><h2>References</h2></div>
              <div className="counts">
                {Object.entries(counts).map(([key, value]) => (
                  <span key={key}>{key}: <strong>{value}</strong></span>
                ))}
              </div>
              <div className="toolbar">
                <button onClick={() => syncReferences("all")} disabled={!!busy || !activeAccountName}>
                  {busy === "syncReferences-all" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
                  Sync all
                </button>
                <button onClick={() => syncReferences("products")} disabled={!!busy || !activeAccountName}>Sync Products</button>
                <button onClick={() => syncReferences("warehouses")} disabled={!!busy || !activeAccountName}>Sync Warehouses</button>
                <button onClick={() => syncReferences("locations")} disabled={!!busy || !activeAccountName}>Sync Locations</button>
                <button onClick={() => syncReferences("priceLists")} disabled={!!busy || !activeAccountName}>Sync Pricelists</button>
              </div>
              {renderProgress(syncProgress, "products", {
                canCancel: busy.startsWith("syncReferences") || !!inventoryReferenceActiveRequestId,
                onCancel: cancelInventoryReferences,
                cancelBusy: busy === "cancelInventoryReferences",
              })}
            </section>
            <section className="step stepSource">
              <div className="stepHeading"><span className="stepIndex">2</span><h2>Source</h2></div>
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
              <div className="stepHeading"><span className="stepIndex">3</span><h2>Validate</h2></div>
              <p>{hasReferences ? "References are available for local enrichment." : "Sync products, warehouses and locations before validation."}</p>
              <button onClick={validateSource} disabled={!!busy || !activeAccountName || !sourcePath || !hasReferences}>
                {busy === "validateSource" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Validate source
              </button>
            </section>
            <section className="step stepPreview">
              <div className="stepHeading"><span className="stepIndex">4</span><h2>Preview</h2></div>
              <p>{validation ? `${validation.inserted} row(s) staged in validated_inventory.` : "Validation preview appears after source validation."}</p>
            </section>
            <section className="step stepRun">
              <div className="stepHeading"><span className="stepIndex">5</span><h2>Run</h2></div>
              <button onClick={previewRun} disabled={!!busy || !validation?.inserted}>
                {busy === "previewRun" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Dry run
              </button>
              <p>Review the dry-run payload in Data before confirming a live run.</p>
            </section>
          </div>
        )}

        {activeTab === "legacy" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>Legacy Tool Workbench</h2>
                <p>Parity map for the legacy TACOS modules being rebuilt on the new platform.</p>
              </div>
            </div>
            {legacyCategories.map((category) => (
              <div className="operationSection" key={category}>
                <h2>{category}</h2>
                <div className="operationGrid">
                  {legacyOperations.filter((operation) => operation.category === category).map((operation) => (
                    <article className="operationCard" key={operation.id}>
                      <div className="operationTitle">
                        <h2>{operation.label}</h2>
                        <span className={operation.status === "active" ? "statusActive" : "statusPending"}>
                          {operation.status === "active" ? "Active" : "Foundation"}
                        </span>
                      </div>
                      <p>{operation.workflow.join(" -> ")}</p>
                      <dl>
                        <dt>Legacy</dt>
                        <dd>{operation.legacyModules.join(", ")}</dd>
                        <dt>Brightpearl</dt>
                        <dd>{operation.apiFamilies.join(", ")}</dd>
                      </dl>
                      {operation.id === "inventory_import" && (
                        <button onClick={() => setActiveTab("tasks")}>
                          <FileSpreadsheet size={18} />
                          Open inventory workflow
                        </button>
                      )}
                      {operation.id === "open_sales" && (
                        <button onClick={() => setActiveTab("openSales")}>
                          <ShoppingCart size={18} />
                          Open sales workflow
                        </button>
                      )}
                      {operation.id === "open_purchases" && (
                        <button onClick={() => setActiveTab("openPurchases")}>
                          <ShoppingCart size={18} />
                          Open purchases workflow
                        </button>
                      )}
                    </article>
                  ))}
                  {!legacyOperations.filter((operation) => operation.category === category).length && (
                    <p className="empty">No operations in this category.</p>
                  )}
                </div>
              </div>
            ))}
            {!legacyOperations.length && <p className="empty">No legacy operations loaded.</p>}
          </section>
        )}

        {activeTab === "openSales" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>Open Sales</h2>
                <p>Validate legacy Open Sales files against synced account references and preview staged orders before live posting is added.</p>
              </div>
              <button onClick={refreshOpenSalesPreview} disabled={!!busy || !activeAccountName}>
                {busy === "previewOpenSales" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Refresh preview
              </button>
            </div>
            <div className="taskFlow openSalesFlow">
              <section className="step stepRefs">
                <div className="stepHeading"><span className="stepIndex">1</span><h2>References</h2></div>
                <div className="toolbar">
                  <button onClick={() => syncOpenSalesReferences("all")} disabled={!!busy || !activeAccountName}>
                    {busy === "syncOpenSales-all" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
                    Sync all
                  </button>
                  <button onClick={() => syncOpenSalesReferences("reference")} disabled={!!busy || !activeAccountName}>Reference data</button>
                  <button onClick={() => syncOpenSalesReferences("contacts")} disabled={!!busy || !activeAccountName}>Contact refs</button>
                  <button onClick={() => syncOpenSalesReferences("products")} disabled={!!busy || !activeAccountName}>Product refs</button>
                </div>
                {showOpenSalesReferenceProgress && renderProgress(openSalesProgress, "records", {
                  canCancel: busy.startsWith("syncOpenSales") || !!openSalesActiveRequestId,
                  onCancel: cancelOpenSales,
                  cancelBusy: busy === "cancelOpenSales",
                })}
              </section>
              <section className="step stepSource">
                <div className="stepHeading"><span className="stepIndex">2</span><h2>Source</h2></div>
                <div className="sourceRow">
                  <input value={openSalesPath} readOnly placeholder="Choose Open Sales CSV/XLSX" />
                  <button onClick={saveOpenSalesTemplate} disabled={!!busy}>
                    <Download size={18} />
                    Template
                  </button>
                  <button onClick={chooseOpenSalesSource} disabled={!!busy}>
                    <FolderOpen size={18} />
                    Choose
                  </button>
                </div>
              </section>
              <section className="step stepValidate">
                <div className="stepHeading"><span className="stepIndex">3</span><h2>Validate</h2></div>
                <p>Checks customers, products, warehouses, channels, price lists, statuses, currencies, shipping and payments.</p>
                <button onClick={validateOpenSalesSource} disabled={!!busy || !activeAccountName || !openSalesPath}>
                  {busy === "validateOpenSales" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                  Validate Open Sales
                </button>
              </section>
              <section className="step stepRun">
                <div className="stepHeading"><span className="stepIndex">4</span><h2>Sync to Brightpearl</h2></div>
                <p>Creates one order at a time, saves the Brightpearl order id before payment, and stops for reconciliation if the outcome is uncertain.</p>
                <input aria-label="Confirm account name for Open Sales sync" placeholder="Type account name" value={openSalesConfirm} disabled={!!busy} onChange={(event) => setOpenSalesConfirm(event.target.value)} />
                <button onClick={runOpenSalesLive} disabled={!!busy || !openSalesPreview?.orders || openSalesConfirm !== activeAccountName}>
                  {busy === "runOpenSales" ? <Loader2 className="spin" size={18} /> : <CheckCircle2 size={18} />}
                  Sync Sales Orders
                </button>
              </section>
            </div>
            {showOpenSalesLiveProgress && renderProgress(openSalesProgress, "orders", {
              canCancel: busy === "runOpenSales" || !!openSalesActiveRequestId,
              onCancel: cancelOpenSales,
              cancelBusy: busy === "cancelOpenSales",
            })}
            {openSalesValidation?.referenceCounts && (
              <div className="counts wideCounts">
                {Object.entries(openSalesValidation.referenceCounts).map(([key, value]) => (
                  <span key={key}>{key}<strong>{String(value)}</strong></span>
                ))}
              </div>
            )}
            {openSalesPreview && (
              <div className="runPreview">
                <div className="resultHeader">
                  <div>
                    <h2>Staged Open Sales preview</h2>
                    <p>{openSalesPreview.orders} order(s), {openSalesPreview.rows} row(s), {openSalesPreview.payments} payment(s), payment total {openSalesPreview.paymentTotal}.</p>
                  </div>
                  <span className="jobState state-running">Checkpoint required</span>
                </div>
                <div className="tableWrap compactTable">
                  <table>
                    <thead>
                      <tr>
                        <th>Order ref</th><th>Rows</th><th>Contact</th><th>Placed</th><th>Currency</th><th>Net</th><th>Tax</th><th>Payment</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(openSalesPreview.previewOrders || []).map((order: any) => (
                        <tr key={order.orderRef}>
                          <td>{order.orderRef}</td>
                          <td>{order.rows}</td>
                          <td>{order.contactId}</td>
                          <td>{order.placedOn}</td>
                          <td>{order.currency}</td>
                          <td>{order.netTotal}</td>
                          <td>{order.taxTotal}</td>
                          <td>{order.paymentAmount ?? ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!(openSalesPreview.previewOrders || []).length && <p className="empty">No staged Open Sales orders yet.</p>}
                </div>
                {!!openSalesValidation?.logs?.length && (
                  <pre>{openSalesValidation.logs.join("\n")}</pre>
                )}
                {openSalesLiveStatus?.orders?.length > 0 && (
                  <div className="liveBatchStatus">
                    <strong>Recent Open Sales checkpoints</strong>
                    {openSalesLiveStatus.orders.slice(0, 10).map((order: any) => (
                      <div key={`${order.order_ref}-${order.updated_at}`}>
                        {order.order_ref}: {order.state}
                        {order.order_id ? `, order ${order.order_id}` : ""}
                        {order.payment_state ? `, payment ${order.payment_state}` : ""}
                        {order.error ? `, ${order.error}` : ""}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </section>
        )}

        {activeTab === "openPurchases" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>Open Purchases</h2>
                <p>Validate legacy Open Purchases files against supplier, product and order references, then post checkpointed POs to Brightpearl.</p>
              </div>
              <button onClick={refreshOpenPurchasesPreview} disabled={!!busy || !activeAccountName}>
                {busy === "previewOpenPurchases" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                Refresh preview
              </button>
            </div>
            <div className="taskFlow openSalesFlow">
              <section className="step stepRefs">
                <div className="stepHeading"><span className="stepIndex">1</span><h2>References</h2></div>
                <div className="toolbar">
                  <button onClick={() => syncOpenPurchasesReferences("all")} disabled={!!busy || !activeAccountName}>
                    {busy === "syncOpenPurchases-all" ? <Loader2 className="spin" size={18} /> : <RefreshCw size={18} />}
                    Sync all
                  </button>
                  <button onClick={() => syncOpenPurchasesReferences("reference")} disabled={!!busy || !activeAccountName}>Reference data</button>
                  <button onClick={() => syncOpenPurchasesReferences("contacts")} disabled={!!busy || !activeAccountName}>Contact refs</button>
                  <button onClick={() => syncOpenPurchasesReferences("products")} disabled={!!busy || !activeAccountName}>Product refs</button>
                </div>
                {showOpenPurchasesReferenceProgress && renderProgress(openPurchasesProgress, "records", {
                  canCancel: busy.startsWith("syncOpenPurchases") || !!openPurchasesActiveRequestId,
                  onCancel: cancelOpenPurchases,
                  cancelBusy: busy === "cancelOpenPurchases",
                })}
              </section>
              <section className="step stepSource">
                <div className="stepHeading"><span className="stepIndex">2</span><h2>Source</h2></div>
                <div className="sourceRow">
                  <input value={openPurchasesPath} readOnly placeholder="Choose Open Purchases CSV/XLSX" />
                  <button onClick={saveOpenPurchasesTemplate} disabled={!!busy}>
                    <Download size={18} />
                    Template
                  </button>
                  <button onClick={chooseOpenPurchasesSource} disabled={!!busy}>
                    <FolderOpen size={18} />
                    Choose
                  </button>
                </div>
              </section>
              <section className="step stepValidate">
                <div className="stepHeading"><span className="stepIndex">3</span><h2>Validate</h2></div>
                <p>Checks suppliers, products, warehouses, channels, price lists, statuses, currencies, shipping and payments.</p>
                <button onClick={validateOpenPurchasesSource} disabled={!!busy || !activeAccountName || !openPurchasesPath}>
                  {busy === "validateOpenPurchases" ? <Loader2 className="spin" size={18} /> : <FileSearch size={18} />}
                  Validate Open Purchases
                </button>
              </section>
              <section className="step stepRun">
                <div className="stepHeading"><span className="stepIndex">4</span><h2>Sync to Brightpearl</h2></div>
                <p>Creates one PO at a time, saves the Brightpearl order id before rows and payment, and stops for reconciliation if the outcome is uncertain.</p>
                <input aria-label="Confirm account name for Open Purchases sync" placeholder="Type account name" value={openPurchasesConfirm} disabled={!!busy} onChange={(event) => setOpenPurchasesConfirm(event.target.value)} />
                <button onClick={runOpenPurchasesLive} disabled={!!busy || !openPurchasesPreview?.orders || openPurchasesConfirm !== activeAccountName}>
                  {busy === "runOpenPurchases" ? <Loader2 className="spin" size={18} /> : <CheckCircle2 size={18} />}
                  Sync POs
                </button>
              </section>
            </div>
            {showOpenPurchasesLiveProgress && renderProgress(openPurchasesProgress, "orders", {
              canCancel: busy === "runOpenPurchases" || !!openPurchasesActiveRequestId,
              onCancel: cancelOpenPurchases,
              cancelBusy: busy === "cancelOpenPurchases",
            })}
            {openPurchasesValidation?.referenceCounts && (
              <div className="counts wideCounts">
                {Object.entries(openPurchasesValidation.referenceCounts).map(([key, value]) => (
                  <span key={key}>{key}<strong>{String(value)}</strong></span>
                ))}
              </div>
            )}
            {openPurchasesPreview && (
              <div className="runPreview">
                <div className="resultHeader">
                  <div>
                    <h2>Staged Open Purchases preview</h2>
                    <p>{openPurchasesPreview.orders} order(s), {openPurchasesPreview.rows} row(s), {openPurchasesPreview.payments} payment(s), payment total {openPurchasesPreview.paymentTotal}.</p>
                  </div>
                  <span className="jobState state-running">Checkpoint required</span>
                </div>
                <div className="tableWrap compactTable">
                  <table>
                    <thead>
                      <tr>
                        <th>Order ref</th><th>Rows</th><th>Supplier</th><th>Placed</th><th>Currency</th><th>Net</th><th>Tax</th><th>Payment</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(openPurchasesPreview.previewOrders || []).map((order: any) => (
                        <tr key={order.orderRef}>
                          <td>{order.orderRef}</td>
                          <td>{order.rows}</td>
                          <td>{order.contactId}</td>
                          <td>{order.placedOn}</td>
                          <td>{order.currency}</td>
                          <td>{order.netTotal}</td>
                          <td>{order.taxTotal}</td>
                          <td>{order.paymentAmount ?? ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!(openPurchasesPreview.previewOrders || []).length && <p className="empty">No staged Open Purchases orders yet.</p>}
                </div>
                {!!openPurchasesValidation?.logs?.length && (
                  <pre>{openPurchasesValidation.logs.join("\n")}</pre>
                )}
                {openPurchasesLiveStatus?.orders?.length > 0 && (
                  <div className="liveBatchStatus">
                    <strong>Recent Open Purchases checkpoints</strong>
                    {openPurchasesLiveStatus.orders.slice(0, 10).map((order: any) => (
                      <div key={`${order.order_ref}-${order.updated_at}`}>
                        {order.order_ref}: {order.state}
                        {order.order_id ? `, order ${order.order_id}` : ""}
                        {order.rows_state ? `, rows ${order.rows_state}` : ""}
                        {order.payment_state ? `, payment ${order.payment_state}` : ""}
                        {order.error ? `, ${order.error}` : ""}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </section>
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
            <div className="resultHeader">
              <div>
                <h2>Job History</h2>
                <p>Recent local work, including validation, sync and live run checkpoints.</p>
              </div>
              <button onClick={refreshHistory} disabled={!!busy}>
                <RefreshCw size={18} /> Refresh
              </button>
            </div>
            {jobs.map((job) => (
              <article className="job" key={job.id}>
                <strong>{job.kind}</strong>
                <span className={`jobState state-${String(job.state || "").replaceAll("_", "-")}`}>{job.state}</span>
                <span>{formatTime(job.updated_at)}</span>
                <span>{job.durationSeconds ? `${job.durationSeconds}s` : "-"}</span>
                <code>{job.message || job.dataset_id || job.source_path || job.id}</code>
              </article>
            ))}
            {!jobs.length && <p className="empty">No local jobs recorded yet.</p>}
          </section>
        )}

        {activeTab === "logs" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>Logs</h2>
                <p>Read and export TACOS diagnostic logs from the active data folder.</p>
              </div>
              <div className="toolbar">
                <select value={selectedLogId} onChange={(event) => { setSelectedLogId(event.target.value); void loadLogs(event.target.value); }}>
                  {logs.map((log) => <option key={log.id} value={log.id}>{log.label}</option>)}
                </select>
                <button onClick={() => void loadLogs()} disabled={!!busy}><RefreshCw size={18} /> Refresh</button>
                <button onClick={exportLog} disabled={!!busy || !logContent}><Download size={18} /> Export</button>
              </div>
            </div>
            {logPath && <p className="meta"><code>{logPath}</code>{logTruncated ? " - showing latest entries" : ""}</p>}
            <pre className="logViewer">{logContent || "No log entries yet."}</pre>
          </section>
        )}

        {activeTab === "help" && (
          <section className="panel">
            <div className="resultHeader">
              <div>
                <h2>About TACOS</h2>
                <p>Desktop migration of the legacy Brightpearl Pro Serv multi-tool.</p>
              </div>
            </div>
            {environment && (
              <dl className="environmentGrid">
                <dt>App</dt><dd>{environment.productName} {environment.desktopVersion}</dd>
                <dt>Legacy baseline</dt><dd>{environment.legacyVersion}</dd>
                <dt>Worker protocol</dt><dd>{environment.protocolVersion}</dd>
                <dt>Python</dt><dd>{environment.pythonVersion}</dd>
                <dt>Platform</dt><dd>{environment.platform}</dd>
                <dt>Data root</dt><dd><code>{environment.dataRoot}</code></dd>
                <dt>Job ledger</dt><dd><code>{environment.ledgerPath}</code></dd>
                <dt>Settings</dt><dd><code>{environment.settingsPath}</code></dd>
              </dl>
            )}
            <p className="meta">Legacy features are being refactored only where they can be made functional in the new platform. Inert controls should be hidden or marked not ported.</p>
          </section>
        )}

        {activeTab === "settings" && (
          <section className="panel settingsGrid">
            <div>
              <h2>Storage</h2>
              <p>Each account has its own local data database, bound by account name.</p>
              {activeAccount && <code>{activeAccount.dataDbPath}</code>}
              <p>The shared job ledger is <code>%LOCALAPPDATA%\TACOSv2\jobs.sqlite</code>. Open SQLite files read-only while TACOS is running.</p>
              {settingsPath && <p>Settings file: <code>{settingsPath}</code></p>}
            </div>
            <div>
              <h2>Logging</h2>
              <label>
                Log level
                <select value={String(appSettings?.log_level || "INFO")} onChange={(event) => updateSetting("log_level", event.target.value)}>
                  {["PAYLOAD", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((level) => <option key={level} value={level}>{level}</option>)}
                </select>
              </label>
              <label>
                Debug output folder
                <input value={String(appSettings?.log_output_dir || "")} onChange={(event) => updateSetting("log_output_dir", event.target.value)} />
              </label>
              <label>
                Exception output folder
                <input value={String(appSettings?.unmatched_output_dir || "")} onChange={(event) => updateSetting("unmatched_output_dir", event.target.value)} />
              </label>
            </div>
            <div>
              <h2>Brightpearl pacing</h2>
              <label>
                Throttle threshold
                <input type="number" min="1" value={String(appSettings?.throttle_threshold || 2)} onChange={(event) => updateSetting("throttle_threshold", event.target.value)} />
              </label>
              <label>
                Download retries
                <input type="number" min="1" value={String(appSettings?.download_max_retries || 3)} onChange={(event) => updateSetting("download_max_retries", event.target.value)} />
              </label>
              <label>
                Upload retries
                <input type="number" min="1" value={String(appSettings?.upload_max_retries || 3)} onChange={(event) => updateSetting("upload_max_retries", event.target.value)} />
              </label>
              <label>
                Stock correction batch size
                <input type="number" min="1" max="500" value={String(appSettings?.stock_correction_batch_size || 50)} onChange={(event) => updateSetting("stock_correction_batch_size", event.target.value)} />
              </label>
            </div>
            <div>
              <h2>Interface</h2>
              <label>
                Appearance
                <select value={String(appSettings?.appearance_mode || "System")} onChange={(event) => updateSetting("appearance_mode", event.target.value)}>
                  {["System", "Light", "Dark"].map((mode) => <option key={mode} value={mode}>{mode}</option>)}
                </select>
              </label>
              <label>
                Theme
                <select value={String(appSettings?.appearance_theme || "Sage")} onChange={(event) => updateSetting("appearance_theme", event.target.value)}>
                  {["Sage", "Brightpearl"].map((theme) => <option key={theme} value={theme}>{theme}</option>)}
                </select>
              </label>
              <button onClick={saveAppSettings} disabled={!!busy || !appSettings}>
                {busy === "saveSettings" ? <Loader2 className="spin" size={18} /> : <Settings size={18} />}
                Save settings
              </button>
              <p>Reference sync uses Brightpearl reads and only publishes complete snapshots. Live inventory corrections require a reviewed dry run and typed account confirmation.</p>
            </div>
          </section>
        )}
      </section>
    </main>
  );
}
