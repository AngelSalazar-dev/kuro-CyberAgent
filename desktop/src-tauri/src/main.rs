// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager};

#[derive(Clone, Default, Serialize)]
struct BackendStatus {
    ready: bool,
    detail: String,
    model: String,
    mode: String,
    python: String,
    script: String,
}

struct KuroState {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    status: Mutex<BackendStatus>,
}

impl KuroState {
    fn write_line(&self, value: &Value) -> Result<(), String> {
        let mut guard = self.stdin.lock().map_err(|e| e.to_string())?;
        let stream = guard
            .as_mut()
            .ok_or_else(|| "the Kuro backend is not running".to_string())?;
        let mut payload = value.to_string();
        payload.push('\n');
        stream
            .write_all(payload.as_bytes())
            .map_err(|e| e.to_string())?;
        stream.flush().map_err(|e| e.to_string())
    }

    fn set_detail(&self, detail: String) {
        if let Ok(mut status) = self.status.lock() {
            status.detail = detail;
        }
    }
}

#[tauri::command]
fn get_status(state: tauri::State<KuroState>) -> BackendStatus {
    state
        .status
        .lock()
        .map(|status| status.clone())
        .unwrap_or_default()
}

#[tauri::command]
fn send_message(
    state: tauri::State<KuroState>,
    text: String,
    model: Option<String>,
    mode: Option<String>,
) -> Result<(), String> {
    let mut params = json!({ "text": text });
    if let Some(value) = model {
        params["model"] = json!(value);
    }
    if let Some(value) = mode {
        params["mode"] = json!(value);
    }
    state.write_line(&json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "send_message",
        "params": params
    }))
}

#[tauri::command]
fn permission_response(
    state: tauri::State<KuroState>,
    id: i64,
    decision: String,
) -> Result<(), String> {
    state.write_line(&json!({
        "jsonrpc": "2.0",
        "method": "permission_response",
        "params": { "id": id, "decision": decision }
    }))
}

fn candidate_pythons() -> Vec<(String, Vec<String>)> {
    let mut out: Vec<(String, Vec<String>)> = Vec::new();
    if let Ok(custom) = std::env::var("KURO_PYTHON") {
        if !custom.trim().is_empty() {
            out.push((custom, Vec::new()));
        }
    }
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(root) = std::env::var("KURO_ROOT") {
        roots.push(PathBuf::from(root));
    }
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        roots.push(PathBuf::from(local).join("kuro"));
    }
    for root in roots {
        let venv = root.join(".venv").join("Scripts").join("python.exe");
        if venv.is_file() {
            out.push((venv.to_string_lossy().to_string(), Vec::new()));
        }
    }
    out.push(("py".to_string(), vec!["-3".to_string()]));
    out.push(("python".to_string(), Vec::new()));
    out.push(("python3".to_string(), Vec::new()));
    out
}

fn script_path(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(custom) = std::env::var("KURO_DESKTOP_SCRIPT") {
        let path = PathBuf::from(custom);
        if path.is_file() {
            return Some(path);
        }
    }
    if let Ok(dir) = app.path().resource_dir() {
        let path = dir.join("kuro-server.py");
        if path.is_file() {
            return Some(path);
        }
    }
    let dev = Path::new(env!("CARGO_MANIFEST_DIR")).join("kuro-server.py");
    if dev.is_file() {
        return Some(dev);
    }
    let parent = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("kuro-server.py");
    if parent.is_file() {
        return Some(parent);
    }
    None
}

fn spawn_backend(script: &Path) -> Result<(Child, ChildStdin, String), String> {
    let mut last = String::from("no python candidate");
    for (program, prefix) in candidate_pythons() {
        let mut command = Command::new(&program);
        for arg in &prefix {
            command.arg(arg);
        }
        command
            .arg(script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8");
        match command.spawn() {
            Ok(mut child) => {
                let stdin = child.stdin.take().ok_or("the backend pipe is missing")?;
                return Ok((child, stdin, program));
            }
            Err(err) => last = format!("{} ({})", program, err),
        }
    }
    Err(format!("no usable python was found: {}", last))
}

fn pump_stdout(app: AppHandle, stdout: std::process::ChildStdout) {
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<Value>(&line) {
                Ok(payload) => {
                    let _ = app.emit("kuro-rpc", payload);
                }
                Err(_) => {
                    let _ = app.emit("kuro-stderr", line);
                }
            }
        }
    });
}

fn pump_stderr(app: AppHandle, stderr: std::process::ChildStderr) {
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            let Ok(line) = line else { break };
            if line.trim().is_empty() {
                continue;
            }
            let _ = app.emit("kuro-stderr", line);
        }
    });
}

fn start_backend(app: &AppHandle) -> Result<(), String> {
    let script = script_path(app)
        .ok_or_else(|| "kuro-server.py was not found next to the app".to_string())?;
    let (mut child, stdin, python) = spawn_backend(&script)?;

    if let Some(stdout) = child.stdout.take() {
        pump_stdout(app.clone(), stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        pump_stderr(app.clone(), stderr);
    }

    let state = app.state::<KuroState>();
    if let Ok(mut guard) = state.stdin.lock() {
        *guard = Some(stdin);
    }
    if let Ok(mut guard) = state.child.lock() {
        *guard = Some(child);
    }
    if let Ok(mut status) = state.status.lock() {
        status.ready = true;
        status.python = python;
        status.script = script.to_string_lossy().to_string();
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(KuroState {
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            status: Mutex::new(BackendStatus {
                model: "deepseek-v4.1-flash".to_string(),
                mode: "auto".to_string(),
                ..Default::default()
            }),
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            send_message,
            permission_response
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(err) = start_backend(&handle) {
                eprintln!("[kuro] the backend did not start: {}", err);
                handle.state::<KuroState>().set_detail(err);
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the Kuro desktop app");
}
