use rand::RngExt;
/// Stub that simulates the Crazyflie drone.
/// Replace with real crazyflie-lib-rs calls when integrating hardware.
///
/// Runs as a tokio task. Sends DroneData at variable intervals (50–200 ms)
/// to mimic the drone's asynchronous log delivery, and drains incoming
/// commands to print them (future: forward to drone setpoint API).
use shared::Command;
use shared::IMAGE_SIZE;
use shared::{ImageFrame, Telemetry};
use tokio::sync::mpsc;
use tokio::time::Duration;

const SIMULATED_IMAGE_RATE_HZ: u64 = 1;

// TODO Why this one doesn't have the command but the GuiData does? Is it because the mspc tx and rx channels?
/// Data produced by the drone and forwarded to the IPC thread.
pub struct DroneData {
    pub image: Option<ImageFrame>,
    pub telemetry: Telemetry,
}

pub async fn run(
    drone_data_tx: mpsc::Sender<DroneData>,
    mut command_rx: mpsc::Receiver<Command>,
) -> Result<(), Box<dyn std::error::Error>> {
    tracing::info!("Fake drone started");

    let mut counter: u64 = 0;
    let mut image_interval =
        tokio::time::interval(Duration::from_millis(1000 / SIMULATED_IMAGE_RATE_HZ));

    loop {
        tokio::select! {
            // 1. Process commands as soon as they arrive (real-time, unblocked)
            // TODO: replace with crazyflie_lib setpoint calls.
            Some(cmd) = command_rx.recv() => {
                tracing::info!(
                    "Command #{:03} — thrust:{:.2} pitch:{:.2} roll:{:.2} yaw:{:.2} action:{:?}",
                    cmd.id,
                    cmd.thrust,
                    cmd.pitch,
                    cmd.roll,
                    cmd.yaw,
                    cmd.action
                );
            }

            // 2. Send image and telemetry at a fixed rate (1Hz)
            _ = image_interval.tick() => {
                // Fake grayscale image
                let mut pixels = [0u8; IMAGE_SIZE];
                for (i, p) in pixels.iter_mut().enumerate() {
                    *p = ((i + counter as usize * 3) % 256) as u8;
                }

                let mut rng = rand::rng();
                let data = DroneData {
                    image: Some(ImageFrame {
                        id: counter,
                        timestamp: std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap()
                            .as_millis() as u64,
                        pixels,
                    }),
                    telemetry: Telemetry {
                        battery_voltage: rng.random_range(3.3..=4.2),
                        battery_percentage: rng.random_range(0.0..=100.0),
                        rssi: rng.random_range(-90.0..=-30.0),
                        connected: true,
                        _pad: [0; 4],
                    },
                };

                if drone_data_tx.send(data).await.is_err() {
                    tracing::error!("Channel closed, shutting down");
                    break;
                }
                counter += 1;
            }
        }
    }

    Ok(())
}
