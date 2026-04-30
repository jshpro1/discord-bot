import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
import math
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

load_dotenv()
TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

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

def get_season(guild_id: int) -> str | None:
    return current_season.get(guild_id)

def next_season(season: str) -> str:
    return SEASON_ORDER[(SEASON_ORDER.index(season) + 1) % 4]

# ─────────────────────────────────────────
# 슬롯 데이터
# ─────────────────────────────────────────
plant_data:  dict[int, dict[int, dict]]         = {}
plant_tasks: dict[int, dict[int, asyncio.Task]] = {}
harvest_events: dict[int, dict[int, asyncio.Event]] = {}

water_data:  dict[int, dict]         = {}
water_tasks: dict[int, asyncio.Task] = {}

# 무역
trade_tasks: dict[int, asyncio.Task] = {}

# 절임통 / 양조통
pickle_tasks: dict[int, asyncio.Task] = {}
brew_tasks:   dict[int, asyncio.Task] = {}


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def calc_growth(crop: str, season: str) -> tuple[float, int, bool, int]:
    days         = CROPS[crop]
    base_min     = days * REAL_MINUTES_PER_SERVER_DAY
    season_mult  = {"봄": 0.8, "겨울": 1.5}.get(season, 1.0)
    in_season    = crop in SEASON_CROPS.get(season, set())
    bonus        = 0.5 if in_season else 1.0
    final_min    = round(base_min * season_mult * bonus, 1)
    water_min    = SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES
    total_waters = math.ceil(final_min / water_min)
    return final_min, water_min, in_season, total_waters

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")

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
    harvest_events.get(user_id, {}).pop(slot, None)
    if user_id in harvest_events and not harvest_events[user_id]:
        harvest_events.pop(user_id, None)

def cancel_water(user_id: int):
    task = water_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    water_data.pop(user_id, None)

def cancel_trade(user_id: int):
    task = trade_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

def cancel_pickle(user_id: int):
    task = pickle_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

def cancel_brew(user_id: int):
    task = brew_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

def get_water_minutes(guild_id: int) -> int:
    season = get_season(guild_id)
    return SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES


# ─────────────────────────────────────────
# on_ready
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    bot.loop.create_task(season_tick())
    synced = await bot.tree.sync()
    print(f"✅ {bot.user} 로그인 완료")
    print(f"🌐 전역 동기화 — {len(synced)}개 커맨드")
    for cmd in synced:
        print(f"   └─ /{cmd.name}")


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

        for guild_id in list(current_season.keys()):
            prev = current_season[guild_id]
            curr = next_season(prev)
            current_season[guild_id] = curr
            print(f"[season_tick] guild={guild_id} 계절 변경: {prev} → {curr}")

            new_water_min = SUMMER_WATER_MINUTES if curr == "여름" else BASE_WATER_MINUTES
            for uid, wdata in list(water_data.items()):
                if wdata.get("guild_id") == guild_id:
                    old_min = wdata["water_min"]
                    if old_min != new_water_min:
                        last_click = wdata["next_water"] - timedelta(minutes=old_min)
                        new_next   = last_click + timedelta(minutes=new_water_min)
                        wdata["water_min"]  = new_water_min
                        wdata["season"]     = curr
                        wdata["next_water"] = new_next


