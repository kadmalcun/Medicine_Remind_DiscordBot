import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime
import asyncio

# --- 設定エリア ---
TOKEN = f'{TOKEN}' # botのトークン
CHANNEL_ID = {CHANNEL_ID} # 通知を送信するチャンネルのID
REMIND_TIME = "13:00"
# ----------------

class MedicineBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.is_taken = False
        self.is_skipped = False
        self.retry_interval = 60  # デフォルトの間隔（分）

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
            await self.send_reminder("💊 **お薬の時間です！**\n飲んだら「完了」、飲まない場合は「今日は飲まない」を押してください。")
            return

        remind_h, remind_m = map(int, REMIND_TIME.split(":"))
        if not self.is_taken and not self.is_skipped:
            if (now_dt.hour > remind_h or (now_dt.hour == remind_h and now_dt.minute > remind_m)):
                passed_min = (now_dt.hour - remind_h) * 60 + (now_dt.minute - remind_m)
                if passed_min > 0 and passed_min % self.retry_interval == 0:
                    await self.send_reminder(f"⚠️ **未完了通知**\nお薬の確認ができていません！（{self.retry_interval}分おきに通知中）")

    async def send_reminder(self, text):
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            view = MedicineView(self)
            await channel.send(f"@everyone {text}", view=view)

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

class MedicineView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="完了！ ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.bot.is_taken = True
        # 第2引数のephemeralをFalseにして全体通知
        await self.disable_all_buttons(interaction, f"✅ {interaction.user.display_name} が服用を記録しました。通知を停止します ✨", False)

    @discord.ui.button(label="今日は飲まない ⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.bot.is_skipped = True
        # 第2引数のephemeralをFalseにして全体通知
        await self.disable_all_buttons(interaction, f"⏭️ {interaction.user.display_name} が今日の分をスキップしました。通知を停止します 💤", False)

    async def disable_all_buttons(self, interaction, message, is_ephemeral):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        # 全体に見える通知を送信
        await interaction.followup.send(message, ephemeral=is_ephemeral)

bot.run(TOKEN)
