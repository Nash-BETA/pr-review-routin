#!/usr/bin/env python3
"""自分・所属チームにレビュー依頼が来ている PR と、自分が作成した PR を Claude にレビューさせ、結果をローカルの HTML に出す。

処理は 3 段に分かれる。
  1. 取得: 自分の gh で依頼中の PR と自分が作成したオープン中の PR を探し、本文・差分・head 時点のファイル一式を作業ディレクトリに展開する
  2. レビュー: claude -p を Read / Grep / Glob だけで動かし、結果を JSON で受け取る
  3. 生成: 保存した JSON から out/index.html を作り直す

GitHub に触れるのは 1 の gh だけで、gh api は常に GET で呼ぶ。PR に書き込む処理はどこにも無い。
2 の claude には GitHub の認証情報もツールも渡さないので、PR にコメントや approve をする手段を持たない。

設定はリポジトリの config/env（.gitignore で除外。例は config/env.example）。標準ライブラリだけで動く。
詳しくは README.md と docs/2026-11-06_SPEC.md を参照。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
# 設定ファイル。リポジトリの config/env に置き、.gitignore で除外している（組織や個人に固有の値を含むため）
CONFIG_FILE = Path(os.environ.get("PR_REVIEW_CONFIG", str(ROOT / "config" / "env"))).expanduser()


def load_config(path: Path) -> None:
    """KEY=VALUE 形式の設定ファイルを読み、環境変数に入れる。すでに環境変数にあるものはそちらを優先する。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def config_path(text: str) -> Path:
    """設定に書いたパスを解決する。~ は展開し、相対パスは設定ファイルのあるディレクトリからの位置とみなす。"""
    path = Path(text.strip()).expanduser()
    return path if path.is_absolute() else CONFIG_FILE.parent / path


def parse_range(text: str, default: range, *, inclusive: bool) -> range:
    """「1-5」「9-20」のような範囲を range にする。inclusive なら終わりも含める。"""
    try:
        start, end = (int(x) for x in text.split("-", 1))
        return range(start, end + 1 if inclusive else end)
    except ValueError:
        return default


load_config(CONFIG_FILE)

# 結果ページと記録。リポジトリの中に置くが .gitignore で除外している
DATA_DIR = ROOT / "data"
REVIEWS_DIR = DATA_DIR / "reviews"
LAST_RUN_FILE = DATA_DIR / "last_run.json"
LOCK_FILE = DATA_DIR / ".lock"
# 実行中の進み具合（どの PR をレビューしているか）。server.py が読んでページに出す。実行が終わったら消す
RUN_STATUS_FILE = DATA_DIR / "run_status.json"
QA_DATA_DIR = DATA_DIR / "qa"  # ページからの質問の会話（server.py が書く）
OUT_HTML = ROOT / "out" / "index.html"
# claude が中を読む作業ディレクトリ。macOS のプライバシー保護（TCC）の対象外の場所に置く。
# リポジトリを ~/Desktop などに置いても、claude に ~/Desktop を読む許可を与えずに済むようにするため
CACHE_DIR = Path(os.environ.get("PR_REVIEW_CACHE_DIR", "~/Library/Caches/pr-review-routine")).expanduser()
WORK_DIR = CACHE_DIR / "work"  # レビュー中だけ使う
QA_WORK_DIR = CACHE_DIR / "qa"  # 質問に答えるため（PR の push ごとに 1 つ）
LOG_DIR = Path(os.environ.get("PR_REVIEW_LOG_DIR", "~/Library/Logs/pr-review-routine")).expanduser()
PROMPT_FILE = ROOT / "prompts" / "review.md"
SCHEMA_FILE = ROOT / "schema" / "review.schema.json"
TEMPLATE_FILE = ROOT / "templates" / "page.html"

# 対象の GitHub organization（必須）
ORG = os.environ.get("PR_REVIEW_ORG", "")
MAX_PER_RUN = int(os.environ.get("PR_REVIEW_MAX_PER_RUN", "5"))
LARGE_DIFF_LINES = int(os.environ.get("PR_REVIEW_LARGE_DIFF_LINES", "3000"))
REVIEW_TIMEOUT_SEC = int(os.environ.get("PR_REVIEW_TIMEOUT_SEC", str(20 * 60)))
MODEL = os.environ.get("PR_REVIEW_MODEL", "")
EFFORT = os.environ.get("PR_REVIEW_EFFORT", "")
MAX_ATTEMPTS = 3
KEEP_REVIEW_VERSIONS = 5
# --again（ページのボタンでの PR 指定）を行わなかったときの終了コード。server.py がページに渡し、ページが案内を出す
EXIT_NOT_RUN = 75  # ほかの実行がロックを持っていた（EX_TEMPFAIL）
EXIT_NOT_TARGET = 66  # 取得し直したら、指定した PR がレビューの対象から外れていた（EX_NOINPUT）
KEEP_INACTIVE_DAYS = 30
# レビュー観点のファイル（「:」区切り）。毎回作業ディレクトリの rules/ にコピーして渡す
# （レビューする Claude は作業ディレクトリの外を読めないため）
RULE_FILES = [config_path(p) for p in os.environ.get("PR_REVIEW_RULE_FILES", "").split(":") if p.strip()]
# レビューの指示に付け足す、利用者ごとの指示（任意）
EXTRA_PROMPT_FILE = config_path(os.environ["PR_REVIEW_EXTRA_PROMPT"]) if os.environ.get("PR_REVIEW_EXTRA_PROMPT") else None
# 自分が作成したオープン中の PR もレビューするか（既定はする。draft も含める）
MY_PRS = os.environ.get("PR_REVIEW_MY_PRS", "1").strip().lower() not in ("0", "false", "no", "off")

# PR ごとの記録が、どの立場で見つけたものか
ROLE_REVIEWER = "reviewer"  # 自分か所属チームにレビュー依頼が来た PR
ROLE_AUTHOR = "author"  # 自分が作成した PR
ROLE_MANUAL = "manual"  # ページに URL を貼って、個別にレビューを頼んだ PR（依頼も来ておらず、自分の PR でもない）

