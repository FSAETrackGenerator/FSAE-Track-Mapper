from datetime import datetime
from serial.tools import list_ports
import threading
import time
import serial


def _nmea_to_decimal(raw_value, direction):
    """Converts NMEA ddmm.mmmm / dddmm.mmmm coordinates to signed decimal degrees."""
    if not raw_value:
        return 0.0
    degrees = int(raw_value / 100)
    minutes = raw_value - degrees * 100
    decimal = degrees + minutes / 60.0
    if direction in ('S', 'W'):
        decimal = -decimal
    return decimal


def _decimal_to_nmea(value, is_latitude):
    """Converts signed decimal degrees back to NMEA ddmm.mmmm / dddmm.mmmm + direction."""
    direction = ('N' if value >= 0 else 'S') if is_latitude else ('E' if value >= 0 else 'W')
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60
    degree_width = 2 if is_latitude else 3
    return f"{degrees:0{degree_width}d}{minutes:07.4f}", direction


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nmea_checksum_valid(sentence):
    """Validates the trailing *hh checksum (XOR of all bytes between $ and *).

    Required before trusting a line as real GPS data rather than noise/garbled bytes picked up
    while scanning baudrates - a wrong baudrate can still occasionally produce a line that looks
    like it starts with a sentence prefix, but it will not have a matching checksum.
    """
    sentence = sentence.strip()
    if not sentence.startswith('$') or '*' not in sentence:
        return False
    body, _, checksum_str = sentence[1:].rpartition('*')
    checksum_str = checksum_str[:2]
    if len(checksum_str) != 2:
        return False
    try:
        expected = int(checksum_str, 16)
    except ValueError:
        return False
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return checksum == expected


def _is_gga_sentence(line):
    """True if line looks like a GGA fix sentence, regardless of talker ID (GP/GN/GL/GA/GB/...).

    Multi-constellation receivers commonly report fixes as $GNGGA rather than $GPGGA, so matching
    only the GPS-specific talker ID misses perfectly valid data from those devices.
    """
    sentence_type = line.split(',', 1)[0] if line else ''
    return sentence_type.startswith('$') and sentence_type.endswith('GGA') and len(sentence_type) == 6


# Human-readable labels for the NMEA GGA "fix quality" field, used by the GUI to show a clear
# lock/no-lock indicator instead of a bare number.
FIX_QUALITY_DESCRIPTIONS = {
    0: "No Fix",
    1: "GPS Fix",
    2: "DGPS Fix",
    3: "PPS Fix",
    4: "RTK Fixed",
    5: "RTK Float",
    6: "Dead Reckoning",
    7: "Manual Input",
    8: "Simulation",
}


def _is_port_busy_error(err):
    """True if an open failure looks like the port is already held by another program
    (e.g. PuTTY, Arduino IDE, u-center) rather than the port simply not existing.
    """
    text = str(err).lower()
    return any(marker in text for marker in (
        'access is denied', 'permissiondenied', 'permissionerror', 'in use', 'busy', 'resource busy'))


