import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime
import asyncio
import json
import os
from dotenv import load_dotenv

# .envファイルから設定を読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
REMIND_TIME = os.getenv("REMIND_TIME", "13:00")
DEFAULT_RETRY_INTERVAL = 60  # デフォルトの追いかけ通知間隔（分）

# 実行時のデータ（設定・服薬ログ）の保存先。個人情報を含むためgit管理外
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "medicine_log.jsonl")

# 通知の片付け後にチャンネルへ残す1行の先頭。起動時の掃除でもこの行だけは残す
SUMMARY_PREFIX = "📝"
CLEANUP_HISTORY_LIMIT = 200  # 起動時の掃除で遡るメッセージ数
NOTICE_DELETE_AFTER = 300  # 再通知設定のお知らせを自動で消すまでの秒数

def normalize_time(text):
    """HH:MM 形式の文字列をゼロ埋めした形に整える（不正なら ValueError）"""
    return datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")

def load_config():
    """config.json を読み込む。無い・壊れている場合は .env の値を初期値として使う"""
    config = {"remind_time": normalize_time(REMIND_TIME), "retry_interval": DEFAULT_RETRY_INTERVAL}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if "remind_time" in saved:
            config["remind_time"] = normalize_time(saved["remind_time"])
        if "retry_interval" in saved:
            config["retry_interval"] = max(1, int(saved["retry_interval"]))
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, OSError) as e:
        print(f"⚠️ {CONFIG_PATH} を読み込めませんでした。初期値を使用します: {e}")
    return config

def _write_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)  # 書き込み途中で落ちても壊れないよう置き換え

def _append_line(path, line):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

class MedicineBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.is_taken = False
        self.is_skipped = False
        self.is_snoozed = False  # スヌーズ中かどうか
        config = load_config()
        self.remind_time = config["remind_time"]  # リマインド時刻（HH:MM）
        self.retry_interval = config["retry_interval"]  # 追いかけ通知間隔（分）
        self._snooze_task = None  # スヌーズ用のタスク
        self.active_views: list = []  # アクティブな MedicineView を追跡
        self.cycle_messages: list = []  # 片付け対象のその日のメッセージ
        self.summary = None  # 完了・スキップ時に残す1行（取り消されたら None）

    async def setup_hook(self):
        # 初回起動時は .env の値で config.json を作成しておく
        if not os.path.exists(CONFIG_PATH):
            await self.save_config()
        await self.tree.sync()
        self.main_loop.start()

    async def cleanup_old_messages(self):
        """再起動前に送った通知を削除する（服薬記録の1行は残す）"""
        channel = self.get_channel(CHANNEL_ID)
        if not channel:
            return
        try:
            async for message in channel.history(limit=CLEANUP_HISTORY_LIMIT):
                if message.author == self.user and not message.content.startswith(SUMMARY_PREFIX):
                    await self._delete_messages([message])
        except discord.Forbidden:
            print("⚠️ メッセージ履歴を読む権限がないため、古い通知を削除できませんでした")

    @tasks.loop(minutes=1)
    async def main_loop(self):
        now = datetime.now().strftime("%H:%M")
        now_dt = datetime.now()

        if now == self.remind_time:
            # 前日分が未記録のまま残っていれば片付けて「記録なし」を残す
            if self.cycle_messages:
                first = self.cycle_messages[0].created_at.astimezone()
                self.summary = f"{SUMMARY_PREFIX} {first:%m/%d} ❓ 服薬の記録がありませんでした"
                await self.record_event("missed")
                await self.finish_cycle()
            self.summary = None
            self.is_taken = False
            self.is_skipped = False
            self.is_snoozed = False
            self._cancel_snooze()
            self.active_views.clear()
            await self.send_reminder("💊 **お薬の時間です！**\n飲んだら「完了」、飲まない場合は「今日は飲まない」を押してください。", kind="scheduled")
            return

        remind_h, remind_m = map(int, self.remind_time.split(":"))
        # スヌーズ中は追いかけ通知をしない（スヌーズタスクが代わりに通知する）
        if not self.is_taken and not self.is_skipped and not self.is_snoozed:
            if (now_dt.hour > remind_h or (now_dt.hour == remind_h and now_dt.minute > remind_m)):
                passed_min = (now_dt.hour - remind_h) * 60 + (now_dt.minute - remind_m)
                if passed_min > 0 and passed_min % self.retry_interval == 0:
                    await self.send_reminder(f"⚠️ **未完了通知**\nお薬の確認ができていません！（{self.retry_interval}分おきに通知中）", kind="retry")

    @main_loop.before_loop
    async def before_main_loop(self):
        await self.wait_until_ready()
        await self.cleanup_old_messages()

    async def send_reminder(self, text, kind):
        """kind: scheduled（定刻）/ retry（追いかけ）/ snooze（スヌーズ後）/ resume（取り消し後）"""
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            old_views, self.active_views = self.active_views, []
            view = MedicineView(self)
            message = await channel.send(f"@everyone {text}", view=view)
            view.message = message
            self.active_views.append(view)
            self.track_message(message)
            await self.record_event("reminder_sent", detail={"kind": kind})

            # 古い通知は消して、ボタン付きの通知を常に最新の1件だけにする
            old_messages = [v.message for v in old_views if v.message]
            for old_view in old_views:
                old_view.stop()
            old_ids = {m.id for m in old_messages}
            self.cycle_messages = [m for m in self.cycle_messages if m.id not in old_ids]
            await self._delete_messages(old_messages)

    def track_message(self, message):
        """完了・スキップ後にまとめて削除するメッセージとして登録する"""
        channel = self.get_channel(CHANNEL_ID)
        if message and channel:
            # フォローアップは15分で webhook 経由の削除ができなくなるため、チャンネル側の参照で持つ
            self.cycle_messages.append(channel.get_partial_message(message.id))

    async def _delete_messages(self, messages):
        for message in messages:
            try:
                await message.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

    async def finish_cycle(self):
        """その日の通知をまとめて削除し、記録を1行だけ残す"""
        messages, self.cycle_messages = self.cycle_messages, []
        for view in self.active_views:
            view.stop()
        self.active_views.clear()
        await self._delete_messages(messages)
        channel = self.get_channel(CHANNEL_ID)
        if channel and self.summary:
            await channel.send(self.summary)

    async def save_config(self):
        """現在の設定を config.json に保存する"""
        config = {"remind_time": self.remind_time, "retry_interval": self.retry_interval}
        try:
            await asyncio.to_thread(_write_json, CONFIG_PATH, config)
        except OSError as e:
            print(f"⚠️ 設定を保存できませんでした: {e}")

    async def record_event(self, event_type, user=None, detail=None):
        """服薬イベントを medicine_log.jsonl に1行追記する"""
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event_type,
            "user_id": user.id if user else None,
            "user_name": user.display_name if user else None,
            "detail": detail or {},
        }
        try:
            await asyncio.to_thread(_append_line, LOG_PATH, json.dumps(entry, ensure_ascii=False))
        except OSError as e:
            print(f"⚠️ ログを記録できませんでした: {e}")

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
                    f"お薬の時間を先送りしていました。飲んだら「完了」を押してください。",
                    kind="snooze"
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
    await bot.save_config()
    # 設定完了は全体に見えるように通知
    await interaction.response.send_message(f"✅ 追いかけ通知の間隔を **{minutes}分** に設定しました。", ephemeral=False)

@bot.tree.command(name="remind_time", description="毎日のリマインド時刻を設定します")
@app_commands.describe(time="リマインドする時刻をHH:MM形式で入力してください（例: 13:00）")
async def set_remind_time(interaction: discord.Interaction, time: str):
    try:
        new_time = normalize_time(time)
    except ValueError:
        await interaction.response.send_message("HH:MM形式（例: 13:00）で入力してください。", ephemeral=True)
        return

    bot.remind_time = new_time
    await bot.save_config()
    await interaction.response.send_message(f"✅ リマインド時刻を **{new_time}** に設定しました。", ephemeral=False)

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
    await bot.record_event("snoozed", user=interaction.user, detail={"hours": hours})

    await interaction.response.send_message(
        f"🔔 {interaction.user.display_name} が再通知を設定しました。**{display}後**にもう一度お知らせします 💤",
        ephemeral=False,
        delete_after=NOTICE_DELETE_AFTER
    )

