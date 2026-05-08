import discord
from discord import app_commands
from discord.ext import commands
import os
import json
import sqlite3
import traceback
from dotenv import load_dotenv
import asyncio
import time
from datetime import datetime, timedelta, timezone
try:
    import psycopg
except ImportError:
    psycopg = None

KST = timezone(timedelta(hours=9))

load_dotenv()
TOKEN = os.getenv("TOKEN")
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
except ValueError:
    OWNER_ID = 0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("STATE_FILE") or os.path.join(BASE_DIR, "wispbyte_state.json")
STATE_BACKUP_FILE = f"{STATE_FILE}.bak"
STATE_DB_FILE = os.getenv("STATE_DB_FILE") or os.path.splitext(STATE_FILE)[0] + ".db"
STATE_BACKEND_NAME = (os.getenv("STATE_BACKEND") or "sqlite").strip().lower()
POSTGRES_DSN = os.getenv("POSTGRES_DSN") or os.getenv("DATABASE_URL") or ""

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
CHANNEL_SEND_INTERVAL_SECONDS = 3
commands_synced = False

def get_retry_after(error: discord.HTTPException, default: float = CHANNEL_SEND_INTERVAL_SECONDS) -> float:
    retry_after = getattr(error, "retry_after", None)
    if retry_after is None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is None:
        text = getattr(error, "text", None)
        if isinstance(text, dict):
            retry_after = text.get("retry_after")
        elif isinstance(text, str):
            try:
                retry_after = json.loads(text).get("retry_after")
            except (ValueError, TypeError, AttributeError):
                retry_after = None
    try:
        return max(float(retry_after), 0)
    except (TypeError, ValueError):
        return default

class RateLimitedMessenger:
    def __init__(self, interval_seconds: float):
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()

    async def send(self, channel: discord.abc.Messageable, *args, **kwargs):
        async with self._lock:
            while True:
                try:
                    message = await channel.send(*args, **kwargs)
                    await asyncio.sleep(self.interval_seconds)
                    return message
                except discord.HTTPException as e:
                    if getattr(e, "status", None) != 429:
                        raise
                    retry_after = get_retry_after(e, self.interval_seconds)
                    print(f"[rate_limit] channel.send blocked; retrying after {retry_after:.2f}s")
                    await asyncio.sleep(retry_after + 1)

messenger = RateLimitedMessenger(CHANNEL_SEND_INTERVAL_SECONDS)

async def safe_channel_send(channel: discord.abc.Messageable, *args, **kwargs):
    return await messenger.send(channel, *args, **kwargs)

# ─────────────────────────────────────────
# 🌱 작물 데이터
# ─────────────────────────────────────────
CROPS = {
    "양배추": 1, "파슬리": 1, "토마토": 1, "옥수수": 1, "콩": 1,
    "딸기": 2, "양파": 2, "고추": 2, "블루베리": 2, "멜론": 2,
    "쌀": 2, "무": 2, "파": 2,
    "브로콜리": 3, "아스파라거스": 3, "바나나": 3, "가지": 3,
    "파인애플": 3, "파프리카": 3, "망고": 3, "포도": 3,
    "배추": 4, "고구마": 4, "순무": 4, "레몬": 4, "마늘": 4,
    "아티초크": 5, "복숭아": 5, "산삼": 5, "오렌지": 5,
}

SEASON_CROPS = {
    "봄":  {"양배추", "파슬리", "딸기", "양파", "브로콜리", "아스파라거스", "아티초크"},
    "여름": {"토마토", "옥수수", "고추", "블루베리", "멜론", "바나나", "가지", "파인애플", "파프리카", "망고", "복숭아"},
    "가을": {"콩", "쌀", "무", "포도", "배추", "고구마", "산삼"},
    "겨울": {"파", "순무", "레몬", "마늘", "오렌지"},
}

SEASON_ORDER = ["봄", "여름", "가을", "겨울"]

CROP_SEASON: dict[str, str] = {
    crop: season
    for season, crops in SEASON_CROPS.items()
    for crop in crops
}

REAL_MINUTES_PER_SERVER_DAY = 48
BASE_WATER_MINUTES          = 48
SUMMER_WATER_MINUTES        = 24
MAX_SLOTS                   = 5

# ─────────────────────────────────────────
# 🫙 절임통 / 🍺 양조통 상수
# ─────────────────────────────────────────
PICKLE_MINUTES = 144   # 인게임 3일
BREW_MINUTES   = 240   # 인게임 5일

# ─────────────────────────────────────────
# 🗓️ 계절 상태 (메모리)
# ─────────────────────────────────────────
current_season: dict[int, str] = {}
default_season = "가을"
last_season_change_at = datetime.now(KST)
season_task: asyncio.Task | None = None
state_save_task: asyncio.Task | None = None
state_loaded = False
last_saved_sections: dict[str, str] = {}
save_retry_sections: dict[str, str] | None = None
save_retry_not_before = 0.0
save_error_log_not_before = 0.0
SAVE_RETRY_COOLDOWN_SECONDS = 60.0

def get_season(guild_id: int | None, *, auto_create: bool = True) -> str | None:
    if guild_id is None:
        return None
    if guild_id not in current_season and auto_create:
        current_season[guild_id] = default_season
        save_state()
        print(f"[season] guild={guild_id} 기본 계절 자동 설정: {default_season}")
    return current_season.get(guild_id)

def ensure_guild_season(guild_id: int) -> str:
    season = get_season(guild_id)
    if season is None:
        raise ValueError("guild_id is required")
    return season

def next_season(season: str) -> str:
    return SEASON_ORDER[(SEASON_ORDER.index(season) + 1) % 4]

# ─────────────────────────────────────────
# 슬롯 데이터
# ─────────────────────────────────────────
plant_data:  dict[int, dict[int, dict]]         = {}
plant_tasks: dict[int, dict[int, asyncio.Task]] = {}

water_data:  dict[int, dict]         = {}
water_tasks: dict[int, asyncio.Task] = {}

# 무역
trade_data:  dict[int, dict]         = {}
trade_tasks: dict[int, asyncio.Task] = {}

# 절임통 / 양조통
pickle_data: dict[int, dict]         = {}
pickle_tasks: dict[int, asyncio.Task] = {}
brew_data:   dict[int, dict]         = {}
brew_tasks:   dict[int, asyncio.Task] = {}


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def encode_state_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): encode_state_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode_state_value(v) for v in value]
    return value

def parse_state_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=KST)
        except ValueError:
            return None
    return None

def decode_state_datetimes(data: dict) -> dict:
    dt_fields = {
        "start", "end", "last_water_click", "growth_started_at",
        "current_deadline", "next_water", "finish_time", "last_season_change_at",
    }
    decoded = dict(data)
    for field in dt_fields:
        if field in decoded:
            decoded[field] = parse_state_datetime(decoded.get(field))
    return decoded

def decode_timer_dict(raw: dict | None, *, nested_slots: bool = False) -> dict:
    decoded = {}
    if not isinstance(raw, dict):
        return decoded

    for raw_key, raw_value in raw.items():
        if not str(raw_key).isdigit():
            continue
        key = int(raw_key)
        if nested_slots:
            slots = {}
            if isinstance(raw_value, dict):
                for raw_slot, slot_value in raw_value.items():
                    if str(raw_slot).isdigit() and isinstance(slot_value, dict):
                        slots[int(raw_slot)] = decode_state_datetimes(slot_value)
            if slots:
                decoded[key] = slots
        elif isinstance(raw_value, dict):
            decoded[key] = decode_state_datetimes(raw_value)

    return decoded

def build_state_dict() -> dict:
    return {
        "default_season": default_season,
        "last_season_change_at": encode_state_value(last_season_change_at),
        "current_season": {str(gid): season for gid, season in current_season.items()},
        "plant_data": encode_state_value(plant_data),
        "water_data": encode_state_value(water_data),
        "pickle_data": encode_state_value(pickle_data),
        "brew_data": encode_state_value(brew_data),
        "trade_data": encode_state_value(trade_data),
    }

def build_state_payload() -> str:
    return json.dumps(build_state_dict(), ensure_ascii=False, separators=(",", ":"))

def build_state_sections() -> dict[str, str]:
    state = build_state_dict()
    return {
        key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for key, value in state.items()
    }

def is_no_space_error(error: Exception) -> bool:
    if getattr(error, "errno", None) == 28:
        return True
    message = str(error).lower()
    return "database or disk is full" in message or "no space left on device" in message

class StateBackend:
    label = "state-backend"

    def load_state_dict(self) -> dict | None:
        raise NotImplementedError

    def save_sections(self, sections: dict[str, str], previous_sections: dict[str, str]):
        raise NotImplementedError

    def has_storage(self) -> bool:
        return True

    def target_description(self) -> str:
        return self.label

    def startup_check(self) -> str:
        return "ready"


