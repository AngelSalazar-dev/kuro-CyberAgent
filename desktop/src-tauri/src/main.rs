// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;
use serde_json::Value;
use std::sync::Mutex;

struct KuroState {
    sidecar: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
}

#[tauri::command]
fn send_message(state: tauri::State<KuroState>, text: String) -> Result<(), String> {
    let child = state.sidecar.lock().map_err(|e| e.to_string())?;
    if let Some(child) = child.as_ref() {
        let msg = serde_json::json!({
            "jsonrpc": "2.0",
            "method": "send_message",
            "params": {"text": text},
            "id": 1
        });
        child.write(format!("{}\n", msg).as_bytes()).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn get_status() -> String {
    "Kuro Desktop v1.0.0".to_string()
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(KuroState {
            sidecar: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![send_message, get_status])
        .setup(|app| {
            let shell = app.shell();
            let (mut rx, child) = shell
                .sidecar("kuro-server")
                .expect("failed to create sidecar command")
                .spawn()
                .expect("failed to spawn sidecar");

            let state = app.state::<KuroState>();
            *state.sidecar.lock().unwrap() = Some(child);

            // Listen for stdout from sidecar
            tauri::async_runtime::spawn(async move {
                use tauri_plugin_shell::process::CommandEvent;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            println!("Sidecar stdout: {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("Sidecar stderr: {}", String::from_utf8_lossy(&line));
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
