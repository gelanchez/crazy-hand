mod app;
mod ipc;

use app::{App, GuiData};
use iceoryx2::config::Config;
use iceoryx2::prelude::{FilePath, SemanticString};
use std::sync::{Arc, Mutex};
use types::{IMAGE_HEIGHT, IMAGE_WIDTH};

const WINDOW_WIDTH: f32 = IMAGE_WIDTH as f32 + 215.0;
const WINDOW_HEIGHT: f32 = IMAGE_HEIGHT as f32 + 100.0;

fn main() -> eframe::Result {
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

    let gui_data = Arc::new(Mutex::new(GuiData::default()));
    let gui_data_ipc = Arc::clone(&gui_data);

    let native_options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([WINDOW_WIDTH, WINDOW_HEIGHT])
            .with_min_inner_size([WINDOW_WIDTH, WINDOW_HEIGHT]),
        ..Default::default()
    };

    eframe::run_native(
        app::GUI_NAME,
        native_options,
        Box::new(|cc| {
            // Run IPC on a separate thread and pass the context for repainting.
            let ctx = cc.egui_ctx.clone();
            std::thread::spawn(move || {
                if let Err(e) = ipc::run(gui_data_ipc, ctx) {
                    tracing::error!("IPC thread error: {e}");
                }
            });
            Ok(Box::new(App::new(gui_data)))
        }),
    )?;

    // Force-exit so the IPC thread loop is terminated when the GUI window closes.
    std::process::exit(0);
}