class SQLiteStateBackend(StateBackend):
    label = "sqlite"

    def __init__(self, db_path: str):
        self.db_path = db_path

    def ensure_schema(self, conn: sqlite3.Connection):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_sections (
                name TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )

    def has_storage(self) -> bool:
        return os.path.exists(self.db_path)

    def target_description(self) -> str:
        return self.db_path

    def startup_check(self) -> str:
        return "ready"

    def load_state_dict(self) -> dict | None:
        if not os.path.exists(self.db_path):
            return None

        with sqlite3.connect(self.db_path) as conn:
            self.ensure_schema(conn)
            rows = conn.execute("SELECT name, payload FROM state_sections").fetchall()
        if not rows:
            return None
        return {name: json.loads(payload) for name, payload in rows}

    def save_sections(self, sections: dict[str, str], previous_sections: dict[str, str]):
        changed_sections = {
            name: payload
            for name, payload in sections.items()
            if previous_sections.get(name) != payload
        }
        if not changed_sections and os.path.exists(self.db_path):
            return

        state_dir = os.path.dirname(self.db_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            self.ensure_schema(conn)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("BEGIN")
            for name, payload in changed_sections.items():
                conn.execute(
                    """
                    INSERT INTO state_sections(name, payload)
                    VALUES(?, ?)
                    ON CONFLICT(name) DO UPDATE SET payload = excluded.payload
                    """,
                    (name, payload),
                )
            conn.commit()


class PostgresStateBackend(StateBackend):
    label = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        if psycopg is None:
            raise RuntimeError(
                "STATE_BACKEND=postgres requires the 'psycopg' package to be installed."
            )
        if not self.dsn:
            raise RuntimeError(
                "STATE_BACKEND=postgres requires POSTGRES_DSN or DATABASE_URL."
            )
        return psycopg.connect(self.dsn)

    def ensure_schema(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS state_sections (
                    name TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def load_state_dict(self) -> dict | None:
        with self.connect() as conn:
            self.ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT name, payload FROM state_sections")
                rows = cur.fetchall()
        if not rows:
            return None
        return {name: json.loads(payload) for name, payload in rows}

    def save_sections(self, sections: dict[str, str], previous_sections: dict[str, str]):
        changed_sections = {
            name: payload
            for name, payload in sections.items()
            if previous_sections.get(name) != payload
        }
        if not changed_sections:
            return

        with self.connect() as conn:
            self.ensure_schema(conn)
            with conn.cursor() as cur:
                for name, payload in changed_sections.items():
                    cur.execute(
                        """
                        INSERT INTO state_sections(name, payload)
                        VALUES(%s, %s)
                        ON CONFLICT(name) DO UPDATE SET payload = EXCLUDED.payload
                        """,
                        (name, payload),
                    )
            conn.commit()

    def target_description(self) -> str:
        return "postgres"

    def startup_check(self) -> str:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                value = cur.fetchone()
        return f"connection=ok result={value[0] if value else 'unknown'}"


class JsonStateFallback:
    def __init__(self, *paths: str):
        self.paths = paths

    def load_state_dict(self) -> tuple[dict | None, str | None]:
        for state_path in self.paths:
            if not os.path.exists(state_path):
                continue
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    return json.load(f), state_path
            except Exception as e:
                print(f"[load_state] 오류: {e} (경로: {state_path})")
        return None, None


def create_state_backend() -> StateBackend:
    if STATE_BACKEND_NAME == "sqlite":
        return SQLiteStateBackend(STATE_DB_FILE)
    if STATE_BACKEND_NAME == "postgres":
        return PostgresStateBackend(POSTGRES_DSN)
    raise ValueError(f"Unsupported STATE_BACKEND: {STATE_BACKEND_NAME}")


state_backend: StateBackend = create_state_backend()
legacy_state_fallback = JsonStateFallback(STATE_FILE, STATE_BACKUP_FILE)

def ensure_state_db(conn: sqlite3.Connection):
    if isinstance(state_backend, SQLiteStateBackend):
        state_backend.ensure_schema(conn)

def state_backend_target() -> str:
    return state_backend.target_description()

def state_backend_log_target() -> str:
    try:
        return state_backend_target()
    except Exception:
        return STATE_DB_FILE

def ensure_state_save_worker():
    global state_save_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if state_save_task is None or state_save_task.done():
        state_save_task = loop.create_task(retry_pending_state_saves())

def log_state_backend_startup():
    try:
        target = state_backend_target()
        if state_backend.label == "postgres":
            check = state_backend.startup_check()
            print(
                f"[state_backend] backend={state_backend.label} target={target} "
                f"psycopg={'yes' if psycopg else 'no'} {check}"
            )
        else:
            print(f"[state_backend] backend={state_backend.label} target={target} {state_backend.startup_check()}")
    except Exception as e:
        print(f"[state_backend] startup check failed: {e}")

def save_state():
    global save_retry_sections, save_retry_not_before
    sections = build_state_sections()
    if sections == last_saved_sections and state_backend.has_storage():
        return
    save_retry_sections = sections
    save_retry_not_before = 0.0
    ensure_state_save_worker()

def persist_loaded_state_to_backend(source_path: str | None):
    global save_retry_sections, save_retry_not_before
    if not source_path or source_path == state_backend_target():
        return

    sections = build_state_sections()
    save_retry_sections = sections
    save_retry_not_before = 0.0
    print(f"[state_backend] queued loaded state migration from {source_path} to {state_backend_target()}")

async def retry_pending_state_saves():
    global last_saved_sections, save_retry_sections, save_retry_not_before, save_error_log_not_before
    while True:
        await asyncio.sleep(1)
        if save_retry_sections is None:
            continue
        if time.monotonic() < save_retry_not_before:
            continue

        sections = save_retry_sections
        previous_sections = last_saved_sections
        now_monotonic = time.monotonic()

        try:
            await asyncio.to_thread(state_backend.save_sections, sections, previous_sections)
            last_saved_sections = sections
            if save_retry_sections == sections:
                save_retry_sections = None
            save_retry_not_before = 0.0
            save_error_log_not_before = 0.0
        except (OSError, sqlite3.Error) as e:
            if is_no_space_error(e):
                save_retry_not_before = now_monotonic + SAVE_RETRY_COOLDOWN_SECONDS
                if now_monotonic >= save_error_log_not_before:
                    print(
                        f"[save_state] 저장 보류: 디스크 공간 부족으로 {SAVE_RETRY_COOLDOWN_SECONDS:.0f}초 뒤 재시도합니다. "
                        f"(경로: {state_backend_log_target()}, 섹션 수: {len(sections)})"
                    )
                    print(f"[save_state] 오류: {e} (경로: {state_backend_log_target()})")
                    save_error_log_not_before = now_monotonic + SAVE_RETRY_COOLDOWN_SECONDS
                continue
            save_retry_not_before = now_monotonic + 5.0
            print(f"[save_state] 오류: {e} (경로: {state_backend_log_target()})")
        except Exception as e:
            save_retry_not_before = now_monotonic + 5.0
            print(f"[save_state] 오류: {e} (경로: {state_backend_log_target()})")

def load_state_legacy_direct():
    global default_season, last_saved_sections
    state = None
    loaded_path = None

    if os.path.exists(STATE_DB_FILE):
        try:
            with sqlite3.connect(STATE_DB_FILE) as conn:
                ensure_state_db(conn)
                rows = conn.execute("SELECT name, payload FROM state_sections").fetchall()
            if rows:
                state = {}
                for name, payload in rows:
                    state[name] = json.loads(payload)
                loaded_path = STATE_DB_FILE
        except Exception as e:
            print(f"[load_state] 오류: {e} (경로: {STATE_DB_FILE})")

    if state is None:
        for state_path in (STATE_FILE, STATE_BACKUP_FILE):
            if not os.path.exists(state_path):
                continue
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                loaded_path = state_path
                break
            except Exception as e:
                print(f"[load_state] 오류: {e} (경로: {state_path})")

    if not isinstance(state, dict):
        return

    try:
        loaded_default = (
            state.get("default_season")
            or state.get("season")
            or state.get("current_season", {}).get("default")
        )
        if loaded_default in SEASON_ORDER:
            default_season = loaded_default

        loaded_current_season = {
            int(gid): season
            for gid, season in state.get("current_season", {}).items()
            if str(gid).isdigit() and season in SEASON_ORDER
        }

        current_season.clear()
        current_season.update(loaded_current_season)
        plant_data.clear()
        plant_data.update(decode_timer_dict(state.get("plant_data"), nested_slots=True))
        water_data.clear()
        water_data.update(decode_timer_dict(state.get("water_data")))
        pickle_data.clear()
        pickle_data.update(decode_timer_dict(state.get("pickle_data")))
        brew_data.clear()
        brew_data.update(decode_timer_dict(state.get("brew_data")))
        trade_data.clear()
        trade_data.update(decode_timer_dict(state.get("trade_data")))
        last_saved_sections = build_state_sections()
        print(f"[load_state] loaded from {loaded_path}")
        persist_loaded_state_to_backend(loaded_path)
    except Exception as e:
        print(f"[load_state] 상태 적용 오류: {e} (경로: {loaded_path})")

def load_state():
    global default_season, last_saved_sections, last_season_change_at
    state = None
    loaded_path = None

    try:
        state = state_backend.load_state_dict()
        if state is not None:
            loaded_path = state_backend_target()
    except Exception as e:
        print(f"[load_state] 오류: {e} (backend={state_backend.label}, path={state_backend_target()})")

    if state is None:
        state, loaded_path = legacy_state_fallback.load_state_dict()

    if not isinstance(state, dict):
        return

    try:
        loaded_default = (
            state.get("default_season")
            or state.get("season")
            or state.get("current_season", {}).get("default")
        )
        if loaded_default in SEASON_ORDER:
            default_season = loaded_default
        loaded_last_season_change_at = parse_state_datetime(state.get("last_season_change_at"))
        if loaded_last_season_change_at is not None:
            last_season_change_at = loaded_last_season_change_at

        loaded_current_season = {
            int(gid): season
            for gid, season in state.get("current_season", {}).items()
            if str(gid).isdigit() and season in SEASON_ORDER
        }

        current_season.clear()
        current_season.update(loaded_current_season)
        plant_data.clear()
        plant_data.update(decode_timer_dict(state.get("plant_data"), nested_slots=True))
        water_data.clear()
        water_data.update(decode_timer_dict(state.get("water_data")))
        pickle_data.clear()
        pickle_data.update(decode_timer_dict(state.get("pickle_data")))
        brew_data.clear()
        brew_data.update(decode_timer_dict(state.get("brew_data")))
        trade_data.clear()
        trade_data.update(decode_timer_dict(state.get("trade_data")))
        last_saved_sections = build_state_sections()
        print(f"[load_state] loaded from {loaded_path}")
    except Exception as e:
        print(f"[load_state] 상태 적용 오류: {e} (경로: {loaded_path})")

async def safe_interaction_error(interaction: discord.Interaction, embed: discord.Embed):
    if interaction.is_expired():
        print("[interaction] expired before error response could be sent")
        return
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.NotFound:
        print("[interaction] expired before error response could be sent")
    except discord.InteractionResponded:
        print("[interaction] error response skipped because interaction was already acknowledged")
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            retry_after = get_retry_after(e)
            print(f"[rate_limit] interaction error response blocked; cooling down {retry_after:.2f}s")
            await asyncio.sleep(retry_after + 1)
            return
        print(f"[interaction] error response failed: {e}")

def calc_growth(crop: str, season: str) -> tuple[float, int, bool]:
    days         = CROPS[crop]
    base_min     = days * REAL_MINUTES_PER_SERVER_DAY
    in_season    = crop in SEASON_CROPS.get(season, set())
    season_speed = 1.0
    if season == "봄":
        season_speed += 0.2
    elif season == "겨울":
        season_speed -= 0.5
    growth_speed = season_speed + (0.5 if in_season else 0.0)
    final_min    = round(base_min / growth_speed, 1)
    water_min    = SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES
    return final_min, water_min, in_season

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def timer_remaining_minutes(finish_time: datetime | None, now: datetime) -> float:
    if finish_time is None:
        return 0
    return max(round((finish_time - now).total_seconds() / 60, 1), 0)

def season_emoji(season: str) -> str:
    return {"봄": "🌸", "여름": "☀️", "가을": "🍂", "겨울": "❄️"}.get(season, "🌿")

def slot_emoji(slot: int) -> str:
    return ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"][slot - 1]

def get_free_plant_slot(user_id: int) -> int | None:
    used = set(plant_data.get(user_id, {}).keys())
    for s in range(1, MAX_SLOTS + 1):
        if s not in used:
            return s
    return None

def cancel_plant(user_id: int, slot: int):
    task = plant_tasks.get(user_id, {}).pop(slot, None)
    if task and not task.done():
        task.cancel()
    if user_id in plant_tasks and not plant_tasks[user_id]:
        plant_tasks.pop(user_id)
    if user_id in plant_data:
        plant_data[user_id].pop(slot, None)
        if not plant_data[user_id]:
            plant_data.pop(user_id)
    save_state()

def cancel_water(user_id: int):
    task = water_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    water_data.pop(user_id, None)
    save_state()

def cancel_trade(user_id: int):
    task = trade_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    trade_data.pop(user_id, None)
    save_state()

def cancel_pickle(user_id: int):
    task = pickle_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    pickle_data.pop(user_id, None)
    save_state()

def cancel_brew(user_id: int):
    task = brew_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    brew_data.pop(user_id, None)
    save_state()

def cancel_task_map(task_map: dict):
    for task in list(task_map.values()):
        if isinstance(task, dict):
            cancel_task_map(task)
        elif task and not task.done():
            task.cancel()
    task_map.clear()

def reset_non_season_state() -> dict[str, int]:
    counts = {
        "plant": sum(len(slots) for slots in plant_data.values()),
        "water": len(water_data),
        "pickle": len(pickle_data),
        "brew": len(brew_data),
        "trade": len(trade_data),
    }

    cancel_task_map(plant_tasks)
    cancel_task_map(water_tasks)
    cancel_task_map(pickle_tasks)
    cancel_task_map(brew_tasks)
    cancel_task_map(trade_tasks)

    plant_data.clear()
    water_data.clear()
    pickle_data.clear()
    brew_data.clear()
    trade_data.clear()

    save_state()
    return counts

def get_water_minutes(guild_id: int) -> int:
    season = get_season(guild_id)
    return SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES

def required_growth_seconds(data: dict) -> float:
    return float(data.get("growth_min", 0)) * 60

def current_growth_progress(data: dict, now: datetime | None = None) -> float:
    now = now or datetime.now(KST)
    progress = float(data.get("growth_progress_sec", 0))
    started = data.get("growth_started_at")
    deadline = data.get("current_deadline")

    if started is not None and deadline is not None:
        active_until = min(now, deadline)
        progress += max((active_until - started).total_seconds(), 0)

    return min(progress, required_growth_seconds(data))

def commit_growth_progress(data: dict, now: datetime | None = None):
    now = now or datetime.now(KST)
    data["growth_progress_sec"] = current_growth_progress(data, now)
    data["growth_started_at"] = None

def pause_growth_if_needed(data: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    started = data.get("growth_started_at")
    deadline = data.get("current_deadline")
    if started is None or deadline is None or now < deadline:
        return False

    commit_growth_progress(data, deadline)
    data["current_deadline"] = None
    data["timer_version"] = data.get("timer_version", 0) + 1
    return True

def get_wet_soil_deadline(user_id: int, guild_id: int | None, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(KST)
    data = water_data.get(user_id)
    if not data or data.get("water_count", 0) <= 0:
        return None
    if guild_id is not None and data.get("guild_id") != guild_id:
        return None

    next_water = data.get("next_water")
    if next_water is None or next_water <= now:
        return None
    return next_water

async def send_harvest_notice(user_id: int, slot: int, channel: discord.TextChannel, mention: str):
    if user_id not in plant_data or slot not in plant_data[user_id]:
        return

    data = plant_data[user_id][slot]
    crop = data["crop"]
    season = data["season"]
    now = datetime.now(KST)

    harvest_embed = discord.Embed(
        title=f"🌾 {slot_emoji(slot)} 슬롯{slot} — {crop} 수확 완료!",
        description=f"{mention} **{crop}** 수확하세요!\n⏰ `{fmt_time(now)}`",
        color=0xFFD700
    )
    harvest_embed.add_field(name="재배 시간", value=f"⏱ `{data['growth_min']}분`", inline=True)
    harvest_embed.add_field(name="계절", value=f"{season_emoji(season)} {season}", inline=True)
    if season == "가을":
        harvest_embed.add_field(name="🍂 가을 보너스", value="2.5% 확률로 수확량 2배!", inline=False)
    await safe_channel_send(channel, content=mention, embed=harvest_embed)
    cancel_plant(user_id, slot)

def apply_season_to_plants(guild_id: int, season: str):
    now = datetime.now(KST)
    for uid, slots in list(plant_data.items()):
        for slot, pdata in list(slots.items()):
            if pdata.get("guild_id") != guild_id:
                continue

            crop = pdata["crop"]
            old_growth_min = max(pdata.get("growth_min", REAL_MINUTES_PER_SERVER_DAY), 0.1)
            old_deadline = pdata.get("current_deadline")
            was_growing = pdata.get("growth_started_at") is not None and old_deadline is not None and now < old_deadline
            progress_sec = current_growth_progress(pdata, now)
            progress_ratio = min(progress_sec / max(old_growth_min * 60, 1), 1)

            growth_min, water_min, in_season = calc_growth(crop, season)
            new_required_sec = growth_min * 60
            new_progress_sec = min(new_required_sec * progress_ratio, new_required_sec)

            pdata["season"] = season
            pdata["growth_min"] = growth_min
            pdata["water_min"] = water_min
            pdata["in_season"] = in_season
            pdata["growth_progress_sec"] = new_progress_sec
            pdata["end"] = now + timedelta(seconds=max(new_required_sec - new_progress_sec, 0))
            pdata["growth_warn_sent"] = False
            pdata["timer_version"] = pdata.get("timer_version", 0) + 1

            if was_growing and new_progress_sec < new_required_sec:
                old_remain = max((old_deadline - now).total_seconds(), 0)
                old_total = max((old_deadline - pdata["growth_started_at"]).total_seconds(), 1)
                remain_ratio = old_remain / old_total
                pdata["growth_started_at"] = now
                pdata["current_deadline"] = now + timedelta(seconds=water_min * 60 * remain_ratio)
            else:
                pdata["growth_started_at"] = None
                pdata["current_deadline"] = None

def apply_season_to_water(guild_id: int, season: str):
    new_water_min = SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES
    now = datetime.now(KST)

    for uid, wdata in list(water_data.items()):
        if wdata.get("guild_id") != guild_id:
            continue

        old_min = wdata.get("water_min", BASE_WATER_MINUTES)
        next_water = wdata.get("next_water", now)
        old_remaining = max((next_water - now).total_seconds(), 0)
        ratio = old_remaining / max(old_min * 60, 1)

        wdata["water_min"] = new_water_min
        wdata["season"] = season
        wdata["next_water"] = now + timedelta(seconds=new_water_min * 60 * ratio)
        wdata["timer_version"] = wdata.get("timer_version", 0) + 1

def change_guild_season(guild_id: int, season: str):
    current_season[guild_id] = season
    apply_season_to_water(guild_id, season)
    apply_season_to_plants(guild_id, season)
    save_state()

def change_all_guild_seasons(season: str) -> tuple[int, int]:
    global default_season, last_season_change_at
    default_season = season
    last_season_change_at = datetime.now(KST)

    target_guild_ids = {guild.id for guild in bot.guilds}
    target_guild_ids.update(current_season.keys())

    changed_count = 0
    for guild_id in target_guild_ids:
        if current_season.get(guild_id) != season:
            changed_count += 1
        current_season[guild_id] = season
        apply_season_to_water(guild_id, season)
        apply_season_to_plants(guild_id, season)

    save_state()
    return len(target_guild_ids), changed_count

def reconcile_season_schedule() -> tuple[str, str, int]:
    global default_season, last_season_change_at
    now = datetime.now(KST)
    prev = default_season
    days_passed = max((now.date() - last_season_change_at.date()).days, 0)
    if days_passed <= 0:
        return prev, default_season, 0

    curr = default_season
    for _ in range(days_passed):
        curr = next_season(curr)

    if curr != prev:
        change_all_guild_seasons(curr)
    else:
        last_season_change_at = now
        save_state()
    return prev, curr, days_passed

async def get_saved_channel(channel_id: int | None):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

async def restore_running_tasks():
    restored = 0
    for user_id, slots in list(plant_data.items()):
        for slot, data in list(slots.items()):
            channel = await get_saved_channel(data.get("channel_id"))
            mention = data.get("user_mention") or f"<@{user_id}>"
            if channel is None:
                slots.pop(slot, None)
                continue
            plant_tasks.setdefault(user_id, {})[slot] = bot.loop.create_task(
                harvest_timer(user_id, slot, channel, mention)
            )
            restored += 1
        if not slots:
            plant_data.pop(user_id, None)

    for user_id, data in list(water_data.items()):
        channel = await get_saved_channel(data.get("channel_id"))
        mention = data.get("user_mention") or f"<@{user_id}>"
        if channel is None:
            water_data.pop(user_id, None)
            continue
        if data.get("awaiting_click"):
            print(f"[restore] dropping pending water prompt for user={user_id} after restart")
            water_data.pop(user_id, None)
            continue
        water_tasks[user_id] = bot.loop.create_task(water_loop(user_id, channel, mention))
        restored += 1

    for user_id, data in list(pickle_data.items()):
        channel = await get_saved_channel(data.get("channel_id"))
        if channel is None:
            pickle_data.pop(user_id, None)
            continue
        pickle_tasks[user_id] = bot.loop.create_task(process_timer_loop("pickle", user_id, channel))
        restored += 1

    for user_id, data in list(brew_data.items()):
        channel = await get_saved_channel(data.get("channel_id"))
        if channel is None:
            brew_data.pop(user_id, None)
            continue
        brew_tasks[user_id] = bot.loop.create_task(process_timer_loop("brew", user_id, channel))
        restored += 1

    for user_id, data in list(trade_data.items()):
        channel = await get_saved_channel(data.get("channel_id"))
        if channel is None:
            trade_data.pop(user_id, None)
            continue
        trade_tasks[user_id] = bot.loop.create_task(process_timer_loop("trade", user_id, channel))
        restored += 1

    save_state()
    print(f"[restore] restored {restored} running timers")

# ─────────────────────────────────────────
# on_ready
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    global season_task, state_save_task, state_loaded, commands_synced
    if not state_loaded:
        log_state_backend_startup()
        load_state()
        prev_season, curr_season, days_passed = reconcile_season_schedule()
        if days_passed > 0:
            print(f"[season_reconcile] startup catch-up: {prev_season} -> {curr_season} ({days_passed}일 보정)")
        state_loaded = True
        await restore_running_tasks()

    created_count = 0
    for guild in bot.guilds:
        if guild.id not in current_season:
            ensure_guild_season(guild.id)
            created_count += 1

    if season_task is None or season_task.done():
        season_task = bot.loop.create_task(season_tick())
    if state_save_task is None or state_save_task.done():
        state_save_task = bot.loop.create_task(retry_pending_state_saves())

    synced = []
    if not commands_synced:
        synced = await bot.tree.sync()
        commands_synced = True
    print(f"✅ {bot.user} 로그인 완료")
    if created_count:
        print(f"🗓️ 기본 계절 자동 설정 — {created_count}개 서버")
    if synced:
        print(f"🌐 전역 동기화 — {len(synced)}개 커맨드")
        for cmd in synced:
            print(f"   └─ /{cmd.name}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    season = ensure_guild_season(guild.id)
    print(f"[on_guild_join] guild={guild.id} 기본 계절 자동 설정: {season}")


# ─────────────────────────────────────────
# 🗓️ 자정 계절 자동 변경 루프
# ─────────────────────────────────────────
async def season_tick():
    await bot.wait_until_ready()
    while True:
        now      = datetime.now(KST)
        midnight = now.replace(hour=0, minute=0, second=5, microsecond=0)
        if now >= midnight:
            midnight = midnight + timedelta(days=1)

        wait_sec = (midnight - now).total_seconds()
        print(f"[season_tick] 다음 계절 변경까지 {wait_sec:.0f}초 대기 ({fmt_time(midnight)} KST)")
        await asyncio.sleep(wait_sec)

        prev = default_season
        curr = next_season(prev)
        total_guilds, changed_count = change_all_guild_seasons(curr)
        print(f"[season_tick] 전체 계절 변경: {prev} → {curr} ({changed_count}/{total_guilds}개 서버 변경)")


# ─────────────────────────────────────────
# /현재계절
# ─────────────────────────────────────────
@bot.tree.command(name="현재계절", description="현재 서버 계절을 확인합니다")
async def cmd_current_season(interaction: discord.Interaction):
    season = get_season(interaction.guild_id)
    if not season:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 계절 미설정",
            description="봇 소유자가 먼저 `!계절설정`으로 계절을 설정해야 합니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    nxt = next_season(season)
    embed = discord.Embed(
        title=f"{season_emoji(season)} 현재 계절: {season}",
        color={"봄": 0xFFB7C5, "여름": 0xFFD700, "가을": 0xFF8C00, "겨울": 0x87CEEB}.get(season, 0x57F287)
    )
    embed.add_field(name="현재 계절", value=f"{season_emoji(season)} **{season}**", inline=True)
    embed.add_field(name="다음 계절", value=f"{season_emoji(nxt)} **{nxt}**",       inline=True)
    crops_str = "  ".join(f"`{c}`" for c in sorted(SEASON_CROPS.get(season, set())))
    embed.add_field(name=f"{season_emoji(season)} 제철 작물", value=crops_str, inline=False)
    embed.set_footer(text="매일 자정(KST) 자동으로 다음 계절로 넘어갑니다")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
# !계절설정 (봇 소유자 전용, 슬래시 명령어에 표시되지 않음)
# ─────────────────────────────────────────
@bot.command(name="계절설정", hidden=True)
async def cmd_set_season(ctx: commands.Context, 계절: str = ""):
    if OWNER_ID == 0:
        await ctx.send("❌ `.env`에 `OWNER_ID=디스코드_유저_ID`를 먼저 설정해주세요.")
        return

    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ 이 명령어는 봇 소유자만 사용할 수 있습니다.")
        return

    if ctx.guild is None:
        await ctx.send("❌ 서버 채널에서만 사용할 수 있습니다.")
        return

    if 계절 not in SEASON_ORDER:
        await ctx.send("❌ 계절은 `봄`, `여름`, `가을`, `겨울` 중 하나로 입력해주세요. 예: `!계절설정 봄`")
        return

    total_guilds, changed_count = change_all_guild_seasons(계절)

    new_water_min = SUMMER_WATER_MINUTES if 계절 == "여름" else BASE_WATER_MINUTES

    nxt = next_season(계절)
    embed = discord.Embed(
        title="✅ 전체 서버 계절 설정 완료",
        color={"봄": 0xFFB7C5, "여름": 0xFFD700, "가을": 0xFF8C00, "겨울": 0x87CEEB}.get(계절, 0x57F287)
    )
    embed.add_field(name="기본/현재 계절", value=f"{season_emoji(계절)} **{계절}**", inline=True)
    embed.add_field(name="다음 계절", value=f"{season_emoji(nxt)} **{nxt}**", inline=True)
    embed.add_field(name="적용 서버", value=f"`{total_guilds}`개 서버 중 `{changed_count}`개 변경", inline=True)
    crops_str = "  ".join(f"`{c}`" for c in sorted(SEASON_CROPS.get(계절, set())))
    embed.add_field(name=f"{season_emoji(계절)} 제철 작물", value=crops_str, inline=False)
    embed.add_field(
        name="💧 물주기 간격",
        value=f"진행 중인 모든 서버의 물주기 간격이 **{new_water_min}분** 기준으로 자동 반영되었습니다.",
        inline=False
    )
    embed.set_footer(text="이 설정은 모든 서버와 새로 들어오는 서버에 적용됩니다 · 매일 자정(KST)에 자동으로 다음 계절로 넘어갑니다")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────
# !데이터초기화 (봇 소유자 전용, 계절 제외)
# ─────────────────────────────────────────
@bot.command(name="데이터초기화", hidden=True)
async def cmd_reset_data(ctx: commands.Context, 확인: str = ""):
    if OWNER_ID == 0:
        await ctx.send("❌ `.env`에 `OWNER_ID=디스코드_유저_ID`를 먼저 설정해주세요.")
        return

    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ 이 명령어는 봇 소유자만 사용할 수 있습니다.")
        return

    if 확인 != "확인":
        await ctx.send(
            "⚠️ 계절 정보만 남기고 심기/물주기/절임통/양조통/무역 데이터를 초기화합니다.\n"
            "정말 실행하려면 `!데이터초기화 확인`을 입력해주세요."
        )
        return

    counts = reset_non_season_state()
    embed = discord.Embed(
        title="✅ JSON 데이터 초기화 완료",
        description="계절 정보는 유지했고, 진행 중이던 타이머 데이터만 초기화했습니다.",
        color=0x57F287
    )
    embed.add_field(name="🌱 심기", value=f"`{counts['plant']}`개", inline=True)
    embed.add_field(name="💧 물주기", value=f"`{counts['water']}`개", inline=True)
    embed.add_field(name="🫙 절임통", value=f"`{counts['pickle']}`개", inline=True)
    embed.add_field(name="🍺 양조통", value=f"`{counts['brew']}`개", inline=True)
    embed.add_field(name="🚢 무역", value=f"`{counts['trade']}`개", inline=True)
    embed.add_field(name="보존됨", value=f"기본 계절 `{default_season}`, 서버별 계절 `{len(current_season)}`개", inline=False)
    await ctx.send(embed=embed)


# ─────────────────────────────────────────
# /도움말
# ─────────────────────────────────────────
@bot.tree.command(name="도움말", description="봇 명령어 목록을 보여줍니다")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 농장 봇 도움말", color=0x57F287)
    embed.add_field(name="🌱 /심기 [작물]",        value="작물 심기 + 수확 알림 (현재 계절 자동 적용, 최대 5슬롯)", inline=False)
    embed.add_field(name="💧 /물주기",             value="물주기 타이머 시작 → ✅ 클릭 시마다 심기 슬롯 진행 (무한반복)", inline=False)
    embed.add_field(name="📊 /상태",               value="심기 슬롯 + 물주기 현황 확인",                              inline=False)
    embed.add_field(name="⛔ /심기취소 [슬롯]",    value="심기 슬롯 취소 (슬롯 생략 시 전체 취소)",                   inline=False)
    embed.add_field(name="⛔ /물주기취소",          value="진행 중인 물주기 중단",                                    inline=False)
    embed.add_field(name="🌾 /작물목록",            value="모든 작물과 성장 정보 보기",                               inline=False)
    embed.add_field(name="🗓️ /현재계절",           value="현재 서버 계절 확인",                                      inline=False)
    embed.add_field(name="─────────────────", value="🫙 **가공 알림**", inline=False)
    embed.add_field(name="🫙 /절임통",             value="절임통 타이머 시작 (인게임 3일 = 144분)",                   inline=False)
    embed.add_field(name="⛔ /절임통취소",          value="진행 중인 절임통 타이머 취소",                             inline=False)
    embed.add_field(name="🍺 /양조통",             value="양조통 타이머 시작 (인게임 5일 = 240분)",                   inline=False)
    embed.add_field(name="⛔ /양조통취소",          value="진행 중인 양조통 타이머 취소",                             inline=False)
    embed.add_field(name="─────────────────", value="🚢 **무역 알림**", inline=False)
    embed.add_field(name="🚢 /무역대기",            value="1시간 뒤 무역 물품 넣기 알림",                             inline=False)
    embed.add_field(name="🚢 /무역종료 [시간]",     value="지정한 시간 뒤 무역 완료 알림 (예: `7시35분` / `30분` / `2시`)", inline=False)
    embed.add_field(name="🏳️ /무역포기",           value="무역 포기 + 3시간 뒤 재확인 알림",                         inline=False)
    embed.add_field(name="⛔ /무역타이머취소",      value="진행 중인 무역 타이머 취소",                               inline=False)
    embed.set_footer(text="서버 1일 = 현실 48분 | 자정(KST) 기준 계절 자동 변경\n🌱 물을 준 동안만 작물이 성장하고, 다음 물주기를 놓치면 성장이 멈춥니다")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
# /작물목록
# ─────────────────────────────────────────
@bot.tree.command(name="작물목록", description="심을 수 있는 모든 작물을 보여줍니다")
async def cmd_croplist(interaction: discord.Interaction):
    await interaction.response.defer()

    current      = get_season(interaction.guild_id)
    DAY_ICON     = {1: "⚡", 2: "🌿", 3: "🌳", 4: "🏔️", 5: "💎"}
    SEASON_COLOR = {"봄": 0xFFB7C5, "여름": 0xFFD700, "가을": 0xFF8C00, "겨울": 0x87CEEB}
    SEASON_DESC  = {
        "봄":  "🌸 봄 작물 — 성장속도 +20%",
        "여름": "☀️ 여름 작물 — 물주기 간격 24분 (절반!)",
        "가을": "🍂 가을 작물 — 2.5% 확률 수확량 2배!",
        "겨울": "❄️ 겨울 작물 — 성장속도 -50%",
    }

    embeds = []
    for season in SEASON_ORDER:
        crops_sorted = sorted(SEASON_CROPS[season], key=lambda c: CROPS[c])
        groups: dict[int, list[str]] = {}
        for c in crops_sorted:
            groups.setdefault(CROPS[c], []).append(c)

        title = f"{season_emoji(season)} {season} 작물"
        if current == season:
            title += "  ◀ 현재 계절"

        embed = discord.Embed(title=title, description=SEASON_DESC[season], color=SEASON_COLOR[season])
        for days in sorted(groups):
            lines = []
            for c in groups[days]:
                g_min, w_min, in_s = calc_growth(c, season)
                bonus = "✅제철" if in_s else "➖비제철"
                lines.append(f"`{c}` {bonus} · ⏱{g_min}분")
            embed.add_field(
                name=f"{DAY_ICON.get(days,'🌱')} {days}일 작물",
                value="\n".join(lines),
                inline=False
            )
        embed.set_footer(text="/심기 [작물명] — 현재 계절 자동 적용!")
        embeds.append(embed)

    guide = discord.Embed(
        title="📖 작물 가이드",
        color=0x57F287,
        description=(
            "**⚡ 1일** — 빠른 수확\n"
            "**🌿 2일** — 무난한 수익\n"
            "**🌳 3일** — 중간 보상\n"
            "**🏔️ 4일** — 높은 보상\n"
            "**💎 5일** — 최고 보상\n"
            "─────────────────\n"
            f"🌱 심기 슬롯 최대 **{MAX_SLOTS}개**\n"
            "💧 물주기 슬롯 최대 **1개** (유저당)\n"
            "⏱️ 작물은 물주기 ✅ 후 다음 물주기 시간까지 성장\n"
            "⏸️ 다음 물주기를 놓치면 성장 정지\n"
            "🌾 필요한 성장 시간을 다 채우면 수확 알림\n"
            "🔔 서버 누구든 ✅ 클릭 시점부터 다음 물주기 타이머 시작"
        )
    )
    embeds.append(guide)
    await interaction.followup.send(embeds=embeds)


# ─────────────────────────────────────────
# /심기
# ─────────────────────────────────────────
@bot.tree.command(name="심기", description="작물을 심고 수확 알림을 시작합니다 (물주기는 /물주기 사용)")
@app_commands.describe(작물="심을 작물 이름 (예: 딸기)")
async def cmd_plant(interaction: discord.Interaction, 작물: str):
    season = get_season(interaction.guild_id)
    if not season:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 계절 미설정",
            description="봇 소유자가 먼저 `!계절설정`으로 계절을 설정해야 합니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    if 작물 not in CROPS:
        similar = [c for c in CROPS if 작물 in c or c in 작물]
        hint    = f"\n💡 혹시 이 작물인가요? → {', '.join(f'`{c}`' for c in similar[:5])}" if similar else ""
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 알 수 없는 작물",
            description=f"`{작물}`은 등록되지 않은 작물입니다.{hint}\n`/작물목록`으로 확인하세요.",
            color=0xED4245
        ), ephemeral=True)
        return

    user_id = interaction.user.id
    slot    = get_free_plant_slot(user_id)
    if slot is None:
        used_info = "\n".join(
            f"{slot_emoji(s)} 슬롯{s}: `{plant_data[user_id][s]['crop']}`"
            for s in sorted(plant_data[user_id].keys())
        )
        await interaction.response.send_message(embed=discord.Embed(
            title=f"⚠️ 심기 슬롯 꽉 참 (최대 {MAX_SLOTS}개)",
            description=f"`/심기취소 [슬롯번호]`로 정리 후 다시 심으세요.\n\n{used_info}",
            color=0xFEE75C
        ), ephemeral=True)
        return

    growth_min, water_min, in_season = calc_growth(작물, season)
    now         = datetime.now(KST)
    sem         = season_emoji(season)
    crop_season = CROP_SEASON.get(작물, "알 수 없음")
    crop_sem    = season_emoji(crop_season)

    wet_soil_deadline = get_wet_soil_deadline(user_id, interaction.guild_id, now)
    plant_data.setdefault(user_id, {})[slot] = {
        "crop": 작물, "season": season, "growth_min": growth_min,
        "water_min": water_min, "in_season": in_season,
        "start": now, "end": now + timedelta(minutes=growth_min),
        # 마지막 물주기 완료 시각 기록용
        "last_water_click": None,
        "growth_progress_sec": 0,
        "growth_started_at": None,
        # 현재 물주기 회차 기준 다음 물주기 예상 시각
        "current_deadline": None,
        "timer_version": 0,
        "user_mention": interaction.user.mention,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
    }
    if wet_soil_deadline is not None:
        pdata = plant_data[user_id][slot]
        pdata["last_water_click"] = now
        pdata["growth_started_at"] = now
        pdata["current_deadline"] = wet_soil_deadline
        pdata["growth_warn_sent"] = False

    bonus_text = (
        "✅ 제철! 성장속도 +50% 보너스"
        if in_season else
        f"❌ 비제철 (제철: {crop_sem} {crop_season})"
    )
    slot_status = "  ".join(
        f"{slot_emoji(s)}`{plant_data[user_id][s]['crop']}`"
        for s in sorted(plant_data[user_id].keys())
    )

    embed = discord.Embed(
        title=f"🌱 [{slot_emoji(slot)} 슬롯{slot}] {작물} 심기 완료!",
        color=0x57F287 if in_season else 0xFFA500
    )
    embed.add_field(name="작물",           value=f"`{작물}`",                      inline=True)
    embed.add_field(name="슬롯",           value=f"{slot_emoji(slot)} {slot}번",   inline=True)
    embed.add_field(name="현재 계절",      value=f"{sem} {season}",               inline=True)
    embed.add_field(name="이 작물의 제철", value=f"{crop_sem} {crop_season}",     inline=True)
    embed.add_field(name="제철 여부",      value=bonus_text,                      inline=False)
    embed.add_field(name="필요 성장 시간", value=f"⏱ `{growth_min}분`", inline=True)
    embed.add_field(name="내 심기 현황",   value=slot_status,                     inline=False)
    if wet_soil_deadline is not None:
        wet_remain_min = max(round((wet_soil_deadline - now).total_seconds() / 60, 1), 0)
        embed.add_field(
            name="💧 젖은 땅 적용",
            value=f"이미 물이 있어서 `{fmt_time(wet_soil_deadline)}`까지 (`{wet_remain_min}분`) 바로 성장합니다.",
            inline=False
        )
    embed.add_field(
        name="⏱️ 수확 조건",
        value="물주기 ✅를 누른 뒤 다음 물주기 시간까지 성장합니다. 물을 안 주면 성장이 멈춥니다.",
        inline=False
    )
    if season == "가을":
        embed.add_field(name="특이사항", value="🍂 수확 시 2.5% 확률로 수확량 2배!", inline=False)
    embed.set_footer(text=f"심기 슬롯 {slot}/{MAX_SLOTS} | 물을 준 동안만 성장합니다!")

    await interaction.response.send_message(content=interaction.user.mention, embed=embed)
    save_state()
    plant_tasks.setdefault(user_id, {})[slot] = bot.loop.create_task(
        harvest_timer(user_id, slot, interaction.channel, interaction.user.mention)
    )




async def harvest_timer(user_id: int, slot: int, channel: discord.TextChannel, mention: str):
    try:
        if user_id not in plant_data or slot not in plant_data[user_id]:
            return

        while True:
            if user_id not in plant_data or slot not in plant_data[user_id]:
                return

            data = plant_data[user_id][slot]
            crop = data["crop"]
            version = data.get("timer_version", 0)
            now = datetime.now(KST)
            pause_growth_if_needed(data, now)
            progress = current_growth_progress(data, now)
            required = required_growth_seconds(data)
            remain_sec = max(required - progress, 0)
            is_growing = data.get("growth_started_at") is not None and data.get("current_deadline") is not None

            if remain_sec <= 0:
                await send_harvest_notice(user_id, slot, channel, mention)
                return

            if not is_growing:
                await asyncio.sleep(5)
                continue

            if remain_sec <= 60 and not data.get("growth_warn_sent"):
                await safe_channel_send(channel, embed=discord.Embed(
                    title=f"🔔 {slot_emoji(slot)} 슬롯{slot} — {crop} 재배 완료 1분 전!",
                    description=f"{mention} **{crop}** 재배 시간이 **1분 후** 완료됩니다.",
                    color=0xFEE75C
                ))
                data["growth_warn_sent"] = True

            deadline = data.get("current_deadline")
            wait_sec = min(remain_sec, max((deadline - now).total_seconds(), 0), 5)
            await asyncio.sleep(max(wait_sec, 1))
            if user_id in plant_data and slot in plant_data[user_id]:
                if plant_data[user_id][slot].get("timer_version", 0) != version:
                    continue

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[harvest_timer] 오류 (user={user_id}, slot={slot}): {e}")
    finally:
        plant_tasks.get(user_id, {}).pop(slot, None)
        if user_id in plant_tasks and not plant_tasks[user_id]:
            plant_tasks.pop(user_id, None)


# ─────────────────────────────────────────
# /물주기
# ─────────────────────────────────────────
@bot.tree.command(name="물주기", description="물주기 타이머를 시작합니다 (서버 누구든 ✅ 클릭 시점 기준, 무한반복)")
async def cmd_water(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        print("[cmd_water] interaction expired before defer")
        return

    season = get_season(interaction.guild_id)
    if not season:
        await interaction.followup.send(embed=discord.Embed(
            title="❌ 계절 미설정",
            description="봇 소유자가 먼저 `!계절설정`으로 계절을 설정해야 합니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    user_id = interaction.user.id

    if user_id in water_data:
        await interaction.followup.send(embed=discord.Embed(
            title="⚠️ 이미 물주기 진행 중",
            description="`/물주기취소`로 먼저 중단한 뒤 다시 시작하세요.",
            color=0xFEE75C
        ), ephemeral=True)
        return

    water_min = SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES
    now = datetime.now(KST)
    sem = season_emoji(season)

    water_data[user_id] = {
        "guild_id": interaction.guild_id,
        "season": season,
        "water_min": water_min,
        "water_count": 0,
        "warn_sent": False,
        "awaiting_click": False,
        "next_water": now,
        "start": now,
        "timer_version": 0,
        "user_mention": interaction.user.mention,
        "channel_id": interaction.channel_id,
    }

    embed = discord.Embed(title="💧 물주기 시작!", color=0x5865F2)
    embed.add_field(name="계절",        value=f"{sem} {season}",             inline=True)
    embed.add_field(name="물주기 간격", value=f"⏱ `{water_min}분`마다",      inline=True)
    embed.add_field(name="반복",        value="♾️ `/물주기취소`까지 무한반복", inline=True)
    embed.add_field(name="타이머 기준", value="✅ 서버 누구든 클릭 시점부터 카운트", inline=True)
    embed.add_field(name="🌱 작물 성장", value="✅ 클릭 후 다음 물주기 시간까지 작물이 성장합니다. 다음 물주기를 놓치면 성장이 멈춥니다.", inline=False)
    embed.set_footer(text="지금 바로 ✅ 반응 클릭해서 첫 물주기!")

    save_state()
    water_tasks[user_id] = bot.loop.create_task(
        water_loop(user_id, interaction.channel, interaction.user.mention)
    )
    await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────
# 💧 물주기 루프
# ─────────────────────────────────────────
async def water_loop(user_id: int, channel: discord.TextChannel, mention: str):
    try:
        while True:
            if user_id not in water_data:
                break

            data          = water_data[user_id]
            water_min     = data["water_min"]
            current_count = data["water_count"]
            season        = data["season"]
            version       = data.get("timer_version", 0)
            next_water    = data.get("next_water", datetime.now(KST))
            warn_sent     = data.get("warn_sent", False)

            if current_count > 0 and next_water > datetime.now(KST) and warn_sent:
                wait_sec = max((next_water - datetime.now(KST)).total_seconds(), 0)
                slept = 0.0
                while slept < wait_sec:
                    await asyncio.sleep(min(5.0, wait_sec - slept))
                    slept += 5.0
                    if user_id not in water_data:
                        break
                    if water_data[user_id].get("timer_version", 0) != version:
                        break

                if user_id not in water_data:
                    break
                if water_data[user_id].get("timer_version", 0) != version:
                    continue

            if current_count > 0 and next_water > datetime.now(KST) and not warn_sent:
                wait_sec = max((next_water - datetime.now(KST)).total_seconds() - 60, 0)
                slept = 0.0
                while slept < wait_sec:
                    await asyncio.sleep(min(5.0, wait_sec - slept))
                    slept += 5.0
                    if user_id not in water_data:
                        break
                    if water_data[user_id].get("timer_version", 0) != version:
                        break

                if user_id not in water_data:
                    break
                data = water_data[user_id]
                if data.get("timer_version", 0) != version:
                    continue

                await safe_channel_send(channel, embed=discord.Embed(
                    title="🔔 물주기 1분 전!",
                    description=(
                        f"{mention} 물주기 **1분 전**! 준비하세요 💧\n"
                        f"(누적 `{current_count}회` 완료 | {season_emoji(data['season'])} {data['season']} 기준 `{data['water_min']}분`마다)"
                    ),
                    color=0xFEE75C
                ))
                data["warn_sent"] = True
                save_state()

                wait_sec = max((data["next_water"] - datetime.now(KST)).total_seconds(), 0)
                slept = 0.0
                while slept < wait_sec:
                    await asyncio.sleep(min(5.0, wait_sec - slept))
                    slept += 5.0
                    if user_id not in water_data:
                        break
                    if water_data[user_id].get("timer_version", 0) != version:
                        break

                if user_id not in water_data:
                    break
                if water_data[user_id].get("timer_version", 0) != version:
                    continue

            water_embed = discord.Embed(
                title="💧 물주기 시간입니다!",
                description=(
                    f"{mention} 지금 물을 주세요!\n"
                    "✅ 를 눌러 완료하세요.\n"
                    "*(서버 누구든 클릭 가능)*"
                ),
                color=0x5865F2
            )
            water_embed.add_field(name="누적 횟수",   value=f"💧 `{current_count}회` 완료", inline=True)
            water_embed.add_field(name="물주기 간격", value=f"⏱ `{water_min}분`마다",       inline=True)
            water_embed.add_field(name="현재 계절",   value=f"{season_emoji(season)} {season}", inline=True)

            data["awaiting_click"] = True
            save_state()
            water_msg = await safe_channel_send(channel, embed=water_embed)
            await water_msg.add_reaction("✅")

            def check(reaction, user):
                return (
                    str(reaction.emoji) == "✅"
                    and reaction.message.id == water_msg.id
                    and not user.bot
                )

            try:
                reaction, reactor = await bot.wait_for(
                    "reaction_add",
                    timeout=water_min * 60 * 10,
                    check=check
                )
            except asyncio.TimeoutError:
                if user_id in water_data:
                    water_data[user_id]["awaiting_click"] = True
                    save_state()
                    await safe_channel_send(channel, embed=discord.Embed(
                        title="⚠️ 물주기 미완료",
                        description=(
                            f"{mention} 물주기를 놓쳤어요!\n"
                            "✅ 를 눌러 계속하거나 `/물주기취소`로 중단하세요."
                        ),
                        color=0xED4245
                    ))
                continue

            if user_id not in water_data:
                break

            data          = water_data[user_id]
            water_min     = data["water_min"]
            season        = data["season"]
            version       = data.get("timer_version", 0)

            data["water_count"] += 1
            current_count    = data["water_count"]
            click_time       = datetime.now(KST)
            next_water_time  = click_time + timedelta(minutes=water_min)
            data["next_water"] = next_water_time
            data["warn_sent"] = False
            data["awaiting_click"] = False

            # ── 심기 슬롯 성장 시작/연장 반영 ──────────────────────
            # 다음 물주기 시간까지 작물 성장을 진행시킨다.
            if user_id in plant_data:
                for slot, pdata in list(plant_data[user_id].items()):
                    pause_growth_if_needed(pdata, click_time)
                    commit_growth_progress(pdata, click_time)
                    pdata["last_water_click"] = click_time   # ← 타이머 트리거
                    pdata["growth_started_at"] = click_time
                    pdata["current_deadline"] = next_water_time
                    pdata["growth_warn_sent"] = False
                    pdata["timer_version"]    = pdata.get("timer_version", 0) + 1

            confirm = discord.Embed(
                title="✅ 물주기 완료!",
                description=f"**{reactor.display_name}** 님이 물주기를 완료했습니다! (누적 `{current_count}회`)",
                color=0x57F287
            )
            confirm.add_field(
                name="다음 물주기",
                value=(
                    f"🕐 `{fmt_time(next_water_time)}`  "
                    f"(1분 전 알림: `{fmt_time(next_water_time - timedelta(minutes=1))}`)"
                ),
                inline=False
            )
            confirm.add_field(name="물주기 간격", value=f"⏱ `{water_min}분` ({season_emoji(season)} {season} 기준)", inline=True)
            await safe_channel_send(channel, embed=confirm)
            save_state()

            wait_sec = max(water_min - 1, 0) * 60
            slept = 0.0
            while slept < wait_sec:
                await asyncio.sleep(min(5.0, wait_sec - slept))
                slept += 5.0
                if user_id not in water_data:
                    break
                data = water_data[user_id]
                if data.get("timer_version", 0) != version:
                    break

            if user_id not in water_data:
                break

            data      = water_data[user_id]
            water_min = data["water_min"]
            season    = data["season"]
            if data.get("timer_version", 0) != version:
                continue

            await safe_channel_send(channel, embed=discord.Embed(
                title="🔔 물주기 1분 전!",
                description=(
                    f"{mention} 물주기 **1분 전**! 준비하세요 💧\n"
                    f"(누적 `{current_count}회` 완료 | {season_emoji(season)} {season} 기준 `{water_min}분`마다)"
                ),
                color=0xFEE75C
            ))

            slept = 0.0
            while slept < 60:
                await asyncio.sleep(min(5.0, 60 - slept))
                slept += 5.0
                if user_id not in water_data:
                    break
                if water_data[user_id].get("timer_version", 0) != version:
                    break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[water_loop] 오류 (user={user_id}): {e}")
    finally:
        water_tasks.pop(user_id, None)


# ─────────────────────────────────────────
# /상태
# ─────────────────────────────────────────
@bot.tree.command(name="상태", description="심기 슬롯과 물주기 슬롯 상태를 모두 확인합니다")
async def cmd_status(interaction: discord.Interaction):
    user_id = interaction.user.id
    p_slots = plant_data.get(user_id, {})

    has_pickle = user_id in pickle_data
    has_brew   = user_id in brew_data
    has_trade  = user_id in trade_data

    if not p_slots and user_id not in water_data and not has_pickle and not has_brew and not has_trade:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 항목 없음",
            description="`/심기`, `/물주기`, `/절임통`, `/양조통`, `/무역대기`로 시작하세요! 🌱",
            color=0xED4245
        ), ephemeral=True)
        return

    now    = datetime.now(KST)
    embeds = []

    # ── 심기 요약 ──
    if p_slots:
        summary = discord.Embed(
            title=f"🌱 심기 현황 ({len(p_slots)}/{MAX_SLOTS} 슬롯)",
            color=0x57F287
        )
        for slot in sorted(p_slots.keys()):
            d          = p_slots[slot]
            pause_growth_if_needed(d, now)
            progress_sec = current_growth_progress(d, now)
            required_sec = required_growth_seconds(d)
            growth_remain = max(round((required_sec - progress_sec) / 60, 1), 0)
            progress_min = round(progress_sec / 60, 1)
            required_min = round(required_sec / 60, 1)

            if growth_remain <= 0:
                timer_str = "🌾 수확 가능"
            elif d.get("growth_started_at") is not None and d.get("current_deadline") is not None:
                deadline = d["current_deadline"]
                active_remain = max(round((deadline - now).total_seconds() / 60, 1), 0)
                timer_str = f"⏳ 성장 중 (`{growth_remain}분` 남음, 이번 물주기 `{active_remain}분` 남음)"
            else:
                timer_str = f"⏸️ 성장 정지 중 (다음 물주기 필요)"

            summary.add_field(
                name=f"{slot_emoji(slot)} 슬롯{slot} — {d['crop']}",
                value=(
                    f"{season_emoji(d['season'])} `{d['season']}` · "
                    f"⏱ 성장 `{progress_min}/{required_min}분` · "
                    f"{timer_str}"
                ),
                inline=False
            )
        embeds.append(summary)

    # ── 물주기 요약 ──
    if user_id in water_data:
        d          = water_data[user_id]
        next_w     = d.get("next_water") or now
        remain_min = timer_remaining_minutes(next_w, now)
        w_summary  = discord.Embed(title="💧 물주기 현황", color=0x5865F2)
        w_summary.add_field(name="누적 횟수",   value=f"💧 `{d['water_count']}회`",                  inline=True)
        w_summary.add_field(name="현재 계절",   value=f"{season_emoji(d['season'])} {d['season']}", inline=True)
        w_summary.add_field(name="물주기 간격", value=f"⏱ `{d['water_min']}분`",                    inline=True)
        w_summary.add_field(
            name="다음 물주기",
            value=f"`{fmt_time(next_w)}` (약 `{remain_min}분` 후)",
            inline=False
        )
        embeds.append(w_summary)

    # ── 절임통 / 양조통 / 무역 요약 ──
    if has_pickle or has_brew:
        proc_embed = discord.Embed(title="🫙 가공 현황", color=0xA8D5A2)
        if has_pickle:
            finish = pickle_data[user_id].get("finish_time") or now
            remain = timer_remaining_minutes(finish, now)
            proc_embed.add_field(name="🫙 절임통", value=f"`{fmt_time(finish)}` 완료 예정\n약 `{remain}분` 후", inline=True)
        if has_brew:
            finish = brew_data[user_id].get("finish_time") or now
            remain = timer_remaining_minutes(finish, now)
            proc_embed.add_field(name="🍺 양조통", value=f"`{fmt_time(finish)}` 완료 예정\n약 `{remain}분` 후", inline=True)
        embeds.append(proc_embed)

    if has_trade:
        d = trade_data[user_id]
        finish = d.get("finish_time") or now
        remain = timer_remaining_minutes(finish, now)
        trade_summary = discord.Embed(title="🚢 무역 현황", color=0x5865F2)
        trade_summary.add_field(name="알림 종류", value=d.get("done_title") or "무역 알림", inline=True)
        trade_summary.add_field(name="예정 시각", value=f"`{fmt_time(finish)}`", inline=True)
        trade_summary.add_field(name="남은 시간", value=f"약 `{remain}분`", inline=True)
        embeds.append(trade_summary)

    await interaction.response.send_message(embeds=embeds)


# ─────────────────────────────────────────
# /심기취소
# ─────────────────────────────────────────
@bot.tree.command(name="심기취소", description="심기 슬롯을 취소합니다")
@app_commands.describe(슬롯="취소할 슬롯 번호 (생략하면 전체 취소)")
@app_commands.choices(슬롯=[
    app_commands.Choice(name="1번 슬롯", value=1),
    app_commands.Choice(name="2번 슬롯", value=2),
    app_commands.Choice(name="3번 슬롯", value=3),
    app_commands.Choice(name="4번 슬롯", value=4),
    app_commands.Choice(name="5번 슬롯", value=5),
])
async def cmd_cancel_plant(interaction: discord.Interaction, 슬롯: int = 0):
    user_id = interaction.user.id
    slots   = plant_data.get(user_id, {})

    if not slots:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 취소할 심기 없음",
            description="진행 중인 심기 슬롯이 없습니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    if 슬롯 != 0:
        if 슬롯 not in slots:
            current = "\n".join(
                f"{slot_emoji(s)} 슬롯{s}: `{slots[s]['crop']}`"
                for s in sorted(slots.keys())
            )
            await interaction.response.send_message(embed=discord.Embed(
                title=f"❌ 심기 슬롯{슬롯} 없음",
                description=f"슬롯{슬롯}은 비어 있습니다.\n\n현재 사용 중:\n{current}",
                color=0xED4245
            ), ephemeral=True)
            return
        crop = slots[슬롯]["crop"]
        cancel_plant(user_id, 슬롯)
        await interaction.response.send_message(embed=discord.Embed(
            title=f"⛔ 🌱 심기 슬롯{슬롯} 취소",
            description=f"**{crop}** 심기 알람이 취소되었습니다.",
            color=0xED4245
        ))
    else:
        cancelled = [(s, slots[s]["crop"]) for s in sorted(slots.keys())]
        for s in list(slots.keys()):
            cancel_plant(user_id, s)
        lines = "\n".join(f"{slot_emoji(s)} 슬롯{s}: **{c}**" for s, c in cancelled)
        await interaction.response.send_message(embed=discord.Embed(
            title="⛔ 🌱 심기 전체 취소",
            description=f"모든 심기 알람이 취소되었습니다.\n\n{lines}",
            color=0xED4245
        ))


# ─────────────────────────────────────────
# /물주기취소
# ─────────────────────────────────────────
@bot.tree.command(name="물주기취소", description="진행 중인 물주기를 중단합니다")
async def cmd_cancel_water(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in water_data:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 물주기 없음",
            description="현재 물주기 타이머가 없습니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    count = water_data[user_id]["water_count"]
    cancel_water(user_id)
    await interaction.response.send_message(embed=discord.Embed(
        title="⛔ 💧 물주기 중단",
        description=f"물주기가 중단되었습니다.\n총 `{count}회` 완료했습니다.",
        color=0xED4245
    ))


# ─────────────────────────────────────────
# 🫙 /절임통
# ─────────────────────────────────────────
PROCESS_TIMER_CONFIG = {
    "pickle": {
        "emoji": "\U0001fad9",
        "name": "\uc808\uc784\ud1b5",
        "warn": "\uc808\uc784\ud1b5\uc774 1\ubd84 \ud6c4 \uc644\ub8cc\ub429\ub2c8\ub2e4!",
        "done": "\uc808\uc784\ud1b5\uc774 \uc644\ub8cc\ub418\uc5c8\uc2b5\ub2c8\ub2e4! \uaebc\ub0b4\uc8fc\uc138\uc694.",
    },
    "brew": {
        "emoji": "\U0001f37a",
        "name": "\uc591\uc870\ud1b5",
        "warn": "\uc591\uc870\ud1b5\uc774 1\ubd84 \ud6c4 \uc644\ub8cc\ub429\ub2c8\ub2e4!",
        "done": "\uc591\uc870\ud1b5\uc774 \uc644\ub8cc\ub418\uc5c8\uc2b5\ub2c8\ub2e4! \uaebc\ub0b4\uc8fc\uc138\uc694.",
    },
    "trade": {
        "emoji": "\U0001f6a2",
        "name": "\ubb34\uc5ed",
        "warn": "\ubb34\uc5ed \uc54c\ub9bc\uc774 1\ubd84 \ud6c4\uc785\ub2c8\ub2e4!",
        "done": "\ubb34\uc5ed \uc2dc\uac04\uc785\ub2c8\ub2e4!",
    },
}

def get_process_timer_maps(kind: str) -> tuple[dict[int, dict], dict[int, asyncio.Task]]:
    if kind == "pickle":
        return pickle_data, pickle_tasks
    if kind == "brew":
        return brew_data, brew_tasks
    if kind == "trade":
        return trade_data, trade_tasks
    raise ValueError(f"unknown process timer kind: {kind}")

def start_process_timer(
    kind: str,
    user_id: int,
    channel: discord.TextChannel,
    channel_id: int | None,
    mention: str,
    start_time: datetime,
    finish_time: datetime,
    **extra_data,
):
    data_map, task_map = get_process_timer_maps(kind)
    data_map[user_id] = {
        "finish_time": finish_time,
        "start": start_time,
        "user_mention": mention,
        "channel_id": channel_id,
        "warn_sent": False,
        **extra_data,
    }
    save_state()
    task_map[user_id] = bot.loop.create_task(process_timer_loop(kind, user_id, channel))

async def process_timer_loop(kind: str, user_id: int, channel: discord.TextChannel):
    config = PROCESS_TIMER_CONFIG[kind]
    data_map, task_map = get_process_timer_maps(kind)
    try:
        data = data_map.get(user_id)
        if not data:
            return

        mention = data.get("user_mention") or f"<@{user_id}>"
        finish_time = data.get("finish_time")
        if finish_time is None:
            data_map.pop(user_id, None)
            save_state()
            return

        warn_time = finish_time - timedelta(minutes=1)
        now = datetime.now(KST)
        if not data.get("warn_sent") and warn_time > now:
            await asyncio.sleep((warn_time - now).total_seconds())

        if user_id not in data_map:
            return

        data = data_map[user_id]
        now = datetime.now(KST)
        if not data.get("warn_sent") and finish_time > now:
            await safe_channel_send(channel, embed=discord.Embed(
                title=f"\U0001f514 {config['name']} 1\ubd84 \uc804",
                description=f"{mention} {data.get('warn_text') or config['warn']}",
                color=0xFEE75C
            ))
            data["warn_sent"] = True
            save_state()
            await asyncio.sleep(max((finish_time - datetime.now(KST)).total_seconds(), 0))

        if user_id not in data_map:
            return

        now_done = datetime.now(KST)
        done_title = data.get("done_title") or f"{config['name']} 완료"
        await safe_channel_send(channel, content=mention, embed=discord.Embed(
            title=f"{config['emoji']} {done_title}",
            description=f"{mention} {data.get('done_text') or config['done']}\n\u23f0 `{fmt_time(now_done)}`",
            color=0x57F287
        ))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[process_timer_loop] error (kind={kind}, user={user_id}): {e}")
    finally:
        task_map.pop(user_id, None)
        if user_id in data_map:
            data_map.pop(user_id, None)
            save_state()

@bot.tree.command(name="절임통", description="절임통 타이머를 시작합니다 (인게임 3일 = 144분)")
async def cmd_pickle(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in pickle_tasks and not pickle_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="⚠️ 이미 절임통 진행 중",
            description="`/절임통취소`로 먼저 취소한 뒤 다시 시작하세요.",
            color=0xFEE75C
        ), ephemeral=True)
        return

    now         = datetime.now(KST)
    finish_time = now + timedelta(minutes=PICKLE_MINUTES)
    mention     = interaction.user.mention
    channel     = interaction.channel

    embed = discord.Embed(title="🫙 절임통 시작!", color=0xA8D5A2)
    embed.add_field(name="시작 시각", value=f"🕐 `{fmt_time(now)}`",         inline=True)
    embed.add_field(name="완료 예정", value=f"🫙 `{fmt_time(finish_time)}`", inline=True)
    embed.add_field(name="소요 시간", value=f"⏱ `{PICKLE_MINUTES}분` (인게임 3일)", inline=True)
    embed.set_footer(text="완료 1분 전에도 미리 알려드립니다!")
    await interaction.response.send_message(content=mention, embed=embed)
    start_process_timer("pickle", user_id, channel, interaction.channel_id, mention, now, finish_time)


# ─────────────────────────────────────────
# /절임통취소
# ─────────────────────────────────────────
@bot.tree.command(name="절임통취소", description="진행 중인 절임통 타이머를 취소합니다")
async def cmd_cancel_pickle(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in pickle_tasks or pickle_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 절임통 없음",
            description="현재 진행 중인 절임통 타이머가 없습니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    cancel_pickle(user_id)
    await interaction.response.send_message(embed=discord.Embed(
        title="⛔ 🫙 절임통 취소",
        description="절임통 타이머가 취소되었습니다.",
        color=0xED4245
    ))


# ─────────────────────────────────────────
# 🍺 /양조통
# ─────────────────────────────────────────
@bot.tree.command(name="양조통", description="양조통 타이머를 시작합니다 (인게임 5일 = 240분)")
async def cmd_brew(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in brew_tasks and not brew_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="⚠️ 이미 양조통 진행 중",
            description="`/양조통취소`로 먼저 취소한 뒤 다시 시작하세요.",
            color=0xFEE75C
        ), ephemeral=True)
        return

    now         = datetime.now(KST)
    finish_time = now + timedelta(minutes=BREW_MINUTES)
    mention     = interaction.user.mention
    channel     = interaction.channel

    embed = discord.Embed(title="🍺 양조통 시작!", color=0xF4A460)
    embed.add_field(name="시작 시각", value=f"🕐 `{fmt_time(now)}`",         inline=True)
    embed.add_field(name="완료 예정", value=f"🍺 `{fmt_time(finish_time)}`", inline=True)
    embed.add_field(name="소요 시간", value=f"⏱ `{BREW_MINUTES}분` (인게임 5일)", inline=True)
    embed.set_footer(text="완료 1분 전에도 미리 알려드립니다!")
    await interaction.response.send_message(content=mention, embed=embed)
    start_process_timer("brew", user_id, channel, interaction.channel_id, mention, now, finish_time)


# ─────────────────────────────────────────
# /양조통취소
# ─────────────────────────────────────────
@bot.tree.command(name="양조통취소", description="진행 중인 양조통 타이머를 취소합니다")
async def cmd_cancel_brew(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in brew_tasks or brew_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 양조통 없음",
            description="현재 진행 중인 양조통 타이머가 없습니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    cancel_brew(user_id)
    await interaction.response.send_message(embed=discord.Embed(
        title="⛔ 🍺 양조통 취소",
        description="양조통 타이머가 취소되었습니다.",
        color=0xED4245
    ))


# ─────────────────────────────────────────
# 🚢 /무역대기
# ─────────────────────────────────────────
@bot.tree.command(name="무역대기", description="1시간 뒤 무역 물품 넣기 알림을 받습니다")
async def cmd_trade_wait(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in trade_tasks and not trade_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="⚠️ 이미 무역 타이머 진행 중",
            description="`/무역타이머취소`로 먼저 취소한 뒤 다시 시작하세요.",
            color=0xFEE75C
        ), ephemeral=True)
        return

    now         = datetime.now(KST)
    finish_time = now + timedelta(hours=1)
    mention     = interaction.user.mention
    channel     = interaction.channel

    embed = discord.Embed(title="🚢 무역 대기 시작!", color=0x5865F2)
    embed.add_field(name="시작 시각", value=f"🕐 `{fmt_time(now)}`",         inline=True)
    embed.add_field(name="완료 예정", value=f"🚢 `{fmt_time(finish_time)}`", inline=True)
    embed.add_field(name="소요 시간", value="⏱ `60분`",                      inline=True)
    embed.set_footer(text="물품 넣기 1분 전에도 미리 알려드립니다!")
    await interaction.response.send_message(content=mention, embed=embed)
    start_process_timer(
        "trade",
        user_id,
        channel,
        interaction.channel_id,
        mention,
        now,
        finish_time,
        warn_text="\ubb34\uc5ed \ubb3c\ud488 \ub123\uae30\uac00 1\ubd84 \ud6c4\uc785\ub2c8\ub2e4!",
        done_title="\ubb34\uc5ed \ubb3c\ud488 \ub123\uae30",
        done_text="\ubb34\uc5ed \ubb3c\ud488\uc744 \ub123\uc5b4\uc8fc\uc138\uc694!",
    )


# ─────────────────────────────────────────
# 🚢 /무역종료
# ─────────────────────────────────────────
@bot.tree.command(name="무역종료", description="지정한 시간 뒤 무역 완료 알림을 받습니다")
@app_commands.describe(시간="예: 7시35분 / 10분 / 2시")
async def cmd_trade_end(interaction: discord.Interaction, 시간: str):
    user_id = interaction.user.id

    try:
        h = 0
        m = 0
        if "시" in 시간:
            parts = 시간.split("시")
            h = int(parts[0])
            if "분" in parts[1]:
                m = int(parts[1].replace("분", "") or 0)
        elif "분" in 시간:
            m = int(시간.replace("분", ""))
        else:
            raise ValueError
        total_min = h * 60 + m
        if total_min <= 0:
            raise ValueError
    except:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 잘못된 시간",
            description="`7시35분`, `10분`, `2시` 형식으로 입력해주세요.",
            color=0xED4245
        ), ephemeral=True)
        return

    if user_id in trade_tasks and not trade_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="⚠️ 이미 무역 타이머 진행 중",
            description="`/무역타이머취소`로 먼저 취소한 뒤 다시 시작하세요.",
            color=0xFEE75C
        ), ephemeral=True)
        return

    now         = datetime.now(KST)
    finish_time = now + timedelta(minutes=total_min)
    mention     = interaction.user.mention
    channel     = interaction.channel

    embed = discord.Embed(title="🚢 무역 종료 타이머 시작!", color=0x5865F2)
    embed.add_field(name="시작 시각", value=f"🕐 `{fmt_time(now)}`",         inline=True)
    embed.add_field(name="완료 예정", value=f"🚢 `{fmt_time(finish_time)}`", inline=True)
    embed.add_field(name="소요 시간", value=f"⏱ `{total_min}분`",            inline=True)
    if total_min > 1:
        embed.set_footer(text="완료 1분 전에도 미리 알려드립니다!")
    await interaction.response.send_message(content=mention, embed=embed)

    start_process_timer(
        "trade",
        user_id,
        channel,
        interaction.channel_id,
        mention,
        now,
        finish_time,
        warn_text="\ubb34\uc5ed\uc774 1\ubd84 \ud6c4 \uc644\ub8cc\ub429\ub2c8\ub2e4! \uc900\ube44\ud558\uc138\uc694.",
        done_title="\ubb34\uc5ed \uc644\ub8cc",
        done_text=f"**{total_min}\ubd84** \ubb34\uc5ed\uc774 \uc644\ub8cc\ub418\uc5c8\uc2b5\ub2c8\ub2e4!",
    )


# ─────────────────────────────────────────
# 🏳️ /무역포기
# ─────────────────────────────────────────
@bot.tree.command(name="무역포기", description="무역을 포기하고 3시간 뒤 다시 무역 확인 알림을 받습니다")
async def cmd_trade_give_up(interaction: discord.Interaction):
    user_id = interaction.user.id

    was_running = user_id in trade_tasks and not trade_tasks[user_id].done()
    cancel_trade(user_id)

    now         = datetime.now(KST)
    remind_time = now + timedelta(hours=3)
    mention     = interaction.user.mention
    channel     = interaction.channel

    desc = "진행 중인 무역 타이머를 취소했습니다.\n" if was_running else ""
    desc += "**3시간** 뒤 무역을 다시 확인하라고 알려드립니다."

    embed = discord.Embed(title="🏳️ 무역 포기", description=desc, color=0xED4245)
    embed.add_field(name="포기 시각",   value=f"🕐 `{fmt_time(now)}`",         inline=True)
    embed.add_field(name="재확인 예정", value=f"🔔 `{fmt_time(remind_time)}`", inline=True)
    embed.add_field(name="대기 시간",   value="⏱ `3시간`",                     inline=True)
    embed.set_footer(text="재확인 1분 전에도 미리 알려드립니다!")
    await interaction.response.send_message(content=mention, embed=embed)

    start_process_timer(
        "trade",
        user_id,
        channel,
        interaction.channel_id,
        mention,
        now,
        remind_time,
        warn_text="\ubb34\uc5ed \uc7ac\ud655\uc778\uae4c\uc9c0 1\ubd84 \ub0a8\uc558\uc2b5\ub2c8\ub2e4!",
        done_title="\ubb34\uc5ed \ud655\uc778 \uc2dc\uac04",
        done_text="\ubb34\uc5ed\uc744 \ub2e4\uc2dc \ud655\uc778\ud574\ubcf4\uc138\uc694!",
    )


# ─────────────────────────────────────────
# ⛔ /무역타이머취소
# ─────────────────────────────────────────
@bot.tree.command(name="무역타이머취소", description="진행 중인 무역 타이머를 취소합니다")
async def cmd_cancel_trade(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in trade_tasks or trade_tasks[user_id].done():
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 무역 없음",
            description="현재 진행 중인 무역 타이머가 없습니다.",
            color=0xED4245
        ), ephemeral=True)
        return

    cancel_trade(user_id)
    await interaction.response.send_message(embed=discord.Embed(
        title="⛔ 🚢 무역 타이머 취소",
        description="진행 중인 무역 타이머가 취소되었습니다.",
        color=0xED4245
    ))


