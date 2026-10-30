import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import asyncio
import os
from dotenv import load_dotenv

# .envファイルから設定を読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
_raw_time = os.getenv("REMIND_TIME", "13:00")
_h, _m = map(int, _raw_time.split(":"))
REMIND_TIME = f"{_h:02d}:{_m:02d}"

class MedicineBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.is_taken = False
        self.is_home_waiting = False  # 「家に帰って飲む」待ち状態
        self.is_snoozed = False  # スヌーズ中かどうか
        self.retry_interval = 60  # デフォルトの追いかけ通知間隔（分）
        self._snooze_task = None  # スヌーズ用のタスク
        self._home_task = None  # 家帰って飲む用のタスク（22時再通知）
        self.active_views: list = []  # アクティブな MedicineView を追跡
        self.missed_doses = 0  # 飲み忘れて翌日に持ち越す錠数

    async def setup_hook(self):
        await self.tree.sync()
        self.main_loop.start()

    @tasks.loop(minutes=1)
    async def main_loop(self):
        now = datetime.now().strftime("%H:%M")
        now_dt = datetime.now()

        if now == REMIND_TIME:
            self.is_taken = False
            self.is_home_waiting = False
            self.is_snoozed = False
            self._cancel_snooze()
            self._cancel_home()
            if self.missed_doses > 0:
                total = self.missed_doses + 1
                text = (
                    "💊 **お薬の時間です！**\n"
                    f"⚠️ 前日までの分を飲み忘れています。今日の分と合わせて **{total}錠** 飲んでください。\n"
                    "飲んだら「完了」、後で飲む場合は「家に帰って飲む」を押してください。"
                )
            else:
                text = "💊 **お薬の時間です！**\n飲んだら「完了」、後で飲む場合は「家に帰って飲む」を押してください。"
            await self.send_reminder(text)
            return

        # 翌日3時を過ぎても飲んでいなければ持ち越す
        if now == "03:00":
            if not self.is_taken:
                self.missed_doses += 1
            self.is_home_waiting = False
            self._cancel_home()
            return

        remind_h, remind_m = map(int, REMIND_TIME.split(":"))
        total_now_min = now_dt.hour * 60 + now_dt.minute
        total_remind_min = remind_h * 60 + remind_m

        # REMIND_TIME以降か、0時〜3時未満（翌日へまたいだ通知継続）
        in_notification_period = (total_now_min > total_remind_min) or (total_now_min < 3 * 60)

        # スヌーズ中・家帰って待ち中は追いかけ通知をしない
        if not self.is_taken and not self.is_home_waiting and not self.is_snoozed:
            if in_notification_period:
                passed_min = total_now_min - total_remind_min
                if passed_min < 0:
                    passed_min += 24 * 60  # 0時跨ぎ対応
                if passed_min > 0 and passed_min % self.retry_interval == 0:
                    await self.send_reminder(f"⚠️ **未完了通知**\nお薬の確認ができていません！（{self.retry_interval}分おきに通知中）")

    async def send_reminder(self, text):
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            # 過去のリマインダーのボタンを消して、最新のものだけ有効にする
            await self._disable_views(self.active_views)
            self.active_views.clear()
            view = MedicineView(self)
            message = await channel.send(f"@everyone {text}", view=view)
            view.message = message
            self.active_views.append(view)

    async def _disable_views(self, views):
        """指定した View からボタンを削除してメッセージを更新する"""
        for view in views:
            view.clear_items()
            if view.message:
                try:
                    await view.message.edit(view=view)
                except (discord.NotFound, discord.HTTPException):
                    pass

    async def _snooze_reminder(self, snooze_hours):
        """スヌーズ時間が経過した後に再通知を送信する"""
        try:
            await asyncio.sleep(snooze_hours * 3600)
            self.is_snoozed = False
            if not self.is_taken and not self.is_home_waiting:
                display = format_hours(snooze_hours)
                await self.send_reminder(
                    f"🔔 **再通知（{display}後）**\n"
                    f"お薬の時間を先送りしていました。飲んだら「完了」を押してください。"
                )
        except asyncio.CancelledError:
            pass

    async def _home_reminder(self):
        """22時になったら再通知する"""
        try:
            now = datetime.now()
            target = now.replace(hour=22, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)

            self.is_home_waiting = False
            if not self.is_taken:
                await self.send_reminder(
                    "🏠 **お帰りなさい！お薬の時間です！**\n"
                    "飲んだら「完了」を押してください。"
                )
        except asyncio.CancelledError:
            pass

    def _cancel_snooze(self):
        """実行中のスヌーズタスクをキャンセルする"""
        if self._snooze_task and not self._snooze_task.done():
            self._snooze_task.cancel()
            self._snooze_task = None

    def _cancel_home(self):
        """実行中のホーム待ちタスクをキャンセルする"""
        if self._home_task and not self._home_task.done():
            self._home_task.cancel()
            self._home_task = None

