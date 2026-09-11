use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::Mutex;

const PROTOCOL_VERSION: i64 = 1;
const MAX_MESSAGE_BYTES: usize = 64 * 1024;

struct WorkerProcess {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

struct EngineState {
    worker: Mutex<Option<WorkerProcess>>,
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
        let candidate = manifest.join("..").join("..").join(".venv").join("Scripts").join("python.exe");
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
    let mut process = WorkerProcess {
        child,
        stdin,
        stdout: BufReader::new(stdout),
    };
    let mut ready = String::new();
    process
        .stdout
        .read_line(&mut ready)
        .map_err(|err| format!("Worker did not become ready: {err}"))?;
    Ok(process)
}

fn validate_request(request: &Value) -> Result<(), String> {
    if request.get("protocolVersion").and_then(Value::as_i64) != Some(PROTOCOL_VERSION) {
        return Err("Unsupported engine protocol version.".to_string());
    }
    let id = request.get("id").and_then(Value::as_str).unwrap_or_default();
    if id.is_empty() || id.len() > 120 {
        return Err("Engine request id is missing or too long.".to_string());
    }
    let method = request.get("method").and_then(Value::as_str).unwrap_or_default();
    match method {
        "importSyntheticCsv"
        | "previewDataset"
        | "listAccounts"
        | "saveAccount"
        | "validateAccount"
        | "syncInventoryReferences"
        | "validateInventoryFile"
        | "saveInventoryExceptionReport"
        | "jobHistory"
        | "cancel"
        | "shutdown" => Ok(()),
        _ => Err("Engine method is not allowed.".to_string()),
    }
}

#[tauri::command]
fn engine_request(state: tauri::State<EngineState>, request: Value) -> Result<Value, String> {
    validate_request(&request)?;
    let encoded = serde_json::to_string(&request).map_err(|err| err.to_string())?;
    if encoded.as_bytes().len() > MAX_MESSAGE_BYTES {
        return Err("Engine request exceeded the pipe message limit.".to_string());
    }
    let request_id = request
        .get("id")
        .and_then(Value::as_str)
        .ok_or("Engine request id is required")?
        .to_string();

    let mut guard = state.worker.lock().map_err(|_| "Engine state is poisoned")?;
    if guard.is_none() {
        *guard = Some(start_worker()?);
    }
    let worker = guard.as_mut().ok_or("Worker could not be started")?;
    writeln!(worker.stdin, "{encoded}").map_err(|err| format!("Could not write to worker: {err}"))?;
    worker.stdin.flush().map_err(|err| err.to_string())?;

    loop {
        let mut line = String::new();
        let read = worker
            .stdout
            .read_line(&mut line)
            .map_err(|err| format!("Could not read worker response: {err}"))?;
        if read == 0 {
            *guard = None;
            return Err("The Python worker exited unexpectedly.".to_string());
        }
        if line.as_bytes().len() > MAX_MESSAGE_BYTES {
            return Err("Worker response exceeded the pipe message limit.".to_string());
        }
        let value: Value = serde_json::from_str(&line).map_err(|err| err.to_string())?;
        if value.get("type").and_then(Value::as_str) == Some("event") {
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
            worker: Mutex::new(None),
        })
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![engine_request])
        .run(tauri::generate_context!())
        .expect("error while running TACOS desktop prototype");
}

impl Drop for WorkerProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}
