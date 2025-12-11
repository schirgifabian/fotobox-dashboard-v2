import asyncio
import os
import datetime

import flet as ft
import gspread
import pandas as pd
import pytz
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from aqara_api import AqaraClient  # aktuell noch nur importiert

# .env laden (muss im Projekt-Root liegen)
load_dotenv()

PAGE_TITLE = "Fotobox Drucker Status"
PAGE_ICON = "🖨️"

# --- NICHT geheime Konfiguration (analog zu Streamlit-Version) -----------------

PRINTERS = {
    "die Fotobox": {
        "key": "standard",
        "warning_threshold": 20,
        "default_max_prints": 400,
        "cost_per_roll_eur": 46.59,
        "has_admin": True,
        "has_aqara": True,
        "has_dsr": True,
        "media_factor": 1,
    },
    "Weinkellerei": {
        "key": "Weinkellerei",
        "warning_threshold": 20,
        "default_max_prints": 400,
        "cost_per_roll_eur": 60,
        "has_admin": True,
        "has_aqara": False,
        "has_dsr": False,
        "media_factor": 2,
    },
}

HEARTBEAT_WARN_MINUTES = 60
LOCAL_TZ = pytz.timezone("Europe/Vienna")


# --------------------------------------------------------------------
# HILFSFUNKTIONEN (Google Sheets & Statistik)
# --------------------------------------------------------------------


def get_gspread_client() -> gspread.Client:
    """
    Für Flet + GitHub verwenden wir einen Service-Account + .env.

    Erwartet:
      - env GOOGLE_SERVICE_ACCOUNT_FILE -> Pfad zur JSON (z. B. secrets/service_account.json)
    """
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/service_account.json")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_file, scopes=scopes)
    return gspread.authorize(creds)


def get_data(sheet_id: str) -> pd.DataFrame:
    try:
        ws = get_gspread_client().open_by_key(sheet_id).sheet1
        return pd.DataFrame(ws.get_all_records())
    except Exception as e:
        print("Fehler beim Laden der Daten:", e)
        return pd.DataFrame()


def _prepare_history_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    if "Timestamp" not in df.columns or "MediaRemaining" not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
    df = df.dropna(subset=["Timestamp", "MediaRemaining"])
    if df.empty:
        return df

    df = df.sort_values("Timestamp")
    df = df.set_index("Timestamp")
    df["MediaRemaining"] = pd.to_numeric(df["MediaRemaining"], errors="coerce")
    df = df.dropna(subset=["MediaRemaining"])
    return df


def compute_print_stats(
    df: pd.DataFrame,
    window_min: int = 30,
    media_factor: int = 2,
) -> dict:
    """
    Berechnet Verbrauch und Druckgeschwindigkeit.
    """
    result = {
        "prints_total": 0,
        "duration_min": 0,
        "ppm_overall": None,
        "ppm_window": None,
    }

    df = _prepare_history_df(df)
    if df.empty or len(df) < 2:
        return result

    first_media_raw = df["MediaRemaining"].iloc[0]
    last_media_raw = df["MediaRemaining"].iloc[-1]

    prints_total = max(0, (first_media_raw - last_media_raw) * media_factor)
    duration_min = (df.index[-1] - df.index[0]).total_seconds() / 60.0

    result["prints_total"] = prints_total
    result["duration_min"] = duration_min

    if duration_min > 0 and prints_total > 0:
        result["ppm_overall"] = prints_total / duration_min

    window_start = df.index[-1] - datetime.timedelta(minutes=window_min)
    dfw = df[df.index >= window_start]
    if len(dfw) >= 2:
        f_m_raw = dfw["MediaRemaining"].iloc[0]
        l_m_raw = dfw["MediaRemaining"].iloc[-1]
        prints_win = max(0, (f_m_raw - l_m_raw) * media_factor)
        dur_win_min = (dfw.index[-1] - dfw.index[0]).total_seconds() / 60.0
        if dur_win_min > 0 and prints_win > 0:
            result["ppm_window"] = prints_win / dur_win_min

    return result


