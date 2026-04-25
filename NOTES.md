# Crazyflie TFM Optimizations & Notes

This document tracks technical decisions and performance optimizations made during the integration of the Crazyflie IPC and GUI.

## 🚀 Performance Optimizations

### GPU Texture Re-use
- **Issue**: Calling `ui.ctx().load_texture()` every frame allocates a new GPU resource, causing high overhead and memory instability.
- **Fix**: Re-use the existing `egui::TextureHandle` by calling `texture.set(color_image, options)`. This performs a memory copy to the existing GPU slot instead of a full re-allocation.

### Signal Handling (Ctrl+C)
- **Issue**: `iceoryx2` hijacks `SIGINT` by default to manage shared memory cleanup, preventing the GUI and CLI from exiting immediately on Ctrl+C.
- **Fix**: Set `.signal_handling_mode(SignalHandlingMode::Disabled)` in both `NodeBuilder` and `WaitSetBuilder`. This restores default OS signal behavior while the app manages its own thread lifecycle.

### IPC Buffer Draining
- **Issue**: High-frequency image streams can cause a backlog in the `iceoryx2` subscriber buffer if the GUI thread skips frames.
- **Fix**: Implemented a "drain loop" in the IPC thread:
  ```rust
  while let Some(sample) = image_subscriber.receive().unwrap() {
      latest = Some(...);
  }
  ```
  This ensures the GUI always processes the absolute newest frame and discards stale ones, preventing lag.

### Zero-Allocation Logging
- **Issue**: Reconstructing a full `ImageFrame` (79KB) with a zeroed-out array just to pass it to `tracing::info!` was causing massive unnecessary stack pressure (3MB/s at 30fps).
- **Fix**: Switched to manual string formatting for image logs to avoid temporary 79KB allocations.

## 🛠️ Architecture Notes

### Telemetry State Management
- **Decision**: Changed `GuiData.telemetry` from `Option<Telemetry>` to a non-optional `Telemetry` initialized with `Telemetry::default()`.
- **Rationale**: Simplifies UI widget code by removing the need for `if let Some(...)` patterns. Telemetry values default to zero until the first IPC packet arrives.

### Timestamp Calculation
- **Fix**: Replaced `SystemTime::now().elapsed()` (which returned 0) with `SystemTime::now().duration_since(UNIX_EPOCH)` to provide standard Unix timestamps for telemetry and image headers.
