import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import asyncio
import json
import os
from dotenv import load_dotenv

# .envファイルから設定を読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
DEFAULT_REMIND_TIME = os.getenv("REMIND_TIME", "13:00")
DEFAULT_RETRY_INTERVAL = 60  # デフォルトの追いかけ通知間隔（分）
ROLLOVER_HOUR = 3  # この時刻（翌日3時）を過ぎても飲んでいなければ持ち越す
HOME_REMIND_HOUR = 22  # 「家に帰って飲む」の再通知時刻
DEFAULT_DOSE_GAP_HOURS = 6  # 2錠以上の日に、1錠飲んでから次の1錠を通知するまでの時間
DEFAULT_DOSE_DEADLINE = "23:00"  # 次の1錠の通知をこれより遅くしない時刻

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
    config = {
        "remind_time": normalize_time(DEFAULT_REMIND_TIME),
        "retry_interval": DEFAULT_RETRY_INTERVAL,
        "dose_gap_hours": DEFAULT_DOSE_GAP_HOURS,
        "dose_deadline": DEFAULT_DOSE_DEADLINE,
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if "remind_time" in saved:
            config["remind_time"] = normalize_time(saved["remind_time"])
        if "retry_interval" in saved:
            config["retry_interval"] = max(1, int(saved["retry_interval"]))
        if "dose_gap_hours" in saved:
            gap = float(saved["dose_gap_hours"])
            if gap <= 0:
                raise ValueError(f"dose_gap_hours は0より大きくすること: {gap}")
            config["dose_gap_hours"] = gap
        if "dose_deadline" in saved:
            config["dose_deadline"] = normalize_time(saved["dose_deadline"])
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, AttributeError, OSError) as e:
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

