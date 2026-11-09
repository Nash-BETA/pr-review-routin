#!/bin/bash
# launchd に登録する（登録済みなら入れ替える）。plist は、このリポジトリを置いた場所を基準に作る。
#   bash bin/install.sh              登録する
#   bash bin/install.sh --uninstall  解除する
# ラベルは既定で local.pr-review-routine / local.pr-review-routine.server。変えるなら PR_REVIEW_LABEL を指定する
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${PR_REVIEW_LABEL:-local.pr-review-routine}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/pr-review-routine"
CONFIG="${PR_REVIEW_CONFIG:-$ROOT/config/env}"
DOMAIN="gui/$(id -u)"

unload() {
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
}

if [ "${1:-}" = "--uninstall" ]; then
  for label in "$LABEL" "$LABEL.server"; do
    unload "$label"
    rm -f "$AGENTS_DIR/$label.plist"
    echo "解除しました: $label"
  done
  exit 0
fi

if [ ! -f "$CONFIG" ]; then
  echo "設定ファイルがありません: $CONFIG" >&2
  echo "config/env.example を config/env に写して作ってください（README の「セットアップ」）" >&2
  exit 1
fi

PYTHON="${PYTHON:-$(command -v python3 || true)}"
CLAUDE="$(command -v claude || true)"
GH="$(command -v gh || true)"
for pair in "python3:$PYTHON" "claude:$CLAUDE" "gh:$GH"; do
  if [ -z "${pair#*:}" ]; then
    echo "${pair%%:*} が見つかりません。インストールしてから実行してください" >&2
    exit 1
  fi
done

# launchd は PATH をほぼ空で渡すため、claude と gh の場所を含む PATH を plist に書く
JOB_PATH="$(dirname "$CLAUDE"):$(dirname "$GH"):/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$AGENTS_DIR" "$LOG_DIR"

render() {
  sed -e "s|__LABEL__|$LABEL|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__ROOT__|$ROOT|g" \
      -e "s|__PATH__|$JOB_PATH|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      "$1" > "$2"
  plutil -lint -s "$2"
}

render "$ROOT/launchd/review.plist.template" "$AGENTS_DIR/$LABEL.plist"
render "$ROOT/launchd/server.plist.template" "$AGENTS_DIR/$LABEL.server.plist"

for label in "$LABEL" "$LABEL.server"; do
  unload "$label"
  launchctl bootstrap "$DOMAIN" "$AGENTS_DIR/$label.plist"
  echo "登録しました: $label"
done

PORT="$(sed -n 's/^PR_REVIEW_PORT=//p' "$CONFIG" | tail -1)"
echo "ページ: http://127.0.0.1:${PORT:-8765}/"
echo "ログ: $LOG_DIR"
