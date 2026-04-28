import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta

load_dotenv()
TOKEN     = os.getenv("TOKEN")
GUILD_IDS = os.getenv("GUILD_IDS", "")  # 쉼표로 구분된 서버 ID 목록

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True  # 리액션 감지 필수

bot = commands.Bot(command_prefix="!", intents=intents)

# 여러 길드 오브젝트 리스트 생성
GUILDS: list[discord.Object] = [
    discord.Object(id=int(gid.strip()))
    for gid in GUILD_IDS.split(",")
    if gid.strip().isdigit()
]

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

# 작물별 제철 계절 역방향 조회용
CROP_SEASON: dict[str, str] = {
    crop: season
    for season, crops in SEASON_CROPS.items()
    for crop in crops
}

REAL_MINUTES_PER_SERVER_DAY = 48
BASE_WATER_MINUTES          = 48
SUMMER_WATER_MINUTES        = 24

# 작물 성장일 수 기준 총 물주기 횟수 (1일=1회, 5일=5회)
def total_water_count(crop: str) -> int:
    return CROPS[crop]

user_tasks: dict[int, asyncio.Task] = {}
user_data:  dict[int, dict]         = {}


# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def calc_growth(crop: str, season: str) -> tuple[float, int, bool]:
    days        = CROPS[crop]
    base_min    = days * REAL_MINUTES_PER_SERVER_DAY
    season_mult = {"봄": 0.8, "겨울": 1.5}.get(season, 1.0)
    in_season   = crop in SEASON_CROPS.get(season, set())
    bonus       = 0.5 if in_season else 1.0
    final_min   = round(base_min * season_mult * bonus, 1)
    water_min   = SUMMER_WATER_MINUTES if season == "여름" else BASE_WATER_MINUTES
    return final_min, water_min, in_season

def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def season_emoji(season: str) -> str:
    return {"봄": "🌸", "여름": "☀️", "가을": "🍂", "겨울": "❄️"}.get(season, "🌿")


# ─────────────────────────────────────────
# on_ready
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    if GUILDS:
        total = 0
        for guild in GUILDS:
            synced = await bot.tree.sync(guild=guild)
            total  = len(synced)
            print(f"⚡ Guild({guild.id}) 동기화 — {len(synced)}개 커맨드")
        print(f"✅ {bot.user} 로그인 완료 | 총 {len(GUILDS)}개 서버 동기화됨")
        for cmd in synced:
            print(f"   └─ /{cmd.name}")
    else:
        synced = await bot.tree.sync()
        print(f"✅ {bot.user} 로그인 완료")
        print(f"🌐 전역 동기화 — {len(synced)}개 커맨드 (최대 1시간 소요)")


# ─────────────────────────────────────────
# /도움말
# ─────────────────────────────────────────
@bot.tree.command(name="도움말", description="봇 명령어 목록을 보여줍니다", guilds=GUILDS)
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 농장 봇 도움말", color=0x57F287)
    embed.add_field(name="/심기 [작물] [계절]", value="작물을 심고 물주기·수확 알림을 시작합니다", inline=False)
    embed.add_field(name="/상태",    value="현재 재배 중인 작물의 상태를 확인합니다", inline=False)
    embed.add_field(name="/작물목록", value="심을 수 있는 모든 작물과 성장 일수를 보여줍니다", inline=False)
    embed.add_field(name="/취소",    value="현재 진행 중인 작물 알람을 취소합니다", inline=False)
    embed.add_field(name="/동기화",  value="[관리자 전용] 슬래시 커맨드 수동 재등록", inline=False)
    embed.add_field(
        name="💧 물주기 방법",
        value="물주기 시간이 되면 알림 메시지에 ✅ 반응을 눌러 완료 처리하세요!\n"
              "✅ 를 누르지 않으면 다음 물주기 타이머가 시작되지 않습니다.",
        inline=False
    )
    embed.set_footer(text="서버 1일 = 현실 48분 | 물주기 알람은 1분 전에 미리 안내됩니다")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
# /작물목록
# ─────────────────────────────────────────
@bot.tree.command(name="작물목록", description="심을 수 있는 모든 작물을 보여줍니다", guilds=GUILDS)
async def cmd_croplist(interaction: discord.Interaction):
    embed  = discord.Embed(title="🌾 작물 목록 (서버 기준 성장일)", color=0xFEE75C)
    groups: dict[int, list[str]] = {}
    for crop, days in CROPS.items():
        groups.setdefault(days, []).append(crop)
    for days in sorted(groups):
        lines = []
        for c in groups[days]:
            csem = season_emoji(CROP_SEASON.get(c, ""))
            cs   = CROP_SEASON.get(c, "?")
            lines.append(f"`{c}` {csem}{cs}")
        embed.add_field(
            name=f"📅 {days}일 (물주기 {days}회)",
            value="  ".join(lines),
            inline=False
        )
    embed.set_footer(text="💡 제철 작물은 성장속도 +50% 보너스! | 작물명 옆 이모지는 제철 계절")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
