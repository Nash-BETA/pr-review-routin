#!/usr/bin/env python3
"""自分・所属チームにレビュー依頼が来ている PR を Claude にレビューさせ、結果をローカルの HTML に出す。

処理は 3 段に分かれる。
  1. 取得: 自分の gh で依頼中の PR を探し、本文・差分・head 時点のファイル一式を作業ディレクトリに展開する
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
KEEP_INACTIVE_DAYS = 30
# レビュー観点のファイル（「:」区切り）。毎回作業ディレクトリの rules/ にコピーして渡す
# （レビューする Claude は作業ディレクトリの外を読めないため）
RULE_FILES = [config_path(p) for p in os.environ.get("PR_REVIEW_RULE_FILES", "").split(":") if p.strip()]
# レビューの指示に付け足す、利用者ごとの指示（任意）
EXTRA_PROMPT_FILE = config_path(os.environ["PR_REVIEW_EXTRA_PROMPT"]) if os.environ.get("PR_REVIEW_EXTRA_PROMPT") else None

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


def needs_review(rec: dict) -> bool:
    if not rec.get("active"):
        return False
    sha = rec["head_sha"]
    if rec.get("attempts", {}).get(sha, 0) >= MAX_ATTEMPTS:
        return False
    # 再依頼されたら、push が無くても自分の指摘への対応状況を出すためにレビューし直す
    if rec.get("rerequest_pending"):
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


def sync_requests(login: str, teams: list[str]) -> int:
    """依頼中の PR を探して保存データを最新にする。依頼が外れた PR は inactive にする。"""
    query = f"is:pr is:open org:{ORG} review-requested:{login}"
    items = gh_json_lines(
        "search/issues",
        '.items[] | {repo: (.repository_url | split("/") | .[-1]), number} | @json',
        paginate=True,
        params={"q": query, "per_page": "100"},
    )

    seen: set[str] = set()
    for item in items:
        repo, number = item["repo"], int(item["number"])
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
        rec = load_record(path) if existed else {
            "repo": repo,
            "number": number,
            "first_seen_at": now_iso(),
            "requested_at": now_iso(),
            "rerequest_count": 0,
            "reviews": [],
            "attempts": {},
        }
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
            "requested_via": via or ["不明"],
            "active": True,
            "last_seen_at": now_iso(),
        })
        rec.pop("inactive_since", None)
        # 今の head 以外の試行回数は不要
        rec["attempts"] = {k: v for k, v in rec.get("attempts", {}).items() if k == rec["head_sha"]}
        write_json(path, rec)
        seen.add(path.name)

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

    return len(items)


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


def prepare_workdir(work: Path, rec: dict, login: str) -> int:
    """レビュー用の作業ディレクトリを作る。差分の行数を返す。"""
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
    if previous:
        write_json(pr_dir / "previous_review.json", {"sha": previous[-1]["sha"], "result": previous[-1]["result"]})

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


def build_prompt(mode: str, diff_lines: int, login: str) -> str:
    if mode == "summary":
        depth = (
            "## レビューの深さ\n\n"
            f"差分が {diff_lines} 行と大きいので、全行を精読しなくてよいです。"
            "変更の全体像（`summary_points`）、リスク、人間が重点的に見るべき箇所（`check_points`）を優先し、"
            "`findings` は目についた `must` と `q` を中心にしてください。"
        )
    else:
        depth = "## レビューの深さ\n\n差分をすべて読み、上の観点で網羅的にレビューしてください。"
    return (PROMPT_FILE.read_text(encoding="utf-8")
            .replace("{{MODE}}", depth)
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


def review_one(path: Path, rec: dict, login: str) -> bool:
    repo, number, sha = rec["repo"], rec["number"], rec["head_sha"]
    work = WORK_DIR / f"{repo}__{number}__{sha[:12]}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    log(f"レビュー開始: {repo}#{number} ({sha[:7]}) {rec['title']}")

    try:
        diff_lines = prepare_workdir(work, rec, login)
        mode = "summary" if diff_lines > LARGE_DIFF_LINES else "full"
        result, stats = run_claude(work, build_prompt(mode, diff_lines, login))
    except (StepError, OSError, tarfile.TarError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        attempts = rec.setdefault("attempts", {})
        attempts[sha] = attempts.get(sha, 0) + 1
        rec["last_error"] = {"sha": sha, "at": now_iso(), "message": str(e)[:1000], "attempts": attempts[sha]}
        write_json(path, rec)
        log(f"レビュー失敗: {repo}#{number} ({attempts[sha]}/{MAX_ATTEMPTS} 回目): {e}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)

    rec.setdefault("reviews", []).append({
        "sha": sha,
        "reviewed_at": now_iso(),
        "mode": mode,
        "diff_lines": diff_lines,
        "stats": stats,
        "result": result,
    })
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
    if error and error.get("sha") == sha and (rec.get("rerequest_pending") or not (reviews and reviews[-1]["sha"] == sha)):
        return "failed" if rec.get("attempts", {}).get(sha, 0) >= MAX_ATTEMPTS else "retrying"
    if rec.get("rerequest_pending"):
        return "pending_rerequest"
    if reviews and reviews[-1]["sha"] == sha:
        return "reviewed"
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
    parser = argparse.ArgumentParser(description="レビュー依頼が来た PR を Claude にレビューさせ、結果を HTML に出す")
    parser.add_argument("--force", action="store_true", help="曜日と時間帯（PR_REVIEW_DAYS / PR_REVIEW_HOURS）の判定を飛ばす")
    parser.add_argument("--dry-run", action="store_true", help="対象の PR を表示するだけで、レビューしない")
    parser.add_argument("--render-only", action="store_true", help="保存済みの結果から HTML だけ作り直す")
    parser.add_argument("--limit", type=int, default=MAX_PER_RUN, help=f"1 回でレビューする最大件数（既定 {MAX_PER_RUN}）")
    parser.add_argument("--only", help="この PR だけレビューする（例: my-repo#123）")
    parser.add_argument("--launchd", action="store_true", help="launchd から起動したとき用。出力を日付ごとのログファイルに書く")
    args = parser.parse_args()

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
        return 0

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
    login, teams = "", []
    reviewed = failed = 0
    try:
        login = my_login()
        teams = my_teams()
        count = sync_requests(login, teams)
        cleanup_qa_workdirs()
        pending = [(p, r) for p, r in all_records() if needs_review(r)]
        if args.only:
            pending = [(p, r) for p, r in pending if f"{r['repo']}#{r['number']}" == args.only]
        pending.sort(key=lambda pr: (pr[1].get("first_seen_at", ""), pr[1]["number"]))
        log(f"依頼中 {count} 件 / レビュー待ち {len(pending)} 件")

        if args.dry_run:
            for _, rec in pending:
                print(f"  {rec['repo']}#{rec['number']} {rec['head_sha'][:7]} {rec['title']} ({', '.join(rec['requested_via'])})")
            render(login, teams)
            return 0

        for path, rec in pending[: max(args.limit, 0)]:
            if review_one(path, rec, login):
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