class MedicineView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.message = None  # send_reminder で設定される

    @discord.ui.button(label="完了！ ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 既に完了・スキップ済みならボタンを無効化して拒否
        if self.bot.is_taken or self.bot.is_skipped:
            await self._already_handled(interaction)
            return
        self.bot.is_taken = True
        self.bot._cancel_snooze()
        await self.bot.record_event("taken", user=interaction.user)
        self.bot.summary = f"{SUMMARY_PREFIX} {datetime.now():%m/%d %H:%M} ✅ {interaction.user.display_name} が服用しました"
        await self.disable_all_buttons(interaction, f"✅ {interaction.user.display_name} が服用を記録しました。通知を停止します ✨", False)
        await self._disable_other_active_views()
        undo_view = UndoActionView(self.bot, action_type="taken")
        undo_msg = await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )
        undo_view.message = undo_msg
        self.bot.track_message(undo_msg)

    @discord.ui.button(label="今日は飲まない ⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_skipped:
            await self._already_handled(interaction)
            return
        self.bot.is_skipped = True
        self.bot._cancel_snooze()
        await self.bot.record_event("skipped", user=interaction.user)
        self.bot.summary = f"{SUMMARY_PREFIX} {datetime.now():%m/%d %H:%M} ⏭️ {interaction.user.display_name} が今日の分をスキップしました"
        await self.disable_all_buttons(interaction, f"⏭️ {interaction.user.display_name} が今日の分をスキップしました。通知を停止します 💤", False)
        await self._disable_other_active_views()
        undo_view = UndoActionView(self.bot, action_type="skipped")
        undo_msg = await interaction.channel.send(
            "↩️ 間違えた場合はこちらから取り消せます（60秒以内）",
            view=undo_view
        )
        undo_view.message = undo_msg
        self.bot.track_message(undo_msg)

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
        followup = await interaction.followup.send(message, ephemeral=is_ephemeral, wait=True)
        if not is_ephemeral:
            self.bot.track_message(followup)

    async def _disable_other_active_views(self):
        """このビュー以外のアクティブな MedicineView のボタンを無効化する"""
        for view in self.bot.active_views:
            if view is not self:
                for item in view.children:
                    item.disabled = True
                if view.message:
                    try:
                        await view.message.edit(view=view)
                    except (discord.NotFound, discord.HTTPException):
                        pass
        self.bot.active_views.clear()

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
        await self.bot.record_event("snoozed", user=interaction.user, detail={"hours": snooze_hours})

        # 本人への確認（ephemeral）
        await interaction.response.edit_message(
            content=f"✅ **{display}後**に再通知します。",
            view=None
        )
        # チャンネル全体への通知
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(
                f"🔔 {self.user_name} が再通知を設定しました。**{display}後**にもう一度お知らせします 💤",
                delete_after=NOTICE_DELETE_AFTER
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
        self.message = None  # confirm/skip ハンドラで設定される

    @discord.ui.button(label="取り消す ↩️", style=discord.ButtonStyle.danger)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action_type == "taken":
            self.bot.is_taken = False
            label = "服用記録"
        else:
            self.bot.is_skipped = False
            label = "スキップ"
        await self.bot.record_event(f"undo_{self.action_type}", user=interaction.user)
        self.bot.summary = None
        self.stop()  # 取り消し済みなので期限切れの片付けは行わない

        button.disabled = True
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.display_name} が{label}を取り消しました。通知を再開します 🔔",
            view=self
        )
        # 即座にリマインドを再送信
        await self.bot.send_reminder("💊 **通知を再開しました！**\n飲んだら「完了」を押してください。", kind="resume")

    async def on_timeout(self):
        """取り消し期限（60秒）が過ぎたら、その日の通知を片付けて1行だけ残す"""
        await self.bot.finish_cycle()

bot.run(TOKEN)
