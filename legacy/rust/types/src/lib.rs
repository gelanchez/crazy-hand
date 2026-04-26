use iceoryx2::prelude::*;

pub const IMAGE_WIDTH: usize = 324;
pub const IMAGE_HEIGHT: usize = 244;
pub const IMAGE_SIZE: usize = IMAGE_WIDTH * IMAGE_HEIGHT;

pub const IMAGE_SERVICE: &str = "crazyflie/image";
pub const IMAGE_EVENT: &str = "crazyflie/image/event";
pub const TELEMETRY_SERVICE: &str = "crazyflie/telemetry";
pub const TELEMETRY_EVENT: &str = "crazyflie/telemetry/event";
pub const COMMAND_SERVICE: &str = "crazyflie/command";
pub const COMMAND_EVENT: &str = "crazyflie/command/event";

/// Grayscale camera frame from the AI deck (324×244, 1 byte per pixel).
/// Stored directly in shared memory — no heap pointers allowed.
#[derive(ZeroCopySend)]
#[repr(C)]
pub struct ImageFrame {
    pub id: u64,
    pub timestamp: u64,
    pub pixels: [u8; IMAGE_WIDTH * IMAGE_HEIGHT],
}

impl std::fmt::Debug for ImageFrame {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ImageFrame")
            .field("id", &self.id)
            .field("timestamp", &self.timestamp)
            .field("pixels", &format_args!("[u8; {}]", IMAGE_SIZE))
            .finish()
    }
}

/// Drone telemetry snapshot.
#[derive(Clone, Copy, Default, ZeroCopySend)]
#[repr(C)]
pub struct Telemetry {
    /// Voltage of the battery (V).
    pub battery_voltage: f32,
    /// Percentage of the battery (%).
    pub battery_percentage: f32,
    /// Received Signal Strength Indication (dBm).
    pub rssi: f32,
    /// Whether the drone is connected.
    pub connected: bool,
    /// Explicit padding to avoid undefined layout.
    pub _pad: [u8; 4],
}

impl std::fmt::Debug for Telemetry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Telemetry")
            .field(
                "battery_voltage",
                &format_args!("{:.2}", self.battery_voltage),
            )
            .field(
                "battery_percentage",
                &format_args!("{:.2}", self.battery_percentage),
            )
            .field("rssi", &format_args!("{:.2}", self.rssi))
            .field("connected", &self.connected)
            .finish()
    }
}

/// Discrete action sent to the drone.
#[derive(Clone, Copy, Debug, Default, ZeroCopySend, PartialEq, Eq)]
#[repr(C)]
pub enum Action {
    #[default]
    None = 0,
    Takeoff = 1,
    Land = 2,
    EmergencyStop = 3,
}

/// Discrete command sent from GUI to control on user interaction.
#[derive(Clone, Copy, Default, ZeroCopySend)]
#[repr(C)]
pub struct Command {
    pub id: u64,
    pub timestamp: u64,
    pub thrust: f32,
    pub pitch: f32,
    pub roll: f32,
    pub yaw: f32,
    pub action: Action,
    pub _pad: [u8; 3], // Explicit padding for alignment
}

impl std::fmt::Debug for Command {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Command")
            .field("id", &self.id)
            .field("thrust", &format_args!("{:.2}", self.thrust))
            .field("pitch", &format_args!("{:.2}", self.pitch))
            .field("roll", &format_args!("{:.2}", self.roll))
            .field("yaw", &format_args!("{:.2}", self.yaw))
            .field("action", &self.action)
            .finish()
    }
}
