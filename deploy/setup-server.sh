#!/usr/bin/env bash
#
# palserver の初期設定（root で一度だけ実行する。何度実行しても同じ状態になる）
#
#   sudo RUNNER_TOKEN=<登録トークン> bash deploy/setup-server.sh
#
# 1. デプロイスクリプトを root 所有で /usr/local/sbin に設置（本番用とテスト用 Bot 用の2つの名前で）
# 2. gh-runner がその2つだけを NOPASSWD で sudo できるようにする
# 3. テスト用 Bot の配置先・venv・systemd サービスを用意する（.env は手で置く）
# 4. このリポジトリ用のセルフホストランナーを gh-runner で登録し、サービス化する
#
# デプロイスクリプトだけを入れ直すときは RUNNER_TOKEN なしで実行すればよい
# （ランナーが登録済みなら 4. は飛ばす）。
set -euo pipefail

REPO_URL=https://github.com/kadmalcun/Medicine_Remind_DiscordBot
RUNNER_USER=gh-runner
RUNNER_DIR=/home/$RUNNER_USER/medicine-runner
RUNNER_NAME=palserver-medicine
RUNNER_LABEL=medicine-bot
DEPLOY_BIN=/usr/local/sbin/medicine-bot-deploy
DEPLOY_BIN_STAGING=/usr/local/sbin/medicine-bot-deploy-staging
SUDOERS=/etc/sudoers.d/medicine-bot
BOT_USER=paladmin
STAGING_DIR=/home/paladmin/bot/medicine_notice_bot_staging
STAGING_SERVICE=medicine_bot_staging.service

log()  { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "root で実行すること"
here=$(cd "$(dirname "$0")" && pwd)
id "$RUNNER_USER" >/dev/null 2>&1 || die "ユーザー $RUNNER_USER が存在しない"

# ----------------------------------------------------------------------------
step "デプロイスクリプトを設置 → $DEPLOY_BIN, $DEPLOY_BIN_STAGING"
# スクリプトは実行時の名前で配置先を切り替えるので、同じ中身を2つの名前で置く
install -o root -g root -m 755 "$here/medicine-bot-deploy" "$DEPLOY_BIN"
install -o root -g root -m 755 "$here/medicine-bot-deploy" "$DEPLOY_BIN_STAGING"
log "root:root 755"

# ----------------------------------------------------------------------------
step "sudoers を設定 → $SUDOERS"
tmp=$(mktemp)
printf '%s ALL=(root) NOPASSWD: %s, %s\n' "$RUNNER_USER" "$DEPLOY_BIN" "$DEPLOY_BIN_STAGING" > "$tmp"
visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "sudoers の構文エラー"; }
install -o root -g root -m 440 "$tmp" "$SUDOERS"
rm -f "$tmp"
log "$(cat "$SUDOERS")"

# ----------------------------------------------------------------------------
step "テスト用 Bot → $STAGING_DIR"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 775 "$STAGING_DIR"
if [[ ! -x $STAGING_DIR/venv/bin/python3 ]]; then
  # 依存パッケージは最初のデプロイで入る（requirements.txt が無い＝変更ありと判定されるため）
  sudo -u "$BOT_USER" python3 -m venv "$STAGING_DIR/venv"
  log "venv を作成"
fi
install -o root -g root -m 644 "$here/$STAGING_SERVICE" "/etc/systemd/system/$STAGING_SERVICE"
systemctl daemon-reload
systemctl enable "$STAGING_SERVICE" >/dev/null 2>&1
log "$STAGING_SERVICE を設置・有効化（起動は最初のデプロイで行う）"
if [[ ! -f $STAGING_DIR/.env ]]; then
  log "注意: $STAGING_DIR/.env がまだ無い。テスト用 Bot のトークンとチャンネルIDを書いておくこと"
fi

# ----------------------------------------------------------------------------
step "セルフホストランナー → $RUNNER_DIR"
if [[ -f $RUNNER_DIR/.runner ]]; then
  log "登録済みなので飛ばす"
else
  [[ -n ${RUNNER_TOKEN:-} ]] || die "RUNNER_TOKEN を指定すること"
  install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 755 "$RUNNER_DIR"
  if [[ ! -x $RUNNER_DIR/config.sh ]]; then
    version=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
                | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')
    log "runner v$version をダウンロード"
    curl -fsSL "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz" \
      | sudo -u "$RUNNER_USER" tar xz -C "$RUNNER_DIR"
  fi
  sudo -u "$RUNNER_USER" -H bash -c "cd '$RUNNER_DIR' && ./config.sh --unattended \
    --url '$REPO_URL' --token '$RUNNER_TOKEN' \
    --name '$RUNNER_NAME' --labels '$RUNNER_LABEL' --replace"
  (cd "$RUNNER_DIR" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
fi
(cd "$RUNNER_DIR" && ./svc.sh status | grep -E 'Active:' || true)

printf '\n設定完了\n'