def read_log_events(path=None):
    """medicine_log.jsonl の全イベントを読み込む（壊れた行は読み飛ばす）"""
    events = []
    try:
        with open(path or LOG_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    entry["ts"] = datetime.fromisoformat(entry["ts"]).astimezone()
                    if not isinstance(entry.get("detail"), dict):
                        entry["detail"] = {}
                    events.append(entry)
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    return events

def cycle_bounds(now, remind_time):
    """now が属する1日分の区切りを返す: (開始＝直近の定刻, 締め＝その後の最初の3時)"""
    h, m = map(int, remind_time.split(":"))
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    rollover = start.replace(hour=ROLLOVER_HOUR, minute=0)
    if rollover <= start:
        rollover += timedelta(days=1)
    return start, rollover

def next_home_remind(after):
    """after より後の最初の22時"""
    target = after.replace(hour=HOME_REMIND_HOUR, minute=0, second=0, microsecond=0)
    if target <= after:
        target += timedelta(days=1)
    return target

def next_dose_time(taken_at, gap_hours, deadline, cycle_start, rollover):
    """1錠飲んだあと、次の1錠を通知する時刻（その日のうちに通知しないなら None）

    taken_at から gap_hours 後とし、その日の deadline（HH:MM）を超えるなら deadline にする。
    deadline 以降に飲んだときと、締め（rollover）以降になるときは None。
    """
    h, m = map(int, deadline.split(":"))
    limit = cycle_start.replace(hour=h, minute=m, second=0, microsecond=0)
    if limit <= cycle_start:
        limit += timedelta(days=1)
    if taken_at >= limit:
        return None
    target = min(taken_at + timedelta(hours=gap_hours), limit)
    return target if target < rollover else None

def _dose_count(value, default):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        return default

def replay_cycle(events, start, end, total=1):
    """[start, end) のイベントを順に適用して、その日の状態を求める（total はその日の合計錠数）"""
    state = {
        "is_taken": False,
        "home_at": None,  # 「家に帰って飲む」を押した日時（待ち中なら）
        "snooze": None,  # (設定した日時, 時間)
        "reminded": False,  # 通知を1回でも送ったか
        "doses": [],  # その日に記録した分: (日時, 記録した人, 錠数)
        "dose_waiting": False,  # 次の1錠の時刻を待っているか
        "dose_next": None,  # 次の1錠を通知する時刻（None ならその日は通知しない）
    }
    for entry in events:
        if not (start <= entry["ts"] < end):
            continue
        event = entry.get("event")
        detail = entry["detail"]
        if event == "taken":
            # 1錠ずつ飲む機能より前のログには count がない。そのときは残りをすべて飲んだとみなす
            rest = max(1, total - sum(d[2] for d in state["doses"]))
            state["doses"].append((entry["ts"], entry.get("user_name"), _dose_count(detail.get("count"), rest)))
            state.update(is_taken=True, home_at=None, snooze=None, dose_waiting=False)
        elif event == "undo_taken":
            state["is_taken"] = False
            if state["doses"]:
                state["doses"].pop()
        elif event == "dose_taken":
            state["doses"].append((entry["ts"], entry.get("user_name"), _dose_count(detail.get("count"), 1)))
            try:
                next_at = datetime.fromisoformat(detail["next_at"]).astimezone() if detail.get("next_at") else None
            except (ValueError, TypeError):
                next_at = None
            state.update(dose_waiting=True, dose_next=next_at, home_at=None, snooze=None)
        elif event == "undo_dose":
            if state["doses"]:
                state["doses"].pop()
            state.update(dose_waiting=False, dose_next=None)
        elif event == "home":
            state.update(home_at=entry["ts"], snooze=None)
        elif event == "undo_home":
            state["home_at"] = None
        elif event == "snoozed":
            state["snooze"] = (entry["ts"], float(detail.get("hours", 0)))
        elif event == "reminder_sent":
            state["reminded"] = True
            # 待っていた再通知が届いたら、その待ちは終わり
            if detail.get("kind") == "home":
                state["home_at"] = None
            elif detail.get("kind") == "snooze":
                state["snooze"] = None
            elif detail.get("kind") == "dose":
                state.update(dose_waiting=False, dose_next=None)
    return state

def carried_doses(events, before=None):
    """持ち越し錠数。before より前で、最後にそれを記録したイベントの値

    再起動の遅れで締めより後に記録した「記録なし」（missed）も、before より前の日の分なら数える。
    """
    missed_doses = 0
    for entry in events:
        if before is not None and entry["ts"] >= before:
            late_missed = entry.get("event") == "missed" and str(entry["detail"].get("cycle", "9999")) < f"{before:%Y-%m-%d}"
            if not late_missed:
                continue
        if "missed_doses" in entry["detail"]:
            try:
                missed_doses = max(0, int(entry["detail"]["missed_doses"]))
            except (ValueError, TypeError):
                pass
    return missed_doses

def restore_state(events, now, remind_time):
    """ログから再起動前の状態を求める（now はタイムゾーン付き）

    返り値の pending_missed は、止まっている間に締め（3時）を過ぎた日の
    開始日時。その日の「記録なし」をまだ記録していなければ入る。
    """
    missed_doses = carried_doses(events)

    start, rollover = cycle_bounds(now, remind_time)
    result = {
        "missed_doses": missed_doses,
        "is_taken": False,
        "home_until": None,  # 22時の再通知を待つ場合、その時刻
        "snooze_until": None,  # 再通知を待つ場合、(その時刻, 時間)
        "in_period": now < rollover,  # 追いかけ通知を出す時間帯か
        "pending_missed": None,
        "pending_doses": [],  # pending_missed の日に記録した分
        "doses": [],  # 今日記録した分: (日時, 記録した人, 錠数)
        "dose_until": None,  # 次の1錠の通知を待つ場合、その時刻
        "dose_wait_rollover": False,  # 今日はもう次の1錠を通知せず、締めまで待つか
    }

    # 直近で締めを過ぎた日（今が締め後なら今日の分、締め前なら前日の分）
    if now >= rollover:
        closed_start, closed_end = start, rollover
    else:
        closed_start, closed_end = cycle_bounds(start - timedelta(minutes=1), remind_time)
    # 合計錠数は、その日が始まる前の持ち越しから求める
    closed = replay_cycle(events, closed_start, closed_end, total=carried_doses(events, closed_start) + 1)
    # 「記録なし」は締めより後に記録されるので、どの日の分かは detail の cycle で見る
    missed_logged = any(
        e.get("event") == "missed" and e["detail"].get("cycle") == f"{closed_start:%Y-%m-%d}"
        for e in events
    )
    if closed["reminded"] and not closed["is_taken"] and not missed_logged:
        result["pending_missed"] = closed_start
        result["pending_doses"] = closed["doses"]

    if not result["in_period"]:
        return result

    current = replay_cycle(events, start, now, total=carried_doses(events, start) + 1)
    result["is_taken"] = current["is_taken"]
    result["doses"] = current["doses"]
    if current["is_taken"]:
        return result
    if current["dose_waiting"]:
        if current["dose_next"] is None:
            result["dose_wait_rollover"] = True
            return result
        if current["dose_next"] > now:
            result["dose_until"] = current["dose_next"]
            return result
        # 止まっている間に次の1錠の時刻を過ぎた → 未完了として通知を送り直す
    if current["home_at"]:
        target = next_home_remind(current["home_at"])
        if target > now:
            result["home_until"] = target
            return result
    if current["snooze"]:
        snoozed_at, hours = current["snooze"]
        until = snoozed_at + timedelta(hours=hours)
        if until > now:
            result["snooze_until"] = (until, hours)
    return result

class MedicineBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.is_taken = False
        self.is_home_waiting = False  # 「家に帰って飲む」待ち状態
        self.is_snoozed = False  # スヌーズ中かどうか
        config = load_config()
        self.remind_time = config["remind_time"]  # リマインド時刻（HH:MM）
        self.retry_interval = config["retry_interval"]  # 追いかけ通知間隔（分）
        self.dose_gap_hours = config["dose_gap_hours"]  # 2錠以上の日の服用間隔（時間）
        self.dose_deadline = config["dose_deadline"]  # 次の1錠の通知をこれより遅くしない（HH:MM）
        self._snooze_task = None  # スヌーズ用のタスク
        self._home_task = None  # 家帰って飲む用のタスク（22時再通知）
        self.active_views: list = []  # アクティブな MedicineView を追跡
        self.missed_doses = 0  # 飲み忘れて翌日に持ち越す錠数
        self.dose_records: list = []  # 今日記録した分: (日時, 記録した人, 錠数)
        self.is_dose_waiting = False  # 次の1錠の時刻を待っているか
        self._dose_task = None  # 次の1錠を通知するタスク
        self.cycle_messages: list = []  # 片付け対象のその日のメッセージ
        self.summary = None  # 完了時に残す1行（取り消されたら None）

    @property
    def total_doses(self):
        """今日飲む錠数（前日までの持ち越し + 今日の分）"""
        return self.missed_doses + 1

    @property
    def taken_doses(self):
        return sum(d[2] for d in self.dose_records)

    @property
    def remaining_doses(self):
        return max(0, self.total_doses - self.taken_doses)

    async def setup_hook(self):
        # 初回起動時は .env の値で config.json を作成しておく
        if not os.path.exists(CONFIG_PATH):
            await self.save_config()
        await self.tree.sync()
        self.main_loop.start()

    @tasks.loop(minutes=1)
    async def main_loop(self):
        now_dt = datetime.now()
        now = now_dt.strftime("%H:%M")

        if now == self.remind_time:
            # 前日分の通知が残っていれば片付ける（記録は締めの時点で残している）
            if self.cycle_messages:
                self.summary = None
                await self.finish_cycle()
            self.summary = None
            self.is_taken = False
            self.is_home_waiting = False
            self.dose_records = []
            self._cancel_snooze()
            self._cancel_home()
            self._cancel_dose()
            await self.send_reminder(self.reminder_text("💊 **お薬の時間です！**", with_home=True), kind="scheduled")
            return

        # 翌日3時を過ぎても飲んでいなければ持ち越す
        if now == f"{ROLLOVER_HOUR:02d}:00":
            if not self.is_taken:
                start, _ = cycle_bounds(now_dt - timedelta(minutes=1), self.remind_time)
                await self.record_missed(start)
            self.is_home_waiting = False
            self._cancel_snooze()
            self._cancel_home()
            self._cancel_dose()
            return

        remind_h, remind_m = map(int, self.remind_time.split(":"))
        total_now_min = now_dt.hour * 60 + now_dt.minute
        total_remind_min = remind_h * 60 + remind_m

        # REMIND_TIME以降か、0時〜3時未満（翌日へまたいだ通知継続）
        in_notification_period = (total_now_min > total_remind_min) or (total_now_min < ROLLOVER_HOUR * 60)

        # スヌーズ中・家帰って待ち中・次の1錠の待ち中は追いかけ通知をしない
        if not self.is_taken and not self.is_home_waiting and not self.is_snoozed and not self.is_dose_waiting:
            if in_notification_period:
                passed_min = total_now_min - total_remind_min
                if passed_min < 0:
                    passed_min += 24 * 60  # 0時跨ぎ対応
                if passed_min > 0 and passed_min % self.retry_interval == 0:
                    await self.send_reminder(self.reminder_text("⚠️ **未完了通知**", "お薬の確認ができていません！", action=False), kind="retry")

    @main_loop.before_loop
    async def before_main_loop(self):
        await self.wait_until_ready()
        await self.cleanup_old_messages()
        await self.restore_from_log()

    def reminder_text(self, title, note=None, with_home=False, action=True):
        """通知文を組み立てる

        持ち越しがあれば見出しの次に錠数を添える。action が真なら、押すボタンの案内で終える。
        """
        lines = [title]
        total, remaining = self.total_doses, self.remaining_doses
        if total > 1:
            if remaining == total:
                lines.append(f"⚠️ 前日までの分を飲み忘れています。今日は **{total}錠** を、1錠ずつ間隔をあけて飲みます。")
            else:
                lines.append(f"今日の {total}錠のうち、残りは **{remaining}錠** です。")
        if note:
            lines.append(note)
        if action:
            done = "1錠飲んだら「1錠飲んだ」を" if total > 1 else "飲んだら「完了」を"
            if with_home:
                lines.append(f"{done}、後で飲むときは「家に帰って飲む」を押してください。")
            else:
                lines.append(f"{done}押してください。")
        return "\n".join(lines)

    def summary_line(self, day):
        """その日を終えるときに残す1行"""
        records = "／".join(f"{ts:%H:%M} {name or '?'} {count}錠" for ts, name, count in self.dose_records)
        taken = self.taken_doses
        if not self.dose_records:
            return f"{SUMMARY_PREFIX} {day:%m/%d} ❓ 服薬の記録がありませんでした"
        if self.remaining_doses == 0 and self.total_doses == 1:
            ts, name, _ = self.dose_records[-1]
            return f"{SUMMARY_PREFIX} {ts:%m/%d %H:%M} ✅ {name} が服用を記録しました"
        if self.remaining_doses == 0:
            return f"{SUMMARY_PREFIX} {day:%m/%d} ✅ {taken}錠の服用を記録しました（{records}）"
        return (
            f"{SUMMARY_PREFIX} {day:%m/%d} ⚠️ {self.total_doses}錠のうち{taken}錠の服用を記録しました"
            f"（{records}）。残りの{self.remaining_doses}錠は翌日に持ち越します"
        )

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

    async def restore_from_log(self):
        """再起動前の服薬状況をログから復元する"""
        try:
            events = await asyncio.to_thread(read_log_events)
        except OSError as e:
            print(f"⚠️ ログを読み込めませんでした: {e}")
            return

        now = datetime.now().astimezone()
        state = restore_state(events, now, self.remind_time)
        self.missed_doses = state["missed_doses"]

        # 止まっている間に締めを過ぎた日があれば、ここで持ち越す
        if state["pending_missed"]:
            self.dose_records = list(state["pending_doses"])
            await self.record_missed(state["pending_missed"])
            self.dose_records = []

        if not state["in_period"]:
            return
        self.is_taken = state["is_taken"]
        self.dose_records = list(state["doses"])
        if state["dose_until"]:
            self.is_dose_waiting = True
            self._dose_task = asyncio.create_task(self._dose_reminder(state["dose_until"]))
            status = f"次の1錠を待っている（{state['dose_until']:%H:%M} に通知）"
        elif state["dose_wait_rollover"]:
            self.is_dose_waiting = True
            status = "残りは翌日に持ち越し"
        elif state["home_until"]:
            self.is_home_waiting = True
            self._home_task = asyncio.create_task(self._home_reminder(state["home_until"]))
            status = f"家に帰って飲む（{state['home_until']:%H:%M} に再通知）"
        elif state["snooze_until"]:
            until, hours = state["snooze_until"]
            self.is_snoozed = True
            self._snooze_task = asyncio.create_task(
                self._snooze_reminder(hours, delay_seconds=(until - now).total_seconds())
            )
            status = f"スヌーズ中（{until:%H:%M} に再通知）"
        elif self.is_taken:
            status = "完了"
        else:
            # 起動時の掃除でボタン付きの通知も消えているので、送り直す
            await self.send_reminder(self.reminder_text("🔄 **Botを再起動しました**", "お薬の確認ができていません！", with_home=True), kind="restart")
            status = "未完了"
        print(f"ℹ️ 服薬状況を復元しました: {status}（持ち越し {self.missed_doses}錠）")

    async def record_missed(self, cycle_start):
        """締めの時点で残っていた錠数を記録し、翌日に持ち越す"""
        self.summary = self.summary_line(cycle_start)
        taken, remaining = self.taken_doses, self.remaining_doses
        self.missed_doses = remaining
        self.is_dose_waiting = False
        await self.record_event("missed", detail={"missed_doses": remaining, "cycle": f"{cycle_start:%Y-%m-%d}", "taken_doses": taken})
        await self.finish_cycle()

    async def send_reminder(self, text, kind):
        """kind: scheduled（定刻）/ retry（追いかけ）/ snooze（スヌーズ後）/ home（22時）/ dose（次の1錠）/ resume（取り消し後）/ restart（再起動後）"""
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
        """その日の終わりにまとめて削除するメッセージとして登録する"""
        channel = self.get_channel(CHANNEL_ID)
        if message and channel:
            # フォローアップは15分で webhook 経由の削除ができなくなるため、チャンネル側の参照で持つ
            self.cycle_messages.append(channel.get_partial_message(message.id))

    async def _disable_views(self, views):
        """指定した View からボタンを削除してメッセージを更新する"""
        for view in views:
            view.clear_items()
            if view.message:
                try:
                    await view.message.edit(view=view)
                except (discord.NotFound, discord.HTTPException):
                    pass

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
        self.summary = None

    async def save_config(self):
        """現在の設定を config.json に保存する"""
        config = {
            "remind_time": self.remind_time,
            "retry_interval": self.retry_interval,
            "dose_gap_hours": self.dose_gap_hours,
            "dose_deadline": self.dose_deadline,
        }
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

    async def _snooze_reminder(self, snooze_hours, delay_seconds=None):
        """スヌーズ時間が経過した後に再通知を送信する（delay_seconds は再起動後の残り時間）"""
        try:
            await asyncio.sleep(snooze_hours * 3600 if delay_seconds is None else delay_seconds)
            self.is_snoozed = False
            if not self.is_taken and not self.is_home_waiting:
                display = format_hours(snooze_hours)
                await self.send_reminder(
                    self.reminder_text("🔔 **再通知**", f"{display}先送りしていたお薬の時間です。"),
                    kind="snooze"
                )
        except asyncio.CancelledError:
            pass

    async def _home_reminder(self, target=None):
        """22時になったら再通知する（target は再起動後に引き継いだ通知時刻）"""
        try:
            now = datetime.now().astimezone()
            target = target or next_home_remind(now)
            await asyncio.sleep(max(0, (target - now).total_seconds()))

            self.is_home_waiting = False
            if not self.is_taken:
                await self.send_reminder(
                    self.reminder_text("🏠 **お帰りなさい！お薬の時間です！**"),
                    kind="home"
                )
        except asyncio.CancelledError:
            pass

    async def _dose_reminder(self, target):
        """次の1錠の時刻になったら通知する"""
        try:
            await asyncio.sleep(max(0, (target - datetime.now().astimezone()).total_seconds()))
            self.is_dose_waiting = False
            if not self.is_taken:
                nth = self.taken_doses + 1
                await self.send_reminder(self.reminder_text(f"💊 **{nth}錠目の時間です！**", with_home=True), kind="dose")
        except asyncio.CancelledError:
            pass

    def _cancel_dose(self):
        """次の1錠の待ちをやめる"""
        self.is_dose_waiting = False
        if self._dose_task and not self._dose_task.done():
            self._dose_task.cancel()
        self._dose_task = None

    async def record_doses(self, user, count):
        """count 錠飲んだことを記録し、(知らせる文, 取り消しの View) を返す。記録できなければ None"""
        if self.is_taken or self.is_home_waiting or self.is_dose_waiting:
            return None
        count = max(1, min(count, self.remaining_doses))
        total, prev_missed = self.total_doses, self.missed_doses
        first = self.taken_doses + 1
        what = "" if total == 1 else (f"{first}錠目の" if count == 1 else f"{count}錠の")
        now = datetime.now().astimezone()
        self._cancel_snooze()
        self._cancel_home()
        self.dose_records.append((now, user.display_name, count))
        name = user.display_name

        if self.remaining_doses == 0:
            self.is_taken = True
            self.summary = self.summary_line(now)
            self.missed_doses = 0
            await self.record_event("taken", user=user, detail={"missed_doses": 0, "prev_missed_doses": prev_missed, "count": count})
            done = "" if total == 1 else "今日の分はこれで終わりです。"
            text = f"✅ {name} が{what}服用を記録しました。{done}通知を止めます。"
            return text, UndoActionView(self, action_type="taken", restore_missed=prev_missed)

        start, rollover = cycle_bounds(now, self.remind_time)
        next_at = next_dose_time(now, self.dose_gap_hours, self.dose_deadline, start, rollover)
        remaining = self.remaining_doses
        self.is_dose_waiting = True
        if next_at:
            self._dose_task = asyncio.create_task(self._dose_reminder(next_at))
        await self.record_event("dose_taken", user=user, detail={
            "count": count, "remaining": remaining, "total": total,
            "next_at": next_at.isoformat(timespec="seconds") if next_at else None,
        })
        text = f"✅ {name} が{what}服用を記録しました。残りは{remaining}錠です。\n"
        if next_at:
            text += f"**{next_at:%H:%M}** に{self.taken_doses + 1}錠目をお知らせします。"
        else:
            text += f"今日はもう通知しません。残りの{remaining}錠は、明日の分と合わせてお知らせします。"
        return text, UndoActionView(self, action_type="dose")

    def _cancel_snooze(self):
        """実行中のスヌーズタスクをキャンセルし、スヌーズ状態を解除する"""
        self.is_snoozed = False
        if self._snooze_task and not self._snooze_task.done():
            self._snooze_task.cancel()
        self._snooze_task = None

    def _cancel_home(self):
        """実行中のホーム待ちタスクをキャンセルする"""
        if self._home_task and not self._home_task.done():
            self._home_task.cancel()
        self._home_task = None

    async def start_snooze(self, hours, user):
        """スヌーズを開始して記録する"""
        self._cancel_snooze()
        self.is_snoozed = True
        self._snooze_task = asyncio.create_task(self._snooze_reminder(hours))
        await self.record_event("snoozed", user=user, detail={"hours": hours})

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

@bot.tree.command(name="interval", description="追いかけ通知の間隔を分単位で設定します")
@app_commands.describe(minutes="通知する間隔を分単位で入力してください。例: 15")
async def set_interval(interaction: discord.Interaction, minutes: int):
    if minutes < 1:
        await interaction.response.send_message("1分以上の間隔を設定してください。", ephemeral=True)
        return

    bot.retry_interval = minutes
    await bot.save_config()
    await interaction.response.send_message(f"✅ 追いかけ通知の間隔を **{minutes}分** に設定しました。", ephemeral=False)

@bot.tree.command(name="remind_time", description="毎日のリマインド時刻を設定します")
@app_commands.describe(time="リマインドする時刻をHH:MM形式で入力してください。例: 13:00")
async def set_remind_time(interaction: discord.Interaction, time: str):
    try:
        new_time = normalize_time(time)
    except ValueError:
        await interaction.response.send_message("13:00 のように、HH:MM形式で入力してください。", ephemeral=True)
        return

    bot.remind_time = new_time
    await bot.save_config()
    await interaction.response.send_message(f"✅ リマインド時刻を **{new_time}** に設定しました。", ephemeral=False)

@bot.tree.command(name="dose_gap", description="2錠以上の日に、1錠飲んでから次の1錠を通知するまでの時間を設定します")
@app_commands.describe(hours="時間で入力してください。例: 6")
async def set_dose_gap(interaction: discord.Interaction, hours: float):
    if hours <= 0:
        await interaction.response.send_message("0より大きい値を設定してください。", ephemeral=True)
        return

    bot.dose_gap_hours = hours
    await bot.save_config()
    await interaction.response.send_message(f"✅ 1錠ずつ飲むときの間隔を **{format_hours(hours)}** に設定しました。", ephemeral=False)

@bot.tree.command(name="dose_deadline", description="次の1錠の通知を、これより遅くしない時刻を設定します")
@app_commands.describe(time="HH:MM形式で入力してください。例: 23:00")
async def set_dose_deadline(interaction: discord.Interaction, time: str):
    try:
        new_time = normalize_time(time)
    except ValueError:
        await interaction.response.send_message("23:00 のように、HH:MM形式で入力してください。", ephemeral=True)
        return

    bot.dose_deadline = new_time
    await bot.save_config()
    await interaction.response.send_message(
        f"✅ 次の1錠の通知を、遅くとも **{new_time}** に出すよう設定しました。", ephemeral=False
    )

@bot.tree.command(name="snooze", description="今すぐスヌーズし、指定した時間後に再通知します")
@app_commands.describe(hours="再通知するまでの時間を入力してください。例: 2")
async def set_snooze(interaction: discord.Interaction, hours: float):
    if hours <= 0:
        await interaction.response.send_message("0より大きい値を設定してください。", ephemeral=True)
        return

    display = format_hours(hours)
    await bot.start_snooze(hours, interaction.user)

    await interaction.response.send_message(
        f"🔔 {interaction.user.display_name} が再通知を設定しました。**{display}後**にもう一度お知らせします。",
        ephemeral=False,
        delete_after=NOTICE_DELETE_AFTER
    )

class MedicineView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.message = None  # send_reminder で設定される
        # 2錠以上の日は1錠ずつ記録する。まとめて飲むボタンは残りが2錠以上のときだけ
        if bot.total_doses > 1:
            self.confirm.label = "1錠飲んだ ✅"
        if bot.remaining_doses < 2:
            self.remove_item(self.bulk)

    @discord.ui.button(label="完了！ ✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await self.bot.record_doses(interaction.user, 1)
        if result is None:
            await self._already_handled(interaction)
            return
        text, undo_view = result
        # 取り消しボタンは、記録したことを知らせるメッセージに付ける
        await self.disable_all_buttons(interaction, text, undo_view)
        await self._disable_other_active_views()

    @discord.ui.button(label="まとめて飲む 💊", style=discord.ButtonStyle.success)
    async def bulk(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting or self.bot.is_dose_waiting:
            await self._already_handled(interaction)
            return
        await interaction.response.send_message(
            "💊 **何錠飲みましたか？**",
            view=DoseCountSelectView(self.bot, self.bot.remaining_doses),
            ephemeral=True
        )

    @discord.ui.button(label="家に帰って飲む 🏠", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting or self.bot.is_dose_waiting:
            await self._already_handled(interaction)
            return
        self.bot.is_home_waiting = True
        self.bot._cancel_snooze()
        self.bot._cancel_home()
        self.bot._home_task = asyncio.create_task(self.bot._home_reminder())
        await self.bot.record_event("home", user=interaction.user)
        undo_view = UndoActionView(self.bot, action_type="home")
        await self.disable_all_buttons(
            interaction,
            f"🏠 {interaction.user.display_name} が「家に帰って飲む」を設定しました。**{HOME_REMIND_HOUR}:00** に再通知します。",
            undo_view
        )
        await self._disable_other_active_views()

    @discord.ui.button(label="後で通知 🔔", style=discord.ButtonStyle.primary)
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.bot.is_taken or self.bot.is_home_waiting or self.bot.is_dose_waiting:
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
        if self.bot.is_dose_waiting:
            await interaction.followup.send("✋ 記録済みです。次の1錠の時間になったらお知らせします。", ephemeral=True)
        else:
            await interaction.followup.send("✋ 今日の服薬は既に記録済みです。", ephemeral=True)

    async def disable_all_buttons(self, interaction, message, undo_view):
        """押された通知のボタンを消し、結果のメッセージを取り消しボタン付きで送る"""
        self.clear_items()
        await interaction.response.edit_message(view=self)
        followup = await interaction.followup.send(message, view=undo_view, wait=True)
        undo_view.message = followup
        self.bot.track_message(followup)

    async def _disable_other_active_views(self):
        others = [v for v in self.bot.active_views if v is not self]
        await self.bot._disable_views(others)
        self.bot.active_views.clear()

class DoseCountSelect(discord.ui.Select):
    """まとめて飲んだ錠数を選ぶドロップダウン"""
    def __init__(self, bot, remaining):
        self._bot = bot
        options = [
            discord.SelectOption(label=f"{n}錠", value=str(n), description="残りをすべて" if n == remaining else None)
            for n in range(2, min(remaining, 25) + 1)  # Discord の選択肢は25個まで
        ]
        super().__init__(placeholder="飲んだ錠数を選んでください", options=options)

    async def callback(self, interaction: discord.Interaction):
        bot = self._bot
        result = await bot.record_doses(interaction.user, int(self.values[0]))
        if result is None:
            await interaction.response.edit_message(content="✋ すでに記録済みです。", view=None)
            return
        text, undo_view = result
        await interaction.response.edit_message(content="✅ 記録しました。", view=None)
        # 通知のボタンを消し、結果はチャンネルに取り消しボタン付きで出す
        await bot._disable_views(bot.active_views)
        bot.active_views.clear()
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            message = await channel.send(text, view=undo_view)
            undo_view.message = message
            bot.track_message(message)


class DoseCountSelectView(discord.ui.View):
    def __init__(self, bot, remaining):
        super().__init__(timeout=60)
        self.add_item(DoseCountSelect(bot, remaining))


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
                label=f"{duration_str}後",
                description=f"{time_str} にお知らせします",
                value=value,
                emoji=emoji
            ))
        super().__init__(placeholder="再通知する時間を選んでください", options=options)

    async def callback(self, interaction: discord.Interaction):
        snooze_hours = float(self.values[0])
        duration_str = format_hours(snooze_hours)
        time_str = (datetime.now() + timedelta(hours=snooze_hours)).strftime("%H:%M")

        await self._bot.start_snooze(snooze_hours, interaction.user)

        await interaction.response.edit_message(
            content=f"✅ {duration_str}後の **{time_str}** に再通知します。",
            view=None
        )
        channel = self._bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(
                f"🔔 {self._user_name} が再通知を設定しました。{duration_str}後の **{time_str}** にもう一度お知らせします。",
                delete_after=NOTICE_DELETE_AFTER
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
        self.action_type = action_type  # "taken"（その日を終えた記録）/ "dose"（途中の記録）/ "home"
        self.restore_missed = restore_missed  # 取り消し時に戻す持ち越し錠数
        self.message = None  # MedicineView.disable_all_buttons で設定される

    @discord.ui.button(label="取り消す ↩️", style=discord.ButtonStyle.danger)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action_type == "taken":
            self.bot.is_taken = False
            self.bot.missed_doses = self.restore_missed
            self.bot.summary = None
            if self.bot.dose_records:
                self.bot.dose_records.pop()
            await self.bot.record_event("undo_taken", user=interaction.user, detail={
                "missed_doses": self.restore_missed, "remaining": self.bot.remaining_doses,
            })
            label = "服用記録"
        elif self.action_type == "dose":
            self.bot._cancel_dose()
            if self.bot.dose_records:
                self.bot.dose_records.pop()
            await self.bot.record_event("undo_dose", user=interaction.user, detail={"remaining": self.bot.remaining_doses})
            label = "服用記録"
        else:  # "home"
            self.bot.is_home_waiting = False
            self.bot._cancel_home()
            await self.bot.record_event("undo_home", user=interaction.user)
            label = "家に帰って飲む設定"
        self.stop()  # 取り消し済みなので期限切れの処理は行わない

        self.clear_items()
        await interaction.response.edit_message(
            content=f"✅ {interaction.user.display_name} が{label}を取り消しました。通知を再開します。",
            view=self
        )
        await self.bot.send_reminder(self.bot.reminder_text("💊 **通知を再開しました！**", with_home=True), kind="resume")

    async def on_timeout(self):
        if self.action_type == "taken":
            # 取り消し期限（60秒）が過ぎたら、その日の通知を片付けて1行だけ残す
            await self.bot.finish_cycle()
            return
        # 「家に帰って飲む」と途中の記録はまだその日が終わっていないので、取り消しボタンだけ消す
        self.clear_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

if __name__ == "__main__":
    bot.run(TOKEN)
