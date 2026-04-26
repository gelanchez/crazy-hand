use std::sync::Arc;
use tokio::sync::mpsc;
use tokio::time::Duration;
use types::{Command, IMAGE_SIZE, ImageFrame, Telemetry};
use crate::wifi_link::WifiLink;
use crazyflie_lib::{Crazyflie, NoTocCache};
use tracing::{info, error};

pub struct DroneData {
    pub image: Option<ImageFrame>,
    pub telemetry: Telemetry,
}

pub async fn run(
    drone_data_tx: mpsc::Sender<DroneData>,
    mut command_rx: mpsc::Receiver<Command>,
) -> Result<(), Box<dyn std::error::Error>> {
    let cf_ip = "192.168.4.1:5000";
    info!("Connecting to Crazyflie at {}...", cf_ip);

    let (image_chunk_tx, image_chunk_rx) = flume::unbounded::<Vec<u8>>();
    
    info!("Establishing WiFi connection to {}...", cf_ip);
    let wifi_link = WifiLink::connect(cf_ip, image_chunk_tx).await?;
    let link = crazyflie_link::Connection::new(Box::new(wifi_link));
    
    info!("Initializing Crazyflie subsystems (fetching TOCs)...");
    let cf = Crazyflie::connect_from_link(link, NoTocCache).await?;
    info!("Connected to Crazyflie! Initializing telemetry...");

    let cf = Arc::new(cf);
    let cf_clone = cf.clone();

    // Telemetry Log Config
    let mut log_config = cf.log.create_block().await?;
    log_config.add_variable("pm.vbat").await?;
    log_config.add_variable("pm.batteryLevel").await?;
    // Radio RSSI might not be available over WiFi link statistics, but let's try the log variable
    // If it fails, we just won't have RSSI.
    let _ = log_config.add_variable("radio.rssi").await;
    
    info!("Starting telemetry log stream...");
    let log_stream = log_config.start(crazyflie_lib::subsystems::log::LogPeriod::from_millis(100)?).await?;
    info!("Telemetry log stream started!");

    let telemetry = Arc::new(tokio::sync::Mutex::new(Telemetry {
        battery_voltage: 0.0,
        battery_percentage: 0.0,
        rssi: 0.0,
        connected: true,
        _pad: [0; 4],
    }));

    // Task for image assembly
    let drone_data_tx_image = drone_data_tx.clone();
    let telemetry_image = telemetry.clone();
    tokio::spawn(async move {
        let mut assembler = ImageAssembler::new();
        let mut counter = 0;
        while let Ok(data) = image_chunk_rx.recv_async().await {
            if let Some(pixels) = assembler.push(data) {
                let frame = ImageFrame {
                    id: counter,
                    timestamp: std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_millis() as u64,
                    pixels,
                };
                let current_telemetry = telemetry_image.lock().await.clone();
                let _ = drone_data_tx_image.send(DroneData {
                    image: Some(frame),
                    telemetry: current_telemetry,
                }).await;
                counter += 1;
            }
        }
    });

    loop {
        tokio::select! {
            Ok(log_data) = log_stream.next() => {
                let mut t = telemetry.lock().await;
                t.battery_voltage = log_data.data.get("pm.vbat").map(|v| v.to_f64_lossy() as f32).unwrap_or(0.0);
                t.battery_percentage = log_data.data.get("pm.batteryLevel").map(|v| v.to_f64_lossy() as f32).unwrap_or(0.0);
                t.rssi = log_data.data.get("radio.rssi").map(|v| v.to_f64_lossy() as f32 * -1.0).unwrap_or(0.0);
            }
            Some(cmd) = command_rx.recv() => {
                // High level commander or regular commander?
                // For now, let's use the regular commander for real-time setpoints
                let _ = cf_clone.commander.setpoint_hover(
                    cmd.roll,
                    cmd.pitch,
                    cmd.yaw,
                    cmd.thrust // In hover mode this is height
                ).await;
            }
            _ = tokio::time::sleep(Duration::from_secs(5)) => {
                if !cf_clone.platform.protocol_version().await.is_ok() {
                    error!("Connection lost to Crazyflie");
                    break;
                }
            }
        }
    }

    Ok(())
}

struct ImageAssembler {
    buffer: Vec<u8>,
    target_size: usize,
}

impl ImageAssembler {
    fn new() -> Self {
        Self {
            buffer: Vec::new(),
            target_size: 0,
        }
    }

    fn push(&mut self, data: Vec<u8>) -> Option<[u8; IMAGE_SIZE]> {
        if data.len() >= 11 && data[0] == 0xBC {
            // New image header
            // magic: u8, width: u16, height: u16, depth: u8, format: u8, size: u32
            let size = u32::from_le_bytes([data[7], data[8], data[9], data[10]]) as usize;
            self.target_size = size;
            self.buffer.clear();
            // The rest of the packet might contain data
            if data.len() > 11 {
                self.buffer.extend_from_slice(&data[11..]);
            }
        } else {
            self.buffer.extend_from_slice(&data);
        }

        if self.target_size > 0 && self.buffer.len() >= self.target_size {
            let mut pixels = [0u8; IMAGE_SIZE];
            let copy_len = self.buffer.len().min(IMAGE_SIZE);
            pixels[..copy_len].copy_from_slice(&self.buffer[..copy_len]);
            self.target_size = 0;
            self.buffer.clear();
            return Some(pixels);
        }

        None
    }
}
