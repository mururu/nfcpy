# -*- coding: latin-1 -*-
# -----------------------------------------------------------------------------
# Copyright 2012, 2017 Stephen Tiedemann <stephen.tiedemann@gmail.com>
#
# Licensed under the EUPL, Version 1.1 or - as soon they
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# https://joinup.ec.europa.eu/software/page/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.
# -----------------------------------------------------------------------------
#
# Transport layer for host to reader communication.
#
import os
import re
import errno
from binascii import hexlify

try:
    import usb1 as libusb
except ImportError:
    raise ImportError("missing usb1 module, try 'pip install libusb1'")

try:
    import serial
except ImportError:
    raise ImportError("missing serial module, try 'pip install pyserial'")

import logging
log = logging.getLogger(__name__)

PATH = re.compile(r'^([a-z]+)(?::|)([a-zA-Z0-9]+|)(?::|)([a-zA-Z0-9]+|)$')


class TTY(object):
    TYPE = "TTY"

    @classmethod
    def find(cls, path):
        if not (path.startswith("tty") or path.startswith("com")):
            return

        match = PATH.match(path)

        if match and match.group(1) == "tty":
            import termios
            if re.match(r'^\D+\d+$', match.group(2)):
                TTYS = re.compile(r'^tty{0}$'.format(match.group(2)))
            elif re.match(r'^\D+$', match.group(2)):
                TTYS = re.compile(r'^tty{0}\d+$'.format(match.group(2)))
            elif re.match(r'^$', match.group(2)):
                TTYS = re.compile(r'^tty(S|ACM|AMA|USB)\d+$')
            else:
                log.error("invalid port in 'tty' path: %r", match.group(2))
                return

            ttys = [fn for fn in os.listdir('/dev') if TTYS.match(fn)]
            if len(ttys) == 0:
                return

            # Sort ttys with custom function to correctly order numbers.
            pattern = re.compile('(\D+)(\d+)')
            ttys.sort(key=lambda s: "%s%3s" % pattern.match(s).groups())
            log.debug('trying /dev/tty%s', ' '.join([tty[3:] for tty in ttys]))

            # Eliminate tty nodes that are not physically present or
            # inaccessible by the current user. Propagate IOError when
            # path designated exactly one device, otherwise just log.
            for i, tty in enumerate(ttys):
                try:
                    try:
                        termios.tcgetattr(open('/dev/%s' % tty))
                        ttys[i] = '/dev/%s' % tty
                    except termios.error:
                        pass
                except IOError as error:
                    if not TTYS.pattern.endswith(r'\d+$'):
                        raise
                    else:
                        log.debug(error)

            ttys = [tty for tty in ttys if tty.startswith('/dev/')]
            log.debug('avail: %s', ' '.join([tty for tty in ttys]))
            return ttys, match.group(3), TTYS.pattern.endswith(r'\d+$')

        if match and match.group(1) == "com":
            if re.match(r'^COM\d+$', match.group(2)):
                return [match.group(2)], match.group(3), False
            if re.match(r'^\d+$', match.group(2)):
                return ["COM" + match.group(2)], match.group(3), False
            if re.match(r'^$', match.group(2)):
                import serial.tools.list_ports
                ports = [p[0] for p in serial.tools.list_ports.comports()]
                log.debug('serial ports: %s', ' '.join([p for p in ports]))
                return ports, match.group(3), True
            log.error("invalid port in 'com' path: %r", match.group(2))

    @property
    def manufacturer_name(self):
        return None

    @property
    def product_name(self):
        return None

    def __init__(self, port=None):
        self.tty = None
        self.open(port)

    def open(self, port, baudrate=115200):
        self.close()
        self.tty = serial.Serial(port, baudrate, timeout=0.05)

    @property
    def port(self):
        return self.tty.port if self.tty else ''

    @property
    def baudrate(self):
        return self.tty.baudrate if self.tty else 0

    @baudrate.setter
    def baudrate(self, value):
        if self.tty:
            self.tty.baudrate = value

    def read(self, timeout):
        if self.tty is not None:
            self.tty.timeout = max(timeout/1E3, 0.05)
            frame = bytearray(self.tty.read(6))
            if frame is None or len(frame) == 0:
                raise IOError(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))
            if frame.startswith(b"\x00\x00\xff\x00\xff\x00"):
                log.log(logging.DEBUG-1, "<<< %s", str(frame).encode("hex"))
                return frame
            LEN = frame[3]
            if LEN == 0xFF:
                frame += self.tty.read(3)
                LEN = frame[5] << 8 | frame[6]
            frame += self.tty.read(LEN + 1)
            log.log(logging.DEBUG-1, "<<< %s", hexlify(frame))
            return frame

    def write(self, frame):
        if self.tty is not None:
            log.log(logging.DEBUG-1, ">>> %s", hexlify(frame))
            self.tty.flushInput()
            try:
                self.tty.write(str(frame))
            except serial.SerialTimeoutException:
                raise IOError(errno.EIO, os.strerror(errno.EIO))

    def close(self):
        if self.tty is not None:
            self.tty.flushOutput()
            self.tty.close()
            self.tty = None


