mod drone;
mod ipc;

use drone::DroneData;
use iceoryx2::config::Config;
use iceoryx2::prelude::{FilePath, SemanticString};
use tokio::sync::mpsc;
use types::Command;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::INFO)
        .init();

    // Iceoryx2 configuration
    let config_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("types/iceoryx2.toml");
    let file_path =
        FilePath::new(config_path.to_str().unwrap().as_bytes()).expect("Invalid config path");
    if let Err(e) = Config::setup_global_config_from_file(&file_path) {
        tracing::error!("Failed to load iceoryx2 config: {}", e);
    }

    // drone → IPC: drone data published to GUI
    let (drone_data_tx, drone_data_rx) = mpsc::channel::<DroneData>(8);
    // IPC → tokio: commands received from GUI, applied to drone
    let (command_tx, command_rx) = mpsc::channel::<Command>(8);

    std::thread::spawn(|| {
        if let Err(e) = ipc::run(drone_data_rx, command_tx) {
            tracing::error!("IPC thread error: {e}");
        }
    });

    drone::run(drone_data_tx, command_rx).await?;

    Ok(())
}