# ─────────────────────────────────────────
# /현재계절
# ─────────────────────────────────────────
@bot.tree.command(name="현재계절", description="현재 서버 계절을 확인합니다")
async def cmd_current_season(interaction: discord.Interaction):
    season = get_season(interaction.guild_id)
    if not season:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 계절 미설정",
            description="관리자가 `/계절설정`으로 먼저 계절을 설정해주세요!",
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
# /계절설정 (관리자)
# ─────────────────────────────────────────
@bot.tree.command(name="계절설정", description="[관리자 전용] 현재 계절을 설정합니다")
@app_commands.describe(계절="설정할 계절")
@app_commands.choices(계절=[
    app_commands.Choice(name="🌸 봄",   value="봄"),
    app_commands.Choice(name="☀️ 여름", value="여름"),
    app_commands.Choice(name="🍂 가을", value="가을"),
    app_commands.Choice(name="❄️ 겨울", value="겨울"),
])
@app_commands.checks.has_permissions(administrator=True)
async def cmd_set_season(interaction: discord.Interaction, 계절: str):
    prev_season = current_season.get(interaction.guild_id)
    current_season[interaction.guild_id] = 계절

    new_water_min = SUMMER_WATER_MINUTES if 계절 == "여름" else BASE_WATER_MINUTES
    for uid, wdata in list(water_data.items()):
        if wdata.get("guild_id") == interaction.guild_id:
            old_min = wdata["water_min"]
            if old_min != new_water_min:
                last_click = wdata["next_water"] - timedelta(minutes=old_min)
                new_next   = last_click + timedelta(minutes=new_water_min)
                wdata["water_min"]  = new_water_min
                wdata["season"]     = 계절
                wdata["next_water"] = new_next
            else:
                wdata["season"] = 계절

    nxt = next_season(계절)
    embed = discord.Embed(
        title="✅ 계절 설정 완료",
        color={"봄": 0xFFB7C5, "여름": 0xFFD700, "가을": 0xFF8C00, "겨울": 0x87CEEB}.get(계절, 0x57F287)
    )
    embed.add_field(name="현재 계절", value=f"{season_emoji(계절)} **{계절}**", inline=True)
    embed.add_field(name="다음 계절", value=f"{season_emoji(nxt)} **{nxt}**",   inline=True)
    crops_str = "  ".join(f"`{c}`" for c in sorted(SEASON_CROPS.get(계절, set())))
    embed.add_field(name=f"{season_emoji(계절)} 제철 작물", value=crops_str, inline=False)
    if prev_season and prev_season != 계절:
        embed.add_field(
            name="💧 물주기 간격 변경",
            value=f"진행 중인 물주기 간격이 **{new_water_min}분**으로 자동 변경되었습니다.",
            inline=False
        )
    embed.set_footer(text="매일 자정(KST)에 자동으로 다음 계절로 넘어갑니다")
    await interaction.response.send_message(embed=embed)


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
    embed.add_field(name="⚙️ /계절설정 [계절]",    value="[관리자] 계절 설정 (매일 자정 자동 변경, 물주기 간격 즉시 적용)", inline=False)
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
    embed.set_footer(text="서버 1일 = 현실 48분 | 자정(KST) 기준 계절 자동 변경")
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
        "봄":  "🌸 봄 작물 — 성장속도 ×0.8 (빠름!)",
        "여름": "☀️ 여름 작물 — 물주기 간격 24분 (절반!)",
        "가을": "🍂 가을 작물 — 2.5% 확률 수확량 2배!",
        "겨울": "❄️ 겨울 작물 — 성장속도 ×1.5 (느림)",
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
                g_min, w_min, in_s, w_count = calc_growth(c, season)
                bonus = "✅제철" if in_s else "➖비제철"
                lines.append(f"`{c}` {bonus} · ⏱{g_min}분 · 💧{w_count}회")
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
            "🌾 물주기 횟수를 다 채우면 수확 알림\n"
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
            description="관리자가 `/계절설정`으로 먼저 계절을 설정해야 합니다!",
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

    growth_min, water_min, in_season, total_waters = calc_growth(작물, season)
    now         = datetime.now(KST)
    finish_time = now + timedelta(minutes=growth_min)
    sem         = season_emoji(season)
    crop_season = CROP_SEASON.get(작물, "알 수 없음")
    crop_sem    = season_emoji(crop_season)

    plant_data.setdefault(user_id, {})[slot] = {
        "crop": 작물, "season": season, "growth_min": growth_min,
        "water_min": water_min, "in_season": in_season,
        "start": now, "end": finish_time,
        "total_waters": total_waters,
        "water_done": 0,
        "user_mention": interaction.user.mention,
        "channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
    }

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
    embed.add_field(name="필요 물주기",    value=f"💧 `{total_waters}회` (물주기는 `/물주기` 사용)", inline=False)
    embed.add_field(name="내 심기 현황",   value=slot_status,                     inline=False)
    if season == "가을":
        embed.add_field(name="특이사항", value="🍂 수확 시 2.5% 확률로 수확량 2배!", inline=False)
    embed.set_footer(text=f"심기 슬롯 {slot}/{MAX_SLOTS} | 물주기를 다 채워야 수확 알림이 옵니다!")

    await interaction.response.send_message(content=interaction.user.mention, embed=embed)
    plant_tasks.setdefault(user_id, {})[slot] = bot.loop.create_task(
        harvest_timer(user_id, slot, interaction.channel, interaction.user.mention)
    )