# 個別にレビューを頼むときに受け付ける PR の書き方
PR_REF_PATTERNS = [
    # https://github.com/<org>/<repo>/pull/<番号>（後ろに /files や #... が付いていてもよい）
    re.compile(r"^https://github\.com/(?P<org>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+)/pull/(?P<number>[0-9]+)(?:[/?#].*)?$"),
    # <repo>#<番号> か <org>/<repo>#<番号>
    re.compile(r"^(?:(?P<org>[A-Za-z0-9-]+)/)?(?P<repo>[A-Za-z0-9._-]+)#(?P<number>[0-9]+)$"),
]

# 自動で動く曜日と時間帯（Mac のタイムゾーン）
#   曜日: 1 = 月曜 〜 7 = 日曜。「1-5」なら月〜金
#   時間帯: 「9-20」なら 9:00〜19:59
BUSINESS_DAYS = parse_range(os.environ.get("PR_REVIEW_DAYS", "1-5"), range(1, 6), inclusive=True)
BUSINESS_HOURS = parse_range(os.environ.get("PR_REVIEW_HOURS", "9-20"), range(9, 20), inclusive=False)

# レビュー用の作業ディレクトリに置く Claude Code の設定。
# 作業ディレクトリの外は permission-mode dontAsk で拒否されるが、念のため認証情報の置き場所も明示的に拒否する。
WORK_SETTINGS = {
    "permissions": {
        "deny": [
            "Read(~/.ssh/**)",
            "Read(~/.config/**)",
            "Read(~/.aws/**)",
            "Read(~/.claude/**)",
            "Read(~/.netrc)",
        ]
    }
}


class StepError(Exception):
    """1 件の PR の処理で起きた、次の回にやり直せる失敗。"""


# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, obj: object) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def gh_api(path: str, *, jq: str | None = None, paginate: bool = False,
           accept: str | None = None, params: dict[str, str] | None = None) -> bytes:
    """gh api を GET で呼ぶ。書き込み系のメソッドはこの関数からは呼べない。"""
    cmd = ["gh", "api", "-X", "GET"]
    if paginate:
        cmd.append("--paginate")
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    for key, value in (params or {}).items():
        cmd += ["-f", f"{key}={value}"]
    if jq:
        cmd += ["--jq", jq]
    cmd.append(path)
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise StepError(f"gh api {path} が失敗しました: {stderr[:500]}")
    return proc.stdout


def gh_json(path: str, **kwargs) -> object:
    return json.loads(gh_api(path, **kwargs))


def gh_json_lines(path: str, jq: str, **kwargs) -> list:
    """jq で 1 行 1 件の JSON（@json）を出させて読む。--paginate の結果を安全に連結するため。"""
    out = gh_api(path, jq=jq, **kwargs).decode("utf-8")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 保存データ
# ---------------------------------------------------------------------------


def record_path(repo: str, number: int) -> Path:
    return REVIEWS_DIR / f"{repo}__{number}.json"


def load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def all_records() -> list[tuple[Path, dict]]:
    return [(path, load_record(path)) for path in sorted(REVIEWS_DIR.glob("*.json"))]


def role(rec: dict) -> str:
    """記録の立場。role を持たない記録は、自分の PR を扱う前の版で作った、依頼された PR の記録。"""
    return rec.get("role", ROLE_REVIEWER)


def parse_pr_ref(text: str) -> tuple[str, int]:
    """PR の URL か <repo>#<番号> を (repo, 番号) にする。設定した organization の PR でなければ ValueError。"""
    text = text.strip()
    for pattern in PR_REF_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        org, repo, number = match.group("org"), match.group("repo"), int(match.group("number"))
        if org and org.lower() != ORG.lower():
            raise ValueError(f"{ORG} の PR ではありません（{org}）。対象は設定した organization の PR だけです")
        if repo in (".", "..") or number <= 0:
            break
        return repo, number
    raise ValueError(f"PR の URL（https://github.com/{ORG}/<リポジトリ>/pull/<番号>）を入力してください")


def rerequest_pending(rec: dict) -> bool:
    """再依頼されて、まだレビューし直していないか。自分の PR には再依頼が無い。"""
    return role(rec) == ROLE_REVIEWER and bool(rec.get("rerequest_pending"))


def needs_review(rec: dict) -> bool:
    if not rec.get("active"):
        return False
    sha = rec["head_sha"]
    if rec.get("attempts", {}).get(sha, 0) >= MAX_ATTEMPTS:
        return False
    if role(rec) == ROLE_MANUAL:
        # 個別に頼んだ PR は、最初の 1 回だけ自動でレビューする。新しい push は、ページのボタンで頼まれたときだけ（利用枠を勝手に使わない）
        return not rec.get("reviews")
    # 再依頼されたら、push が無くても自分の指摘への対応状況を出すためにレビューし直す
    if rerequest_pending(rec):
        return True
    return not any(r["sha"] == sha for r in rec.get("reviews", []))


# ---------------------------------------------------------------------------
# 1. 取得
# ---------------------------------------------------------------------------


def my_login() -> str:
    return gh_api("user", jq=".login").decode("utf-8").strip()


def my_teams() -> list[str]:
    jq = f'.[] | select(.organization.login == "{ORG}") | .slug | @json'
    return gh_json_lines("user/teams", jq, paginate=True)


def search_prs(query: str) -> list[tuple[str, int]]:
    """PR を検索し、(リポジトリ名, 番号) の一覧を返す。"""
    items = gh_json_lines(
        "search/issues",
        '.items[] | {repo: (.repository_url | split("/") | .[-1]), number} | @json',
        paginate=True,
        params={"q": query, "per_page": "100"},
    )
    return [(item["repo"], int(item["number"])) for item in items]


def new_record(repo: str, number: int, rec_role: str) -> dict:
    return {
        "repo": repo,
        "number": number,
        "role": rec_role,
        "first_seen_at": now_iso(),
        "requested_at": now_iso(),
        "rerequest_count": 0,
        "reviews": [],
        "attempts": {},
    }