def humanize_minutes(minutes: float) -> str:
    if minutes is None or minutes <= 0:
        return "0 Min."
    m = int(minutes)
    h = m // 60
    r = m % 60
    if h > 0:
        return f"{h} Std. {r} Min."
    else:
        return f"{r} Min."


def evaluate_status_simple(
    raw_status: str,
    media_remaining: int,
    timestamp: str,
    warning_threshold: int,
) -> tuple[str, str, str, float | None]:
    """
    Vereinfachte Statusauswertung.
    Gibt zurück:
      (status_mode, display_text, display_color, minutes_diff)
    """
    raw_status_l = (raw_status or "").lower().strip()

    hard_errors = [
        "paper end",
        "ribbon end",
        "paper jam",
        "ribbon error",
        "paper definition error",
        "data error",
    ]
    cover_open_kw = ["cover open"]
    cooldown_kw = ["head cooling down"]
    printing_kw = ["printing", "processing", "drucken"]
    idle_kw = ["idle", "standby mode"]

    # 1) Harte Fehler
    if any(k in raw_status_l for k in hard_errors):
        status_mode = "error"
        display_text = f"🔴 STÖRUNG: {raw_status}"
        display_color = "red"

    # 2) Deckel offen
    elif any(k in raw_status_l for k in cover_open_kw):
        status_mode = "cover_open"
        display_text = "⚠️ Deckel offen!"
        display_color = "orange"

    # 3) Papier fast leer
    elif media_remaining <= warning_threshold:
        status_mode = "low_paper"
        display_text = f"⚠️ Papier fast leer – {media_remaining} Bilder übrig"
        display_color = "orange"

    # 4) Kopf kühlt ab
    elif any(k in raw_status_l for k in cooldown_kw):
        status_mode = "cooldown"
        display_text = "🟡 Kopf kühlt ab"
        display_color = "orange"

    # 5) Druckt gerade
    elif any(k in raw_status_l for k in printing_kw):
        status_mode = "printing"
        display_text = "🟢 Druckt gerade"
        display_color = "green"

    # 6) Leerlauf
    elif any(k in raw_status_l for k in idle_kw) or raw_status_l == "":
        status_mode = "ready"
        display_text = "✅ Bereit"
        display_color = "green"

    else:
        status_mode = "ready"
        display_text = f"✅ Bereit ({raw_status})"
        display_color = "green"

    # Heartbeat / „Stale“-Erkennung
    minutes_diff: float | None = None
    ts_parsed = pd.to_datetime(timestamp, errors="coerce")
    if ts_parsed is not None and not pd.isna(ts_parsed):
        if ts_parsed.tzinfo is None:
            ts_parsed = LOCAL_TZ.localize(ts_parsed)
        now_ts = datetime.datetime.now(LOCAL_TZ)
        minutes_diff = (now_ts - ts_parsed).total_seconds() / 60.0
        if minutes_diff > HEARTBEAT_WARN_MINUTES:
            status_mode = "stale"
            display_text = f"⚠️ Keine aktuellen Daten (seit {int(minutes_diff)} Min)"
            display_color = "orange"

    return status_mode, display_text, display_color, minutes_diff


# --------------------------------------------------------------------
# FLET APP
# --------------------------------------------------------------------


class FotoboxApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = PAGE_TITLE
        self.page.window_width = 900
        self.page.window_height = 700
        self.page.padding = 20

        # „Session-State“
        self.event_mode = False
        self.sound_enabled = False
        self.ntfy_active = True

        # Sheet-ID aus ENV ziehen, falls vorhanden
        default_sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")

        # UI-Controls
        self.printer_dropdown = ft.Dropdown(
            label="Drucker",
            options=[ft.dropdown.Option(name) for name in PRINTERS.keys()],
            value="die Fotobox",
            width=260,
            on_change=self.on_printer_change,
        )

        self.sheet_id_field = ft.TextField(
            label="Google Sheet ID",
            value=default_sheet_id,
            width=420,
            on_change=self.on_sheet_change,
        )

        self.event_switch = ft.Switch(
            label="Event-Ansicht (nur Status)",
            value=self.event_mode,
            on_change=self.on_event_toggle,
        )
        self.sound_switch = ft.Switch(
            label="Sound bei Warnungen (nur Anzeige, noch ohne Logik)",
            value=self.sound_enabled,
            on_change=self.on_sound_toggle,
        )
        self.ntfy_switch = ft.Switch(
            label="Push-Benachrichtigungen aktiv (nur Anzeige, Logik TODO)",
            value=self.ntfy_active,
            on_change=self.on_ntfy_toggle,
        )

        # Status-Anzeige
        self.status_text = ft.Text("System wartet auf Start…", size=20, weight=ft.FontWeight.BOLD)
        self.timestamp_text = ft.Text("", size=12, color=ft.Colors.GREY)
        self.status_badge = ft.Container(
            content=ft.Text("–", size=16),
            padding=10,
            bgcolor=ft.Colors.GREY_200,
            border_radius=18,
        )

        # Papier-Progress
        self.progress_bar = ft.ProgressBar(width=400, value=0.0)
        self.progress_label = ft.Text("Papierstatus: –", size=14)

        # Statistik
        self.stats_text = ft.Text("", size=13)

        # Log
        self.log_text = ft.Text("", size=12, color=ft.Colors.GREY_600, selectable=True)

        # Layout zusammenbauen
        header_row = ft.Row(
            controls=[
                ft.Text(f"{PAGE_ICON} {PAGE_TITLE}", size=26, weight=ft.FontWeight.BOLD),
            ],
        )

        config_row = ft.Row(
            controls=[
                self.printer_dropdown,
                self.sheet_id_field,
            ],
            spacing=16,
        )

        switches_row = ft.Row(
            controls=[
                self.event_switch,
                self.sound_switch,
                self.ntfy_switch,
            ],
            spacing=18,
        )

        status_card = ft.Container(
            content=ft.Column(
                controls=[
                    self.status_text,
                    ft.Row([self.status_badge, self.timestamp_text], spacing=10),
                    ft.Container(height=8),
                    self.progress_bar,
                    self.progress_label,
                    ft.Container(height=8),
                    self.stats_text,
                ],
                spacing=8,
            ),
            padding=16,
            border_radius=16,
            bgcolor=ft.Colors.WHITE,
            border=ft.border.all(1, ft.Colors.GREY_300),
        )

        log_card = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("Log", weight=ft.FontWeight.BOLD),
                    self.log_text,
                ]
            ),
            padding=16,
            border_radius=12,
            bgcolor=ft.Colors.GREY_50,
            border=ft.border.all(1, ft.Colors.GREY_200),
        )

        self.page.add(
            ft.Column(
                controls=[
                    header_row,
                    ft.Container(height=10),
                    config_row,
                    switches_row,
                    ft.Divider(),
                    status_card,
                    ft.Container(height=10),
                    log_card,
                ],
                spacing=10,
            )
        )

        # Live-Loop starten
        self.page.run_task(self.live_loop)

    # ----------------------------------------------------------------
    # Event-Handler
    # ----------------------------------------------------------------

    def on_printer_change(self, e: ft.ControlEvent):
        self.append_log(f"Drucker gewechselt auf: {self.printer_dropdown.value}")

    def on_sheet_change(self, e: ft.ControlEvent):
        self.append_log("Sheet-ID geändert.")

    def on_event_toggle(self, e: ft.ControlEvent):
        self.event_mode = self.event_switch.value
        self.append_log(f"Event-Ansicht: {self.event_mode}")

    def on_sound_toggle(self, e: ft.ControlEvent):
        self.sound_enabled = self.sound_switch.value
        self.append_log(f"Sound: {self.sound_enabled}")

    def on_ntfy_toggle(self, e: ft.ControlEvent):
        self.ntfy_active = self.ntfy_switch.value
        self.append_log(f"ntfy: {self.ntfy_active}")

    # ----------------------------------------------------------------
    # Live-Loop & Status-Update
    # ----------------------------------------------------------------

    async def live_loop(self):
        while True:
            await self.update_status()
            await asyncio.sleep(10)

    async def update_status(self):
        sheet_id = self.sheet_id_field.value.strip() or os.environ.get("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            self.status_text.value = "Bitte Google Sheet ID eintragen."
            self.page.update()
            return

        printer_cfg = PRINTERS.get(self.printer_dropdown.value)
        media_factor = printer_cfg.get("media_factor", 1) if printer_cfg else 1
        warning_threshold = printer_cfg.get("warning_threshold", 20) if printer_cfg else 20
        max_prints = printer_cfg.get("default_max_prints", 400) if printer_cfg else 400
        cost_per_roll = printer_cfg.get("cost_per_roll_eur") if printer_cfg else None

        df = get_data(sheet_id)
        if df.empty:
            self.status_text.value = "System wartet auf Start…"
            self.timestamp_text.value = "Noch keine Druckdaten empfangen."
            self.status_badge.content.value = "–"
            self.status_badge.bgcolor = ft.Colors.GREY_200
            self.progress_bar.value = 0.0
            self.progress_label.value = "Papierstatus: –"
            self.stats_text.value = ""
            self.page.update()
            return

        last = df.iloc[-1]
        timestamp = str(last.get("Timestamp", ""))
        raw_status = str(last.get("Status", ""))

        try:
            media_remaining_raw = int(last.get("MediaRemaining", 0))
        except Exception:
            media_remaining_raw = 0

        media_remaining = media_remaining_raw * media_factor

        status_mode, display_text, display_color, minutes_diff = evaluate_status_simple(
            raw_status=raw_status,
            media_remaining=media_remaining,
            timestamp=timestamp,
            warning_threshold=warning_threshold,
        )

        # Status-Badge + Text
        self.status_text.value = display_text
        self.status_badge.content.value = status_mode.upper()
        if status_mode == "error":
            self.status_badge.bgcolor = ft.Colors.RED_200
        elif status_mode in ("low_paper", "cover_open", "cooldown", "stale"):
            self.status_badge.bgcolor = ft.Colors.ORANGE_200
        else:
            self.status_badge.bgcolor = ft.Colors.GREEN_200

        if minutes_diff is not None:
            self.timestamp_text.value = f"Letztes Signal: {timestamp} (vor {int(minutes_diff)} Min)"
        else:
            self.timestamp_text.value = f"Letztes Signal: {timestamp}"

        # Papier-Progress
        if status_mode == "error" and media_remaining == 0:
            progress_val = 0.0
        else:
            progress_val = max(0.0, min(1.0, media_remaining / max_prints))

        self.progress_bar.value = progress_val
        self.progress_label.value = f"Papierstatus: {media_remaining} von {max_prints} Drucken verbleibend"

        # Stats
        stats = compute_print_stats(df, window_min=30, media_factor=media_factor)
        prints_total = stats.get("prints_total", 0)
        duration_min = stats.get("duration_min", 0)
        ppm_overall = stats.get("ppm_overall")
        ppm_window = stats.get("ppm_window")

        stats_lines = [
            f"Verbrauch seit Start: {prints_total} Drucke in {humanize_minutes(duration_min)}",
        ]
        if ppm_overall:
            stats_lines.append(f"Ø Geschwindigkeit: {ppm_overall:0.2f} Drucke/Min")
        if ppm_window:
            stats_lines.append(f"Letzte 30 Min: {ppm_window:0.2f} Drucke/Min")

        if cost_per_roll and max_prints > 0:
            cost_per_print = cost_per_roll / max_prints
            cost_used = prints_total * cost_per_print
            stats_lines.append(f"Kosten seit Start (ca.): {cost_used:0.2f} €")

        self.stats_text.value = "\n".join(stats_lines)

        self.page.update()

    # ----------------------------------------------------------------
    # Hilfs-Log
    # ----------------------------------------------------------------

    def append_log(self, msg: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        prefix = f"[{now}] "
        if self.log_text.value:
            self.log_text.value = prefix + msg + "\n" + self.log_text.value
        else:
            self.log_text.value = prefix + msg
        self.page.update()


def main(page: ft.Page):
    FotoboxApp(page)


if __name__ == "__main__":
    ft.app(target=main)
