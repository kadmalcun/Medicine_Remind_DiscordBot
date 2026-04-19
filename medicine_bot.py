import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime
import asyncio
import os
from dotenv import load_dotenv

# .envファイルから設定を読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
REMIND_TIME = os.getenv("REMIND_TIME", "13:00")

class MedicineBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.is_taken = False
        self.is_skipped = False
        self.is_snoozed = False  # スヌーズ中かどうか
        self.retry_interval = 60  # デフォルトの追いかけ通知間隔（分）
        self._snooze_task = None  # スヌーズ用のタスク

    async def setup_hook(self):
        await self.tree.sync()
        self.main_loop.start()

    @tasks.loop(minutes=1)
    async def main_loop(self):
        now = datetime.now().strftime("%H:%M")
        now_dt = datetime.now()

        if now == REMIND_TIME:
            self.is_taken = False
            self.is_skipped = False
            self.is_snoozed = False
            self._cancel_snooze()
            await self.send_reminder("💊 **お薬の時間です！**\n飲んだら「完了」、飲まない場合は「今日は飲まない」を押してください。")
            return

        remind_h, remind_m = map(int, REMIND_TIME.split(":"))
        # スヌーズ中は追いかけ通知をしない（スヌーズタスクが代わりに通知する）
        if not self.is_taken and not self.is_skipped and not self.is_snoozed:
            if (now_dt.hour > remind_h or (now_dt.hour == remind_h and now_dt.minute > remind_m)):
                passed_min = (now_dt.hour - remind_h) * 60 + (now_dt.minute - remind_m)
                if passed_min > 0 and passed_min % self.retry_interval == 0:
                    await self.send_reminder(f"⚠️ **未完了通知**\nお薬の確認ができていません！（{self.retry_interval}分おきに通知中）")

    async def send_reminder(self, text):
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            view = MedicineView(self)
            await channel.send(f"@everyone {text}", view=view)

    async def _snooze_reminder(self, snooze_hours):
        """スヌーズ時間が経過した後に再通知を送信する"""
        try:
            await asyncio.sleep(snooze_hours * 3600)
            # スヌーズ解除して再通知
            self.is_snoozed = False
            if not self.is_taken and not self.is_skipped:
                display = format_hours(snooze_hours)
                await self.send_reminder(
                    f"🔔 **再通知（{display}後）**\n"
                    f"お薬の時間を先送りしていました。飲んだら「完了」を押してください。"
                )
        except asyncio.CancelledError:
            pass

    def _cancel_snooze(self):
        """実行中のスヌーズタスクをキャンセルする"""
        if self._snooze_task and not self._snooze_task.done():
            self._snooze_task.cancel()
            self._snooze_task = None

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
        # エラーメッセージは本人にのみ表示
        await interaction.response.send_message("1分以上の間隔を設定してください。", ephemeral=True)
        return
    
    bot.retry_interval = minutes
    # 設定完了は全体に見えるように通知
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

    @discord.ui.button(label="完了！ ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 既に完了・スキップ済みならボタンを無効化して拒否
        if self.bot.is_taken or self.bot.is_skipped:
            await self._already_handled(interaction)
            return
        self.bot.is_taken = True
        self.bot._cancel_snooze()
        # 第2引数のephemeralをFalseにして全体通知
        await self.disable_all_buttons(interaction, f"✅ {interaction.user.display_name} が服用を記録しました。通知を停止します ✨", False)
        # 取り消しボタンを送信
        undo_view = UndoActionView(self.bot, action_type="taken")
        await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )

    @discord.ui.button(label="今日は飲まない ⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_skipped:
            await self._already_handled(interaction)
            return
        self.bot.is_skipped = True
        self.bot._cancel_snooze()
        # 第2引数のephemeralをFalseにして全体通知
        await self.disable_all_buttons(interaction, f"⏭️ {interaction.user.display_name} が今日の分をスキップしました。通知を停止します 💤", False)
        # 取り消しボタンを送信
        undo_view = UndoActionView(self.bot, action_type="skipped")
        await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )

    @discord.ui.button(label="後で通知 🔔", style=discord.ButtonStyle.primary)
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_skipped:
            await self._already_handled(interaction)
            return
        # 時間選択のドロップダウンを表示（本人のみに見える）
        view = SnoozeSelectView(self.bot, interaction.user.display_name)
        await interaction.response.send_message(
            "⏰ **何時間後に再通知しますか？**",
            view=view,
            ephemeral=True
        )

    async def _already_handled(self, interaction):
        """既に処理済みの場合、ボタンを無効化して通知する"""
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✋ 今日の服薬は既に記録済みです。", ephemeral=True)

    async def disable_all_buttons(self, interaction, message, is_ephemeral):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        # 全体に見える通知を送信
        await interaction.followup.send(message, ephemeral=is_ephemeral)