# ─────────────────────────────────────────
# 🌾 수확 타이머 — 물주기 횟수 기반
# ─────────────────────────────────────────
async def harvest_timer(user_id: int, slot: int, channel: discord.TextChannel, mention: str):
    try:
        if user_id not in plant_data or slot not in plant_data[user_id]:
            return

        data    = plant_data[user_id][slot]
        crop    = data["crop"]
        season  = data["season"]
        total_w = data["total_waters"]

        # 수확 완료 이벤트 등록
        event = asyncio.Event()
        harvest_events.setdefault(user_id, {})[slot] = event

        # 물주기 횟수가 다 찰 때까지 대기
        await event.wait()

        if user_id not in plant_data or slot not in plant_data[user_id]:
            return

        now = datetime.now(KST)
        harvest_embed = discord.Embed(
            title=f"🌾 {slot_emoji(slot)} 슬롯{slot} — {crop} 수확 완료!",
            description=f"{mention} **{crop}** 수확하세요!\n⏰ `{fmt_time(now)}`",
            color=0xFFD700
        )
        harvest_embed.add_field(name="총 물주기 횟수", value=f"💧 `{total_w}회`", inline=True)
        harvest_embed.add_field(name="계절",           value=f"{season_emoji(season)} {season}", inline=True)
        if season == "가을":
            harvest_embed.add_field(name="🍂 가을 보너스", value="2.5% 확률로 수확량 2배!", inline=False)

        cancel_plant(user_id, slot)
        await channel.send(content=mention, embed=harvest_embed)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[harvest_timer] 오류 (user={user_id}, slot={slot}): {e}")
    finally:
        harvest_events.get(user_id, {}).pop(slot, None)
        if user_id in harvest_events and not harvest_events[user_id]:
            harvest_events.pop(user_id, None)
        plant_tasks.get(user_id, {}).pop(slot, None)
        if user_id in plant_tasks and not plant_tasks[user_id]:
            plant_tasks.pop(user_id, None)


