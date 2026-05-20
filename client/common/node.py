import ctypes
import logging
import signal
import time

import iceoryx2

from dataclasses import dataclass, field
from pathlib import Path

from client.common.utils import setup_logging


@dataclass
class PublisherPort:
    publisher: object
    notifier: object
    event: object


@dataclass
class SubscriberPort:
    subscriber: object
    listener: object
    event: object


@dataclass
class BlackboardEntry:
    key: object
    value_type: object
    entry: object


@dataclass
class BlackboardPort:
    service: object
    port: object
    entries: dict[str, BlackboardEntry] = field(default_factory=dict)


class Node:
    IOX2_CONFIG = Path(__file__).parent / "iceoryx2.toml"
    BLACKBOARD_KEY_TYPE = ctypes.c_uint64

    def __init__(self, name, level=logging.INFO, console_level=logging.INFO, handle_signals=True):
        self.name = name
        self.logger = setup_logging(self.name, level=level, console_level=console_level)

        Node.setup_iceoryx2_config()
        try:
            iceoryx2.set_log_level(iceoryx2.LogLevel.Error)
        except Exception:
            pass
        self.node = (
            iceoryx2.NodeBuilder.new()
            .name(iceoryx2.NodeName.new(self.name))
            .create(iceoryx2.ServiceType.Ipc)
        )

        if handle_signals:
            try:
                signal.signal(signal.SIGINT, self._signal_portr)
            except ValueError:
                # signal.signal only works in the main thread
                pass

        self.running = True
        self.logger.info(f"Node {self.name} initialized")

    def _signal_portr(self, sig, frame):
        self.running = False

    def create_publisher(self, name, data_type, event_id) -> PublisherPort:
        service = (
            self.node.service_builder(iceoryx2.ServiceName.new(name))
                .publish_subscribe(data_type)
                .open_or_create()
        )
        publisher = service.publisher_builder().create()

        event = (
            self.node.service_builder(iceoryx2.ServiceName.new(name))
                .event()
                .open_or_create()
        )
        notifier = event.notifier_builder().create()
        event = iceoryx2.EventId.new(event_id)

        self.logger.info(f"{name} service created")
        self.logger.info(f"Event {event_id} created")

        return PublisherPort(publisher=publisher, notifier=notifier, event=event)

    def create_subscriber(self, name, data_type, event_id, check_interruption=lambda: False) -> SubscriberPort:
        def should_stop():
            return not self.running or check_interruption()

        service = None
        while not should_stop():
            try:
                service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(name))
                    .publish_subscribe(data_type)
                    .open_or_create()
                )
                break
            except iceoryx2.PublishSubscribeOpenError:
                time.sleep(0.1)
        
        if service is None:
            return None

        self.logger.info(f"{name} service connected")

        subscriber = service.subscriber_builder().create()

        event_service = None
        while not should_stop():
            try:
                event_service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(name))
                    .event()
                    .open_or_create()
                )
                break
            except Exception:
                time.sleep(0.1)
        
        if event_service is None:
            return None

        self.logger.info(f"Event {event_id} connected")
        listener = event_service.listener_builder().create()
        event_id = iceoryx2.EventId.new(event_id)

        return SubscriberPort(subscriber=subscriber, listener=listener, event=event_id)

    @staticmethod
    def setup_iceoryx2_config() -> None:
        iceoryx2.config.setup_global_config_from_file(iceoryx2.FilePath.new(str(Node.IOX2_CONFIG)))

    def create_blackboard_writer(self, name, entries) -> BlackboardPort:
        builder = (
            self.node.service_builder(iceoryx2.ServiceName.new(name))
            .blackboard_creator(Node.BLACKBOARD_KEY_TYPE)
        )

        for _, field in entries.items():
            builder = builder.add(field.key, field.default)

        service = builder.create()
        writer = service.writer_builder().create()    
        blackboard_entries = {}

        for entry_name, field in entries.items():
            blackboard_entries[entry_name] = BlackboardEntry(
                key=field.key,
                value_type=field.value_type,
                entry=writer.entry(field.key, field.value_type),
            )

        self.logger.info(f"{name} blackboard writer created")
        return BlackboardPort(service=service, port=writer, entries=blackboard_entries)

    def create_blackboard_reader(self, name, entries, check_interruption=lambda: False) -> BlackboardPort | None:
        def should_stop():
            return (not self.running or check_interruption())

        service = None

        while not should_stop():
            try:
                service = (
                    self.node.service_builder(iceoryx2.ServiceName.new(name))
                    .blackboard_opener(Node.BLACKBOARD_KEY_TYPE)
                    .open()
                )
                break
            except Exception:
                time.sleep(0.1)

        if service is None:
            return None

        reader = service.reader_builder().create()    
        blackboard_entries = {}

        for entry_name, field in entries.items():
            blackboard_entries[entry_name] = BlackboardEntry(
                key=field.key,
                value_type=field.value_type,
                entry=reader.entry(field.key, field.value_type),
            )

        self.logger.info(f"{name} blackboard reader connected")
        return BlackboardPort(service=service, port=reader, entries=blackboard_entries)

    @staticmethod
    def blackboard_write(blackboard: BlackboardPort, entry_name: str, value):
        if entry_name not in blackboard.entries:
            raise KeyError(f"Unknown blackboard entry: {entry_name}")
        blackboard_entry = blackboard.entries[entry_name]
        ctypes_value = blackboard_entry.value_type(value)
        blackboard_entry.entry.update_with_copy(ctypes_value)

    @staticmethod
    def blackboard_read(blackboard: BlackboardPort, entry_name: str):
        if entry_name not in blackboard.entries:
            raise KeyError(f"Unknown blackboard entry: {entry_name}")
        entry = blackboard.entries[entry_name]
        return entry.entry.get().decode_as(entry.value_type).value