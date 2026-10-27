"""
Minimal NTRIP (v1/v2) client.

Talks to an NTRIP caster such as Geoscience Australia's AUSCORS broadcaster
(ntrip.data.gnss.ga.gov.au) to:
  * fetch the sourcetable (list of available mountpoints/sites), and
  * stream RTCM correction data for a chosen mountpoint, periodically
    uploading a $GPGGA position sentence as some casters require it to
    select/compute the correction stream.

Implemented directly on top of `socket`/`ssl` since NTRIP is just HTTP(S)
with a long-lived response body, so no extra dependencies are required.
"""

import base64
import socket
import ssl
import threading
import time


class NtripError(Exception):
    pass


class NtripClient:
    def __init__(self, host, port, mountpoint='', username='', password='',
                 use_tls=None, gga_provider=None, on_data=None, on_status=None):
        self.host = host
        self.port = int(port)
        self.mountpoint = (mountpoint or '').lstrip('/')
        self.username = username or ''
        self.password = password or ''
        # Port 443 is TLS by convention for the GA caster; anything else defaults to plain TCP.
        self.use_tls = use_tls if use_tls is not None else (self.port == 443)
        self.gga_provider = gga_provider  # callable () -> NMEA GGA string (no CRLF), or None
        self.on_data = on_data            # callable(bytes) for each chunk of RTCM data received
        self.on_status = on_status        # callable(str) for status/error text updates

        self._sock = None
        self._thread = None
        self._running = False
        self.connected = False
        self.bytes_received = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _emit_status(self, msg):
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _auth_header(self):
        if not self.username and not self.password:
            return ''
        token = base64.b64encode(f"{self.username}:{self.password}".encode('utf-8')).decode('ascii')
        return f"Authorization: Basic {token}\r\n"

    def _open_socket(self, timeout=10):
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.use_tls:
            ctx = ssl.create_default_context()
            return ctx.wrap_socket(raw, server_hostname=self.host)
        return raw

    def _request(self, path):
        return (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"User-Agent: NTRIP FSAETrackMapper/1.0\r\n"
            f"{self._auth_header()}"
            f"Connection: close\r\n\r\n"
        )

    @staticmethod
    def _status_line_ok(text):
        first_line = text.splitlines()[0] if text else ''
        return '200' in first_line, first_line

    # ------------------------------------------------------------------
    # Sourcetable (mountpoint list)
    # ------------------------------------------------------------------
    def fetch_sourcetable(self, timeout=10):
        """Connects, requests '/', and returns a list of mountpoint dicts."""
        sock = self._open_socket(timeout=timeout)
        try:
            sock.sendall(self._request('/').encode('ascii'))
            sock.settimeout(timeout)
            data = b''
            while b'ENDSOURCETABLE' not in data:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()

        text = data.decode('ascii', errors='replace')
        header, _, body = text.partition('\r\n\r\n')
        ok, first_line = self._status_line_ok(header)
        if not ok:
            raise NtripError(f"Sourcetable request failed: {first_line or 'no response from caster'}")
        return self._parse_sourcetable(body)

    @staticmethod
    def _parse_sourcetable(body):
        # STR;mountpoint;identifier;format;format-details;carrier;nav-system;network;
        #     country;latitude;longitude;nmea;solution;generator;compr-encrp;
        #     authentication;fee;bitrate;misc
        mounts = []
        for line in body.splitlines():
            if not line.startswith('STR;'):
                continue
            fields = line.split(';')

            def field(i):
                return fields[i] if i < len(fields) else ''

            try:
                lat = float(field(9)) if field(9) else None
                lon = float(field(10)) if field(10) else None
            except ValueError:
                lat = lon = None

            mounts.append({
                'mountpoint': field(1),
                'identifier': field(2) or field(1),
                'format': field(3),
                'carrier': field(5),
                'nav_system': field(6),
                'network': field(7),
                'country': field(8),
                'latitude': lat,
                'longitude': lon,
                'authentication': field(15),
            })
        return mounts

    # ------------------------------------------------------------------
    # Streaming connection
    # ------------------------------------------------------------------
    def connect(self):
        if self._running:
            return
        if not self.mountpoint:
            raise NtripError("Mountpoint is required to connect.")
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._running = False
        self.connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _run(self):
        try:
            self._sock = self._open_socket(timeout=15)
            self._sock.sendall(self._request('/' + self.mountpoint).encode('ascii'))
            self._sock.settimeout(10)

            header_buf = b''
            while b'\r\n\r\n' not in header_buf and len(header_buf) < 4096:
                chunk = self._sock.recv(1)
                if not chunk:
                    break
                header_buf += chunk
            header_text = header_buf.decode('ascii', errors='replace')
            ok, first_line = self._status_line_ok(header_text)
            if not ok:
                raise NtripError(f"Connection rejected by caster: {first_line or 'no response'}")

            self.connected = True
            self.bytes_received = 0
            self._emit_status(f"Connected to {self.mountpoint}")

            last_gga = 0
            self._sock.settimeout(1.0)
            while self._running:
                if self.gga_provider and time.time() - last_gga > 10:
                    last_gga = time.time()
                    gga = self.gga_provider()
                    if gga:
                        try:
                            self._sock.sendall((gga + "\r\n").encode('ascii'))
                        except Exception:
                            pass
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self.bytes_received += len(chunk)
                if self.on_data:
                    try:
                        self.on_data(chunk)
                    except Exception:
                        pass
        except Exception as e:
            self._emit_status(f"NTRIP error: {e}")
        finally:
            self.connected = False
            self._running = False
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._emit_status("Disconnected")


if __name__ == "__main__":
    import sys

    client = NtripClient(host="ntrip.data.gnss.ga.gov.au", port=2101)
    try:
        table = client.fetch_sourcetable()
    except NtripError as e:
        print(f"Failed to fetch sourcetable: {e}")
        sys.exit(1)
    for m in table:
        print(f"{m['mountpoint']:14s} {m['identifier']}")
