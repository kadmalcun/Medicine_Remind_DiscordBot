# 💊 Discord お薬リマインダーBot (Medicine Reminder Bot)

毎日決まった時間にDiscordでお薬の服用をリマインドし、「完了」ボタンが押されるまで一定間隔で追いかけ通知を行うBotです。飲み忘れ防止に役立ちます。

## ✨ 主な機能

* **定刻リマインド:** 毎日設定した時間（例：13:00）に、通知メッセージと操作ボタンを送信します。
* **しつこい追いかけ通知:** 「完了」または「今日は飲まない」ボタンが押されるまで、指定した間隔（デフォルト60分）で未完了通知を送り続けます。
* **直感的なボタン操作:** メッセージ内のボタンをクリックするだけで、服用記録やスキップ操作が完了します。操作後はボタンが非アクティブになり、誤操作を防ぎます。
* **再通知（スヌーズ）機能:** 「後で通知」を選ぶと一時的に追いかけ通知を止め、指定時間後に再通知します。
* **誤操作の取り消し:** 間違えて「完了」や「スキップ」を押してしまった場合、60秒以内であれば操作を取り消すことができます。
* **ステータスの全体共有:** 誰が「完了」や「スキップ」、再通知を設定したかがチャンネル全体に通知されます。
* **スラッシュコマンド連携:** Discordのチャット上から、追いかけ通知の間隔や再通知の設定を変更できます。
* **安全な設定管理:** `.env` ファイルを使ってトークンなどの秘密情報を安全に管理します。

## 🛠 動作環境

* **Python 3.8 以上**
* **discord.py** (バージョン 2.0以上)

## 🚀 セットアップ手順

### 1. Botアカウントの作成と設定
1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、新しいApplication（Bot）を作成します。
2. 左メニューの **Bot** セクションを開き、**Token** を取得します（絶対に外部へ公開しないでください）。
3. 同じく **Bot** セクションを少し下にスクロールし、「Privileged Gateway Intents」の **MESSAGE CONTENT INTENT** を **ON** にして保存します。
4. 左メニューの **OAuth2 > URL Generator** で、`bot` と `applications.commands` の2つにチェックを入れます。
5. 生成されたURLを開き、Botを自身のサーバーへ招待します。

### 2. 設定ファイルの編集
リポジトリにある `.env.example` をコピーして `.env` ファイルを作成し、ご自身の環境に合わせて書き換えてください。

```bash
cp .env.example .env
```

`.env` の内容：
```ini
# Botのトークン（Discord Developer Portalで取得）
DISCORD_TOKEN=your_token_here

# 通知を送信するチャンネルのID（チャンネルを右クリック→IDをコピー）
CHANNEL_ID=123456789012345678

# リマインド時刻（HH:MM形式）
REMIND_TIME=13:00
```

### 3. ライブラリのインストール
必要なPythonパッケージをインストールします。

```bash
pip install -r requirements.txt
```

### 4. 実行
スクリプトを実行してBotを起動します。

```bash
python3 medicine_bot.py
```

> **💡 運用上のヒント**
> Botはターミナルを閉じると停止してしまいます。Linux環境のサーバーで24時間稼働させる場合は、`nohup python3 medicine_bot.py &` でバックグラウンド実行するか、`tmux`、または `systemd` を使用してサービス化することをおすすめします。

## 💻 使い方

### 日々の操作（ボタン）
指定した時間になると、通知用チャンネルにボタン付きのメッセージが届きます。
* **完了！ ✅** : お薬を飲んだことを記録し、本日の追いかけ通知を完全に停止します。間違えた場合、60秒以内なら取り消しが可能です。
* **今日は飲まない ⏭️** : 体調などの理由で服用しない場合に押します。本日の追いかけ通知を停止します。こちらも60秒以内なら取り消せます。
* **後で通知 🔔** : 押した後にドロップダウンメニューから時間（15分後〜6時間後など）を選ぶと、指定した時間後に再度リマインドします。

### スラッシュコマンド
Discordのチャット入力欄でコマンドを入力して設定を変更できます。
* **`/interval minutes:[分数]`**
  * 追いかけ通知の間隔を設定します。
  * 例: `/interval minutes:30` （未完了時の通知を30分間隔に変更します）
* **`/snooze hours:[時間]`**
  * 指定した時間後に再通知を即座に設定します。
  * 例: `/snooze hours:1.5` （1時間30分後に再通知します）

※ *注意事項: コマンドで変更した設定はメモリ上に保存されるため、Botのプログラムを再起動するとデフォルトの設定にリセットされます。*


### 🚀 Botをサービス化する手順

#### ステップ1：サービス設定ファイルを作成する
まず、OSに「こういうBotを動かしてね」と指示する設定ファイルを作ります。
ターミナルで以下のコマンドを実行し、テキストエディタ（nano）を開きます。

```bash
sudo nano /etc/systemd/system/medicine_bot.service
```
*(※パスワードを聞かれたら、paladminユーザーのパスワードを入力してください)*

#### ステップ2：設定を書き込む
開いた真っ黒な画面に、以下の内容をそのままコピー＆ペーストしてください。

```ini
[Unit]
Description=Medicine Reminder Discord Bot
After=network.target

[Service]
# 実行するユーザー名と、Botのファイルがあるディレクトリ
User=paladmin
WorkingDirectory=/home/paladmin/bot

# 実行するコマンド（python3の絶対パスとファイルの絶対パス）
ExecStart=/usr/bin/python3 /home/paladmin/bot/medicine_bot.py

# エラーで落ちた場合、常に自動再起動する設定
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

貼り付けたら、以下のキーボード操作で保存して閉じます。
1. `Ctrl + O` （アルファベットのオー）を押して保存
2. `Enter` を押して確定
3. `Ctrl + X` を押して閉じる

#### ステップ3：OSに設定を読み込ませて起動する
ファイルを作ったら、以下のコマンドを上から順に1行ずつ実行してください。

```bash
# 1. 今作った新しい設定ファイルをOSに認識させる
sudo systemctl daemon-reload

# 2. Botを起動する
sudo systemctl start medicine_bot.service

# 3. サーバー起動時に、自動でBotも起動するように設定する
sudo systemctl enable medicine_bot.service
```

#### ステップ4：ちゃんと動いているか確認する
最後に、以下のコマンドでBotの状態を確認します。

```bash
sudo systemctl status medicine_bot.service
```

緑色で **`active (running)`** と表示されていれば、大成功です！
（確認画面から抜けるには `q` キーを押してください）

---

### 💡 今後の管理コマンド

サービス化した後は、これらのコマンドで簡単にBotを操作できるようになります。プログラムを書き換えた後は `restart` をするだけでOKです。

* **Botを止める:** `sudo systemctl stop medicine_bot.service`
* **Botを再起動する:** `sudo systemctl restart medicine_bot.service`
* **ログを見る:** `journalctl -u medicine_bot.service -e`

最初は少し難しく感じるかもしれませんが、一度設定してしまうと放置でずっと動き続けてくれるので、非常に快適になりますよ！ぜひ試してみてください。