# /심기
# ─────────────────────────────────────────
@bot.tree.command(name="심기", description="작물을 심고 물주기 알림을 시작합니다", guilds=GUILDS)
@app_commands.describe(작물="심을 작물 이름 (예: 딸기)", 계절="현재 계절 선택")
@app_commands.choices(계절=[
    app_commands.Choice(name="🌸 봄",   value="봄"),
    app_commands.Choice(name="☀️ 여름", value="여름"),
    app_commands.Choice(name="🍂 가을", value="가을"),
    app_commands.Choice(name="❄️ 겨울", value="겨울"),
])
async def cmd_plant(interaction: discord.Interaction, 작물: str, 계절: str):
    if 작물 not in CROPS:
        similar = [c for c in CROPS if 작물 in c or c in 작물]
        hint    = f"\n💡 혹시 이 작물을 찾으셨나요? → {', '.join(f'`{c}`' for c in similar[:5])}" if similar else ""
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 알 수 없는 작물",
            description=f"`{작물}`은(는) 등록되지 않은 작물입니다.{hint}\n\n`/작물목록`으로 전체 목록을 확인하세요.",
            color=0xED4245
        ), ephemeral=True)
        return

    user_id = interaction.user.id
    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        user_tasks.pop(user_id, None)
        user_data.pop(user_id, None)

    growth_min, water_min, in_season = calc_growth(작물, 계절)
    now          = datetime.now()
    finish_time  = now + timedelta(minutes=growth_min)
    next_water   = now + timedelta(minutes=water_min)
    total_waters = total_water_count(작물)
    sem          = season_emoji(계절)
    crop_season  = CROP_SEASON.get(작물, "알 수 없음")
    crop_sem     = season_emoji(crop_season)

    user_data[user_id] = {
        "crop": 작물, "season": 계절, "growth_min": growth_min,
        "water_min": water_min, "in_season": in_season,
        "start": now, "next_water": next_water, "end": finish_time,
        "water_count": 0, "total_waters": total_waters,
        "user_mention": interaction.user.mention,
        "channel_id": interaction.channel_id,
    }

    if in_season:
        bonus_text = "✅ 제철! 성장속도 +50% 보너스 적용"
    else:
        bonus_text = (
            f"❌ 비제철 (이 작물의 제철: {crop_sem} {crop_season})\n"
            f"   보너스 없이 기본 성장속도로 계산됩니다."
        )

    embed = discord.Embed(
        title=f"🌱 {작물} 심기 완료!",
        color=0x57F287 if in_season else 0xFFA500
    )
    embed.add_field(name="작물",           value=f"`{작물}`",                         inline=True)
    embed.add_field(name="현재 계절",      value=f"{sem} {계절}",                    inline=True)
    embed.add_field(name="이 작물의 제철", value=f"{crop_sem} {crop_season}",        inline=True)
    embed.add_field(name="제철 여부",      value=bonus_text,                         inline=False)
    embed.add_field(name="총 물주기 횟수", value=f"💧 총 `{total_waters}회` 필요",  inline=True)
    embed.add_field(name="물 유지시간",    value=f"⏱ `{water_min}분`마다",          inline=True)
    embed.add_field(name="최종 성장시간",  value=f"⏱ `{growth_min}분` (현실 기준)", inline=False)
    embed.add_field(name="심은 시간",      value=f"🕐 `{fmt_time(now)}`",            inline=True)
    embed.add_field(name="첫 물주기",      value=f"💦 `{fmt_time(next_water)}`",     inline=True)
    embed.add_field(name="수확 예정",      value=f"🌾 `{fmt_time(finish_time)}`",    inline=True)
    if 계절 == "가을":
        embed.add_field(name="특이사항", value="🍂 수확 시 2.5% 확률로 수확량 2배!", inline=False)
    embed.set_footer(text="💡 물주기 알림이 오면 메시지에 ✅ 반응을 눌러 완료하세요!")

    await interaction.response.send_message(content=interaction.user.mention, embed=embed)
    user_tasks[user_id] = bot.loop.create_task(water_loop(interaction))