class USB(object):
    TYPE = "USB"

    @classmethod
    def find(cls, path):
        if not path.startswith("usb"):
            return

        log.debug("using libusb-{0}.{1}.{2}".format(*libusb.getVersion()[0:3]))

        usb_or_none = re.compile(r'^(usb|)$')
        usb_vid_pid = re.compile(r'^usb(:[0-9a-fA-F]{4})(:[0-9a-fA-F]{4})?$')
        usb_bus_dev = re.compile(r'^usb(:[0-9]{1,3})(:[0-9]{1,3})?$')
        match = None

        for regex in (usb_vid_pid, usb_bus_dev, usb_or_none):
            m = regex.match(path)
            if m is not None:
                log.debug("path matches {0!r}".format(regex.pattern))
                if regex is usb_vid_pid:
                    match = [int(s.strip(':'), 16) for s in m.groups() if s]
                    match = dict(zip(['vid', 'pid'], match))
                if regex is usb_bus_dev:
                    match = [int(s.strip(':'), 10) for s in m.groups() if s]
                    match = dict(zip(['bus', 'adr'], match))
                if regex is usb_or_none:
                    match = dict()
                break
        else:
            return None

        with libusb.USBContext() as context:
            devices = context.getDeviceList(skip_on_error=True)
            vid, pid = match.get('vid'), match.get('pid')
            bus, dev = match.get('bus'), match.get('adr')
            if vid is not None:
                devices = [d for d in devices if d.getVendorID() == vid]
            if pid is not None:
                devices = [d for d in devices if d.getProductID() == pid]
            if bus is not None:
                devices = [d for d in devices if d.getBusNumber() == bus]
            if dev is not None:
                devices = [d for d in devices if d.getDeviceAddress() == dev]
            return [(d.getVendorID(), d.getProductID(), d.getBusNumber(),
                     d.getDeviceAddress()) for d in devices]

    def __init__(self, usb_bus, dev_adr):
        self.context = libusb.USBContext()
        self.open(usb_bus, dev_adr)

    def __del__(self):
        self.close()
        if self.context:
            self.context.exit()

    def open(self, usb_bus, dev_adr):
        self.usb_dev = None
        self.usb_out = None
        self.usb_inp = None

        for dev in self.context.getDeviceList(skip_on_error=True):
            if ((dev.getBusNumber() == usb_bus and
                 dev.getDeviceAddress() == dev_adr)):
                break
        else:
            log.error("no device {0} on bus {1}".format(dev_adr, usb_bus))
            raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))

        try:
            first_setting = dev.iterSettings().next()
        except StopIteration:
            log.error("no usb configuration settings, please replug device")
            raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))

        def transfer_type(x):
            return x & libusb.TRANSFER_TYPE_MASK

        def endpoint_dir(x):
            return x & libusb.ENDPOINT_DIR_MASK

        for endpoint in first_setting.iterEndpoints():
            ep_addr = endpoint.getAddress()
            ep_attr = endpoint.getAttributes()
            if transfer_type(ep_attr) == libusb.TRANSFER_TYPE_BULK:
                if endpoint_dir(ep_addr) == libusb.ENDPOINT_IN:
                    if not self.usb_inp:
                        self.usb_inp = endpoint
                if endpoint_dir(ep_addr) == libusb.ENDPOINT_OUT:
                    if not self.usb_out:
                        self.usb_out = endpoint

        if not (self.usb_inp and self.usb_out):
            log.error("no bulk endpoints for read and write")
            raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))

        try:
            # workaround the PN533's buggy USB implementation
            self._manufacturer_name = dev.getManufacturer()
            self._product_name = dev.getProduct()
        except libusb.USBErrorIO:
            self._manufacturer_name = None
            self._product_name = None

        try:
            self.usb_dev = dev.open()
            self.usb_dev.claimInterface(0)
        except libusb.USBErrorAccess:
            raise IOError(errno.EACCES, os.strerror(errno.EACCES))
        except libusb.USBErrorBusy:
            raise IOError(errno.EBUSY, os.strerror(errno.EBUSY))
        except libusb.USBErrorNoDevice:
            raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))

    def close(self):
        if self.usb_dev:
            self.usb_dev.close()
        self.usb_dev = None
        self.usb_out = None
        self.usb_inp = None

    @property
    def manufacturer_name(self):
        return self._manufacturer_name

    @property
    def product_name(self):
        return self._product_name

    def read(self, timeout=0):
        if self.usb_inp is not None:
            try:
                ep_addr = self.usb_inp.getAddress()
                frame = self.usb_dev.bulkRead(ep_addr, 300, timeout)
            except libusb.USBErrorTimeout:
                raise IOError(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))
            except libusb.USBErrorNoDevice:
                raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))
            except libusb.USBError as error:
                log.error("%r", error)
                raise IOError(errno.EIO, os.strerror(errno.EIO))

            if len(frame) == 0:
                log.error("bulk read returned zero data")
                raise IOError(errno.EIO, os.strerror(errno.EIO))

            frame = bytearray(frame)
            log.log(logging.DEBUG-1, "<<< %s", hexlify(frame))
            return frame

    def write(self, frame, timeout=0):
        if self.usb_out is not None:
            log.log(logging.DEBUG-1, ">>> %s", hexlify(frame))
            try:
                ep_addr = self.usb_out.getAddress()
                self.usb_dev.bulkWrite(ep_addr, bytes(frame), timeout)
                if len(frame) % self.usb_out.getMaxPacketSize() == 0:
                    self.usb_dev.bulkWrite(ep_addr, b'', timeout)
            except libusb.USBErrorTimeout:
                raise IOError(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))
            except libusb.USBErrorNoDevice:
                raise IOError(errno.ENODEV, os.strerror(errno.ENODEV))
            except libusb.USBError as error:
                log.error("%r", error)
                raise IOError(errno.EIO, os.strerror(errno.EIO))