import ctypes
import logging
import signal
import time
from dataclasses import dataclass, field

import iceoryx2

from client.common.constants import IOX2_CONFIG
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
    BLACKBOARD_KEY_TYPE = ctypes.c_uint64

    def __init__(self, name, level=logging.INFO, console_level=logging.INFO, handle_signals=True):
        self.name = name
        self.logger = setup_logging(self.name, level=level, console_level=console_level)

        Node.setup_iceoryx2_config()
        try:
            iceoryx2.set_log_level(iceoryx2.LogLevel.Error)
        except Exception:
            pass
        self.node = iceoryx2.NodeBuilder.new().name(iceoryx2.NodeName.new(self.name)).create(iceoryx2.ServiceType.Ipc)

        if handle_signals:
            try:
                signal.signal(signal.SIGINT, self._signal_handler)
            except ValueError:
                # signal.signal only works in the main thread
                pass

        self.running = True
        self.logger.info(f"Node {self.name} initialized")

    def stop(self):
        self.running = False

    def _signal_handler(self, _sig, _frame):
        self.stop()

    @staticmethod
    def setup_iceoryx2_config() -> None:
        iceoryx2.config.setup_global_config_from_file(iceoryx2.FilePath.new(str(IOX2_CONFIG)))

    def create_publisher(self, name, data_type, event_id) -> PublisherPort:
        service = (
            self.node.service_builder(iceoryx2.ServiceName.new(name)).publish_subscribe(data_type).open_or_create()
        )
        publisher = service.publisher_builder().create()

        event = self.node.service_builder(iceoryx2.ServiceName.new(name)).event().open_or_create()
        notifier = event.notifier_builder().create()
        event = iceoryx2.EventId.new(event_id)

        self.logger.info(f"{name} service created")
        self.logger.info(f"Event {event_id.name} created")

        return PublisherPort(publisher=publisher, notifier=notifier, event=event)

    def create_subscriber(self, name, data_type, event_id, check_interruption=lambda: False) -> SubscriberPort | None:
        service = None
        while self.running and not check_interruption():
            try:
                service = (
                    self.node
                    .service_builder(iceoryx2.ServiceName.new(name))
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
        while self.running and not check_interruption():
            try:
                event_service = self.node.service_builder(iceoryx2.ServiceName.new(name)).event().open_or_create()
                break
            except Exception:
                time.sleep(0.1)

        if event_service is None:
            return None

        self.logger.info(f"Event {event_id.name} connected")
        listener = event_service.listener_builder().create()
        event_id = iceoryx2.EventId.new(event_id)

        return SubscriberPort(subscriber=subscriber, listener=listener, event=event_id)

    def create_blackboard_writer(self, name, entries) -> BlackboardPort:
        builder = self.node.service_builder(iceoryx2.ServiceName.new(name)).blackboard_creator(Node.BLACKBOARD_KEY_TYPE)

        for _, f in entries.items():
            builder = builder.add(f.key, f.default)

        service = builder.create()
        writer = service.writer_builder().create()
        blackboard_entries = {}

        for entry_name, f in entries.items():
            blackboard_entries[entry_name] = BlackboardEntry(
                key=f.key,
                value_type=f.value_type,
                entry=writer.entry(f.key, f.value_type),
            )

        self.logger.info(f"{name} blackboard writer created")
        return BlackboardPort(service=service, port=writer, entries=blackboard_entries)

    def create_blackboard_reader(self, name, entries, check_interruption=lambda: False) -> BlackboardPort | None:
        service = None

        while self.running and not check_interruption():
            try:
                service = (
                    self.node
                    .service_builder(iceoryx2.ServiceName.new(name))
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

        for entry_name, f in entries.items():
            blackboard_entries[entry_name] = BlackboardEntry(
                key=f.key,
                value_type=f.value_type,
                entry=reader.entry(f.key, f.value_type),
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