# ─────────────────────────────────────────
# 에러 핸들러
# ─────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = getattr(getattr(interaction, "command", None), "name", "unknown")
    user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
    guild_id = getattr(interaction, "guild_id", "unknown")
    channel_id = getattr(interaction, "channel_id", "unknown")

    print(
        f"[app_command_error] command=/{command_name} user={user_id} guild={guild_id} "
        f"channel={channel_id} error={error.__class__.__name__}: {error}"
    )

    if isinstance(error, app_commands.MissingPermissions):
        desc, color = "이 명령어는 **서버 관리자**만 사용할 수 있습니다.", 0xED4245
    elif isinstance(error, app_commands.CommandOnCooldown):
        desc, color = f"`{round(error.retry_after, 1)}초` 후 다시 시도하세요.", 0xFEE75C
    elif isinstance(error, app_commands.CommandInvokeError):
        print("[app_command_error] original traceback follows:")
        traceback.print_exception(type(error.original), error.original, error.original.__traceback__)
        desc, color = f"명령어 실행 중 오류\n```{str(error.original)}```", 0xED4245
    else:
        print("[app_command_error] traceback follows:")
        traceback.print_exception(type(error), error, error.__traceback__)
        desc, color = f"예기치 않은 오류\n```{str(error)}```", 0xED4245

    embed = discord.Embed(title="⚠️ 오류", description=desc, color=color)
    await safe_interaction_error(interaction, embed)


# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────
bot.run(TOKEN)
