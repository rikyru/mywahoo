"""SQLModel models and database session management (SQLite on a Docker volume)."""
import gzip
import json
import os
from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import settings


class WahooToken(SQLModel, table=True):
    """Single-user app: exactly one row (id=1) holding the OAuth tokens."""
    id: int = Field(default=1, primary_key=True)
    user_id: int = 0
    user_name: str = ""
    access_token: str
    refresh_token: str
    expires_at: int  # unix epoch seconds (computed from expires_in at exchange time)


class IgnoredImport(SQLModel, table=True):
    """Google exercise uids the user deleted by hand: never re-import them."""
    id: int = Field(primary_key=True)  # = Google exercise uid (= imported workout id)


class AppSetting(SQLModel, table=True):
    """Simple key/value store for runtime settings editable from the UI."""
    key: str = Field(primary_key=True)
    value: str = ""


class RouteAssessment(SQLModel, table=True):
    """A planned route (GPX, e.g. from Komoot) with its AI feasibility verdict."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    sport: str = "Bici"        # Bici | Escursione | Corsa
    distance_km: float = 0.0
    ascent_m: float = 0.0
    max_gradient: Optional[float] = None
    content: str = ""          # AI verdict (markdown)
    profile_json: str = "[]"   # elevation profile for the chart
    route_json: str = "{}"     # full parsed route summary (to re-assess later)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class TrainingPlan(SQLModel, table=True):
    """An AI-generated training plan, broken into per-day sessions."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""
    goal: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class PlanSession(SQLModel, table=True):
    """One session of a TrainingPlan; can be edited and marked done (which
    creates a manual Workout)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, foreign_key="trainingplan.id")
    order: int = 0
    day_label: str = ""
    date: Optional[datetime] = Field(default=None, index=True)
    title: str = ""
    sport: str = ""
    duration_min: int = 0
    description: str = ""
    done: bool = False
    workout_id: Optional[int] = None


class Conversation(SQLModel, table=True):
    """A saved AI chat thread (e.g. a training-plan request) to review later."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class ChatMessage(SQLModel, table=True):
    """One message (user or assistant) within a Conversation."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True, foreign_key="conversation.id")
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


def get_setting(key: str, default: str = "") -> str:
    with Session(engine) as session:
        row = session.get(AppSetting, key)
        return row.value if row and row.value else default


def set_setting(key: str, value: str) -> None:
    with Session(engine) as session:
        row = session.get(AppSetting, key)
        if row:
            row.value = value
        else:
            row = AppSetting(key=key, value=value)
        session.add(row)
        session.commit()


class GoogleToken(SQLModel, table=True):
    """Single row (id=1): OAuth tokens for the Google Health API (ex Fitbit)."""
    id: int = Field(default=1, primary_key=True)
    access_token: str
    refresh_token: str
    expires_at: int  # unix epoch seconds


class Workout(SQLModel, table=True):
    """One row per Wahoo workout. Summary fields come from the webhook payload /
    workout_summary endpoint and are refined by the FIT session message when the
    file is downloaded and parsed (has_fit=True)."""
    id: int = Field(primary_key=True)  # Wahoo workout id
    name: str = ""
    sport: str = ""        # normalized sport label (from FIT or workout_type_id)
    sub_sport: str = ""
    start_date: datetime = Field(index=True)
    duration_s: int = 0          # total elapsed
    moving_s: int = 0            # timer time
    distance_m: float = 0.0
    ascent_m: float = 0.0
    avg_speed_ms: float = 0.0
    max_speed_ms: float = 0.0
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    avg_power: Optional[float] = None
    max_power: Optional[float] = None
    np_power: Optional[float] = None     # normalized power (from FIT or computed)
    avg_cadence: Optional[float] = None
    calories: Optional[float] = None
    tss: Optional[float] = None          # only if present in FIT
    intensity_factor: Optional[float] = None
    has_fit: bool = False                # True once the FIT was downloaded AND parsed
    fit_path: str = ""                   # path of the stored .fit file
    raw_summary: str = "{}"              # raw JSON from Wahoo (webhook or API)
    manual: bool = False                 # created by the user (not imported)
    notes: str = ""                      # free-text description (manual/home workouts)
    rpe: Optional[float] = None          # 1-10 perceived effort: estimated by the AI
                                         # from `notes` (kept apart from avg_hr, which
                                         # must stay measured data), used for the load
                                         # model when there is no heart rate
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WorkoutStream(SQLModel, table=True):
    """Per-record series from the FIT file, stored as gzip-compressed JSON.

    Shape of the JSON: {"t": [...], "power": [...], "hr": [...], "cadence": [...],
    "speed": [...], "alt": [...], "latlng": [[lat, lng], ...]}
    Arrays are aligned on "t" (seconds from start); missing samples are null.
    """
    workout_id: int = Field(primary_key=True, foreign_key="workout.id")
    data: bytes  # gzip(json)
    n_records: int = 0


class AiAnalysis(SQLModel, table=True):
    """Cached Claude analysis, one per workout."""
    workout_id: int = Field(primary_key=True, foreign_key="workout.id")
    content: str
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PeriodSummary(SQLModel, table=True):
    """Cached Claude summary for a period, keyed by period+start date."""
    key: str = Field(primary_key=True)  # e.g. "week:2026-06-08"
    content: str
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


def pack_streams(streams: dict) -> bytes:
    return gzip.compress(json.dumps(streams, separators=(",", ":")).encode())


def unpack_streams(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob))


os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
os.makedirs(settings.fit_dir, exist_ok=True)
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate()


# Additive column migrations (create_all does NOT add columns to existing tables)
_MIGRATIONS = {
    "routeassessment": [("route_json", "TEXT DEFAULT '{}'"),
                        ("sport", "TEXT DEFAULT 'Bici'")],
    "workout": [("manual", "INTEGER DEFAULT 0"), ("notes", "TEXT DEFAULT ''"),
                ("rpe", "REAL")],
}


def _migrate() -> None:
    with engine.connect() as conn:
        for table, cols in _MIGRATIONS.items():
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table not created yet (nothing to migrate)
            for name, decl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()


def get_session():
    with Session(engine) as session:
        yield session