def format_hours(hours):
    """時間数を読みやすい文字列に変換する"""
    if hours < 1:
        return f"{int(hours * 60)}分"
    elif hours == int(hours):
        return f"{int(hours)}時間"
    else:
        h = int(hours)
        m = int((hours - h) * 60)
        return f"{h}時間{m}分"

bot = MedicineBot()

@bot.tree.command(name="interval", description="追いかけ通知の間隔（分）を設定します")
@app_commands.describe(minutes="通知する間隔を分単位で入力してください（例: 15）")
async def set_interval(interaction: discord.Interaction, minutes: int):
    if minutes < 1:
        await interaction.response.send_message("1分以上の間隔を設定してください。", ephemeral=True)
        return

    bot.retry_interval = minutes
    await interaction.response.send_message(f"✅ 追いかけ通知の間隔を **{minutes}分** に設定しました。", ephemeral=False)

@bot.tree.command(name="snooze", description="指定した時間後に再通知します（即時スヌーズ）")
@app_commands.describe(hours="再通知するまでの時間を入力してください（例: 2）")
async def set_snooze(interaction: discord.Interaction, hours: float):
    if hours <= 0:
        await interaction.response.send_message("0より大きい値を設定してください。", ephemeral=True)
        return

    display = format_hours(hours)
    bot.is_snoozed = True
    bot._cancel_snooze()
    bot._snooze_task = asyncio.create_task(bot._snooze_reminder(hours))

    await interaction.response.send_message(
        f"🔔 {interaction.user.display_name} が再通知を設定しました。**{display}後**にもう一度お知らせします 💤",
        ephemeral=False
    )

class MedicineView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.message = None  # send_reminder で設定される

    @discord.ui.button(label="完了！ ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting:
            await self._already_handled(interaction)
            return
        self.bot.is_taken = True
        self.bot._cancel_snooze()
        self.bot._cancel_home()
        prev_missed = self.bot.missed_doses
        self.bot.missed_doses = 0
        await self.disable_all_buttons(interaction, f"✅ {interaction.user.display_name} が服用を記録しました。通知を停止します ✨", False)
        await self._disable_other_active_views()
        undo_view = UndoActionView(self.bot, action_type="taken", restore_missed=prev_missed)
        undo_msg = await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )
        undo_view.message = undo_msg

    @discord.ui.button(label="家に帰って飲む 🏠", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting:
            await self._already_handled(interaction)
            return
        self.bot.is_home_waiting = True
        self.bot._cancel_snooze()
        self.bot._cancel_home()
        self.bot._home_task = asyncio.create_task(self.bot._home_reminder())
        await self.disable_all_buttons(
            interaction,
            f"🏠 {interaction.user.display_name} が「家に帰って飲む」を設定しました。**22:00** に再通知します 💤",
            False
        )
        await self._disable_other_active_views()
        undo_view = UndoActionView(self.bot, action_type="home")
        undo_msg = await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )
        undo_view.message = undo_msg

    @discord.ui.button(label="後で通知 🔔", style=discord.ButtonStyle.primary)
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting:
            await self._already_handled(interaction)
            return
        view = SnoozeSelectView(self.bot, interaction.user.display_name)
        await interaction.response.send_message(
            "⏰ **何時間後に再通知しますか？**",
            view=view,
            ephemeral=True
        )

    async def _already_handled(self, interaction):
        self.clear_items()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✋ 今日の服薬は既に記録済みです。", ephemeral=True)

    async def disable_all_buttons(self, interaction, message, is_ephemeral):
        self.clear_items()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(message, ephemeral=is_ephemeral)

    async def _disable_other_active_views(self):
        others = [v for v in self.bot.active_views if v is not self]
        await self.bot._disable_views(others)
        self.bot.active_views.clear()

