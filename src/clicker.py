import itertools
import json
import os
import enum
import pathlib
import asyncio
import threading
from collections import defaultdict

from aioquic.asyncio import serve
from aioquic.asyncio import protocol as aio_protocol
from aioquic.quic import events as quic_events
from aioquic.h3 import events as h3_events
from aioquic.h3 import connection as h3_connection
from aioquic.quic import connection as quic_connection
from aioquic.quic import configuration as quic_configuration


class SimpleScoreStore:
    """
    Простое хранилище игроков и их счетов.
    (In-Memory)
    """

    def __init__(self) -> None:
        self.scores = defaultdict(itertools.count)

    def inc_score(self, user: str) -> int:
        """ Увеличить счет на единицу и вернуть текущее значение. """
        return next(self.scores[user])


# Создадим экземпляр хранилища счетов игроков.
simple_score_store = SimpleScoreStore()


class ClickerHandler:
    """
    Обработчик запросов Кликера.
    """

    def __init__(
            self,
            connection: h3_connection.H3Connection,
            stream_id: int,
    ) -> None:
        self.connection = connection
        self.session_stream_id = stream_id

    def create_wt_unidirectional_stream(self) -> int:
        """
        Создать однонаправленный поток WebTransport.
        :return: Идентификатор потока.
        """
        return self.connection.create_webtransport_stream(
            session_id=self.session_stream_id, is_unidirectional=True
        )

    def send_stream_data(
            self,
            stream_id: int,
            data: bytes,
            end_stream: bool = False
    ) -> None:
        """
        Отправить данные в указанный поток.

        :param stream_id: Идентификатор потока, в который отправляются данные.
        :param data: Данные для отправки.
        :param end_stream: Отметка о необходимости закрыть поток.
        """
        self.connection._quic.send_stream_data(
            stream_id=stream_id,
            data=data,
            end_stream=end_stream,
        )

    def send_datagram(self, data: bytes, stream_id: int | None = None) -> None:
        """
        Отправка данных с использованием дейтаграммы (DatagramFrame).
        :param stream_id: Идентификатор потока, куда нужно будет направить данные.
        :param data: Данные для отправки.
        """
        if stream_id is None:
            stream_id = self.session_stream_id
        self.connection.send_datagram(stream_id, data=data)

    def stop_stream(self, stream_id: int, code: int) -> None:
        """
        Отправьте DatagramFrame с STOP_SENDING в указанный поток.
        :param stream_id: Идентификатор потока который планируется остановить.
        :param code: Код ошибки.
        """
        self.connection._quic.stop_stream(stream_id, code)

    def reset_stream(self, stream_id: int, code: int) -> None:
        """
        Отправьте DatagramFrame с RESET_STREAM в указанный поток.
        :param stream_id: Идентификатор потока который планируется сбросить.
        :param code: the reason of the error.
        """
        self.connection._quic.reset_stream(stream_id, code)

    def h3_event_received(self, event: h3_events.H3Event):
        """
        Обработать событие HTTP/3.
        
        :param event: Экземпляр события HTTP/3.
        """
        if isinstance(event, h3_events.DatagramReceived):
            self._datagram_received(event.data)

        elif isinstance(event, h3_events.WebTransportStreamDataReceived):
            self._stream_data_received(event.stream_id, event.data, event.stream_ended)

    def _stream_data_received(self, stream_id: int, data: bytes, stream_ended: bool):
        """
        Обработать данные полученные в потоке WebTransport.

        :param stream_id: Идентификатор потока, из которого получены данные.
        :param data: Полученные данные.
        :param stream_ended: Был ли установлен бит FIN в кадре STREAM.
        """
        res = self.payload_to_score(data)
        if quic_connection.stream_is_unidirectional(stream_id):
            response_stream_id = self.create_wt_unidirectional_stream()
        else:
            response_stream_id = stream_id
        if res is not None:
            res = json.dumps({"status": 200, "score": res}).encode('utf-8')
        else:
            res = json.dumps({"status": 400}).encode('utf-8')
        self.send_stream_data(response_stream_id, res, end_stream=False)
        if stream_ended:
            self._stream_closed(stream_id)

    def _datagram_received(self, data: bytes):
        """
        Обработать данные полученные в сеансе WebTransport из дейтаграммы.

        :param data: Полученные данные.
        """
        res = self.payload_to_score(data)
        if res is not None:
            self.send_datagram(json.dumps({"status": 200, "score": res}).encode('utf-8'))
        else:
            self.send_datagram(json.dumps({"status": 400}).encode('utf-8'))

    @staticmethod
    def payload_to_score(data: bytes) -> int | None:
        """
        Получить счет пользователя.

        :param data: Данные, которые указывают на пользователя и команду.
        :return: Счет пользователя.
        """
        res = None
        data: dict = json.loads(data.decode('utf-8')) if len(data) > 0 else {}
        username = data.get('username')
        type_ = data.get('type')
        if type_ == 'increment':
            if username:
                res = simple_score_store.inc_score(username)
        return res

    def _stream_closed(self, stream_id: int):
        """
        Вызывается при закрытии потока WebTransport.

        :param stream_id: Идентификатор потока.
        """
        self.send_stream_data(stream_id, b'', end_stream=True)

    def stream_reset(
            self,
            stream_id: int,
            error_code: int
    ) -> None:
        """
        Обработать событие, когда Удаленный узел запросил сброс потока.

        :param stream_id: Идентификатор потока.
        :param error_code: Код ошибки.
        """
        pass


