use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::Emitter;

const PROTOCOL_VERSION: i64 = 1;
const MAX_MESSAGE_BYTES: usize = 64 * 1024;

#[derive(Clone)]
struct WorkerProcess {
    child: Arc<Mutex<Child>>,
    stdin: Arc<Mutex<ChildStdin>>,
    stdout: Arc<Mutex<BufReader<ChildStdout>>>,
}

struct EngineState {
    worker: Arc<Mutex<Option<WorkerProcess>>>,
    request_lock: Arc<Mutex<()>>,
}

fn worker_script() -> Result<PathBuf, String> {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let dev_path = manifest.join("..").join("engine").join("worker.py");
    if cfg!(debug_assertions) && dev_path.exists() {
        return Ok(dev_path);
    }
    let exe = std::env::current_exe().map_err(|err| err.to_string())?;
    let packaged = exe
        .parent()
        .ok_or("Could not resolve executable directory")?
        .join("engine-dist")
        .join("tacos-engine")
        .join("tacos-engine.exe");
    Ok(packaged)
}

fn python_exe() -> String {
    std::env::var("TACOS_ENGINE_PYTHON").unwrap_or_else(|_| {
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let candidate = manifest
            .join("..")
            .join("..")
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        if candidate.exists() {
            candidate.to_string_lossy().to_string()
        } else {
            "python".to_string()
        }
    })
}

fn start_worker() -> Result<WorkerProcess, String> {
    let worker = worker_script()?;
    let mut command = if worker.extension().and_then(|value| value.to_str()) == Some("exe") {
        Command::new(worker)
    } else {
        let mut command = Command::new(python_exe());
        command.arg(worker);
        command
    };
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Could not start Python worker: {err}"))?;
    let stdin = child.stdin.take().ok_or("Worker stdin unavailable")?;
    let stdout = child.stdout.take().ok_or("Worker stdout unavailable")?;
    let mut stderr = child.stderr.take().ok_or("Worker stderr unavailable")?;
    let mut stdout_reader = BufReader::new(stdout);
    let mut ready = String::new();
    stdout_reader
        .read_line(&mut ready)
        .map_err(|err| format!("Worker did not become ready: {err}"))?;
    if ready.is_empty() {
        let mut details = String::new();
        let _ = stderr.read_to_string(&mut details);
        let message = details.trim();
        return Err(if message.is_empty() {
            "The Python worker exited before becoming ready.".to_string()
        } else {
            format!("The Python worker exited before becoming ready: {message}")
        });
    }
    let ready_value: Value = serde_json::from_str(&ready)
        .map_err(|err| format!("Worker returned an invalid ready message: {err}"))?;
    if ready_value.get("type").and_then(Value::as_str) != Some("ready") {
        return Err("Worker returned an unexpected ready message.".to_string());
    }
    std::thread::spawn(move || {
        let _ = std::io::copy(&mut stderr, &mut std::io::sink());
    });
    Ok(WorkerProcess {
        child: Arc::new(Mutex::new(child)),
        stdin: Arc::new(Mutex::new(stdin)),
        stdout: Arc::new(Mutex::new(stdout_reader)),
    })
}

fn validate_request(request: &Value) -> Result<(), String> {
    if request.get("protocolVersion").and_then(Value::as_i64) != Some(PROTOCOL_VERSION) {
        return Err("Unsupported engine protocol version.".to_string());
    }
    let id = request
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if id.is_empty() || id.len() > 120 {
        return Err("Engine request id is missing or too long.".to_string());
    }
    let method = request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match method {
        "importSyntheticCsv"
        | "previewDataset"
        | "goLiveOperations"
        | "legacyOperations"
        | "appSettings"
        | "saveAppSettings"
        | "appLogs"
        | "readAppLog"
        | "exportAppLog"
        | "appEnvironment"
        | "listAccounts"
        | "saveAccount"
        | "removeAccount"
        | "validateAccount"
        | "syncInventoryReferences"
        | "inventoryPriceLists"
        | "previewInventoryRun"
        | "saveInventoryRunPreview"
        | "runInventoryBatch"
        | "inventoryLiveStatus"
        | "inventoryRunResume"
        | "validateInventoryFile"
        | "validateOpenSalesFile"
        | "previewOpenSalesRun"
        | "saveOpenSalesTemplate"
        | "syncOpenSalesReferences"
        | "runOpenSalesOrder"
        | "openSalesLiveStatus"
        | "validateOpenPurchasesFile"
        | "previewOpenPurchasesRun"
        | "saveOpenPurchasesTemplate"
        | "syncOpenPurchasesReferences"
        | "runOpenPurchasesOrder"
        | "openPurchasesLiveStatus"
        | "saveInventoryExceptionReport"
        | "jobHistory"
        | "cancel"
        | "shutdown" => Ok(()),
        _ => Err("Engine method is not allowed.".to_string()),
    }
}

fn event_to_emit(value: &Value) -> Option<(&'static str, Value)> {
    if value.get("type").and_then(Value::as_str) != Some("event") {
        return None;
    }
    let data = value.get("data").cloned().unwrap_or(Value::Null);
    let channel = if data.get("operation").and_then(Value::as_str) == Some("reference_sync") {
        "reference-sync-progress"
    } else {
        "engine-event"
    };
    Some((
        channel,
        if channel == "engine-event" {
            value.clone()
        } else {
            data
        },
    ))
}

#[tauri::command]
async fn engine_request(
    app: tauri::AppHandle,
    state: tauri::State<'_, EngineState>,
    request: Value,
) -> Result<Value, String> {
    validate_request(&request)?;
    let worker_state = Arc::clone(&state.worker);
    let request_lock = Arc::clone(&state.request_lock);
    tauri::async_runtime::spawn_blocking(move || {
        engine_request_blocking(app, worker_state, request_lock, request)
    })
    .await
    .map_err(|err| format!("Engine task failed: {err}"))?
}