# ─────────────────────────────────────────
# /물주기
# ─────────────────────────────────────────
@bot.tree.command(name="물주기", description="물주기 타이머를 시작합니다 (서버 누구든 ✅ 클릭 시점 기준, 무한반복)")
async def cmd_water(interaction: discord.Interaction):
    season = get_season(interaction.guild_id)
    if not season:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 계절 미설정",
            description="관리자가 `/계절설정`으로 먼저 계절을 설정해야 합니다!",
            color=0xED4245
        ), ephemeral=True)
        return

    user_id = interaction.user.id

    if user_id in water_data:
        await interaction.response.send_message(embed=discord.Embed(
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
        "next_water": now,
        "start": now,
        "user_mention": interaction.user.mention,
        "channel_id": interaction.channel_id,
    }

    embed = discord.Embed(title="💧 물주기 시작!", color=0x5865F2)
    embed.add_field(name="계절",        value=f"{sem} {season}",             inline=True)
    embed.add_field(name="물주기 간격", value=f"⏱ `{water_min}분`마다",      inline=True)
    embed.add_field(name="반복",        value="♾️ `/물주기취소`까지 무한반복", inline=True)
    embed.add_field(name="타이머 기준", value="✅ 서버 누구든 클릭 시점부터 카운트", inline=True)
    embed.set_footer(text="지금 바로 ✅ 반응 클릭해서 첫 물주기!")

    await interaction.response.send_message(embed=embed)
    water_tasks[user_id] = bot.loop.create_task(
        water_loop(user_id, interaction.channel, interaction.user.mention)
    )


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

            water_msg = await channel.send(embed=water_embed)
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
                    await channel.send(embed=discord.Embed(
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

            data["water_count"] += 1
            current_count   = data["water_count"]
            click_time      = datetime.now(KST)
            next_water_time = click_time + timedelta(minutes=water_min)
            data["next_water"] = next_water_time

            # ── 심기 슬롯 물주기 진행 반영 ──────────────────────
            if user_id in plant_data:
                for slot, pdata in list(plant_data[user_id].items()):
                    pdata["water_done"] = pdata.get("water_done", 0) + 1
                    done  = pdata["water_done"]
                    total = pdata["total_waters"]
                    crop  = pdata["crop"]

                    if done == total - 1:
                        # 마지막 1회 남음 예고
                        await channel.send(embed=discord.Embed(
                            title=f"🔔 {slot_emoji(slot)} 슬롯{slot} — {crop} 물주기 1회 남음!",
                            description=f"{pdata['user_mention']} **{crop}** 다음 물주기 후 수확 가능합니다! 🌾",
                            color=0xFEE75C
                        ))
                    elif done >= total:
                        # 수확 이벤트 발동
                        event = harvest_events.get(user_id, {}).get(slot)
                        if event:
                            event.set()

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
            await channel.send(embed=confirm)

            await asyncio.sleep(max(water_min - 1, 0) * 60)

            if user_id not in water_data:
                break

            data      = water_data[user_id]
            water_min = data["water_min"]
            season    = data["season"]

            await channel.send(embed=discord.Embed(
                title="🔔 물주기 1분 전!",
                description=(
                    f"{mention} 물주기 **1분 전**! 준비하세요 💧\n"
                    f"(누적 `{current_count}회` 완료 | {season_emoji(season)} {season} 기준 `{water_min}분`마다)"
                ),
                color=0xFEE75C
            ))

            await asyncio.sleep(60)

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

    has_pickle = user_id in pickle_tasks and not pickle_tasks[user_id].done()
    has_brew   = user_id in brew_tasks   and not brew_tasks[user_id].done()

    if not p_slots and user_id not in water_data and not has_pickle and not has_brew:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 항목 없음",
            description="`/심기`, `/물주기`, `/절임통`, `/양조통`으로 시작하세요! 🌱",
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
            d             = p_slots[slot]
            done          = d.get("water_done", 0)
            total         = d["total_waters"]
            remaining_w   = total - done
            summary.add_field(
                name=f"{slot_emoji(slot)} 슬롯{slot} — {d['crop']}",
                value=(
                    f"{season_emoji(d['season'])} `{d['season']}` · "
                    f"💧 물주기 `{done}/{total}회` · "
                    f"남은 횟수 `{remaining_w}회`"
                ),
                inline=False
            )
        embeds.append(summary)

    # ── 물주기 요약 ──
    if user_id in water_data:
        d          = water_data[user_id]
        next_w     = d.get("next_water", now)
        remain_min = max(round((next_w - now).total_seconds() / 60, 1), 0)
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

    # ── 절임통 / 양조통 요약 ──
    if has_pickle or has_brew:
        proc_embed = discord.Embed(title="🫙 가공 현황", color=0xA8D5A2)
        if has_pickle:
            proc_embed.add_field(name="🫙 절임통", value="진행 중", inline=True)
        if has_brew:
            proc_embed.add_field(name="🍺 양조통", value="진행 중", inline=True)
        embeds.append(proc_embed)

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

    async def _timer():
        try:
            await asyncio.sleep((PICKLE_MINUTES - 1) * 60)
            await channel.send(embed=discord.Embed(
                title="🔔 절임통 완료 1분 전!",
                description=f"{mention} 절임통이 **1분 후** 완료됩니다! 준비하세요 🫙",
                color=0xFEE75C
            ))
            await asyncio.sleep(60)
            now_done = datetime.now(KST)
            await channel.send(content=mention, embed=discord.Embed(
                title="🫙 절임통 완료!",
                description=f"{mention} 절임통이 완료되었습니다! 꺼내주세요 🫙\n⏰ `{fmt_time(now_done)}`",
                color=0x57F287
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[절임통] 오류 (user={user_id}): {e}")
        finally:
            pickle_tasks.pop(user_id, None)

    pickle_tasks[user_id] = bot.loop.create_task(_timer())


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

    async def _timer():
        try:
            await asyncio.sleep((BREW_MINUTES - 1) * 60)
            await channel.send(embed=discord.Embed(
                title="🔔 양조통 완료 1분 전!",
                description=f"{mention} 양조통이 **1분 후** 완료됩니다! 준비하세요 🍺",
                color=0xFEE75C
            ))
            await asyncio.sleep(60)
            now_done = datetime.now(KST)
            await channel.send(content=mention, embed=discord.Embed(
                title="🍺 양조통 완료!",
                description=f"{mention} 양조통이 완료되었습니다! 꺼내주세요 🍺\n⏰ `{fmt_time(now_done)}`",
                color=0x57F287
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[양조통] 오류 (user={user_id}): {e}")
        finally:
            brew_tasks.pop(user_id, None)

    brew_tasks[user_id] = bot.loop.create_task(_timer())


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

    async def _timer():
        try:
            await asyncio.sleep(59 * 60)
            await channel.send(embed=discord.Embed(
                title="🔔 무역 물품 넣기 1분 전!",
                description=f"{mention} 무역 물품 넣기가 **1분 후**입니다! 준비하세요 🚢",
                color=0xFEE75C
            ))
            await asyncio.sleep(60)
            now_done = datetime.now(KST)
            await channel.send(content=mention, embed=discord.Embed(
                title="🚢 무역 물품 넣기!",
                description=f"{mention} 무역 물품을 넣어주세요!\n⏰ `{fmt_time(now_done)}`",
                color=0x57F287
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[무역대기] 오류 (user={user_id}): {e}")
        finally:
            trade_tasks.pop(user_id, None)

    trade_tasks[user_id] = bot.loop.create_task(_timer())


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

    async def _timer():
        try:
            warn_sec = max((total_min - 1) * 60, 0)
            await asyncio.sleep(warn_sec)
            if total_min > 1:
                await channel.send(embed=discord.Embed(
                    title="🔔 무역 완료 1분 전!",
                    description=f"{mention} 무역이 **1분 후** 완료됩니다! 준비하세요 🚢",
                    color=0xFEE75C
                ))
                await asyncio.sleep(60)
            now_done = datetime.now(KST)
            await channel.send(content=mention, embed=discord.Embed(
                title="🚢 무역 완료!",
                description=f"{mention} **{total_min}분** 무역이 완료되었습니다!\n⏰ `{fmt_time(now_done)}`",
                color=0x57F287
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[무역종료] 오류 (user={user_id}): {e}")
        finally:
            trade_tasks.pop(user_id, None)

    trade_tasks[user_id] = bot.loop.create_task(_timer())


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

    async def _timer():
        try:
            await asyncio.sleep(179 * 60)
            await channel.send(embed=discord.Embed(
                title="🔔 무역 재확인 1분 전!",
                description=f"{mention} 무역 재확인까지 **1분** 남았습니다! 🚢",
                color=0xFEE75C
            ))
            await asyncio.sleep(60)
            now_done = datetime.now(KST)
            await channel.send(content=mention, embed=discord.Embed(
                title="🚢 무역 확인 시간!",
                description=f"{mention} 무역을 다시 확인해보세요!\n⏰ `{fmt_time(now_done)}`",
                color=0xFFD700
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[무역포기] 오류 (user={user_id}): {e}")
        finally:
            trade_tasks.pop(user_id, None)

    trade_tasks[user_id] = bot.loop.create_task(_timer())


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
    if isinstance(error, app_commands.MissingPermissions):
        desc, color = "이 명령어는 **서버 관리자**만 사용할 수 있습니다.", 0xED4245
    elif isinstance(error, app_commands.CommandOnCooldown):
        desc, color = f"`{round(error.retry_after, 1)}초` 후 다시 시도하세요.", 0xFEE75C
    elif isinstance(error, app_commands.CommandInvokeError):
        desc, color = f"명령어 실행 중 오류\n```{str(error.original)}```", 0xED4245
    else:
        desc, color = f"예기치 않은 오류\n```{str(error)}```", 0xED4245

    embed = discord.Embed(title="⚠️ 오류", description=desc, color=color)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────
bot.run(TOKEN)