class WebTransportProtocol(aio_protocol.QuicConnectionProtocol):
    """
    WebTransportProtocol обрабатывает соединения WebTransport и
    перенаправляет транспортные события соответствующему обработчику.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http: h3_connection.H3Connection | None = None
        self._handler: ClickerHandler | None = None

    def quic_event_received(self, event: quic_events.QuicEvent):
        if isinstance(event, quic_events.ProtocolNegotiated):
            if event.alpn_protocol == "h3":
                self._http = h3_connection.H3Connection(
                    self._quic,
                    enable_webtransport=True,
                )
        elif isinstance(event, quic_events.StreamReset) and self._handler is not None:
            self._handler.stream_reset(event.stream_id, event.error_code)

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                self.h3_event_received(h3_event)

    def h3_event_received(self, event: h3_events.H3Event):
        """
        Обработка событий HTTP/3.
        """
        if isinstance(event, h3_events.HeadersReceived):
            headers = {}
            for header, value in event.headers:
                headers[header] = value

            if headers.get(b":method") == b"CONNECT":
                self.connect_received(event.stream_id, headers)
            else:
                self._send_headers(event.stream_id, status_code=404, end_stream=True)

        if self._handler is not None:
            self._handler.h3_event_received(event)

    def connect_received(self, stream_id: int, request_headers: dict[bytes, bytes]):
        """
        Обработка события когда удаленный узел отправил запрос с методом CONNECT.

        :param stream_id: Идентификатор потока.
        :param request_headers: Заголовки запросов, полученные от удаленного узла.
        """
        if request_headers.get(b":protocol") == b"webtransport":
            authority = request_headers.get(b":authority")
            path = request_headers.get(b":path")
            self._handshake_wt(stream_id, authority, path)
        else:
            self._send_headers(
                stream_id, status_code=404, end_stream=True,
                details=b"Unsupported protocol.",
            )

    def _handshake_wt(self, stream_id: int, authority: bytes | None, path: bytes | None, ):
        """
        Обработка события когда удаленный узел запросил соединение по протоколу WebTransport.
        :param stream_id: Идентификатор потока.
        :param authority: В контексте HTTP/3, "authority" обычно используется для обозначения имени хоста или домена.
        :param path: Путь, который должен быть доступен для WebTransport.
        """
        if self._http is None:
            self._send_headers(
                stream_id, status_code=500, end_stream=True,
                details=b"H3Connection not created.",
            )
        if authority is None or path is None:
            self._send_headers(
                stream_id, status_code=400, end_stream=True,
                details=b":authority` and `:path` must be provided.",
            )
        elif path == b"/clicker":
            self._handler = ClickerHandler(connection=self._http, stream_id=stream_id)
            self._send_headers(stream_id, status_code=200)
        else:
            self._send_headers(stream_id, status_code=404, details=b"Path not found.", end_stream=True)

    def _send_headers(
            self,
            stream_id: int,
            status_code: int,
            details: bytes | None = None,
            end_stream=False
    ):
        """
        Отправить заголовки HTTP/3.
        :param stream_id: Идентификатор потока.
        :param status_code: Код ошибки.
        :param end_stream: Отметка о необходимости завершить поток.
        """
        headers = [(b":status", str(status_code).encode())]
        if details is not None:
            headers.append((b"details", details))
        if status_code == 200:
            headers.append((b"sec-webtransport-http3-draft", b"draft02"))
        self._http.send_headers(
            stream_id=stream_id, headers=headers, end_stream=end_stream
        )


class ServerStatus(enum.Enum):
    """ Состояния работы сервера. """
    # ожидает запуска
    WAITING_TO_START = 1
    # запущен
    RUNNING = 2
    # остановлен
    STOPPED = 3


class ClickerServer:
    """ Сервер кликера. """

    def __init__(
            self,
            host: str,
            port: int,
            certfile: os.PathLike,
            keyfile: os.PathLike | None = None,
            password: bytes | str | None = None,
    ):
        self._thread: threading.Thread | None = None
        self._host = host
        self._port = port
        self._configuration = quic_configuration.QuicConfiguration(
            alpn_protocols=h3_connection.H3_ALPN,
            is_client=False,
            max_datagram_frame_size=65536,
        )
        self._configuration.load_cert_chain(
            certfile=certfile,
            keyfile=keyfile,
            password=password,
        )
        self._status = ServerStatus.WAITING_TO_START
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self, non_blocking_mode: bool = False):
        """
        Запустить сервер, если он еще не запущен.
        :param non_blocking_mode: Запустить в неблокирующем режиме.
        """
        if self._status == ServerStatus.RUNNING:
            return
        if non_blocking_mode:
            self._thread = threading.Thread(
                target=self._run_until_stopped,
                daemon=True,
            )
            self._thread.start()
        else:
            self._run_until_stopped()

    def stop(self):
        """
        Остановить сервер.

        Будет выполнена попытка остановить цикл событий,
        запущенный в отдельном потоке, как можно скорее.
        """
        if self._status == ServerStatus.RUNNING:
            if self._loop:
                if self._thread:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                    self._thread.join(timeout=15.0)
                    if self._thread.is_alive():
                        raise RuntimeError(
                            "Не удалось остановить сервер за 15 секунд. "
                            "Возможно, он завис."
                        )
                else:
                    self._loop.stop()
        self._status = ServerStatus.STOPPED
        self._thread = None
        self._loop = None

    def _run_until_stopped(self):
        self._status = ServerStatus.RUNNING
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(
            serve(
                host=self._host,
                port=self._port,
                configuration=self._configuration,
                create_protocol=WebTransportProtocol
            )
        )
        self._loop.run_forever()

    def status(self) -> ServerStatus:
        """
        Возвращает текущий статус сервера.
        :return: Статус сервера.
        """
        return self._status


if __name__ == '__main__':
    cert_file = pathlib.Path(os.path.join(os.path.dirname(__file__), 'certificate.pem'))
    key_for_cert_file = pathlib.Path(os.path.join(os.path.dirname(__file__), 'certificate.key'))
    if not cert_file.exists() or not key_for_cert_file.exists():
        raise RuntimeError("Не найден файл сертификата и ключа.")
    server = ClickerServer(
        host='::1',
        port=4433,
        certfile=cert_file,
        keyfile=key_for_cert_file,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        server.stop()
