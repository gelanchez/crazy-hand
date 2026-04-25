use crate::app::GuiData;
use iceoryx2::prelude::*;
use shared::{
    COMMAND_EVENT, COMMAND_SERVICE, Command, IMAGE_EVENT, IMAGE_SERVICE, ImageFrame,
    TELEMETRY_EVENT, TELEMETRY_SERVICE, Telemetry,
};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub fn run(
    gui_data: Arc<Mutex<GuiData>>,
    ctx: egui::Context,
) -> Result<(), Box<dyn std::error::Error>> {
    let node = NodeBuilder::new()
        .name(&"gui".try_into()?)
        .signal_handling_mode(SignalHandlingMode::Disabled) // Allow CTRL+C to work
        .create::<ipc::Service>()?;

    // Inbound: image
    let image_service = node
        .service_builder(&IMAGE_SERVICE.try_into()?)
        .publish_subscribe::<ImageFrame>()
        .open_or_create()?;
    let image_subscriber = image_service.subscriber_builder().create()?;

    let image_event_service = node
        .service_builder(&IMAGE_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let image_listener = image_event_service.listener_builder().create()?;

    // Inbound: telemetry
    let telemetry_service = node
        .service_builder(&TELEMETRY_SERVICE.try_into()?)
        .publish_subscribe::<Telemetry>()
        .open_or_create()?;
    let telemetry_subscriber = telemetry_service.subscriber_builder().create()?;

    let telemetry_event_service = node
        .service_builder(&TELEMETRY_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let telemetry_listener = telemetry_event_service.listener_builder().create()?;

    // Outbound: command
    let command_service = node
        .service_builder(&COMMAND_SERVICE.try_into()?)
        .publish_subscribe::<Command>()
        .open_or_create()?;
    let command_pub = command_service.publisher_builder().create()?;

    let command_event_service = node
        .service_builder(&COMMAND_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let command_notifier = command_event_service.notifier_builder().create()?;

    // Waitset for notifications
    let waitset = WaitSetBuilder::new()
        .signal_handling_mode(SignalHandlingMode::Disabled) // Allow CTRL+C to work
        .create::<ipc::Service>()?;

    // Guards for inbounds
    let image_guard = waitset.attach_notification(&image_listener)?;
    let telemetry_guard = waitset.attach_notification(&telemetry_listener)?;
    // TODO Magic number, why 16 ms?
    // 16 ms tick to flush pending command without starving the receive path.
    let command_tick = waitset.attach_interval(Duration::from_millis(16))?;

    tracing::info!("[gui] IPC ready");

    let mut last_image_id: Option<u64> = None;

    loop {
        waitset.wait_and_process_once_with_timeout(
            |id| {
                // New image arrived.
                if id.has_event_from(&image_guard) {
                    while image_listener.try_wait_one().unwrap().is_some() {}
            // Drain the buffer; keep only the latest frame, log any gaps.
                    let mut latest: Option<(u64, u64, Vec<u8>)> = None;
                    while let Some(sample) = image_subscriber.receive().unwrap() {
                        latest = Some((sample.id, sample.timestamp, sample.pixels.to_vec()));
                    }
                    if let Some((id, timestamp, pixels)) = latest {
                        if let Some(previous_id) = last_image_id {
                            let gap = id.saturating_sub(previous_id + 1);
                            if gap > 0 {
                                tracing::warn!("Dropped {gap} image frame(s) (id {previous_id} → {id})");
                            }
                        }
                        tracing::info!("Image received: ImageFrame {{ id: {id}, timestamp: {timestamp}, pixels: [u8; {}] }}", shared::IMAGE_SIZE);
                        last_image_id = Some(id);
                        // TODO send timestamp and id to GUI
                        // TODO Calculate instantaneous and average framerate and send it to GUI
                        let mut data = gui_data.lock().unwrap();
                        data.image = Some(pixels);
                        data.last_image_time = std::time::Instant::now();
                        ctx.request_repaint();
                    }
                }

                // New telemetry arrived.
                if id.has_event_from(&telemetry_guard) {
                    while telemetry_listener.try_wait_one().unwrap().is_some() {}
                    while let Some(sample) = telemetry_subscriber.receive().unwrap() {
                        tracing::info!("Telemetry received: {:?}", *sample);
                        let mut data = gui_data.lock().unwrap();
                        data.telemetry = *sample;
                        data.last_telemetry_time = std::time::Instant::now();
                        ctx.request_repaint();
                    }
                }

                // TODO Why?
                // Periodic command flush.
                if id.has_event_from(&command_tick) {
                    let cmd = gui_data.lock().unwrap().command.take();
                    if let Some(command) = cmd {
                        tracing::info!("Command sent: {:?}", command);
                        command_pub
                            .loan_uninit()
                            .unwrap()
                            .write_payload(command)
                            .send()
                            .unwrap();
                        command_notifier.notify().unwrap();
                    }
                }
                CallbackProgression::Continue
            },
            Duration::from_millis(16),
        )?;
    }
}