def apply_pr(rec: dict, pr: dict) -> None:
    """GitHub から取った PR の情報を記録に入れ、有効にする。"""
    rec.update({
        "url": pr["html_url"],
        "title": pr["title"],
        "author": pr["user"]["login"],
        "base": pr["base"]["ref"],
        "base_sha": pr["base"]["sha"],
        "head": pr["head"]["ref"],
        "head_sha": pr["head"]["sha"],
        "draft": bool(pr.get("draft")),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "body": pr.get("body") or "",
        "active": True,
        "last_seen_at": now_iso(),
    })
    rec.pop("inactive_since", None)
    # 今の head 以外の試行回数は不要
    rec["attempts"] = {k: v for k, v in rec.get("attempts", {}).items() if k == rec["head_sha"]}


def sync_requests(login: str, teams: list[str], seen: set[str]) -> int:
    """依頼中の PR を探して保存データを最新にする。見つけた記録のファイル名を seen に足す。"""
    # 作成者はレビュー依頼の対象にならないが、念のため自分の PR を除き、「自分の PR」と重ならないようにする
    found = search_prs(f"is:pr is:open org:{ORG} review-requested:{login} -author:{login}")

    for repo, number in found:
        pr = gh_json(f"repos/{ORG}/{repo}/pulls/{number}")
        requested = gh_json(f"repos/{ORG}/{repo}/pulls/{number}/requested_reviewers")

        via = []
        if any(u["login"].lower() == login.lower() for u in requested.get("users", [])):
            via.append("自分")
        via += [t["slug"] for t in requested.get("teams", []) if t["slug"] in teams]

        # 自分が最後にレビューを出した時刻。レビューを出すと GitHub は依頼を外す（チーム経由の依頼も外れる）ので、
        # それより後に依頼中として見えていれば再依頼されたと分かる
        reviews = gh_json_lines(
            f"repos/{ORG}/{repo}/pulls/{number}/reviews",
            '.[] | {user: .user.login, submitted_at} | @json',
            paginate=True,
            params={"per_page": "100"},
        )
        mine = [r["submitted_at"] for r in reviews if (r.get("user") or "").lower() == login.lower() and r.get("submitted_at")]
        my_last_review_at = max(mine) if mine else None

        path = record_path(repo, number)
        existed = path.exists()
        rec = load_record(path) if existed else new_record(repo, number, ROLE_REVIEWER)
        if existed and role(rec) == ROLE_MANUAL:
            # 個別に頼んでいた PR に、正式に依頼が来た。「確認済み」を外して目に入るようにする
            rec["requested_at"] = now_iso()
        rec["role"] = ROLE_REVIEWER
        # my_last_review_at を持たない古い記録は、初めて見た扱いにする（再依頼とは判定しない）
        known = existed and "my_last_review_at" in rec
        was_inactive = existed and not rec.get("active", True)
        # 検索結果は少し遅れて更新されるため、レビューを出した直後に古い結果を拾うことがある。
        # 依頼先（requested_reviewers）に実際に自分かチームが入っているときだけ再依頼と判定する
        reviewed_since = (known and bool(via) and my_last_review_at is not None
                          and my_last_review_at != rec.get("my_last_review_at"))
        if was_inactive or reviewed_since:
            rec["requested_at"] = now_iso()
            rec["rerequest_count"] = rec.get("rerequest_count", 0) + 1
            rec["rerequest_pending"] = True
            rec["attempts"] = {}
            log(f"再依頼を検知: {repo}#{number}")
        # 古い検索結果を拾ったとき（依頼先が空）は更新しない。更新すると、その後の本当の再依頼を見逃す
        if via or not known:
            rec["my_last_review_at"] = my_last_review_at
        rec["requested_via"] = via or ["不明"]
        apply_pr(rec, pr)
        write_json(path, rec)
        seen.add(path.name)

    return len(found)


def sync_my_prs(login: str, seen: set[str]) -> int:
    """自分が作成したオープン中の PR（draft も含む）を探して保存データを最新にする。見つけた記録のファイル名を seen に足す。

    依頼された PR と違い、再依頼は無い。head が変わったらレビューし直す。
    """
    found = search_prs(f"is:pr is:open org:{ORG} author:{login}")
    for repo, number in found:
        path = record_path(repo, number)
        if path.name in seen:  # 依頼された PR として扱ったものは重ねない（検索の時点で除いているので、通常は起きない）
            continue
        pr = gh_json(f"repos/{ORG}/{repo}/pulls/{number}")
        rec = load_record(path) if path.exists() else new_record(repo, number, ROLE_AUTHOR)
        rec["role"] = ROLE_AUTHOR
        rec["requested_via"] = []
        apply_pr(rec, pr)
        write_json(path, rec)
        seen.add(path.name)
    return len(found)


def sync_manual(seen: set[str]) -> int:
    """個別に頼んだ PR を GitHub から取り直す。オープン中のものを seen に足し、その件数を返す。

    クローズ・マージされたものは seen に足さないので、deactivate_unseen が inactive にする。
    依頼された PR・自分の PR として見つかったものは、そちらの扱いになっている（seen に入っている）ので飛ばす。
    """
    count = 0
    for path, rec in all_records():
        if path.name in seen or role(rec) != ROLE_MANUAL or not rec.get("active"):
            continue
        try:
            pr = gh_json(f"repos/{ORG}/{rec['repo']}/pulls/{rec['number']}")
        except StepError as e:
            # 取れなかった（読む権限が外れた・一時的な失敗など）。この回は今の状態のまま残し、ほかの PR の処理は続ける
            log(f"個別に頼んだ PR を取れませんでした: {rec['repo']}#{rec['number']}: {e}")
            seen.add(path.name)
            continue
        if pr.get("state") != "open":
            continue
        apply_pr(rec, pr)
        write_json(path, rec)
        seen.add(path.name)
        count += 1
    return count


