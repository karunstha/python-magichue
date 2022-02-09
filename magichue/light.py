from abc import ABCMeta, abstractmethod
import struct
from datetime import datetime
import socket
import select
import logging

from .commands import (
    Command,
    TurnON,
    TurnOFF,
    QueryStatus,
    QueryCurrentTime
)
from .exceptions import (
    InvalidData,
    DeviceOffline,
    DeviceDisconnected,
)

from .magichue import Status
from . import modes
from . import bulb_types


_LOGGER = logging.getLogger(__name__)


class AbstractLight(metaclass=ABCMeta):
    '''An abstract class of MagicHue Light.'''

    status: Status
    allow_fading: bool

    def __repr__(self):
        on = 'on' if self.status.on else 'off'
        class_name = self.__class__.__name__
        if self.status.mode.value != modes._NORMAL:
            return '<%s: %s (%s)>' % (class_name, on, self.status.mode.name)
        else:
            if self.status.bulb_type == bulb_types.BULB_RGBWW:
                return '<{}: {} (r:{} g:{} b:{} w:{})>'.format(
                    class_name,
                    on,
                    *(self.status.rgb()),
                    self.status.w,
                )
            if self.status.bulb_type == bulb_types.BULB_RGBWWCW:
                return '<{}: {} (r:{} g:{} b:{} w:{} cw:{})>'.format(
                    class_name,
                    on,
                    *(self.status.rgb()),
                    self.status.w,
                    self.status.cw,
                )
            if self.status.bulb_type == bulb_types.BULB_TAPE:
                return '<{}: {} (r:{} g:{} b:{})>'.format(
                    class_name,
                    on,
                    *(self.status.rgb()),
                )

    @abstractmethod
    def _send_command(self, cmd: Command, send_only: bool = True):
        pass

    def _get_status_data(self):
        data = self._send_command(QueryStatus, send_only=False)
        return data

    def get_current_time(self) -> datetime:
        '''Get bulb clock time.'''

        data = self._send_command(QueryCurrentTime, send_only=False)
        bulb_date = datetime(
            data[3] + 2000,  # Year
            data[4],         # Month
            data[5],         # Date
            data[6],         # Hour
            data[7],         # Minute
            data[8],         # Second
        )
        return bulb_date

    def turn_on(self):
        self._send_command(TurnON)
        self.status.on = True

    def turn_off(self):
        self._send_command(TurnOFF)
        self.status.on = False

    def _update_status(self):
        data = self._get_status_data()
        self.status.parse(data)

    def _apply_status(self):
        data = self.status.make_data()
        cmd = Command.from_array(data)
        self._send_command(cmd)


class RemoteLight(AbstractLight):

    _LOGGER = logging.getLogger(__name__ + '.RemoteLight')

    def __init__(self, api, macaddr: str):
        self.api = api
        self.macaddr = macaddr
        self.status = Status()

    def _send_command(self, cmd: Command, send_only: bool = True):
        self._LOGGER.debug('Sending command({}) to: {}'.format(
            cmd.__name__,
            self.macaddr,
        ))
        if send_only:
            return self.api._send_command(cmd, self.macaddr)
        else:
            data = self.str2hexarray(self._send_request(cmd))
            if len(data) != cmd.response_len:
                raise InvalidData(
                    'Expect length: %d, got %d\n%s' % (
                        cmd.response_len, len(data), str(data)
                    )
                )
            return data

    def _send_request(self, cmd: Command):
        return self.api._send_request(cmd, self.macaddr)

    @staticmethod
    def str2hexarray(hexstr: str) -> tuple:
        ls = [int(hexstr[i:i+2], 16) for i in range(0, len(hexstr), 2)]
        return tuple(ls)


class LocalLight(AbstractLight):

    _LOGGER = logging.getLogger(__name__ + '.LocalLight')

    port = 5577
    timeout = 1

    def __init__(self, ipaddr):
        self.ipaddr = ipaddr
        self._connect()
        self.status = Status()

    def _connect(self):
        self._LOGGER.debug('Trying to make a connection with bulb(%s)' % self.ipaddr)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.ipaddr, self.port))
        self._LOGGER.debug('Connection has been established with %s' % self.ipaddr)

    def _send(self, data):
        self._LOGGER.debug(
            'Trying to send data(%s) to %s' % (str(data), self.ipaddr)
        )
        if self._sock._closed:
            raise DeviceDisconnected
        self._sock.send(data)

    def _receive(self, length):
        self._LOGGER.debug(
            'Trying to receive %d bytes data from %s' % (length, self.ipaddr)
        )
        if self._sock._closed:
            raise DeviceDisconnected

        data = self._sock.recv(length)
        self._LOGGER.debug(
            'Got %d bytes data from %s' % (len(data), self.ipaddr)
        )
        self._LOGGER.debug('Received data: %s' % str(data))
        return data

    def _flush_receive_buffer(self):
        self._LOGGER.debug('Flushing receive buffer')
        if self._sock._closed:
            raise DeviceDisconnected
        while True:
            read_sock, _, _ = select.select([self._sock], [], [], self.timeout)
            if not read_sock:
                self._LOGGER.debug('Nothing received. buffer has been flushed')
                break
            self._LOGGER.debug('There is stil something in the buffer')
            _ = self._receive(255)
            if not _:
                raise DeviceDisconnected

    def _send_command(self, cmd: Command, send_only: bool = True):
        self._LOGGER.debug('Sending command({}) to {}: {}'.format(
            cmd.__name__,
            self.ipaddr,
            cmd.byte_string(),
        ))
        if send_only:
            self._send(cmd.byte_string())
        else:
            self._flush_receive_buffer()
            self._send(cmd.byte_string())
            data = self._receive(cmd.response_len)
            decoded_data = struct.unpack(
                    '!%dB' % len(data),
                    data
            )
            if len(data) == cmd.response_len:
                return decoded_data
            else:
                raise InvalidData(
                    'Expect length: %d, got %d\n%s' % (
                        cmd.response_len,
                        len(decoded_data),
                        str(decoded_data)
                    )
                )

    def _connect(self, timeout=3):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((self.ipaddr, self.port))
