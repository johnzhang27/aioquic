import asyncio
from typing import Any, Callable, Dict, Optional, Text, Tuple, Union, cast

from .. import events
from ..connection import NetworkAddress, QuicConnection

QuicConnectionIdHandler = Callable[[bytes], None]
QuicStreamHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]


class QuicConnectionProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        connection: QuicConnection,
        stream_handler: Optional[QuicStreamHandler] = None,
    ):
        self._connection = connection
        self._connection_id_issued_handler: QuicConnectionIdHandler = lambda c: None
        self._connection_id_retired_handler: QuicConnectionIdHandler = lambda c: None

        if stream_handler is not None:
            self._stream_handler = stream_handler
        else:
            self._stream_handler = lambda r, w: None

    def close(self) -> None:
        self._connection.close()
        self._send_pending()

    def connect(self, addr: NetworkAddress, protocol_version: int) -> None:
        self._connection.connect(
            addr, now=self._loop.time(), protocol_version=protocol_version
        )
        self._send_pending()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        loop = asyncio.get_event_loop()

        self._closed = asyncio.Event()
        self._connected_waiter = loop.create_future()
        self._loop = loop
        self._ping_waiter: Optional[asyncio.Future[None]] = None
        self._send_task: Optional[asyncio.Handle] = None
        self._stream_readers: Dict[int, asyncio.StreamReader] = {}
        self._timer: Optional[asyncio.TimerHandle] = None
        self._timer_at: Optional[float] = None
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: Union[bytes, Text], addr: NetworkAddress) -> None:
        self._connection.receive_datagram(
            cast(bytes, data), addr, now=self._loop.time()
        )
        self._send_pending()

    async def create_stream(
        self, is_unidirectional: bool = False
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """
        Create a QUIC stream and return a pair of (reader, writer) objects.

        The returned reader and writer objects are instances of :class:`asyncio.StreamReader`
        and :class:`asyncio.StreamWriter` classes.
        """
        stream = self._connection.create_stream(is_unidirectional=is_unidirectional)
        return self._create_stream(stream.stream_id)

    def request_key_update(self) -> None:
        """
        Request an update of the encryption keys.
        """
        self._connection.request_key_update()
        self._send_pending()

    async def ping(self) -> None:
        """
        Pings the remote host and waits for the response.
        """
        assert self._ping_waiter is None, "already await a ping"
        self._ping_waiter = self._loop.create_future()
        self._connection.send_ping(id(self._ping_waiter))
        self._send_soon()
        await asyncio.shield(self._ping_waiter)

    async def wait_closed(self) -> None:
        """
        Wait for the connection to be closed.
        """
        await self._closed.wait()

    async def wait_connected(self) -> None:
        """
        Wait for the TLS handshake to complete.
        """
        await asyncio.shield(self._connected_waiter)

    def _create_stream(
        self, stream_id: int
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        adapter = QuicStreamAdapter(self, stream_id)
        reader = asyncio.StreamReader()
        writer = asyncio.StreamWriter(adapter, None, reader, None)
        self._stream_readers[stream_id] = reader
        return reader, writer

    def _handle_timer(self) -> None:
        now = max(self._timer_at, self._loop.time())

        self._timer = None
        self._timer_at = None

        self._connection.handle_timer(now=now)

        self._send_pending()

    def _send_pending(self) -> None:
        self._send_task = None

        # process events
        event = self._connection.next_event()
        while event is not None:
            if isinstance(event, events.ConnectionIdIssued):
                self._connection_id_issued_handler(event.connection_id)
            elif isinstance(event, events.ConnectionIdRetired):
                self._connection_id_retired_handler(event.connection_id)
            elif isinstance(event, events.ConnectionTerminated):
                for reader in self._stream_readers.values():
                    reader.feed_eof()
                if not self._connected_waiter.done():
                    self._connected_waiter.set_exception(ConnectionError)
                self._closed.set()
            elif isinstance(event, events.HandshakeCompleted):
                self._connected_waiter.set_result(None)
            elif isinstance(event, events.PongReceived):
                waiter = self._ping_waiter
                self._ping_waiter = None
                waiter.set_result(None)
            elif isinstance(event, events.StreamDataReceived):
                reader = self._stream_readers.get(event.stream_id, None)
                if reader is None:
                    reader, writer = self._create_stream(event.stream_id)
                    self._stream_handler(reader, writer)
                reader.feed_data(event.data)
                if event.end_stream:
                    reader.feed_eof()

            event = self._connection.next_event()

        # send datagrams
        for data, addr in self._connection.datagrams_to_send(now=self._loop.time()):
            self._transport.sendto(data, addr)

        # re-arm timer
        timer_at = self._connection.get_timer()
        if self._timer is not None and self._timer_at != timer_at:
            self._timer.cancel()
            self._timer = None
        if self._timer is None and timer_at is not None:
            self._timer = self._loop.call_at(timer_at, self._handle_timer)
        self._timer_at = timer_at

    def _send_soon(self) -> None:
        if self._send_task is None:
            self._send_task = self._loop.call_soon(self._send_pending)


class QuicStreamAdapter(asyncio.Transport):
    def __init__(self, protocol: QuicConnectionProtocol, stream_id: int):
        self.protocol = protocol
        self.stream_id = stream_id

    def can_write_eof(self) -> bool:
        return True

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """
        Get information about the underlying QUIC stream.
        """
        if name == "connection":
            return self.protocol._connection
        elif name == "stream_id":
            return self.stream_id

    def write(self, data):
        self.protocol._connection.send_stream_data(self.stream_id, data)
        self.protocol._send_soon()

    def write_eof(self):
        self.protocol._connection.send_stream_data(self.stream_id, b"", end_stream=True)
        self.protocol._send_soon()
