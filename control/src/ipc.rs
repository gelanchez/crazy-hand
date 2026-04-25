use crate::drone::DroneData;
use iceoryx2::prelude::*;
use shared::{
    COMMAND_EVENT, COMMAND_SERVICE, Command, IMAGE_EVENT, IMAGE_SERVICE, ImageFrame,
    TELEMETRY_EVENT, TELEMETRY_SERVICE, Telemetry,
};
use tokio::sync::mpsc;

pub fn run(
    mut drone_data_rx: mpsc::Receiver<DroneData>,
    command_tx: mpsc::Sender<Command>,
) -> Result<(), Box<dyn std::error::Error>> {
    let node = NodeBuilder::new()
        .name(&"control".try_into()?)
        .signal_handling_mode(SignalHandlingMode::Disabled) // Allow CTRL+C to work
        .create::<ipc::Service>()?;

    // Outbound: image
    let image_service = node
        .service_builder(&IMAGE_SERVICE.try_into()?)
        .publish_subscribe::<ImageFrame>()
        .open_or_create()?;
    let image_publisher = image_service.publisher_builder().create()?;

    let image_event_service = node
        .service_builder(&IMAGE_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let image_notifier = image_event_service.notifier_builder().create()?;

    // Outbound: telemetry
    let telemetry_service = node
        .service_builder(&TELEMETRY_SERVICE.try_into()?)
        .publish_subscribe::<Telemetry>()
        .open_or_create()?;
    let telemetry_publisher = telemetry_service.publisher_builder().create()?;

    let telemetry_event_service = node
        .service_builder(&TELEMETRY_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let telemetry_notifier = telemetry_event_service.notifier_builder().create()?;

    // Inbound: command
    let command_service = node
        .service_builder(&COMMAND_SERVICE.try_into()?)
        .publish_subscribe::<Command>()
        .open_or_create()?;
    let command_subscriber = command_service.subscriber_builder().create()?;

    let command_event_service = node
        .service_builder(&COMMAND_EVENT.try_into()?)
        .event()
        .open_or_create()?;
    let command_listener = command_event_service.listener_builder().create()?;

    // Waitset for notifications
    let waitset = WaitSetBuilder::new()
        .signal_handling_mode(SignalHandlingMode::Disabled) // Allow CTRL+C to work
        .create::<ipc::Service>()?;

    // Guards for inbounds
    let command_guard = waitset.attach_notification(&command_listener)?;

    tracing::info!("IPC ready");

    // TODO Compare both IPC implementations and understand their implementation
    loop {
        // Block up to 16 ms waiting for an incoming command notification.
        // Parameter order: closure first, then timeout.
        waitset.wait_and_process_once_with_timeout(
            |id| {
                if id.has_event_from(&command_guard) {
                    while command_listener.try_wait_one().unwrap().is_some() {}
                    while let Some(sample) = command_subscriber.receive().unwrap() {
                        let command = *sample;
                        tracing::info!("Command received: {:?}", command);
                        let _ = command_tx.blocking_send(command);
                    }
                }
                CallbackProgression::Continue
            },
            // TODO Why 16 ms?
            std::time::Duration::from_millis(16),
        )?;

        // Drain everything the drone task produced since the last iteration.
        while let Ok(data) = drone_data_rx.try_recv() {
            if let Some(frame) = data.image {
                tracing::info!("Image sent: {:?}", frame);
                image_publisher.loan_uninit()?.write_payload(frame).send()?;
                image_notifier.notify()?;
            }
            tracing::info!("Telemetry sent: {:?}", data.telemetry);
            telemetry_publisher
                .loan_uninit()?
                .write_payload(data.telemetry)
                .send()?;
            telemetry_notifier.notify()?;
        }
    }
}
