# pr-review-routine

自分（または所属チーム）に GitHub のレビュー依頼が来た PR を、Mac 上の Claude Code が自動でレビューし、結果をローカルのページで見られるようにする仕組み。
ページから PR について Claude に質問もできる。**PR には一切書き込まない。**

仕様と設計の理由は [docs/2026-11-06_SPEC.md](docs/2026-11-06_SPEC.md) を参照。

## できること

- 15 分ごとに、自分宛て・チーム宛てのレビュー依頼を探してレビューする（曜日と時間帯は設定で変えられる）
- 新しい push や再依頼があれば、レビューし直す
- 自分が GitHub に書いた指摘が直ったかを、指摘ごとに出す
- 結果ページ（`http://127.0.0.1:8765/`）で、PR ごと・指摘ごとに Claude に質問できる

## 必要なもの

- macOS
- [Claude Code](https://code.claude.com/)（`claude` コマンド。ログイン済みであること）
- [GitHub CLI](https://cli.github.com/)（`gh` コマンド。`gh auth login` 済みであること）
- Python 3.12 以降（標準ライブラリだけで動く）

## PR に書き込まないための作り

- GitHub に触れるのは `lib/pr_review.py` の `gh api` だけで、常に `-X GET` で呼ぶ
- レビューと質問への回答は `claude -p` を次の制限で動かす。Claude は PR に書き込む手段を持たない
  - `--tools "Read,Grep,Glob"`: ファイルを読むツールしか持たない（Bash・Edit・WebFetch は存在しない）
  - `--strict-mcp-config`: MCP サーバー・コネクタを使わない
  - `--setting-sources project`: `~/.claude/settings.json` の許可設定や hooks を持ち込まない
  - `--permission-mode dontAsk`: 作業ディレクトリの外は読めない（拒否された操作はログに出る）
- 質問用サーバーは `127.0.0.1` にだけ bind し、起動ごとに作るトークンが付いた要求と、`Host` が `127.0.0.1` / `localhost` の要求だけを受け付ける

PR の本文やコードに悪意のある指示が入っていても、Claude には実行する手段が無いので、影響はレビュー文の内容にとどまる。

## セットアップ

```bash
# 1) 設定ファイルを作り、PR_REVIEW_ORG などを書く（config/env は .gitignore で除外している）
cp config/env.example config/env
chmod 600 config/env
vi config/env

# 2) launchd に登録する（定期レビューと質問用サーバー）
bash bin/install.sh
```

### リポジトリを `~/Desktop`・`~/Documents`・`~/Downloads` に置く場合

これらは macOS のプライバシー保護（TCC）の対象で、launchd から起動したプロセスは、許可が無いと読めない。

- launchd からは python3 を直接起動する（bash は通さない）。**python3 に、そのフォルダを読む許可が要る**
  - 「システム設定 → プライバシーとセキュリティ → ファイルとフォルダ」で Python に許可する。一覧に無ければ「フルディスクアクセス」に追加する
- Claude が中を読む作業ディレクトリは `~/Library/Caches/pr-review-routine` に置くので、`claude` への許可は要らない
- それ以外の場所（例: `~/src`）に置けば、許可は要らない

許可が無いと、launchd のジョブは何も言わずに止まる。`~/Library/Logs/pr-review-routine/launchd.err.log`（サーバーは `server.launchd.err.log`）に `Operation not permitted` が出ていないかを見る。
**Homebrew などで Python を上げると、許可が外れることがある。** 上げたあとはログを確認し、外れていたら許可を付け直す。

## 動作確認

`--force` で曜日と時間帯の判定を飛ばす。ターミナルから動かす分は、launchd に登録する前でも試せる。

```bash
# 対象の PR を確かめる（レビューはしない）
python3 lib/pr_review.py --dry-run --force

# 1 件だけ今すぐレビューする
python3 lib/pr_review.py --force --limit 1

# ページを開く（登録後はサーバー経由で開くと質問もできる）
open http://127.0.0.1:8765/
```

ターミナルからの実行はターミナルの許可で動くので、上の TCC の問題は見つけられない。登録後に launchd 経由でも動くかを確かめる。

```bash
# レビューのジョブを今すぐ動かし、ログを見る
launchctl kickstart -p gui/$(id -u)/local.pr-review-routine
tail ~/Library/Logs/pr-review-routine/$(date +%Y%m%d).log
tail ~/Library/Logs/pr-review-routine/launchd.err.log   # Operation not permitted が出ていないか
```

## 見方

```bash
open http://127.0.0.1:8765/   # サーバー経由（Claude に質問できる）
open out/index.html           # ファイルを直接開く（読むだけ。サーバーが止まっていても見られる）
```

- 未確認の PR が上に並ぶ。「確認済み」にチェックすると下に回る（チェックはブラウザに保存）
- 新しい push や再依頼があると、自動でレビューし直され、確認済みのチェックも外れる
- 自分が GitHub に指摘を書いた PR には「自分の指摘への対応状況」の欄が出る
- URL に `?all` を付けると「依頼が外れた PR」も表示し、`#qa` を付けると質問欄をすべて開く

### Claude に質問する

- 各 PR のカードの下にある「Claude に質問」を開いて質問する（⌘ + Enter で送信）
- 指摘の「これについて聞く」を押すと、その指摘を引用した状態で質問欄に入る
- 答えは書きながら表示される。10〜60 秒ほどかかる
- 会話は push ごとに 1 つ。同じ push の間は前のやりとりを覚えている
- コメントの文面を頼んだら、GitHub に貼るのは自分で行う

レビューと質問は、どちらもサブスクリプションの利用枠を使う。

## 設定

`config/env` に書く。項目は [config/env.example](config/env.example) を参照。パスは `~` から書くか、`config/` からの相対パスで書く。
`config/` の中は `env.example` 以外を `.gitignore` で除外しているので、自分用の追加の指示などもここに置ける。主な項目は次のとおり。

| 項目 | 内容 |
|---|---|
| `PR_REVIEW_ORG` | 対象の GitHub organization（必須） |
| `PR_REVIEW_RULE_FILES` | レビュー観点のファイル（`:` 区切り）。毎回コピーして Claude に渡す |
| `PR_REVIEW_EXTRA_PROMPT` | レビューの指示に付け足す、自分用の指示のファイル。観点ファイルの優先順位などを書く |
| `PR_REVIEW_DAYS` / `PR_REVIEW_HOURS` | 自動で動く曜日と時間帯（既定は月〜金の 9〜20 時） |

レビューの書き方（長さ・並び順・`[must]` / `[q]` / `[imo]` / `[nit]` の使い分け）は `prompts/review.md`、質問への答え方は `prompts/qa.md` で決めている。

## 構成

| パス | 内容 |
|---|---|
| `lib/pr_review.py` | 取得 → レビュー → ページ生成の本体 |
| `lib/server.py` | ページの配信と、ページからの質問への回答 |
| `bin/install.sh` | launchd への登録・解除 |
| `launchd/*.plist.template` | launchd の定義のテンプレート（`install.sh` が埋める） |
| `prompts/` | レビューと質問への回答の指示 |
| `schema/review.schema.json` | レビュー結果の JSON の形 |
| `templates/page.html` | 結果ページのテンプレート |
| `config/env.example` | 設定の例 |

実行中に作られるもの（リポジトリには入らない）:

| パス | 内容 |
|---|---|
| `config/env` など | 自分用の設定・追加の指示（`.gitignore` で除外） |
| `data/` | PR ごとの記録・レビュー結果・質問の会話（`.gitignore` で除外） |
| `out/index.html` | 結果ページ（`.gitignore` で除外） |
| `~/Library/Caches/pr-review-routine/` | Claude が読む作業ディレクトリ（PR の head 時点のリポジトリ一式を含む）。依頼が外れたら消す |
| `~/Library/Logs/pr-review-routine/` | ログ（日付ごと。14 日で消す） |

`config/` の設定には組織や個人に固有の値が、`data/` と `out/` には非公開のコードや PR の中身が入る。**コミットしない・共有しない。**

## 運用コマンド

```bash
# 特定の PR だけ今すぐレビューする
python3 lib/pr_review.py --force --only "my-repo#123"

# ページだけ作り直す（テンプレートを直したとき）
python3 lib/pr_review.py --render-only

# コードやプロンプトを直したあと、launchd のジョブを入れ替える
bash bin/install.sh

# 予定を待たずにレビューのジョブを動かす / サーバーを再起動する
launchctl kickstart -p gui/$(id -u)/local.pr-review-routine
launchctl kickstart -k gui/$(id -u)/local.pr-review-routine.server

# 解除する
bash bin/install.sh --uninstall

# ログ
ls -lt ~/Library/Logs/pr-review-routine/
```

同じ head で 3 回失敗して止まった PR は、`data/reviews/<repo>__<番号>.json` の `attempts` を空（`{}`）にすると、次の回にやり直す。

## 注意

- **Mac が起きている間だけ動く。** スリープ中の分は、復帰後に 1 回だけ実行される
- 1 件のレビューは 1 分前後。大きな PR はもっとかかる