class SnoozeSelect(discord.ui.Select):
    """スヌーズ時間を選択するドロップダウン（実際の通知時刻を表示）"""
    _OPTIONS = [
        ("0.25", "⏰"),
        ("0.5",  "⏰"),
        ("1",    "🕐"),
        ("2",    "🕑"),
        ("3",    "🕒"),
        ("4",    "🕓"),
        ("6",    "🕕"),
        ("7",    "🕖"),
        ("8",    "🕗"),
    ]

    def __init__(self, bot, user_name):
        self._bot = bot
        self._user_name = user_name
        now = datetime.now()
        options = []
        for value, emoji in self._OPTIONS:
            hours = float(value)
            notify_dt = now + timedelta(hours=hours)
            time_str = notify_dt.strftime("%H:%M")
            duration_str = format_hours(hours)
            options.append(discord.SelectOption(
                label=f"{time_str}（{duration_str}後）",
                value=value,
                emoji=emoji
            ))
        super().__init__(placeholder="再通知する時間を選んでください", options=options)

    async def callback(self, interaction: discord.Interaction):
        snooze_hours = float(self.values[0])
        duration_str = format_hours(snooze_hours)
        time_str = (datetime.now() + timedelta(hours=snooze_hours)).strftime("%H:%M")

        self._bot.is_snoozed = True
        self._bot._cancel_snooze()
        self._bot._snooze_task = asyncio.create_task(self._bot._snooze_reminder(snooze_hours))

        await interaction.response.edit_message(
            content=f"✅ **{time_str}**（{duration_str}後）に再通知します。",
            view=None
        )
        channel = self._bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(
                f"🔔 {self._user_name} が再通知を設定しました。**{time_str}**（{duration_str}後）にもう一度お知らせします 💤"
            )


class SnoozeSelectView(discord.ui.View):
    """スヌーズ時間を選択するドロップダウンメニュー"""
    def __init__(self, bot, user_name):
        super().__init__(timeout=60)
        self.add_item(SnoozeSelect(bot, user_name))

    async def on_timeout(self):
        pass

class UndoActionView(discord.ui.View):
    """完了・家帰って飲む設定の取り消しボタン"""
    def __init__(self, bot, action_type, restore_missed=0):
        super().__init__(timeout=60)  # 60秒で期限切れ
        self.bot = bot
        self.action_type = action_type  # "taken" or "home"
        self.restore_missed = restore_missed  # 取り消し時に戻す持ち越し錠数
        self.message = None  # confirm/skip ハンドラで設定される

    @discord.ui.button(label="取り消す ↩️", style=discord.ButtonStyle.danger)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action_type == "taken":
            self.bot.is_taken = False
            self.bot.missed_doses = self.restore_missed
            label = "服用記録"
        else:  # "home"
            self.bot.is_home_waiting = False
            self.bot._cancel_home()
            label = "家に帰って飲む設定"

        button.disabled = True
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.display_name} が{label}を取り消しました。通知を再開します 🔔",
            view=self
        )
        await self.bot.send_reminder("💊 **通知を再開しました！**\n飲んだら「完了」を押してください。")

    async def on_timeout(self):
        self.clear_items()
        if self.message:
            try:
                await self.message.edit(
                    content="↩️ 取り消し期限が切れました（60秒経過）",
                    view=self
                )
            except (discord.NotFound, discord.HTTPException):
                pass

bot.run(TOKEN)