def add_manual(repo: str, number: int) -> str | None:
    """URL を貼って頼まれた PR を記録に足し、その PR を <repo>#<番号> で返す。
    見つからない・オープン中でないときは足さずに None を返す。

    依頼された PR・自分の PR としてすでに一覧にあるものは、その扱いのまま（立場を変えない）。
    依頼が外れた PR やクローズした PR の記録があれば、個別に頼んだ PR として有効にし直す。
    """
    try:
        pr = gh_json(f"repos/{ORG}/{repo}/pulls/{number}")
    except StepError as e:
        if "HTTP 404" not in str(e):
            raise
        # 番号の誤り・Issue の番号・読む権限が無いなど。実行の失敗にはしない（ページ上部に失敗が出続けるため）
        log(f"{repo}#{number} は見つかりませんでした（番号の誤り・Issue の番号・読む権限が無いなど）")
        return None
    if pr.get("state") != "open":
        log(f"{repo}#{number} はオープン中ではないため、個別レビューに足しません（{pr.get('state')}）")
        return None
    # GitHub はリポジトリ名の大文字小文字を区別しない。記録は検索結果と同じ正式な名前で持つ
    # （入力どおりの名前で持つと、後から依頼が来たときに検索結果と同じ PR だと分からなくなる）
    repo = pr["base"]["repo"]["name"]
    path = record_path(repo, number)
    rec = load_record(path) if path.exists() else new_record(repo, number, ROLE_MANUAL)
    if not rec.get("active"):
        # 依頼された PR だったときの再依頼の情報は引き継がない（個別レビューのカードに「再依頼」が出ないように）
        rec.update({"role": ROLE_MANUAL, "requested_via": [], "requested_at": now_iso(),
                    "rerequest_count": 0, "rerequest_pending": False})
    apply_pr(rec, pr)
    write_json(path, rec)
    log(f"個別レビューに足しました: {repo}#{number}（{role(rec)}）")
    return f"{repo}#{number}"


def deactivate_unseen(seen: set[str]) -> None:
    """今回見つからなかった PR（依頼が外れた・クローズした）を inactive にし、外れてから日が経ったものは消す。"""
    limit = now() - dt.timedelta(days=KEEP_INACTIVE_DAYS)
    for path, rec in all_records():
        if path.name in seen:
            continue
        if rec.get("active"):
            rec["active"] = False
            rec["inactive_since"] = now_iso()
            write_json(path, rec)
        elif dt.datetime.fromisoformat(rec.get("inactive_since", now_iso())) < limit:
            path.unlink()
            (QA_DATA_DIR / path.name).unlink(missing_ok=True)


def sync(login: str, teams: list[str]) -> tuple[int, int, int]:
    """依頼中の PR・自分の PR・個別に頼んだ PR を取り直して保存データを最新にする。それぞれの件数を返す。

    検索が途中で失敗したときは StepError で抜けるので、見つからなかった PR を inactive にする処理まで進まない。
    """
    seen: set[str] = set()
    requested = sync_requests(login, teams, seen)
    mine = sync_my_prs(login, seen) if MY_PRS else 0
    manual = sync_manual(seen)
    deactivate_unseen(seen)
    return requested, mine, manual


def qa_workdir(repo: str, number: int, sha: str) -> Path:
    return QA_WORK_DIR / f"{repo}__{number}__{sha[:12]}"


def cleanup_qa_workdirs() -> None:
    """依頼が外れた PR や、古い push の質問用作業ディレクトリを消す（会話の記録は data/qa に残る）。"""
    if not QA_WORK_DIR.exists():
        return
    keep = {qa_workdir(r["repo"], r["number"], r["head_sha"]).name for _, r in all_records() if r.get("active")}
    for d in QA_WORK_DIR.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def fetch_diff(repo: str, number: int, base_sha: str, head_sha: str) -> str:
    """ベースとの分岐点から head までの差分を取る。大きすぎて diff 形式で返らないときはファイル単位で組み立てる。"""
    accept = "application/vnd.github.diff"
    try:
        return gh_api(f"repos/{ORG}/{repo}/compare/{base_sha}...{head_sha}", accept=accept).decode("utf-8", "replace")
    except StepError as compare_error:
        log(f"  compare の diff を取れなかったため、ファイル単位の差分に切り替えます: {compare_error}")
    patches = gh_json_lines(
        f"repos/{ORG}/{repo}/pulls/{number}/files",
        '.[] | {filename, status, patch} | @json',
        paginate=True,
        params={"per_page": "100"},
    )
    chunks = []
    for f in patches:
        patch = f.get("patch") or "（差分が大きいかバイナリのため省略されています）"
        chunks.append(f"### {f['filename']} ({f['status']})\n{patch}\n")
    return "\n".join(chunks)