class SnoozeSelectView(discord.ui.View):
    """スヌーズ時間を選択するドロップダウンメニュー"""
    def __init__(self, bot, user_name):
        super().__init__(timeout=60)  # 60秒で選択期限切れ
        self.bot = bot
        self.user_name = user_name

    @discord.ui.select(
        placeholder="再通知する時間を選んでください",
        options=[
            discord.SelectOption(label="15分後",    value="0.25",  emoji="⏰"),
            discord.SelectOption(label="30分後",    value="0.5",   emoji="⏰"),
            discord.SelectOption(label="1時間後",   value="1",     emoji="🕐"),
            discord.SelectOption(label="1時間半後", value="1.5",   emoji="🕜"),
            discord.SelectOption(label="2時間後",   value="2",     emoji="🕑"),
            discord.SelectOption(label="3時間後",   value="3",     emoji="🕒"),
            discord.SelectOption(label="4時間後",   value="4",     emoji="🕓"),
            discord.SelectOption(label="6時間後",   value="6",     emoji="🕕"),
        ]
    )
    async def select_snooze_time(self, interaction: discord.Interaction, select: discord.ui.Select):
        snooze_hours = float(select.values[0])
        display = format_hours(snooze_hours)

        self.bot.is_snoozed = True
        self.bot._cancel_snooze()
        # 選択された時間でスヌーズタスクを開始
        self.bot._snooze_task = asyncio.create_task(self.bot._snooze_reminder(snooze_hours))

        # 本人への確認（ephemeral）
        await interaction.response.edit_message(
            content=f"✅ **{display}後**に再通知します。",
            view=None
        )
        # チャンネル全体への通知
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(
                f"🔔 {self.user_name} が再通知を設定しました。**{display}後**にもう一度お知らせします 💤"
            )

    async def on_timeout(self):
        """タイムアウト時は何もしない（追いかけ通知が継続する）"""
        pass

class UndoActionView(discord.ui.View):
    """完了・スキップの取り消しボタン"""
    def __init__(self, bot, action_type):
        super().__init__(timeout=60)  # 60秒で期限切れ
        self.bot = bot
        self.action_type = action_type  # "taken" or "skipped"

    @discord.ui.button(label="取り消す ↩️", style=discord.ButtonStyle.danger)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action_type == "taken":
            self.bot.is_taken = False
            label = "服用記録"
        else:
            self.bot.is_skipped = False
            label = "スキップ"

        button.disabled = True
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.display_name} が{label}を取り消しました。通知を再開します 🔔",
            view=self
        )
        # 即座にリマインドを再送信
        await self.bot.send_reminder("💊 **通知を再開しました！**\n飲んだら「完了」を押してください。")

    async def on_timeout(self):
        """タイムアウト時にボタンを無効化する"""
        for item in self.children:
            item.disabled = True
        # タイムアウト時はメッセージを編集できないため、何もしない

bot.run(TOKEN)
