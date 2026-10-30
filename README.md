# 💊 Discord お薬リマインダーBot (Medicine Reminder Bot)

毎日決まった時間にDiscordでお薬の服用をリマインドし、「完了」ボタンが押されるまで一定間隔で追いかけ通知を行うBotです。飲み忘れ防止に役立ちます。

## ✨ 主な機能

* **定刻リマインド:** 毎日設定した時間（例：13:00）に、通知メッセージと操作ボタンを送信します。
* **しつこい追いかけ通知:** 「完了」ボタンが押されるまで、指定した間隔（デフォルト60分）で未完了通知を送り続けます。日付をまたいでも翌日3時まで通知します。
* **直感的なボタン操作:** メッセージ内のボタンをクリックするだけで、服用記録や後で飲む設定ができます。新しい通知を送ると古い通知のボタンは消え、最新の通知だけ操作できます。
* **再通知（スヌーズ）機能:** 「後で通知」を選ぶと一時的に追いかけ通知を止め、指定時間後に再通知します。
* **誤操作の取り消し:** 間違えて「完了」や「家に帰って飲む」を押してしまった場合、60秒以内であれば操作を取り消すことができます。
* **ステータスの全体共有:** 誰が「完了」や「家に帰って飲む」、再通知を設定したかがチャンネル全体に通知されます。
* **スラッシュコマンド連携:** Discordのチャット上から、追いかけ通知の間隔や再通知の設定を変更できます。
* **安全な設定管理:** `.env` ファイルを使ってトークンなどの秘密情報を安全に管理します。

## 🛠 動作環境

* **Python 3.8 以上**
* **discord.py** (バージョン 2.0以上)

## 🚀 セットアップ手順

### 1. Botアカウントの作成と設定
1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセスし、新しいApplication（Bot）を作成します。
2. 左メニューの **Bot** セクションを開き、**Token** を取得します（絶対に外部へ公開しないでください）。
3. 左メニューの **OAuth2 > URL Generator** で、`bot` と `applications.commands` の2つにチェックを入れます。
4. 生成されたURLを開き、Botを自身のサーバーへ招待します。

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
* **家に帰って飲む 🏠** : 外出中などですぐ飲めない場合に押します。追いかけ通知を止め、**22:00** に再通知します。こちらも60秒以内なら取り消せます。
* **後で通知 🔔** : 押した後にドロップダウンメニューから時間（15分後〜8時間後。選択肢には実際の通知時刻も表示されます）を選ぶと、指定した時間後に再度リマインドします。
* **飲み忘れの持ち越し:** 翌日3時までに「完了」が押されなかった場合、その分を翌日に持ち越し、次のリマインドで「今日の分と合わせて○錠」と案内します。

### スラッシュコマンド
Discordのチャット入力欄でコマンドを入力して設定を変更できます。
* **`/interval minutes:[分数]`**
  * 追いかけ通知の間隔を設定します。
  * 例: `/interval minutes:30` （未完了時の通知を30分間隔に変更します）
* **`/snooze hours:[時間]`**
  * 指定した時間後に再通知を即座に設定します。
  * 例: `/snooze hours:1.5` （1時間30分後に再通知します）

※ *注意事項: コマンドで変更した設定や飲み忘れの持ち越し錠数はメモリ上に保存されるため、Botを再起動する（デプロイを含む）とリセットされます。*


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
WorkingDirectory=/home/paladmin/bot/medicine_notice_bot

# 実行するコマンド（venv内のpython3の絶対パスとファイルの絶対パス）
ExecStart=/home/paladmin/bot/medicine_notice_bot/venv/bin/python3 /home/paladmin/bot/medicine_notice_bot/medicine_bot.py

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

---

### 🤖 GitHub Actions による自動デプロイ

`main` ブランチに push する（PRをマージする）と、`.github/workflows/deploy.yml` が次の順で実行されます。Actions 画面の「Run workflow」から手動でも実行できます。README だけの変更ではデプロイしません。

1. **validate**（GitHub のランナー）: 依存パッケージのインストールと構文チェック
2. **deploy**（palserver のセルフホストランナー、ラベル `medicine-bot`）: `sudo /usr/local/sbin/medicine-bot-deploy` を実行
   * `medicine_bot.py` と `requirements.txt` を `/home/paladmin/bot/medicine_notice_bot` に配置（`.env` と `venv` はサーバー側のものを使います）
   * `requirements.txt` が変わったときだけ `venv` に依存パッケージをインストール
   * `medicine_bot.service` を再起動し、15秒後も落ちずに動いているかを確認
   * 起動に失敗したら、配置前の状態（`/var/backups/medicine-bot` に5世代まで退避）に戻してジョブを失敗にします

#### セキュリティ上の構成
* ランナーは専用ユーザー `gh-runner` で動きます（`paladmin` の権限は持ちません）。
* `gh-runner` が sudo できるのは `/usr/local/sbin/medicine-bot-deploy` だけです。これはリポジトリ内の `deploy/medicine-bot-deploy` を root 所有でコピーしたもので、リポジトリ側のスクリプトを直接 root で実行することはありません。**`deploy/medicine-bot-deploy` を変更したら、サーバーへの再設置が必要です**（下の手順を `RUNNER_TOKEN` なしで再実行）。
* ワークフローは `pull_request` では動かしません。また、フォークからのPRのワークフローは「Require approval for all external contributors」に設定しています。

#### 初回セットアップ（サーバー側）
1. GitHub の **Settings > Actions > Runners > New self-hosted runner** に表示される登録トークンを控えます（`gh api -X POST repos/kadmalcun/Medicine_Remind_DiscordBot/actions/runners/registration-token --jq .token` でも取得できます。有効期限は1時間）。
2. palserver にこのリポジトリを取得し、root で初期設定スクリプトを実行します。デプロイスクリプトの設置、sudoers の設定、ランナーの登録とサービス化までを行います。
   ```bash
   sudo RUNNER_TOKEN=<登録トークン> bash deploy/setup-server.sh
   ```
3. Runners の画面で `palserver-medicine` が「Idle」になれば準備完了です。
