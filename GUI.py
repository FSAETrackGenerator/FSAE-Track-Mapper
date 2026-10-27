"""
FSAE Track Mapper - live GPS cone recorder.

Connects to a GPS receiver (via GPS.py), plots live position on a 2D map,
lets you record orange/purple/left/right cone and reference-position
placemarks at the current fix, edit or delete recorded points, and export/
import a .kml file compatible with FSAE_track_virtualiser_blender.py.

Optionally connects to an NTRIP caster (e.g. Geoscience Australia's AUSCORS
broadcaster at ntrip.data.gnss.ga.gov.au) to stream RTCM correction data to
the GPS receiver for RTK-corrected fixes.
"""

import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from xml.sax.saxutils import escape

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from GPS import GPS, FIX_QUALITY_DESCRIPTIONS
from NTRIP import NtripClient, NtripError

# Reuse the exact same KML placemark parser as the Blender addon so imported
# files round-trip with identical name/type/index parsing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from FSAE_track_virtualiser_blender import parse_kml_features  # noqa: E402

# Colors for the GPS lock indicator, keyed by NMEA GGA fix-quality code (see
# GPS.FIX_QUALITY_DESCRIPTIONS for the matching labels).
FIX_QUALITY_COLORS = {
    0: '#c62828',  # No Fix - red
    1: '#2e7d32',  # GPS Fix - green
    2: '#1565c0',  # DGPS Fix - blue
    3: '#1565c0',  # PPS Fix - blue
    4: '#00695c',  # RTK Fixed - teal
    5: '#ef6c00',  # RTK Float - orange
    6: '#6a1b9a',  # Dead Reckoning - purple
    7: '#6a1b9a',  # Manual Input - purple
    8: '#6a1b9a',  # Simulation - purple
}

CONE_TYPES = [
    ('orange_cone', 'Orange Cone', '#ff6a00'),
    ('purple_cone', 'Purple Cone', '#6a1fd0'),
    ('left_cone', 'Left Cone', '#1976d2'),
    ('right_cone', 'Right Cone', '#f2c200'),
]
REFERENCE_TYPE = 'reference_position'
DEFAULT_COLOR = '#888888'
CONE_COLORS = {t: c for t, _, c in CONE_TYPES}
CONE_COLORS[REFERENCE_TYPE] = '#e53935'
EARTH_RADIUS_M = 6_378_137.0


def latlon_to_local_xy_m(latitude, longitude, origin_latitude, origin_longitude):
    """Project nearby WGS84 coordinates to local east/north metres."""
    x = math.radians(longitude - origin_longitude) * EARTH_RADIUS_M * math.cos(
        math.radians(origin_latitude))
    y = math.radians(latitude - origin_latitude) * EARTH_RADIUS_M
    return x, y


def name_for(p):
    if p['type'] == REFERENCE_TYPE:
        return REFERENCE_TYPE
    if p['type'] in ('left_cone', 'right_cone') and p.get('group'):
        return f"{p['type']}-{p['idx']}_{p['group']}"
    return p['type']


class TrackMapperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("FSAE Track Mapper")
        self.root.geometry("1200x750")

        self.gps = None
        self.polling = False
        self.next_id = 1
        self.placemarks = []  # list of {id, type, idx, group, lat, lon, alt}
        self.selected_id = None
        # Start Chicane tags every Left/Right cone recorded next with the same auto-incrementing
        # "_<group>" suffix (e.g. left_cone-3_2), until ended or a new chicane is started.
        self.chicane_active = False
        self.chicane_group_counter = 0
        self.current_chicane_group = None
        # First data point/fix auto-fits the view; afterwards pan/zoom is left alone across
        # redraws so scrolling/dragging the map isn't reset every time a point is recorded.
        self._view_fitted = False
        self._map_origin_latlon = None
        self._tracking_span_m = (100.0, 100.0)
        self._updating_plot = False
        self.live_marker = None
        self._rendered_live_fix = False

        self.ntrip = None
        self.ntrip_mounts = []  # cached sourcetable entries from the last "Fetch Sites"
        self._ntrip_profiles_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'ntrip_profiles.json')
        self._ntrip_window = None  # lazily-created Toplevel; reused via deiconify/withdraw
        self._init_ntrip_vars()

        self._build_ui()
        self.refresh_plot()
        self.root.after(100, self._refresh_live_display)

    def _init_ntrip_vars(self):
        """Creates the NTRIP config Tk variables up front so they exist (and can be auto-loaded
        from a saved profile) whether or not the NTRIP dialog has been opened yet.
        """
        self.ntrip_profile_var = tk.StringVar()
        self.ntrip_host_var = tk.StringVar(value="ntrip.data.gnss.ga.gov.au")
        self.ntrip_port_var = tk.StringVar(value="2101")
        self.ntrip_tls_var = tk.BooleanVar(value=False)
        self.ntrip_user_var = tk.StringVar()
        self.ntrip_pass_var = tk.StringVar()
        self.ntrip_city_var = tk.StringVar()
        self.ntrip_mountpoint_var = tk.StringVar()
        self.ntrip_status_var = tk.StringVar(value="Not connected")

        # Auto-load the first saved profile (if any) so reopening the app restores the last config.
        profiles = self._list_ntrip_profiles()
        if profiles:
            self.ntrip_profile_var.set(profiles[0])
            self.load_ntrip_profile()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)

        self.connect_btn = ttk.Button(top, text="Connect", command=self.on_connect_clicked)
        self.connect_btn.pack(side=tk.LEFT)
        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self.on_disconnect_clicked, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(top, text="Port:").pack(side=tk.LEFT, padx=(12, 4))
        self.port_var = tk.StringVar(value="Auto")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, state='readonly', width=22)
        self.port_combo.pack(side=tk.LEFT)
        self._refresh_port_list()
        ttk.Button(top, text="Refresh", command=self._refresh_port_list, width=8).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(top, text="Baudrate:").pack(side=tk.LEFT, padx=(12, 4))
        self.baudrate_var = tk.StringVar(value="Auto")
        self.baudrate_combo = ttk.Combobox(
            top, textvariable=self.baudrate_var, state='readonly', width=8,
            values=["Auto"] + [str(b) for b in GPS.valid_baudrates],
        )
        self.baudrate_combo.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=12)

        ttk.Button(top, text="NTRIP...", command=self.open_ntrip_dialog).pack(side=tk.LEFT, padx=(12, 4))
        ttk.Label(top, text="NTRIP:").pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(top, textvariable=self.ntrip_status_var).pack(side=tk.LEFT)

        ttk.Button(top, text="Fit View", command=self.fit_view).pack(side=tk.RIGHT)

        live = ttk.LabelFrame(self.root, text="Live GPS Data", padding=8)
        live.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))

        self.lock_var = tk.StringVar(value="Not Connected")
        self.lock_label = tk.Label(live, textvariable=self.lock_var, width=14, relief='solid', bd=1,
                                    bg='#616161', fg='white', font=('TkDefaultFont', 9, 'bold'))
        self.lock_label.grid(row=0, column=0, sticky='w', padx=(0, 16))

        self.live_vars = {}
        fields = [('lat', 'Latitude'), ('lon', 'Longitude'), ('alt', 'Altitude (m)'),
                  ('fix', 'Fix Quality'), ('sats', 'Satellites'), ('hdop', 'HDOP'), ('time', 'Time')]
        for i, (key, label) in enumerate(fields):
            ttk.Label(live, text=f"{label}:").grid(row=0, column=2 * i + 1, sticky='e', padx=(0, 4))
            var = tk.StringVar(value="--")
            self.live_vars[key] = var
            ttk.Label(live, textvariable=var, width=12).grid(row=0, column=2 * i + 2, sticky='w')

        record = ttk.LabelFrame(self.root, text="Record Landmark at Current Fix", padding=8)
        record.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))

        for type_key, label, color in CONE_TYPES:
            btn = tk.Button(record, text=label, bg=color, fg='white', activebackground=color,
                             command=lambda t=type_key: self.record_point(t))
            btn.pack(side=tk.LEFT, padx=4)

        ref_btn = tk.Button(record, text="Reference Position", bg=CONE_COLORS[REFERENCE_TYPE], fg='white',
                             activebackground=CONE_COLORS[REFERENCE_TYPE],
                             command=lambda: self.record_point(REFERENCE_TYPE))
        ref_btn.pack(side=tk.LEFT, padx=(16, 4))

        self.chicane_btn = tk.Button(record, text="Start Chicane", command=self.on_chicane_clicked)
        self.chicane_btn.pack(side=tk.LEFT, padx=(16, 4))
        self.chicane_status_var = tk.StringVar(value="No chicane active.")
        ttk.Label(record, textvariable=self.chicane_status_var).pack(side=tk.LEFT, padx=(6, 0))

        body = ttk.Frame(self.root)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # --- Map (left) ---
        map_frame = ttk.Frame(body)
        map_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.figure = Figure(figsize=(6, 6), dpi=100)
        self.ax = self.figure.add_subplot(111)
        map_controls = ttk.Frame(map_frame)
        map_controls.pack(side=tk.TOP, fill=tk.X)
        self.track_live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            map_controls, text="Track live position", variable=self.track_live_var,
            command=self.on_tracking_toggled,
        ).pack(side=tk.LEFT)
        self.canvas = FigureCanvasTkAgg(self.figure, master=map_frame)
        self.toolbar = NavigationToolbar2Tk(self.canvas, map_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect('button_press_event', self.on_map_click)
        self.canvas.mpl_connect('scroll_event', self.on_scroll)

        # --- Side panel (right): list + edit + import/export ---
        side = ttk.Frame(body, width=380)
        side.pack(side=tk.LEFT, fill=tk.BOTH, padx=(8, 0))
        side.pack_propagate(False)

        ttk.Label(side, text="Recorded Placemarks").pack(anchor='w')
        columns = ('name', 'lat', 'lon', 'alt')
        self.tree = ttk.Treeview(side, columns=columns, show='headings', selectmode='browse', height=14)
        for col, label, width in [('name', 'Name', 130), ('lat', 'Lat', 80), ('lon', 'Lon', 80), ('alt', 'Alt', 60)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor='center')
        self.tree.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        self.tree.bind('<<TreeviewSelect>>', self.on_tree_select)

        edit = ttk.LabelFrame(side, text="Edit Selected Point", padding=8)
        edit.pack(fill=tk.X)

        self.edit_vars = {'lat': tk.StringVar(), 'lon': tk.StringVar(), 'alt': tk.StringVar(), 'group': tk.StringVar()}
        for i, key in enumerate(('lat', 'lon', 'alt', 'group')):
            label = "Chicane Group" if key == 'group' else key.capitalize()
            ttk.Label(edit, text=label + ":").grid(row=i, column=0, sticky='e', pady=2)
            ttk.Entry(edit, textvariable=self.edit_vars[key], width=18).grid(row=i, column=1, sticky='w', pady=2)

        btn_row = ttk.Frame(edit)
        btn_row.grid(row=4, column=0, columnspan=2, pady=(6, 0))
        ttk.Button(btn_row, text="Apply Changes", command=self.apply_edit).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Delete Selected", command=self.delete_selected).pack(side=tk.LEFT, padx=(6, 0))

        io_row = ttk.Frame(side)
        io_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(io_row, text="Export KML...", command=self.export_kml).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(io_row, text="Import KML...", command=self.import_kml).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

    def open_ntrip_dialog(self):
        """Opens the NTRIP config dialog, building it the first time and simply raising it on
        later calls (so entries/combobox state and the Connect/Disconnect buttons persist).
        """
        if self._ntrip_window is not None and self._ntrip_window.winfo_exists():
            self._ntrip_window.deiconify()
            self._ntrip_window.lift()
            self._ntrip_window.focus_set()
            return

        self._ntrip_window = tk.Toplevel(self.root)
        self._ntrip_window.title("NTRIP Corrections (RTCM)")
        self._ntrip_window.resizable(False, False)
        # Hide rather than destroy on close so the next open doesn't need to rebuild everything.
        self._ntrip_window.protocol("WM_DELETE_WINDOW", self._ntrip_window.withdraw)
        self._build_ntrip_dialog_contents(self._ntrip_window)

    def _build_ntrip_dialog_contents(self, parent):
        container = ttk.Frame(parent, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        row1 = ttk.Frame(container)
        row1.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(row1, text="Saved Config:").pack(side=tk.LEFT)
        self.ntrip_profile_combo = ttk.Combobox(
            row1, textvariable=self.ntrip_profile_var, state='readonly', width=18,
            values=self._list_ntrip_profiles())
        self.ntrip_profile_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.ntrip_profile_combo.bind('<<ComboboxSelected>>', lambda e: self.load_ntrip_profile())
        ttk.Button(row1, text="Save As...", command=self.save_ntrip_profile).pack(side=tk.LEFT)
        ttk.Button(row1, text="Delete", command=self.delete_ntrip_profile).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(row1, text="Host:").pack(side=tk.LEFT, padx=(16, 4))
        ttk.Entry(row1, textvariable=self.ntrip_host_var, width=26).pack(side=tk.LEFT)
        ttk.Label(row1, text="Port:").pack(side=tk.LEFT, padx=(8, 4))
        ttk.Entry(row1, textvariable=self.ntrip_port_var, width=6).pack(side=tk.LEFT)
        ttk.Checkbutton(row1, text="TLS", variable=self.ntrip_tls_var).pack(side=tk.LEFT, padx=(8, 0))

        row2 = ttk.Frame(container)
        row2.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="Username:").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.ntrip_user_var, width=16).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(row2, text="Password:").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.ntrip_pass_var, width=16, show='*').pack(side=tk.LEFT, padx=(4, 12))

        row3 = ttk.Frame(container)
        row3.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Label(row3, text="City/Site:").pack(side=tk.LEFT)
        self.ntrip_city_combo = ttk.Combobox(row3, textvariable=self.ntrip_city_var, state='readonly', width=32)
        self.ntrip_city_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.ntrip_city_combo.bind('<<ComboboxSelected>>', self.on_ntrip_city_selected)
        ttk.Button(row3, text="Fetch Sites...", command=self.fetch_ntrip_sites).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(row3, text="Mountpoint:").pack(side=tk.LEFT)
        ttk.Entry(row3, textvariable=self.ntrip_mountpoint_var, width=16).pack(side=tk.LEFT, padx=(4, 0))

        row4 = ttk.Frame(container)
        row4.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))
        self.ntrip_connect_btn = ttk.Button(row4, text="Connect NTRIP", command=self.on_ntrip_connect_clicked)
        self.ntrip_connect_btn.pack(side=tk.LEFT)
        self.ntrip_disconnect_btn = ttk.Button(
            row4, text="Disconnect NTRIP", command=self.on_ntrip_disconnect_clicked, state=tk.DISABLED)
        self.ntrip_disconnect_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(row4, textvariable=self.ntrip_status_var).pack(side=tk.LEFT, padx=12)

    # ------------------------------------------------------------------
    # NTRIP configuration profiles (saved to ntrip_profiles.json for quick reloading)
    # ------------------------------------------------------------------
    def _load_ntrip_profiles(self):
        if not os.path.exists(self._ntrip_profiles_path):
            return {}
        try:
            with open(self._ntrip_profiles_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_ntrip_profiles(self, profiles):
        with open(self._ntrip_profiles_path, 'w', encoding='utf-8') as f:
            json.dump(profiles, f, indent=2)

    def _list_ntrip_profiles(self):
        return sorted(self._load_ntrip_profiles().keys())

    def save_ntrip_profile(self):
        name = simpledialog.askstring("Save NTRIP Config", "Profile name:", parent=self.root)
        if not name:
            return
        profiles = self._load_ntrip_profiles()
        profiles[name] = {
            'host': self.ntrip_host_var.get().strip(),
            'port': self.ntrip_port_var.get().strip(),
            'tls': self.ntrip_tls_var.get(),
            'username': self.ntrip_user_var.get().strip(),
            'password': self.ntrip_pass_var.get(),
            'mountpoint': self.ntrip_mountpoint_var.get().strip(),
            'city': self.ntrip_city_var.get(),
        }
        self._save_ntrip_profiles(profiles)
        self.ntrip_profile_combo['values'] = self._list_ntrip_profiles()
        self.ntrip_profile_var.set(name)
        messagebox.showinfo(
            "Save NTRIP Config",
            f"Saved profile '{name}'.\n\nNote: the username/password are stored in plain text in "
            f"{os.path.basename(self._ntrip_profiles_path)} next to GUI.py.")

    def load_ntrip_profile(self):
        name = self.ntrip_profile_var.get()
        profile = self._load_ntrip_profiles().get(name)
        if not profile: 
            return
        self.ntrip_host_var.set(profile.get('host', ''))
        self.ntrip_port_var.set(str(profile.get('port', '')))
        self.ntrip_tls_var.set(bool(profile.get('tls', False)))
        self.ntrip_user_var.set(profile.get('username', ''))
        self.ntrip_pass_var.set(profile.get('password', ''))
        self.ntrip_mountpoint_var.set(profile.get('mountpoint', ''))
        self.ntrip_city_var.set(profile.get('city', ''))

    def delete_ntrip_profile(self):
        name = self.ntrip_profile_var.get()
        profiles = self._load_ntrip_profiles()
        if not name or name not in profiles:
            return
        if not messagebox.askyesno("Delete NTRIP Config", f"Delete saved profile '{name}'?"):
            return
        del profiles[name]
        self._save_ntrip_profiles(profiles)
        self.ntrip_profile_combo['values'] = self._list_ntrip_profiles()
        self.ntrip_profile_var.set('')

    # ------------------------------------------------------------------
    # NTRIP sourcetable (city/site dropdown) + connect / disconnect
    # ------------------------------------------------------------------
    def fetch_ntrip_sites(self):
        host = self.ntrip_host_var.get().strip()
        if not host:
            messagebox.showerror("Fetch Sites", "Host is required.")
            return
        try:
            port = int(self.ntrip_port_var.get().strip())
        except ValueError:
            messagebox.showerror("Fetch Sites", "Port must be a number.")
            return
        use_tls = self.ntrip_tls_var.get() or port == 443
        username = self.ntrip_user_var.get().strip()
        password = self.ntrip_pass_var.get()

        self.ntrip_status_var.set("Fetching sourcetable...")
        threading.Thread(
            target=self._fetch_ntrip_sites_worker, args=(host, port, use_tls, username, password), daemon=True
        ).start()

    def _fetch_ntrip_sites_worker(self, host, port, use_tls, username, password):
        client = NtripClient(host=host, port=port, use_tls=use_tls, username=username, password=password)
        try:
            mounts = client.fetch_sourcetable()
        except (NtripError, OSError) as e:
            self.root.after(0, lambda: self.ntrip_status_var.set(f"Fetch failed: {e}"))
            return
        self.root.after(0, lambda: self._populate_ntrip_sites(mounts))

    def _populate_ntrip_sites(self, mounts):
        self.ntrip_mounts = mounts
        if not mounts:
            self.ntrip_status_var.set("No mountpoints returned by caster (check login credentials).")
            return
        labels = sorted((f"{m['identifier']} ({m['mountpoint']})" for m in mounts), key=str.lower)
        self.ntrip_city_combo['values'] = labels
        self.ntrip_status_var.set(f"Found {len(mounts)} site(s) - pick one from City/Site.")

    def on_ntrip_city_selected(self, _event):
        label = self.ntrip_city_var.get()
        for m in self.ntrip_mounts:
            if label == f"{m['identifier']} ({m['mountpoint']})":
                self.ntrip_mountpoint_var.set(m['mountpoint'])
                break

    def on_ntrip_connect_clicked(self):
        host = self.ntrip_host_var.get().strip()
        mountpoint = self.ntrip_mountpoint_var.get().strip()
        if not host or not mountpoint:
            messagebox.showerror("NTRIP", "Host and Mountpoint are required.")
            return
        try:
            port = int(self.ntrip_port_var.get().strip())
        except ValueError:
            messagebox.showerror("NTRIP", "Port must be a number.")
            return

        self.ntrip = NtripClient(
            host=host, port=port, mountpoint=mountpoint,
            username=self.ntrip_user_var.get().strip(),
            password=self.ntrip_pass_var.get(),
            use_tls=self.ntrip_tls_var.get() or port == 443,
            gga_provider=self._ntrip_gga_provider,
            on_data=self._on_ntrip_data,
            on_status=self._on_ntrip_status,
        )
        try:
            self.ntrip.connect()
        except NtripError as e:
            messagebox.showerror("NTRIP", str(e))
            self.ntrip = None
            return
        self.ntrip_status_var.set("Connecting...")
        self.ntrip_connect_btn.config(state=tk.DISABLED)
        self.ntrip_disconnect_btn.config(state=tk.NORMAL)

    def on_ntrip_disconnect_clicked(self):
        if self.ntrip:
            self.ntrip.disconnect()
        self.ntrip = None
        self.ntrip_status_var.set("Not connected")
        self.ntrip_connect_btn.config(state=tk.NORMAL)
        self.ntrip_disconnect_btn.config(state=tk.DISABLED)

    def _ntrip_gga_provider(self):
        return self.gps.build_gga_sentence() if self.gps else None

    def _on_ntrip_data(self, data):
        if self.gps:
            self.gps.send_rtcm(data)

    def _on_ntrip_status(self, msg):
        self.root.after(0, lambda: self.ntrip_status_var.set(msg))
        if msg == "Disconnected":
            self.root.after(0, lambda: self.ntrip_connect_btn.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.ntrip_disconnect_btn.config(state=tk.DISABLED))

    # ------------------------------------------------------------------
    # GPS connect / live polling
    # ------------------------------------------------------------------
    def _refresh_port_list(self):
        ports = GPS.list_available_ports()
        labels = ["Auto"] + [f"{device} - {desc}" if desc else device for device, desc in ports]
        current = self.port_var.get()
        self.port_combo['values'] = labels
        # Keep the current selection if it's still present (e.g. after a disconnect), else reset to Auto.
        if current not in labels:
            self.port_var.set("Auto")

    def on_connect_clicked(self):
        self.connect_btn.config(state=tk.DISABLED)
        self.baudrate_combo.config(state='disabled')
        self.port_combo.config(state='disabled')
        self.status_var.set("Searching for GPS receiver...")
        baud_selection = self.baudrate_var.get()
        force_baudrate = int(baud_selection) if baud_selection != "Auto" else None
        port_selection = self.port_var.get()
        # Port labels are "COM13 - description" (or just "COM13"); only the device name is needed.
        force_port = port_selection.split(' - ', 1)[0] if port_selection != "Auto" else None
        threading.Thread(target=self._connect_worker, args=(force_baudrate, force_port), daemon=True).start()

    def _connect_worker(self, force_baudrate=None, force_port=None):
        gps = GPS(force_baudrate=force_baudrate, force_port=force_port)
        if gps.connected:
            self.gps = gps
            self.polling = True
            threading.Thread(target=self._poll_loop, daemon=True).start()
            self.root.after(0, lambda: self.status_var.set(f"Connected: {gps.description}"))
            self.root.after(0, lambda: self.disconnect_btn.config(state=tk.NORMAL))
            return

        self.root.after(0, lambda: self.connect_btn.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.baudrate_combo.config(state='readonly'))
        self.root.after(0, lambda: self.port_combo.config(state='readonly'))

        if gps.busy_ports:
            # Distinguish "port exists but is locked by another program" from "nothing found" -
            # this was previously reported as a generic failure even though the fix is simple
            # (close whatever else has the port open, e.g. PuTTY/Arduino IDE/u-center).
            busy_list = ", ".join(sorted(gps.busy_ports))
            self.root.after(0, lambda: self.status_var.set(
                f"Port busy: {busy_list} is already open in another program "
                f"(e.g. PuTTY, Arduino IDE, u-center). Close it and try again."))
        elif force_port and not gps.description:
            self.root.after(0, lambda: self.status_var.set(f"Port {force_port} was not found. Is it plugged in?"))
        elif gps.description:
            # A GPS-labeled port was located but never produced a valid fix within the scan window.
            self.root.after(0, lambda: self.status_var.set(
                f"GPS found ({gps.description}) but is not providing data."))
        else:
            self.root.after(0, lambda: self.status_var.set("No GPS device found."))

    def _poll_loop(self):
        while self.polling and self.gps:
            try:
                self.gps.update()
            except Exception:
                pass
            time.sleep(0.005)

    def on_disconnect_clicked(self):
        self.polling = False
        if self.gps:
            self.gps.disconnect()
        self.gps = None
        self.status_var.set("Not connected")
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.baudrate_combo.config(state='readonly')
        self.port_combo.config(state='readonly')
        self._set_lock_indicator(None)

    def _set_lock_indicator(self, fix_quality):
        """Updates the colored GPS lock indicator. None means "not connected"."""
        if fix_quality is None:
            self.lock_var.set("Not Connected")
            self.lock_label.config(bg='#616161', fg='white')
            return
        label = FIX_QUALITY_DESCRIPTIONS.get(fix_quality, f"Unknown ({fix_quality})")
        color = FIX_QUALITY_COLORS.get(fix_quality, '#555555')
        self.lock_var.set(label)
        self.lock_label.config(bg=color, fg='white')

    def _refresh_live_display(self):
        fix = self.get_live_fix()
        if fix:
            self.live_vars['lat'].set(f"{fix['latitude']:.6f}")
            self.live_vars['lon'].set(f"{fix['longitude']:.6f}")
            self.live_vars['alt'].set(f"{fix['altitude']:.2f}")
            self.live_vars['fix'].set(str(fix['fix_quality']))
            self.live_vars['sats'].set(str(fix['satellites']))
            self.live_vars['hdop'].set(f"{fix['hdop']:.2f}")
            self.live_vars['time'].set(fix['time'].strftime('%H:%M:%S'))
            self._set_lock_indicator(fix['fix_quality'])
            self._update_live_plot(fix)
        self.root.after(100, self._refresh_live_display)

    def get_live_fix(self):
        if not self.gps:
            return None
        return self.gps.get_fix()

    # ------------------------------------------------------------------
    # Chicane grouping
    # ------------------------------------------------------------------
    def on_chicane_clicked(self):
        if self.chicane_active:
            self.chicane_active = False
        else:
            self.chicane_group_counter += 1
            self.current_chicane_group = self.chicane_group_counter
            self.chicane_active = True
        self._update_chicane_ui()

    def _update_chicane_ui(self):
        if self.chicane_active:
            self.chicane_btn.config(text=f"End Chicane (Group {self.current_chicane_group})")
            self.chicane_status_var.set(
                f"Chicane {self.current_chicane_group} active - Left/Right cones recorded now are tagged _{self.current_chicane_group}.")
        else:
            self.chicane_btn.config(text="Start Chicane")
            self.chicane_status_var.set("No chicane active.")

    def _sync_chicane_counter(self):
        max_group = 0
        for p in self.placemarks:
            if p['type'] in ('left_cone', 'right_cone') and p.get('group'):
                max_group = max(max_group, p['group'])
        self.chicane_group_counter = max(self.chicane_group_counter, max_group)

    # ------------------------------------------------------------------
    # Placemark management
    # ------------------------------------------------------------------
    def record_point(self, ptype):
        fix = self.get_live_fix()
        if not fix or not fix['fix_quality']:
            messagebox.showwarning("No GPS Fix", "No live GPS fix available yet.")
            return
        group = self.current_chicane_group if (ptype in ('left_cone', 'right_cone') and self.chicane_active) else None
        self.add_placemark(ptype, fix['latitude'], fix['longitude'], fix['altitude'], group=group)

    def add_placemark(self, ptype, lat, lon, alt, idx=None, group=None):
        if ptype == REFERENCE_TYPE:
            for existing in [p for p in self.placemarks if p['type'] == REFERENCE_TYPE]:
                self.remove_placemark(existing['id'])
        if ptype not in ('left_cone', 'right_cone'):
            group = None
        if idx is None:
            # Scoped to (type, group) so each chicane's count restarts at 1 instead of
            # continuing the running total of every left/right cone ever recorded.
            existing_idx = [p['idx'] for p in self.placemarks if p['type'] == ptype and p.get('group') == group]
            idx = (max(existing_idx) + 1) if existing_idx else 1

        p = {'id': self.next_id, 'type': ptype, 'idx': idx, 'group': group, 'lat': lat, 'lon': lon, 'alt': alt}
        self.next_id += 1
        self.placemarks.append(p)
        self.tree.insert('', 'end', iid=str(p['id']),
                          values=(name_for(p), f"{lat:.6f}", f"{lon:.6f}", f"{alt:.2f}"))
        self.refresh_plot()
        return p

    def remove_placemark(self, pid):
        idx = next((i for i, p in enumerate(self.placemarks) if p['id'] == pid), None)
        if idx is None:
            return
        self.placemarks.pop(idx)
        if self.tree.exists(str(pid)):
            self.tree.delete(str(pid))
        if self.selected_id == pid:
            self.selected_id = None
        self.refresh_plot()

    def get_selected_placemark(self):
        if self.selected_id is None:
            return None
        return next((p for p in self.placemarks if p['id'] == self.selected_id), None)

    def select_placemark(self, pid):
        self.selected_id = pid
        p = self.get_selected_placemark()
        if p:
            if self.tree.selection() != (str(pid),):
                self.tree.selection_set(str(pid))
                self.tree.see(str(pid))
            self.edit_vars['lat'].set(f"{p['lat']:.6f}")
            self.edit_vars['lon'].set(f"{p['lon']:.6f}")
            self.edit_vars['alt'].set(f"{p['alt']:.2f}")
            self.edit_vars['group'].set(str(p['group']) if p.get('group') else '')
        self.refresh_plot()

    def on_tree_select(self, _event):
        selection = self.tree.selection()
        if selection:
            self.select_placemark(int(selection[0]))

    def apply_edit(self):
        p = self.get_selected_placemark()
        if not p:
            messagebox.showinfo("Edit Point", "Select a point in the list first.")
            return
        try:
            p['lat'] = float(self.edit_vars['lat'].get())
            p['lon'] = float(self.edit_vars['lon'].get())
            p['alt'] = float(self.edit_vars['alt'].get())
        except ValueError:
            messagebox.showerror("Edit Point", "Latitude, longitude and altitude must be numbers.")
            return

        group_text = self.edit_vars['group'].get().strip()
        if p['type'] in ('left_cone', 'right_cone'):
            if group_text:
                try:
                    p['group'] = int(group_text)
                except ValueError:
                    messagebox.showerror("Edit Point", "Chicane group must be a whole number, or blank to ungroup.")
                    return
                self._sync_chicane_counter()
            else:
                p['group'] = None
        else:
            p['group'] = None

        self.tree.item(str(p['id']), values=(name_for(p), f"{p['lat']:.6f}", f"{p['lon']:.6f}", f"{p['alt']:.2f}"))
        self.refresh_plot()

    def delete_selected(self):
        p = self.get_selected_placemark()
        if not p:
            messagebox.showinfo("Delete Point", "Select a point in the list first.")
            return
        self.remove_placemark(p['id'])

    # ------------------------------------------------------------------
    # Map plotting
    # ------------------------------------------------------------------
    def _get_map_origin(self):
        reference = next((p for p in self.placemarks if p['type'] == REFERENCE_TYPE), None)
        if reference:
            self._map_origin_latlon = (reference['lat'], reference['lon'])
        elif self._map_origin_latlon is None:
            first_placemark = self.placemarks[0] if self.placemarks else None
            fix = self.get_live_fix()
            if first_placemark:
                self._map_origin_latlon = (first_placemark['lat'], first_placemark['lon'])
            elif fix and fix['fix_quality']:
                self._map_origin_latlon = (fix['latitude'], fix['longitude'])
        return self._map_origin_latlon

    def _map_xy(self, latitude, longitude):
        origin = self._get_map_origin()
        if origin is None:
            return 0.0, 0.0
        return latlon_to_local_xy_m(latitude, longitude, origin[0], origin[1])

    def on_tracking_toggled(self):
        if self.track_live_var.get():
            self._capture_tracking_span()
        self.refresh_plot()

    def _capture_tracking_span(self, _axis=None):
        if not self.track_live_var.get() or self._updating_plot:
            return
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        width, height = abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])
        if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
            self._tracking_span_m = (width, height)

    def refresh_plot(self):
        has_data = bool(self.placemarks) or bool((fix := self.get_live_fix()) and fix['fix_quality'])
        should_fit_view = has_data and not self._view_fitted
        prev_xlim, prev_ylim = self.ax.get_xlim(), self.ax.get_ylim()

        self._updating_plot = True
        self.ax.clear()
        # Axes.clear() resets its callback registry, so reattach after every redraw.
        self.ax.callbacks.connect('xlim_changed', self._capture_tracking_span)
        self.ax.callbacks.connect('ylim_changed', self._capture_tracking_span)
        self.ax.set_xlabel("East (m)")
        self.ax.set_ylabel("North (m)")
        self.ax.set_title("Track Map (local metres; origin at reference point or first position)")

        by_type = {}
        for p in self.placemarks:
            by_type.setdefault(p['type'], []).append(p)

        map_points = []
        for ptype, points in by_type.items():
            color = CONE_COLORS.get(ptype, DEFAULT_COLOR)
            projected = [self._map_xy(p['lat'], p['lon']) for p in points]
            xs = [xy[0] for xy in projected]
            ys = [xy[1] for xy in projected]
            map_points.extend(projected)
            self.ax.scatter(xs, ys, c=color, label=ptype, s=40, edgecolors='black', zorder=3)

        selected = self.get_selected_placemark()
        if selected:
            selected_x, selected_y = self._map_xy(selected['lat'], selected['lon'])
            self.ax.scatter([selected_x], [selected_y], s=180, facecolors='none',
                             edgecolors='#00e5ff', linewidths=2, zorder=4)

        fix = self.get_live_fix()
        self.live_marker = self.ax.scatter(
            [], [], c='#00ff66', marker='x', s=80, label='live position', zorder=5)
        self._rendered_live_fix = bool(fix and fix['fix_quality'])
        if self._rendered_live_fix:
            live_x, live_y = self._map_xy(fix['latitude'], fix['longitude'])
            map_points.append((live_x, live_y))
            self.live_marker.set_offsets([[live_x, live_y]])

        if by_type or self._rendered_live_fix:
            self.ax.legend(loc='upper right', fontsize='small')
        self.ax.set_aspect('equal', adjustable='box')

        if self.track_live_var.get() and fix and fix['fix_quality']:
            width, height = self._tracking_span_m
            self.ax.set_xlim(live_x - width / 2, live_x + width / 2)
            self.ax.set_ylim(live_y - height / 2, live_y + height / 2)
            self._view_fitted = True
        elif should_fit_view:
            self._fit_map_points(map_points)
            self._view_fitted = True
        else:
            # ax.clear() resets the view, so restore whatever pan/zoom the user had set.
            self.ax.set_xlim(prev_xlim)
            self.ax.set_ylim(prev_ylim)

        self._updating_plot = False
        self.canvas.draw_idle()

    def _update_live_plot(self, fix):
        """Update only the live marker, avoiding a full axes clear/rebuild during GPS polling."""
        has_fix = bool(fix['fix_quality'])
        if has_fix != self._rendered_live_fix:
            self.refresh_plot()
            return
        if self.live_marker is None:
            self.refresh_plot()
            return
        if has_fix:
            live_x, live_y = self._map_xy(fix['latitude'], fix['longitude'])
            self.live_marker.set_offsets([[live_x, live_y]])
            if self.track_live_var.get():
                width, height = self._tracking_span_m
                self.ax.set_xlim(live_x - width / 2, live_x + width / 2)
                self.ax.set_ylim(live_y - height / 2, live_y + height / 2)
        self.canvas.draw_idle()

    def _fit_map_points(self, points):
        if not points:
            return
        xs, ys = zip(*points)
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        padding = max((x_max - x_min) * 0.05, (y_max - y_min) * 0.05, 5.0)
        self.ax.set_xlim(x_min - padding, x_max + padding)
        self.ax.set_ylim(y_min - padding, y_max + padding)

    def fit_view(self):
        """Recentre/rescale the map to show all current data, overriding manual pan/zoom."""
        if self.track_live_var.get():
            self.track_live_var.set(False)
        points = [self._map_xy(p['lat'], p['lon']) for p in self.placemarks]
        fix = self.get_live_fix()
        if fix and fix['fix_quality']:
            points.append(self._map_xy(fix['latitude'], fix['longitude']))
        self._fit_map_points(points)
        self._view_fitted = bool(points)
        self.canvas.draw_idle()

    def on_scroll(self, event):
        if event.xdata is None or event.ydata is None:
            return
        scale_factor = 1 / 1.2 if event.button == 'up' else 1.2
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        new_width = (xlim[1] - xlim[0]) * scale_factor
        new_height = (ylim[1] - ylim[0]) * scale_factor
        relx = (xlim[1] - event.xdata) / (xlim[1] - xlim[0])
        rely = (ylim[1] - event.ydata) / (ylim[1] - ylim[0])
        self.ax.set_xlim(event.xdata - new_width * (1 - relx), event.xdata + new_width * relx)
        self.ax.set_ylim(event.ydata - new_height * (1 - rely), event.ydata + new_height * rely)
        self.canvas.draw_idle()

    def on_map_click(self, event):
        if self.toolbar.mode or event.xdata is None or event.ydata is None or not self.placemarks:
            return
        nearest = min(
            self.placemarks,
            key=lambda p: sum((coord - click) ** 2 for coord, click in zip(
                self._map_xy(p['lat'], p['lon']), (event.xdata, event.ydata))))
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        tolerance = 0.03 * max(abs(x1 - x0), abs(y1 - y0), 1e-9)
        nearest_x, nearest_y = self._map_xy(nearest['lat'], nearest['lon'])
        dist = ((nearest_x - event.xdata) ** 2 + (nearest_y - event.ydata) ** 2) ** 0.5
        if dist <= tolerance:
            self.select_placemark(nearest['id'])

    # ------------------------------------------------------------------
    # KML export / import
    # ------------------------------------------------------------------
    def export_kml(self):
        if not self.placemarks:
            messagebox.showinfo("Export KML", "No placemarks to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".kml", filetypes=[("KML files", "*.kml")])
        if not path:
            return

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<kml xmlns="http://www.opengis.net/kml/2.2">',
            '  <Document>',
            '    <name>FSAE_Track</name>',
        ]
        for p in self.placemarks:
            lines.append('    <Placemark>')
            lines.append(f'      <name>{escape(name_for(p))}</name>')
            lines.append('      <Point>')
            lines.append(f'        <coordinates>{p["lon"]},{p["lat"]},{p["alt"]}</coordinates>')
            lines.append('      </Point>')
            lines.append('    </Placemark>')
        lines.append('  </Document>')
        lines.append('</kml>')

        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        messagebox.showinfo("Export KML", f"Exported {len(self.placemarks)} placemark(s) to {path}")

    def import_kml(self):
        path = filedialog.askopenfilename(filetypes=[("KML files", "*.kml")])
        if not path:
            return
        try:
            features = parse_kml_features(path)
        except Exception as e:
            messagebox.showerror("Import KML", f"Failed to parse KML: {e}")
            return
        if not features:
            messagebox.showinfo("Import KML", "No placemarks found in file.")
            return
        if self.placemarks and not messagebox.askyesno(
                "Import KML", f"Replace current {len(self.placemarks)} placemark(s) with {len(features)} from file?"):
            return

        self.placemarks.clear()
        self.tree.delete(*self.tree.get_children())
        self.next_id = 1
        self.selected_id = None
        self._map_origin_latlon = None
        for f in features:
            self.add_placemark(f['type'], f['lat'], f['lon'], f['alt'], idx=f['idx'], group=f.get('group'))
        self._sync_chicane_counter()


def main():
    root = tk.Tk()
    app = TrackMapperApp(root)

    def on_close():
        app.on_disconnect_clicked()
        app.on_ntrip_disconnect_clicked()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