def clip(text: str | None, limit: int = 3000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…（以下省略）"


def fetch_my_comments(repo: str, number: int, login: str, author: str) -> dict | None:
    """自分がこの PR に書いたレビュー・コメントと、それへの返信を集める。何も書いていなければ None。"""
    me = login.lower()
    reviews = gh_json_lines(
        f"repos/{ORG}/{repo}/pulls/{number}/reviews",
        '.[] | {user: .user.login, state, body, submitted_at, commit: ((.commit_id // "")[0:7])} | @json',
        paginate=True,
        params={"per_page": "100"},
    )
    my_reviews = [r for r in reviews if (r.get("user") or "").lower() == me]

    comments = gh_json_lines(
        f"repos/{ORG}/{repo}/pulls/{number}/comments",
        '.[] | {id, in_reply_to_id, user: .user.login, body, path, line, original_line, created_at} | @json',
        paginate=True,
        params={"per_page": "100"},
    )
    roots = [c for c in comments if not c.get("in_reply_to_id") and (c.get("user") or "").lower() == me]
    threads = []
    for root in sorted(roots, key=lambda c: c["created_at"])[:50]:
        replies = sorted((c for c in comments if c.get("in_reply_to_id") == root["id"]), key=lambda c: c["created_at"])
        threads.append({
            "path": root["path"],
            "line": root["line"],
            "original_line": root["original_line"],
            # 今の差分に該当行が無い（その後の push で変わった）
            "outdated": root["line"] is None,
            "created_at": root["created_at"],
            "comment": clip(root["body"]),
            "replies": [{"author": c["user"], "created_at": c["created_at"], "body": clip(c["body"])} for c in replies],
        })

    if not my_reviews and not threads:
        return None

    first_at = min([r["submitted_at"] for r in my_reviews if r.get("submitted_at")] + [t["created_at"] for t in threads])
    issue_comments = gh_json_lines(
        f"repos/{ORG}/{repo}/issues/{number}/comments",
        '.[] | {user: .user.login, body, created_at} | @json',
        paginate=True,
        params={"per_page": "100"},
    )
    conversation = [
        {"author": c["user"], "created_at": c["created_at"], "body": clip(c["body"])}
        for c in issue_comments
        if c["created_at"] >= first_at and (c.get("user") or "").lower() in (me, author.lower())
    ][-20:]

    return {
        "reviewer": login,
        "reviews": [
            {"state": r["state"], "submitted_at": r["submitted_at"], "commit": r["commit"], "body": clip(r.get("body"))}
            for r in my_reviews
        ],
        "threads": threads,
        "conversation": conversation,
    }


def extract_tarball(repo: str, sha: str, dest: Path) -> None:
    """head 時点のリポジトリ一式を GitHub の tarball から展開する。手元の clone には触れない。"""
    archive = dest.parent / "src.tar.gz"
    with archive.open("wb") as fh:
        proc = subprocess.run(
            ["gh", "api", "-X", "GET", f"repos/{ORG}/{repo}/tarball/{sha}"],
            stdout=fh, stderr=subprocess.PIPE, timeout=600,
        )
    if proc.returncode != 0:
        raise StepError(f"tarball の取得に失敗しました: {proc.stderr.decode('utf-8', 'replace')[:500]}")

    staging = dest.parent / "_tar"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging, filter="data")
    tops = [p for p in staging.iterdir()]
    if len(tops) != 1 or not tops[0].is_dir():
        raise StepError("tarball の構成が想定と違います（最上位のディレクトリが 1 つではありません）")
    tops[0].rename(dest)
    staging.rmdir()
    archive.unlink()


def prepare_workdir(work: Path, rec: dict, login: str, *, rerun: bool = False) -> int:
    """レビュー用の作業ディレクトリを作る。差分の行数を返す。rerun は review_one と同じ。"""
    repo, number = rec["repo"], rec["number"]
    base_sha, head_sha = rec["base_sha"], rec["head_sha"]

    write_json(work / ".claude" / "settings.json", WORK_SETTINGS)

    pr_dir = work / "pr"
    write_json(pr_dir / "meta.json", {
        "repository": f"{ORG}/{repo}",
        "number": number,
        "url": rec["url"],
        "title": rec["title"],
        "author": rec["author"],
        "base_branch": rec["base"],
        "head_branch": rec["head"],
        "head_sha": head_sha,
        "draft": rec["draft"],
        "additions": rec["additions"],
        "deletions": rec["deletions"],
        "changed_files": rec["changed_files"],
        "role": role(rec),
        "requested_via": rec["requested_via"],
    })
    write_atomic(pr_dir / "body.md", rec["body"] or "（本文なし）")

    compare = gh_json(
        f"repos/{ORG}/{repo}/compare/{base_sha}...{head_sha}",
        jq='{commits: [.commits[] | {sha: .sha[0:7], message: .commit.message}], '
           'files: [.files[]? | {filename, status, additions, deletions}]}',
    )
    write_json(pr_dir / "commits.json", compare["commits"])
    write_json(pr_dir / "files.json", compare["files"])

    diff = fetch_diff(repo, number, base_sha, head_sha)
    write_atomic(pr_dir / "diff.patch", diff)

    previous = rec.get("reviews", [])
    if rerun:
        # 同じ push をレビューし直すときは、その push の前回の結果を渡さない（引きずられず、新しい目で見てもらうため）。
        # 前の push のレビューがあれば、それを「前回」として渡す
        previous = [r for r in previous if r["sha"] != head_sha]
    if previous:
        write_json(pr_dir / "previous_review.json", {"sha": previous[-1]["sha"], "result": previous[-1]["result"]})

    # 自分の指摘への対応状況は、ほかの人の PR（依頼された PR・個別に頼んだ PR）だけで出す
    # （自分の PR に自分が書くのは、レビュアーへの返信のため）
    if role(rec) != ROLE_AUTHOR:
        my_comments = fetch_my_comments(repo, number, login, rec["author"])
        if my_comments:
            write_json(pr_dir / "my_review_comments.json", my_comments)

    rules_dir = work / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    for i, rule in enumerate(RULE_FILES, 1):
        if not rule.exists():
            log(f"  レビュー観点のファイルが見つかりません: {rule}")
            continue
        dest = rules_dir / rule.name
        if dest.exists():  # 別の場所に同じ名前のファイルがあっても上書きしない
            dest = rules_dir / f"{i}_{rule.name}"
        shutil.copy(rule, dest)

    extract_tarball(repo, head_sha, work / "src")
    return diff.count("\n")


# ---------------------------------------------------------------------------
# 2. レビュー
# ---------------------------------------------------------------------------


def extra_prompt() -> str:
    """利用者ごとの追加の指示（PR_REVIEW_EXTRA_PROMPT）。無ければ空文字。"""
    if EXTRA_PROMPT_FILE is None:
        return ""
    if not EXTRA_PROMPT_FILE.exists():
        log(f"  追加の指示のファイルが見つかりません: {EXTRA_PROMPT_FILE}")
        return ""
    return "## 利用者ごとの追加の指示\n\n" + EXTRA_PROMPT_FILE.read_text(encoding="utf-8").strip()