# ─────────────────────────────────────────
# 💧 물주기 루프 (✅ 리액션 방식)
# ─────────────────────────────────────────
async def water_loop(interaction: discord.Interaction):
    user_id = interaction.user.id
    channel = interaction.channel
    try:
        while user_id in user_data:
            data      = user_data[user_id]
            water_min = data["water_min"]
            crop      = data["crop"]
            mention   = data["user_mention"]
            water_count   = data["water_count"]
            total_waters  = data["total_waters"]

            # ── 1분 전 예고 알림 ──
            await asyncio.sleep(max(water_min - 1, 0) * 60)
            if user_id not in user_data:
                break

            await channel.send(embed=discord.Embed(
                title="🔔 곧 물주기 시간!",
                description=f"{mention} **{crop}** 물주기 **1분 전**입니다! 준비하세요 💧\n"
                            f"진행도: `{water_count}/{total_waters}회`",
                color=0xFEE75C
            ))

            await asyncio.sleep(1 * 60)
            if user_id not in user_data:
                break

            # ── 물주기 메시지 전송 + ✅ 리액션 추가 ──
            data["water_count"] += 1
            current_count = data["water_count"]
            now       = datetime.now()

            # 완료 체크 표시 생성 (완료된 것 ✅, 남은 것 ⬜)
            check_marks = "".join(
                "✅" if i < current_count else "⬜"
                for i in range(total_waters)
            )

            # 마지막 물주기인지 확인
            is_last = current_count >= total_waters

            water_embed = discord.Embed(
                title="💧 물주기 시간!",
                description=(
                    f"{mention} **{crop}** 물을 주세요!\n"
                    f"물주기 완료 후 아래 ✅ 를 눌러주세요!"
                ),
                color=0x5865F2
            )
            water_embed.add_field(
                name="진행도",
                value=f"{check_marks}\n`{current_count}/{total_waters}회`",
                inline=False
            )
            if not is_last:
                next_time = now + timedelta(minutes=water_min)
                data["next_water"] = next_time
                water_embed.add_field(name="다음 물주기", value=f"`{fmt_time(next_time)}`", inline=True)
                water_embed.add_field(name="수확 예정",   value=f"`{fmt_time(data['end'])}`", inline=True)
            else:
                water_embed.add_field(
                    name="🌾 마지막 물주기!",
                    value="✅ 를 누르면 수확 완료 처리됩니다!",
                    inline=False
                )

            water_msg = await channel.send(embed=water_embed)
            await water_msg.add_reaction("✅")

            # ── ✅ 리액션 대기 (본인만 유효) ──
            def check(reaction, user):
                return (
                    str(reaction.emoji) == "✅"
                    and reaction.message.id == water_msg.id
                    and user.id == user_id
                    and not user.bot
                )

            try:
                await bot.wait_for("reaction_add", timeout=water_min * 60 * 2, check=check)
            except asyncio.TimeoutError:
                # 타임아웃 시 미완료 알림 후 다음 루프 진행
                if user_id in user_data:
                    await channel.send(embed=discord.Embed(
                        title="⚠️ 물주기 미완료",
                        description=f"{mention} **{crop}** 물주기를 놓쳤습니다!\n"
                                    f"작물이 시들 수 있어요. ✅ 를 눌러 계속 진행하거나 `/취소` 하세요.",
                        color=0xED4245
                    ))
                continue  # 타임아웃 후에도 루프 유지

            if user_id not in user_data:
                break

            # ── ✅ 눌린 경우 처리 ──
            if is_last:
                # 마지막 물주기 → 수확
                harvest_embed = discord.Embed(
                    title="🌾 수확 완료!",
                    description=f"{mention} **{crop}** 수확할 시간입니다!\n⏰ `{fmt_time(datetime.now())}`",
                    color=0xFFD700
                )
                harvest_embed.add_field(
                    name="총 물주기",
                    value=f"{'✅' * total_waters} `{total_waters}회` 완료!",
                    inline=False
                )
                if data["season"] == "가을":
                    harvest_embed.add_field(name="🍂 가을 보너스", value="2.5% 확률로 수확량 2배!", inline=False)
                await channel.send(embed=harvest_embed)
                user_data.pop(user_id, None)
                break
            else:
                # 중간 물주기 완료 → 다음 타이머 시작
                confirmed_embed = discord.Embed(
                    title="✅ 물주기 완료!",
                    description=f"{mention} **{crop}** 물주기 `{current_count}/{total_waters}회` 완료!\n"
                                f"다음 물주기까지 기다려 주세요.",
                    color=0x57F287
                )
                confirmed_embed.add_field(
                    name="진행도",
                    value="".join("✅" if i < current_count else "⬜" for i in range(total_waters)),
                    inline=False
                )
                await channel.send(embed=confirmed_embed)
                # 루프 계속 → 다음 물주기 대기

    except asyncio.CancelledError:
        pass
    finally:
        user_tasks.pop(user_id, None)