fn worker_handles(
    worker_state: &Arc<Mutex<Option<WorkerProcess>>>,
) -> Result<WorkerProcess, String> {
    let mut guard = worker_state
        .lock()
        .map_err(|_| "Engine state is poisoned")?;
    if guard.is_none() {
        *guard = Some(start_worker()?);
    }
    guard
        .clone()
        .ok_or("Worker could not be started".to_string())
}

fn write_worker_request(worker: &WorkerProcess, encoded: &str) -> Result<(), String> {
    let mut stdin = worker
        .stdin
        .lock()
        .map_err(|_| "Worker stdin is poisoned")?;
    writeln!(stdin, "{encoded}").map_err(|err| format!("Could not write to worker: {err}"))?;
    stdin.flush().map_err(|err| err.to_string())
}

fn cancel_marker_path(request_id: &str) -> PathBuf {
    let safe: String = request_id
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' || ch == '.' {
                ch
            } else {
                '_'
            }
        })
        .collect();
    std::env::temp_dir()
        .join("TACOSv2-cancel")
        .join(format!("{safe}.cancel"))
}

fn write_cancel_marker(request: &Value, fallback_id: &str) -> Result<(), String> {
    let target_id = request
        .get("params")
        .and_then(Value::as_object)
        .and_then(|params| params.get("id"))
        .and_then(Value::as_str)
        .unwrap_or(fallback_id);
    let marker = cancel_marker_path(target_id);
    if let Some(parent) = marker.parent() {
        fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    fs::write(marker, "cancelled").map_err(|err| err.to_string())
}

fn engine_request_blocking(
    app: tauri::AppHandle,
    worker_state: Arc<Mutex<Option<WorkerProcess>>>,
    request_lock: Arc<Mutex<()>>,
    request: Value,
) -> Result<Value, String> {
    let encoded = serde_json::to_string(&request).map_err(|err| err.to_string())?;
    if encoded.as_bytes().len() > MAX_MESSAGE_BYTES {
        return Err("Engine request exceeded the pipe message limit.".to_string());
    }
    let request_id = request
        .get("id")
        .and_then(Value::as_str)
        .ok_or("Engine request id is required")?
        .to_string();
    let method = request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or_default();

    let worker = worker_handles(&worker_state)?;
    if method == "cancel" {
        write_cancel_marker(&request, &request_id)?;
        write_worker_request(&worker, &encoded)?;
        return Ok(json!({
            "ok": true,
            "result": {"cancelQueued": true},
            "error": Value::Null
        }));
    }

    let _request_guard = request_lock
        .lock()
        .map_err(|_| "Engine request lock is poisoned")?;
    write_worker_request(&worker, &encoded)?;

    loop {
        let mut line = String::new();
        let read = worker
            .stdout
            .lock()
            .map_err(|_| "Worker stdout is poisoned")?
            .read_line(&mut line)
            .map_err(|err| format!("Could not read worker response: {err}"))?;
        if read == 0 {
            if let Ok(mut guard) = worker_state.lock() {
                *guard = None;
            }
            return Err("The Python worker exited unexpectedly.".to_string());
        }
        if line.as_bytes().len() > MAX_MESSAGE_BYTES {
            return Err("Worker response exceeded the pipe message limit.".to_string());
        }
        let value: Value = serde_json::from_str(&line).map_err(|err| err.to_string())?;
        if let Some((channel, payload)) = event_to_emit(&value) {
            app.emit(channel, payload).map_err(|err| err.to_string())?;
            continue;
        }
        if value.get("id").and_then(Value::as_str) == Some(&request_id) {
            return Ok(json!({
                "ok": value.get("ok").and_then(Value::as_bool).unwrap_or(false),
                "result": value.get("result").cloned().unwrap_or(Value::Null),
                "error": value.get("error").cloned().unwrap_or(Value::Null)
            }));
        }
    }
}

fn main() {
    tauri::Builder::default()
        .manage(EngineState {
            worker: Arc::new(Mutex::new(None)),
            request_lock: Arc::new(Mutex::new(())),
        })
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![engine_request])
        .run(tauri::generate_context!())
        .expect("error while running TACOS desktop prototype");
}

impl Drop for WorkerProcess {
    fn drop(&mut self) {
        if Arc::strong_count(&self.child) == 1 {
            if let Ok(mut child) = self.child.lock() {
                let _ = child.kill();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_account_price_list_lookup() {
        let request = json!({
            "protocolVersion": 1,
            "id": "price-lists-test",
            "method": "inventoryPriceLists",
            "params": {"accountName": "demo"}
        });
        assert!(validate_request(&request).is_ok());
        for method in [
            "previewInventoryRun",
            "saveInventoryRunPreview",
            "runInventoryBatch",
            "inventoryLiveStatus",
            "inventoryRunResume",
        ] {
            let request = json!({
                "protocolVersion": 1,
                "id": "run-preview-test",
                "method": method,
                "params": {"accountName": "demo"}
            });
            assert!(validate_request(&request).is_ok());
        }
    }

    #[test]
    fn routes_reference_progress_as_direct_payload() {
        let event = json!({
            "type": "event",
            "event": "progress",
            "data": {"operation": "reference_sync", "percent": 57, "completed": 137000}
        });
        let (channel, payload) = event_to_emit(&event).expect("event should be routed");
        assert_eq!(channel, "reference-sync-progress");
        assert_eq!(payload["percent"], 57);
        assert_eq!(payload["completed"], 137000);
    }
}