# 立場ごとの指示。{{ROLE}} はレビューの依頼の冒頭に、{{ROLE_RULES}} は「書き方」の末尾に入る
ROLE_PROMPTS = {
    ROLE_REVIEWER: {
        "intro": "あなたは {{LOGIN}} の代わりに、{{LOGIN}} にレビュー依頼が来た 1 件の Pull Request を下読みします。",
        "rules": "",
    },
    ROLE_MANUAL: {
        # レビュー依頼は来ていないが、本人が読みたくて頼んだ PR。書き方は依頼された PR と同じにする
        "intro": "あなたは {{LOGIN}} の代わりに、{{LOGIN}} が個別に頼んだ 1 件の Pull Request を下読みします（{{LOGIN}} にレビュー依頼は来ていません）。",
        "rules": "",
    },
    ROLE_AUTHOR: {
        "intro": (
            "あなたは、{{LOGIN}} が自分で作成した 1 件の Pull Request をレビューします。"
            "レビュアーに指摘されそうな点を、レビュアーより先に本人が見つけて直すためのセルフチェックです。"
        ),
        "rules": (
            "### 自分が作成した PR のとき\n\n"
            "`pr/meta.json` の `role` が `author` なので、上の書き方のうち次の点を変える。\n\n"
            "- `suggestion`: 本人がどう直すかを 1 文で書く。提案形式・質問形式にしなくてよい。\n"
            "- `q`: レビュアーに意図を聞かれそうな箇所に使う。`suggestion` には、PR 本文やコードコメントに書いておくと良い説明を書く。\n"
            "- `check_points`: レビュー依頼の前に本人が確かめておくこと（動作確認・PR 本文の不足など）を書く。\n"
            "- `my_comments_status`: 空配列にする。"
        ),
    },
}


def build_prompt(mode: str, diff_lines: int, login: str, rec_role: str) -> str:
    if mode == "summary":
        depth = (
            "## レビューの深さ\n\n"
            f"差分が {diff_lines} 行と大きいので、全行を精読しなくてよいです。"
            "変更の全体像（`summary_points`）、リスク、人間が重点的に見るべき箇所（`check_points`）を優先し、"
            "`findings` は目についた `must` と `q` を中心にしてください。"
        )
    else:
        depth = "## レビューの深さ\n\n差分をすべて読み、上の観点で網羅的にレビューしてください。"
    texts = ROLE_PROMPTS[rec_role]
    return (PROMPT_FILE.read_text(encoding="utf-8")
            .replace("{{MODE}}", depth)
            .replace("{{ROLE}}", texts["intro"])
            .replace("{{ROLE_RULES}}", texts["rules"])
            .replace("{{LOGIN}}", login)
            .replace("{{EXTRA}}", extra_prompt()))