class GPS:
    # Shared list of baudrates discover_gps scans when no force_baudrate is configured.
    valid_baudrates = [4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

    def __init__(self, force_baudrate=None, force_port=None):
        #core gps information
        self.time = datetime.now()
        self.latitude = 0
        self.latitude_direction = ''
        self.longitude = 0
        self.longitude_direction = ''


        #extra information
        self.position_fix_indicator = 0
        self.satellites_used = 0
        self.HDOP = 0
        self.altitude = 0
        self.altitude_units = ''

        # When set, discover_gps only tries this baudrate instead of scanning valid_baudrates.
        self.force_baudrate = force_baudrate
        # When set, discover_gps only tries this specific port instead of scanning every port
        # (and skips the description-based "looks like a GPS" filter), e.g. for COM13 when its
        # USB-serial chip description doesn't mention "GPS"/"CH340".
        self.force_port = force_port
        self.device = None
        self._last_open_failed = False
        # Set as soon as a port's description advertises a GPS device, even if it never goes
        # on to produce a valid fix below - lets the GUI tell "found but silent" apart from
        # "nothing plugged in" instead of just reporting not-connected in both cases.
        self.description = ''
        # Ports that failed to open with an "access denied"/"in use" style error during
        # discover_gps - lets the GUI report "already open in another program" distinctly from
        # "no GPS device found" at all.
        self.busy_ports = set()
        # guards the fields above since update() is typically polled from a background thread
        # while the GUI reads a snapshot from the main thread
        self.lock = threading.Lock()
        #initialisation protocol
        self.connected = False
        self.connected = self.discover_gps()

    def update_config(self, serial, baudrate):
        self.serial = serial
        self.baudrate = baudrate

    @staticmethod
    def list_available_ports():
        """Returns (device, description) pairs for every serial port currently enumerated by the OS."""
        return [(p.device, p.description) for p in list_ports.comports()]

    def get_fix(self):
        """Thread-safe snapshot of the latest parsed fix, safe to call from another thread."""
        with self.lock:
            return {
                'time': self.time,
                'latitude': self.latitude,
                'longitude': self.longitude,
                'altitude': self.altitude,
                'altitude_units': self.altitude_units,
                'fix_quality': self.position_fix_indicator,
                'satellites': self.satellites_used,
                'hdop': self.HDOP,
            }

    def disconnect(self):
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        self.connected = False

    def send_rtcm(self, data):
        """Writes raw RTCM correction bytes (from an NTRIP client) to the GPS device."""
        if not self.device:
            return False
        try:
            with self.lock:
                self.device.write(data)
            return True
        except Exception:
            return False

    def build_gga_sentence(self):
        """Builds a $GPGGA sentence from the latest fix, e.g. to upload to an NTRIP caster."""
        with self.lock:
            lat, lon, alt = self.latitude, self.longitude, self.altitude
            fix, sats, hdop = self.position_fix_indicator, self.satellites_used, self.HDOP
            alt_units = self.altitude_units or 'M'
        if not fix:
            return None
        lat_str, lat_dir = _decimal_to_nmea(lat, True)
        lon_str, lon_dir = _decimal_to_nmea(lon, False)
        time_str = datetime.utcnow().strftime('%H%M%S.00')
        body = (f"GPGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},"
                f"{fix},{sats:02d},{hdop:.1f},{alt:.1f},{alt_units},0.0,M,,")
        checksum = 0
        for ch in body:
            checksum ^= ord(ch)
        return f"${body}*{checksum:02X}"

    def _try_port_baudrate(self, port_device, baudrate, read_seconds=2):
        """Opens port_device at baudrate and watches for a structurally-valid, checksum-passing
        GGA sentence (regardless of talker ID or whether it currently has a fix). Leaves
        self.device/self.port/self.baudrate set and returns True on success; closes and returns
        False otherwise. Sets self._last_open_failed when the port itself couldn't be opened at
        all, so callers can skip straight to the next port instead of retrying every baudrate -
        baudrate has no bearing on whether the OS/driver will grant access to the port.
        """
        self._last_open_failed = False
        try:
            device = serial.Serial(port_device, baudrate, timeout=1)
        except Exception as e:
            print(f"Error opening {port_device} at baudrate {baudrate}: {e}")
            self._last_open_failed = True
            if _is_port_busy_error(e):
                self.busy_ports.add(port_device)
            return False
        try:
            start_time = time.time()
            while time.time() - start_time < read_seconds:
                line = device.readline().decode('ascii', errors='replace').strip()
                if line and _is_gga_sentence(line) and _nmea_checksum_valid(line):
                    self.device = device
                    self.port = port_device
                    self.baudrate = baudrate
                    self.sentence_parser(line)
                    return True
        except Exception as e:
            print(f"Error reading {port_device} at baudrate {baudrate}: {e}")
        device.close()
        return False

    def discover_gps(self):
        all_ports = list(list_ports.comports())
        baudrates_to_try = [self.force_baudrate] if self.force_baudrate else self.valid_baudrates

        if self.force_port:
            # Explicit port chosen by the user (e.g. COM13): try it directly at every candidate
            # baudrate, skipping the description-based filter entirely since that's exactly what
            # can cause a real GPS on an unrecognised USB-serial chip to be skipped.
            description = next((p.description for p in all_ports if p.device == self.force_port), '')
            self.description = description
            for baudrate in baudrates_to_try:
                if self._try_port_baudrate(self.force_port, baudrate):
                    print(f"GPS found on port {self.force_port} with baudrate {baudrate}")
                    self.description = f"{description or self.force_port} ({self.force_port} @ {baudrate} baud)"
                    return True
                if self._last_open_failed:
                    break  # the port itself couldn't be opened - a different baudrate won't help
                time.sleep(0.3)
            return False

        # Pass 1: ports whose description advertises a GPS device get first try. Record the
        # description as soon as one is found, before we know whether it will yield real data.
        for port in all_ports:
            if not (port.description and ("GPS" in port.description or "CH340" in port.description)):
                continue
            self.description = port.description
            for baudrate in baudrates_to_try:
                if self._try_port_baudrate(port.device, baudrate):
                    print(f"GPS found on port {port.device} with baudrate {baudrate}")
                    self.description = f"{port.description} ({port.device} @ {baudrate} baud)"
                    return True
                if self._last_open_failed:
                    break
                time.sleep(0.3)

        # Pass 2: brute-force scan every port/baudrate for any parsable sentence, in case the
        # device description doesn't mention "GPS" at all.
        for port in all_ports:
            for baudrate in baudrates_to_try:
                if self._try_port_baudrate(port.device, baudrate):
                    print(f"GPS found on port {port.device} with baudrate {baudrate}")
                    self.description = f"{port.description or port.device} ({port.device} @ {baudrate} baud)"
                    return True
                if self._last_open_failed:
                    break

        return False

    def update(self):
        if not self.device:
            return
        try:
            line = self.device.readline().decode('ascii', errors='replace').strip()
        except Exception:
            return
        if line:
            self.sentence_parser(line)


    def sentence_parser(self, sentence):
        split_sentence = sentence.split(',')
        sentence_type = split_sentence[0] if split_sentence else ''

        if not _is_gga_sentence(sentence_type):
            return False
        # Reject anything that isn't a properly checksummed GGA sentence - this is what stops a
        # mismatched baudrate scan from "succeeding" on garbled bytes that merely happen to start
        # with a plausible-looking prefix.
        if not _nmea_checksum_valid(sentence):
            return False

        try:
            time_str = split_sentence[1] if len(split_sentence) > 1 else ''
            parsed_fix = _safe_int(split_sentence[6]) if len(split_sentence) > 6 else 0
            parsed_sats = _safe_int(split_sentence[7]) if len(split_sentence) > 7 else 0
            parsed_hdop = _safe_float(split_sentence[8]) if len(split_sentence) > 8 else 0.0

            with self.lock:
                if time_str:
                    fmt = "%H%M%S.%f" if '.' in time_str else "%H%M%S"
                    try:
                        self.time = datetime.strptime(time_str, fmt)
                    except ValueError:
                        pass
                self.position_fix_indicator = parsed_fix
                self.satellites_used = parsed_sats
                self.HDOP = parsed_hdop

                # Lat/lon/alt fields are blank when the receiver has no fix yet (fix == 0) - in
                # that case leave the last known position in place rather than raising/zeroing it,
                # while fix_quality above still correctly reports "no lock".
                has_position = (parsed_fix and len(split_sentence) > 5
                                 and split_sentence[2] and split_sentence[4])
                if has_position:
                    self.latitude = _nmea_to_decimal(float(split_sentence[2]), split_sentence[3])
                    self.latitude_direction = split_sentence[3]
                    self.longitude = _nmea_to_decimal(float(split_sentence[4]), split_sentence[5])
                    self.longitude_direction = split_sentence[5]
                    if len(split_sentence) > 10:
                        self.altitude = _safe_float(split_sentence[9])
                        self.altitude_units = split_sentence[10]
            return True
        except Exception as e:
            print(f"Error parsing {sentence_type} sentence: {e}")
            return False


if __name__ == "__main__":
    gps = GPS()

    while True:
        gps.update()
        lock_state = FIX_QUALITY_DESCRIPTIONS.get(gps.position_fix_indicator, "Unknown")
        print(f"Time: {gps.time}, Fix: {lock_state} ({gps.satellites_used} sats), "
              f"Latitude: {gps.latitude} {gps.latitude_direction}, "
              f"Longitude: {gps.longitude} {gps.longitude_direction}, Altitude: {gps.altitude} {gps.altitude_units}")