# ─────────────────────────────────────────
# /상태
# ─────────────────────────────────────────
@bot.tree.command(name="상태", description="현재 재배 중인 작물의 상태를 확인합니다", guilds=GUILDS)
async def cmd_status(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in user_data:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 진행 중인 작물 없음",
            description="재배 중인 작물이 없습니다. `/심기`로 시작하세요! 🌱",
            color=0xED4245
        ), ephemeral=True)
        return

    data    = user_data[user_id]
    now     = datetime.now()
    elapsed = round((now - data["start"]).total_seconds() / 60, 1)
    remain  = round(max(data["growth_min"] - elapsed, 0), 1)
    sem     = season_emoji(data["season"])
    total   = data["total_waters"]
    done    = data["water_count"]
    check_marks = "".join("✅" if i < done else "⬜" for i in range(total))

    embed = discord.Embed(title="📊 현재 작물 상태", color=0x57F287)
    embed.add_field(name="작물",        value=f"`{data['crop']}`",                             inline=True)
    embed.add_field(name="계절",        value=f"{sem} {data['season']}",                      inline=True)
    embed.add_field(name="제철 보너스", value="✅ 적용 중" if data["in_season"] else "➖ 없음", inline=True)
    embed.add_field(name="심은 시간",   value=f"`{fmt_time(data['start'])}`",                 inline=True)
    embed.add_field(name="다음 물주기", value=f"`{fmt_time(data['next_water'])}`",            inline=True)
    embed.add_field(name="수확 예정",   value=f"`{fmt_time(data['end'])}`",                   inline=True)
    embed.add_field(name="경과 시간",   value=f"`{elapsed}분`",                               inline=True)
    embed.add_field(name="남은 시간",   value=f"`{remain}분`",                                inline=True)
    embed.add_field(name="물주기 진행도", value=f"{check_marks}\n`{done}/{total}회`",         inline=False)
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────
# /취소
# ─────────────────────────────────────────
@bot.tree.command(name="취소", description="현재 진행 중인 작물 알람을 취소합니다", guilds=GUILDS)
async def cmd_cancel(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in user_tasks and user_id not in user_data:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ 취소할 작물 없음",
            description="실행 중인 작물이 없습니다. `/심기`로 시작하세요! 🌱",
            color=0xED4245
        ), ephemeral=True)
        return

    crop = user_data.get(user_id, {}).get("crop", "알 수 없음")
    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        user_tasks.pop(user_id, None)
    user_data.pop(user_id, None)

    await interaction.response.send_message(embed=discord.Embed(
        title="⛔ 알람 취소됨",
        description=f"**{crop}** 재배 알람이 취소되었습니다.\n다시 시작하려면 `/심기`를 사용하세요.",
        color=0xED4245
    ))


# ─────────────────────────────────────────
# /동기화 (관리자 전용)
# ─────────────────────────────────────────
@bot.tree.command(name="동기화", description="[관리자 전용] 슬래시 커맨드를 수동으로 재등록합니다", guilds=GUILDS)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_sync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if GUILDS:
        total_cmds = 0
        for guild in GUILDS:
            synced     = await bot.tree.sync(guild=guild)
            total_cmds = len(synced)
        lines = "\n".join(f"  └─ `/{c.name}`" for c in synced)
        await interaction.followup.send(
            f"⚡ {len(GUILDS)}개 서버 동기화 완료! `{total_cmds}`개 커맨드\n{lines}",
            ephemeral=True
        )
    else:
        synced = await bot.tree.sync()
        await interaction.followup.send(f"🌐 전역 동기화 완료! `{len(synced)}`개 커맨드", ephemeral=True)


# ─────────────────────────────────────────
# 에러 핸들러
# ─────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        desc = "이 명령어는 **서버 관리자**만 사용할 수 있습니다."
        color = 0xED4245
    elif isinstance(error, app_commands.MissingRequiredArgument):
        desc  = f"필수 입력값이 빠져 있습니다: `{error.param.name}`\n`/도움말`을 참고하세요."
        color = 0xFEE75C
    elif isinstance(error, app_commands.BadArgument):
        desc  = "입력값이 올바르지 않습니다. `/작물목록`에서 작물명을 확인하세요."
        color = 0xFEE75C
    elif isinstance(error, app_commands.CommandOnCooldown):
        desc  = f"`{round(error.retry_after, 1)}초` 후에 다시 시도해주세요."
        color = 0xFEE75C
    else:
        desc  = f"예기치 않은 오류가 발생했습니다.\n```{str(error)}```"
        color = 0xED4245

    embed = discord.Embed(title="⚠️ 오류", description=desc, color=color)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────
bot.run(TOKEN)