def run_claude(work: Path, prompt: str) -> tuple[dict, dict]:
    schema = json.dumps(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")), ensure_ascii=False)
    cmd = [
        "claude", "-p", prompt,
        # 使えるツールをファイルの読み取りだけにする（Bash・Edit・WebFetch などは存在しない）
        "--tools", "Read,Grep,Glob",
        # MCP サーバー・コネクタを使わせない
        "--strict-mcp-config",
        # ~/.claude/settings.json の許可設定・hooks を持ち込まず、作業ディレクトリの設定だけを読む
        "--setting-sources", "project",
        # 事前に許可されていない操作（作業ディレクトリ外の読み取りなど）は確認なしで拒否する
        "--permission-mode", "dontAsk",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", schema,
        "--max-turns", "150",
    ]
    if MODEL:
        cmd += ["--model", MODEL]
    if EFFORT:
        cmd += ["--effort", EFFORT]

    try:
        proc = subprocess.run(cmd, cwd=work, capture_output=True, timeout=REVIEW_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise StepError(f"claude が {REVIEW_TIMEOUT_SEC} 秒以内に終わりませんでした") from e

    stdout = proc.stdout.decode("utf-8", "replace")
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError as e:
        stderr = proc.stderr.decode("utf-8", "replace")
        raise StepError(f"claude の出力を JSON として読めません (exit={proc.returncode}): {stderr[:300]} {stdout[:300]}") from e

    if proc.returncode != 0 or out.get("is_error") or out.get("subtype") != "success":
        raise StepError(f"claude が失敗しました (exit={proc.returncode}, subtype={out.get('subtype')}): {str(out.get('result'))[:500]}")

    result = out.get("structured_output")
    if not isinstance(result, dict) or "summary_points" not in result:
        raise StepError("claude の結果に structured_output がありません")

    denials = out.get("permission_denials") or []
    for d in denials:
        target = d.get("tool_input", {}).get("file_path") or d.get("tool_input", {}).get("path")
        log(f"  拒否された操作: {d.get('tool_name')} {target}")

    stats = {
        "num_turns": out.get("num_turns"),
        "duration_ms": out.get("duration_ms"),
        "models": sorted((out.get("modelUsage") or {}).keys()),
        "permission_denials": len(denials),
    }
    return result, stats


def review_one(path: Path, rec: dict, login: str, *, rerun: bool = False) -> bool:
    """1 件をレビューする。rerun は、レビュー済みの push をページのボタンでレビューし直すとき。"""
    repo, number, sha = rec["repo"], rec["number"], rec["head_sha"]
    work = WORK_DIR / f"{repo}__{number}__{sha[:12]}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    log(f"レビュー開始: {repo}#{number} ({sha[:7]}) {rec['title']}")

    try:
        diff_lines = prepare_workdir(work, rec, login, rerun=rerun)
        mode = "summary" if diff_lines > LARGE_DIFF_LINES else "full"
        result, stats = run_claude(work, build_prompt(mode, diff_lines, login, role(rec)))
    except (StepError, OSError, tarfile.TarError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        attempts = rec.setdefault("attempts", {})
        attempts[sha] = attempts.get(sha, 0) + 1
        rec["last_error"] = {"sha": sha, "at": now_iso(), "message": str(e)[:1000], "attempts": attempts[sha]}
        write_json(path, rec)
        log(f"レビュー失敗: {repo}#{number} ({attempts[sha]}/{MAX_ATTEMPTS} 回目): {e}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)

    review = {
        "sha": sha,
        "reviewed_at": now_iso(),
        "mode": mode,
        "diff_lines": diff_lines,
        "stats": stats,
        "result": result,
    }
    reviews = rec.get("reviews", [])
    if rerun:
        # ページは、これを見て「確認済み」を外す（同じ push なので SHA では見分けられない）
        review["rerun"] = True
        # 同じ push を何度レビューし直しても、前の push のレビューが履歴から押し出されないよう、
        # その push の以前のレビューし直し分は置き換える（最初のレビューは残す）
        reviews = [r for r in reviews if not (r["sha"] == sha and r.get("rerun"))]
    rec["reviews"] = reviews + [review]
    rec["reviews"] = rec["reviews"][-KEEP_REVIEW_VERSIONS:]
    rec["last_error"] = None
    rec["rerequest_pending"] = False
    rec["attempts"] = {}
    write_json(path, rec)
    log(f"レビュー完了: {repo}#{number} 指摘 {len(result.get('findings', []))} 件 / {stats['num_turns']} ターン")
    return True


# ---------------------------------------------------------------------------
# 3. 生成
# ---------------------------------------------------------------------------


def pr_status(rec: dict) -> str:
    if not rec.get("active"):
        return "inactive"
    sha = rec["head_sha"]
    reviews = rec.get("reviews", [])
    error = rec.get("last_error")
    # 個別に頼んだ PR の新しい push は、自動ではレビューしない（ボタンで頼む）。
    # ボタンでのレビューに失敗していても「次の回に再試行」にはならないので、この状態のまま出す（エラーはページがカードに出す）
    outdated = role(rec) == ROLE_MANUAL and bool(reviews) and reviews[-1]["sha"] != sha
    if error and error.get("sha") == sha and (rerequest_pending(rec) or not (reviews and reviews[-1]["sha"] == sha)):
        if outdated:
            return "outdated"
        return "failed" if rec.get("attempts", {}).get(sha, 0) >= MAX_ATTEMPTS else "retrying"
    if rerequest_pending(rec):
        return "pending_rerequest"
    if reviews and reviews[-1]["sha"] == sha:
        return "reviewed"
    if outdated:
        return "outdated"
    return "pending_update" if reviews else "pending"


def script_json(obj: object) -> str:
    """<script type="application/json"> に埋め込んでも閉じタグなどとして解釈されない JSON にする。"""
    text = json.dumps(obj, ensure_ascii=False)
    for src, dst in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(src, dst)
    return text


def render(login: str = "", teams: list[str] | None = None) -> None:
    prs = []
    for _, rec in all_records():
        reviews = rec.get("reviews", [])
        prs.append({
            "repo": rec["repo"],
            "number": rec["number"],
            "url": rec.get("url"),
            "title": rec.get("title"),
            "author": rec.get("author"),
            "base": rec.get("base"),
            "draft": rec.get("draft"),
            "additions": rec.get("additions"),
            "deletions": rec.get("deletions"),
            "changed_files": rec.get("changed_files"),
            "role": role(rec),
            "requested_via": rec.get("requested_via", []),
            "active": rec.get("active", False),
            "head_sha": rec.get("head_sha"),
            "first_seen_at": rec.get("first_seen_at"),
            "requested_at": rec.get("requested_at") or rec.get("first_seen_at"),
            "rerequest_count": rec.get("rerequest_count", 0),
            "inactive_since": rec.get("inactive_since"),
            "status": pr_status(rec),
            "last_error": rec.get("last_error"),
            "latest": reviews[-1] if reviews else None,
            "older": list(reversed(reviews[:-1])),
        })

    last_run = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8")) if LAST_RUN_FILE.exists() else None
    data = {
        "generated_at": now_iso(),
        "org": ORG,
        "login": login,
        "teams": teams or [],
        "my_prs": MY_PRS,
        "max_attempts": MAX_ATTEMPTS,
        "last_run": last_run,
        "prs": prs,
    }
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    if "/*__DATA__*/null" not in template:
        raise RuntimeError("templates/page.html にデータの埋め込み位置（/*__DATA__*/null）がありません")
    write_atomic(OUT_HTML, template.replace("/*__DATA__*/null", script_json(data)))


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def in_business_hours(t: dt.datetime) -> bool:
    return t.isoweekday() in BUSINESS_DAYS and t.hour in BUSINESS_HOURS


def redirect_output_to_log(path: Path) -> None:
    """launchd から起動したときに、標準出力と標準エラーをログファイルへ向ける（子プロセスの出力も含む）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description="レビュー依頼が来た PR・自分が作成した PR・個別に頼んだ PR を Claude にレビューさせ、結果を HTML に出す")
    parser.add_argument("--force", action="store_true", help="曜日と時間帯（PR_REVIEW_DAYS / PR_REVIEW_HOURS）の判定を飛ばす")
    parser.add_argument("--dry-run", action="store_true", help="対象の PR を表示するだけで、レビューしない")
    parser.add_argument("--render-only", action="store_true", help="保存済みの結果から HTML だけ作り直す")
    parser.add_argument("--limit", type=int, default=MAX_PER_RUN, help=f"1 回でレビューする最大件数（既定 {MAX_PER_RUN}）")
    parser.add_argument("--only", help="この PR だけレビューする（例: my-repo#123）")
    parser.add_argument("--again", action="store_true",
                        help="--only と一緒に使う。レビュー済みの PR や、失敗の上限に達した PR もレビューし直す（ページの「今すぐレビュー」）")
    parser.add_argument("--add", metavar="PR",
                        help="依頼が来ていない PR も、個別にレビューする（PR の URL か my-repo#123）。--only PR --again と同じように今すぐレビューする")
    parser.add_argument("--launchd", action="store_true",
                        help="launchd（と server.py）から起動したとき用。出力を日付ごとのログファイルに書く")
    args = parser.parse_args()
    args.add_target = None
    if args.add:
        if args.only or args.dry_run or args.render_only:
            # --dry-run でも記録は足されてしまい、次の定期実行でレビューされる。--render-only では何もしない
            parser.error("--add は --only・--dry-run・--render-only と一緒に指定できません")
        try:
            args.add_target = parse_pr_ref(args.add)
        except ValueError as e:
            parser.error(str(e))
        args.only, args.again = f"{args.add_target[0]}#{args.add_target[1]}", True
    if args.again and not args.only:
        parser.error("--again は --only と一緒に指定してください")

    if args.launchd:
        redirect_output_to_log(LOG_DIR / f"{now():%Y%m%d}.log")
        for old in LOG_DIR.glob("2*.log"):  # 日付のログは 14 日で消す
            if old.stat().st_mtime < (now() - dt.timedelta(days=14)).timestamp():
                old.unlink(missing_ok=True)

    if not ORG:
        log(f"PR_REVIEW_ORG が設定されていません。{CONFIG_FILE} に書いてください（例は config/env.example）")
        return 2

    for d in (REVIEWS_DIR, WORK_DIR, OUT_HTML.parent):
        d.mkdir(parents=True, exist_ok=True)

    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("前回の実行が終わっていないため、今回は何もしません")
        # ページのボタンからの PR 指定（--again）は、定期実行と重なると行われない。
        # それをサーバー経由でページに伝えるため、失敗とは別の終了コードにする
        return EXIT_NOT_RUN if args.again else 0

    # 前回の実行が強制終了されていると、進み具合のファイルが残っている。ページに古い状況を出さないよう消す
    RUN_STATUS_FILE.unlink(missing_ok=True)

    if args.render_only:
        render()
        log(f"HTML を作り直しました: {OUT_HTML}")
        return 0

    if not args.force and not in_business_hours(now()):
        return 0

    # 前回の実行が途中で止まったときの作業ディレクトリを片付ける
    for leftover in WORK_DIR.iterdir():
        shutil.rmtree(leftover, ignore_errors=True)

    started = now_iso()
    try:
        return run_once(args, started)
    finally:
        RUN_STATUS_FILE.unlink(missing_ok=True)


def write_progress(started: str, only: str | None, **fields: object) -> None:
    """実行の進み具合を書く。定期実行・ページのボタンからの実行のどちらでも、server.py がこれをページに出す。"""
    write_json(RUN_STATUS_FILE, {"started_at": started, "only": only, **fields})


def run_once(args: argparse.Namespace, started: str) -> int:
    """1 回分の取得 → レビュー → 生成。ロックを取ったあとに main から呼ぶ。"""
    write_progress(started, args.only, phase="sync")
    login, teams = "", []
    reviewed = failed = 0
    try:
        login = my_login()
        teams = my_teams()
        requested, mine, manual = sync(login, teams)
        cleanup_qa_workdirs()
        if args.add_target:
            # 取り直したあとに足す。足した記録は正式なリポジトリ名で持つので、対象もその名前で探す
            # （見つからない・オープン中でなければ足さないので、下で対象が見つからず EXIT_NOT_TARGET になる）
            args.only = add_manual(*args.add_target) or args.only
        if args.only:
            # --again なら、レビュー済みの PR や、失敗の上限に達した PR もレビューし直す（ページの「今すぐレビュー」）
            pending = [(p, r) for p, r in all_records()
                       if f"{r['repo']}#{r['number']}" == args.only
                       and (needs_review(r) or (args.again and r.get("active")))]
            if not pending:
                log(f"{args.only} はレビューの対象ではありません（依頼が外れた・クローズした・レビュー済みなど）")
        else:
            pending = [(p, r) for p, r in all_records() if needs_review(r)]
        # 依頼された PR を先にレビューする（相手を待たせているため）。自分の PR と個別に頼んだ PR はその後
        pending.sort(key=lambda pr: (role(pr[1]) != ROLE_REVIEWER, pr[1].get("first_seen_at", ""), pr[1]["number"]))
        log(f"依頼中 {requested} 件 / {f'自分の PR {mine} 件' if MY_PRS else '自分の PR は対象外'} / 個別 {manual} 件"
            f" / レビュー待ち {len(pending)} 件")

        if args.dry_run:
            for _, rec in pending:
                where = {ROLE_AUTHOR: "自分の PR", ROLE_MANUAL: "個別"}.get(role(rec)) or ", ".join(rec["requested_via"])
                print(f"  {rec['repo']}#{rec['number']} {rec['head_sha'][:7]} {rec['title']} ({where})")
            render(login, teams)
            return 0

        targets = pending[: max(args.limit, 0)]
        for i, (path, rec) in enumerate(targets, 1):
            write_progress(started, args.only, phase="review", current=f"{rec['repo']}#{rec['number']}",
                           title=rec.get("title", ""), index=i, total=len(targets))
            rerun = False
            if args.again:
                # 失敗の上限に達していても、回数を数え直してレビューする
                rec["attempts"] = {}
                # 定期実行でもレビューする状態（レビュー待ち・再依頼・失敗）なら、定期実行と同じに扱う。
                # 定期実行では飛ばす、レビュー済みの push をやり直すときだけ「レビューし直し」にする
                # （個別に頼んだ PR の新しい push も定期実行では飛ばすが、push が違うのでレビューし直しではない）
                rerun = not needs_review(rec) and any(r["sha"] == rec["head_sha"] for r in rec.get("reviews", []))
            if review_one(path, rec, login, rerun=rerun):
                reviewed += 1
            else:
                failed += 1
            render(login, teams)  # 1 件ごとにページを更新する
    except StepError as e:
        write_json(LAST_RUN_FILE, {"started_at": started, "finished_at": now_iso(), "ok": False,
                                   "message": str(e)[:1000], "reviewed": reviewed, "failed": failed})
        render(login, teams)
        log(f"実行失敗: {e}")
        return 1

    write_json(LAST_RUN_FILE, {"started_at": started, "finished_at": now_iso(), "ok": True,
                               "message": "", "reviewed": reviewed, "failed": failed})
    render(login, teams)
    log(f"実行完了: レビュー {reviewed} 件 / 失敗 {failed} 件")
    # ボタンで指定した PR が、取得し直した結果レビューの対象から外れていた（依頼が外れた・クローズした）
    return EXIT_NOT_TARGET if args.again and not targets else 0


if __name__ == "__main__":
    sys.exit(main())
