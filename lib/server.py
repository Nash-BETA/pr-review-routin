#!/usr/bin/env python3
"""PR 自動レビューのページを配信し、ページからの質問に Claude が答えるローカルサーバー。

  GET  /          結果ページ（out/index.html）に、この起動の合言葉（トークン）を埋め込んで返す
  GET  /api/qa    保存済みの会話を返す
  POST /api/ask   質問を受け取り、Claude の答えを 1 行 1 件の JSON で流しながら返す
  GET  /api/run   レビューの実行状況（定期実行・ボタンからの実行のどちらでも）を返す
  POST /api/run   レビューを今すぐ実行する（pr_review.py を起動する）。PR を指定するとその PR だけを、URL なら一覧に無い PR も個別にレビューする
  POST /api/remove 個別にレビューした PR を一覧から外す

守り:
  - 127.0.0.1 にだけ bind する。ほかのマシンからは繋がらない
  - /api/* は、ページに埋め込んだトークンがヘッダーに付いているときだけ受け付ける。
    ほかのサイトのページはこのページの中身を読めないので、トークンを知り得ない
  - Host ヘッダーが 127.0.0.1 / localhost でなければ拒否する（DNS リバインディング対策）
  - 答える Claude は自動レビューと同じ制限で動かす（作業ディレクトリのファイルを読むだけ。
    GitHub・ネットワーク・Bash には触れない）。PR に書き込む手段は持たない

標準ライブラリだけで動く。詳しくは docs/2026-11-06_SPEC.md を参照。
"""

from __future__ import annotations

import fcntl
import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# pr_review を読み込むと config/env の値が環境変数に入る（すでにある環境変数が優先される）。
# ボタンから起動する pr_review.py には読み込む前の環境変数を渡し、サーバーを起動したあとに config/env を直しても効くようにする
BASE_ENV = dict(os.environ)
import pr_review as m  # noqa: E402

PORT = int(os.environ.get("PR_REVIEW_PORT", "8765"))
QA_PROMPT_FILE = m.ROOT / "prompts" / "qa.md"
TOKEN = secrets.token_urlsafe(32)
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MAX_QUESTION_CHARS = 4000
QA_TIMEOUT_SEC = 15 * 60

_login: str | None = None
_login_guard = threading.Lock()
_pr_locks: dict[str, threading.Lock] = {}
_pr_locks_guard = threading.Lock()
# 同時に答える数の上限（Mac への負荷と利用枠の消費を抑える）
_concurrency = threading.Semaphore(2)


def login() -> str:
    global _login
    with _login_guard:
        if _login is None:
            _login = m.my_login()
        return _login


def pr_lock(key: str) -> threading.Lock:
    with _pr_locks_guard:
        return _pr_locks.setdefault(key, threading.Lock())


# ---------------------------------------------------------------------------
# 会話の保存
# ---------------------------------------------------------------------------


def qa_path(repo: str, number: int) -> Path:
    return m.QA_DATA_DIR / f"{repo}__{number}.json"


def load_qa(repo: str, number: int) -> dict:
    path = qa_path(repo, number)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"repo": repo, "number": number, "conversations": []}


def all_conversations() -> dict[str, list]:
    out = {}
    for path in sorted(m.QA_DATA_DIR.glob("*.json")):
        qa = json.loads(path.read_text(encoding="utf-8"))
        out[f"{qa['repo']}#{qa['number']}"] = [
            {"sha": c["sha"], "started_at": c["started_at"], "messages": c["messages"]}
            for c in qa.get("conversations", [])
        ]
    return out


# ---------------------------------------------------------------------------
# 作業ディレクトリ（push ごとに 1 つ。会話もこの単位）
# ---------------------------------------------------------------------------


def prepare_qa_workdir(rec: dict) -> Path:
    work = m.qa_workdir(rec["repo"], rec["number"], rec["head_sha"])
    ready = work / ".ready"
    if not ready.exists():
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        try:
            m.prepare_workdir(work, rec, login())
            (work / "pr" / "previous_review.json").unlink(missing_ok=True)  # 下で auto_review.json として置く
            ready.touch()
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
    # 自動レビューの結果は、質問のたびに最新を置き直す。同じ push でも、ボタンでのレビューし直しや再依頼で
    # 結果が変わり、ページの指摘の番号（「これについて聞く」）が指すものも変わるため
    reviews = rec.get("reviews") or []
    if reviews:
        m.write_json(work / "pr" / "auto_review.json", {"sha": reviews[-1]["sha"], "result": reviews[-1]["result"]})
    return work


def latest_review_key(rec: dict) -> str | None:
    """最新の自動レビューを見分けるキー。中身から作る（時刻は秒単位なので、同じ秒に終わったレビューを見分けられない）。"""
    reviews = rec.get("reviews") or []
    if not reviews:
        return None
    latest = {k: reviews[-1].get(k) for k in ("sha", "reviewed_at", "result")}
    return hashlib.sha256(json.dumps(latest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def describe_tool(block: dict) -> str:
    name = block.get("name", "")
    args = block.get("input") or {}
    target = args.get("file_path") or args.get("pattern") or args.get("path") or ""
    work_marker = "/qa/"
    if work_marker in target:
        # 作業ディレクトリまでのパスは長いので落とす
        target = target.split(work_marker, 1)[1].split("/", 1)[-1]
    if name == "Read" and target:
        return f"{target} を読んでいます"
    if name == "Grep" and target:
        return f"「{target}」を検索しています"
    if name == "Glob" and target:
        return f"{target} に合うファイルを探しています"
    return f"{name} を使っています"


# ---------------------------------------------------------------------------
# 今すぐ実行（ページのボタン）
# ---------------------------------------------------------------------------

REVIEW_SCRIPT = m.ROOT / "lib" / "pr_review.py"
_run_guard = threading.Lock()
_run_proc: subprocess.Popen | None = None  # このサーバーが起動した pr_review.py（動いている間だけ持つ）
_run_target: str | None = None  # その対象の PR（すべてなら None）
_run_last: dict | None = None  # このサーバーが最後に起動した実行の結果


def review_locked() -> bool:
    """pr_review.py の実行（定期実行など）が、実行中のロックを持っているか。

    ロックを一瞬だけ取ってすぐ放して確かめる。ちょうどその瞬間に定期実行が始まると、その回は
    「前回の実行が終わっていない」として何もせずに終わるが、次の回（15 分後）に動くので困らない。
    """
    try:
        fh = m.LOCK_FILE.open("a")
    except OSError:
        return False
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def run_state() -> dict:
    """実行の状況。ページは、実行中ならボタンを押せなくし、終わったら結果を読み直す。"""
    with _run_guard:
        mine, target, last = _run_proc is not None, _run_target, _run_last
    running = mine or review_locked()
    progress = None
    if running:
        try:
            progress = json.loads(m.RUN_STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress = None  # 始まったばかりで、まだ書かれていない
    return {"running": running, "target": target, "progress": progress, "last": last}


def start_run(target: str | None, *, add: bool = False) -> int | None:
    """pr_review.py を起動し、その pid を返す。すでに実行中なら起動せずに None を返す。

    曜日と時間帯の設定に関係なく動かす（--force）。target（<repo>#<番号>）を指定したら、その PR だけを、
    レビュー済みや失敗の上限に達していてもレビューし直す（--only --again）。
    add なら、一覧に無い PR を個別にレビューする PR として足してからレビューする（--add）。
    """
    global _run_proc, _run_target
    with _run_guard:
        if _run_proc is not None or review_locked():
            return None
        # 出力は pr_review.py が日付ごとのログに書く（--launchd）。定期実行と同じ場所で見られるようにするため
        cmd = [sys.executable, str(REVIEW_SCRIPT), "--force", "--launchd"]
        # = でつなぐ。値が - で始まっても、オプションと取り違えられないようにするため
        if target and add:
            cmd += [f"--add={target}"]
        elif target:
            cmd += [f"--only={target}", "--again"]
        # 別のセッションで起動する。レビューの途中でサーバーを再起動しても、レビューは最後まで行う
        proc = subprocess.Popen(cmd, cwd=m.ROOT, env=BASE_ENV, stdin=subprocess.DEVNULL, start_new_session=True)
        _run_proc, _run_target = proc, target
    threading.Thread(target=wait_run, args=(proc, target), daemon=True).start()
    m.log(f"今すぐ実行: {target or 'すべて'} (pid={proc.pid})")
    return proc.pid


def wait_run(proc: subprocess.Popen, target: str | None) -> None:
    global _run_proc, _run_target, _run_last
    code = proc.wait()
    with _run_guard:
        _run_proc, _run_target = None, None
        # pid はページが「自分が押した実行の結果か」を見分けるために使う
        _run_last = {"pid": proc.pid, "target": target, "exit_code": code, "finished_at": m.now_iso()}
    m.log(f"今すぐ実行が終わりました: {target or 'すべて'} (exit={code})")


def remove_manual(repo: str, number: int) -> str:
    """個別に頼んだ PR を一覧から外す（記録・質問の会話・質問用の作業ディレクトリを消す）。

    結果を "ok" / "busy"（レビューの実行中か、この PR の質問に答えている途中）/ "missing" / "not_manual" で返す。
    実行中の pr_review.py や質問への回答は記録や会話を書き戻すので、外したものが戻ってこないよう、
    どちらのロックも取ってから、記録を読み直して立場を確かめて消す。
    """
    me = login()  # 初回は gh を呼ぶので、ロックを取る前に済ませる（その間ほかの問い合わせを止めないため）
    qa_lock = pr_lock(f"{repo}#{number}")
    if not qa_lock.acquire(blocking=False):
        return "busy"
    try:
        with _run_guard:
            if _run_proc is not None:
                return "busy"
            m.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            with m.LOCK_FILE.open("a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "busy"
                path = m.record_path(repo, number)
                if not path.exists():
                    return "missing"
                # 確かめてからロックを取るまでに、定期実行が依頼された PR に変えていることがある
                if m.role(m.load_record(path)) != m.ROLE_MANUAL:
                    return "not_manual"
                path.unlink()
                qa_path(repo, number).unlink(missing_ok=True)
                for work in m.QA_WORK_DIR.glob(f"{repo}__{number}__*"):
                    shutil.rmtree(work, ignore_errors=True)
                m.render(me)  # ファイルで直接開いたときも、外したものが出ないようにする
    finally:
        qa_lock.release()
    m.log(f"個別レビューから外しました: {repo}#{number}")
    return "ok"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pr-review-routine"

    def log_message(self, fmt: str, *args) -> None:
        m.log(f"{self.address_string()} {fmt % args}")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # 実行中のページは数秒ごとに状況を聞きに来るので、成功した問い合わせはログに残さない
        if self.command == "GET" and self.path == "/api/run" and code == 200:
            return
        super().log_request(code, size)

    # ---- 共通 ----

    def host_ok(self) -> bool:
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-PRR-Token", ""), TOKEN)

    def send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, obj: object) -> None:
        self.send_text(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(min(length, 100_000)))
        if not isinstance(req, dict):
            raise ValueError("JSON のオブジェクトではありません")
        return req

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        if not self.host_ok():
            return self.send_text(403, "forbidden host")
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not m.OUT_HTML.exists():
                return self.send_text(503, "まだページがありません。レビューの初回実行を待ってください。")
            html = m.OUT_HTML.read_text(encoding="utf-8")
            # 終了コードの意味は pr_review.py が決める。ページに同じ値を書かないよう、ここで渡す
            html = html.replace("/*__QA__*/null", json.dumps({
                "token": TOKEN,
                "exit_codes": {"not_run": m.EXIT_NOT_RUN, "not_target": m.EXIT_NOT_TARGET},
            }))
            self.send_response(200)
            body = html.encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            return None
        if path == "/api/qa":
            if not self.token_ok():
                return self.send_text(403, "forbidden")
            return self.send_json(200, {"conversations": all_conversations()})
        if path == "/api/run":
            if not self.token_ok():
                return self.send_text(403, "forbidden")
            return self.send_json(200, run_state())
        return self.send_text(404, "not found")

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        if not self.host_ok() or not self.token_ok():
            return self.send_text(403, "forbidden")
        if self.path == "/api/ask":
            return self.handle_ask()
        if self.path == "/api/run":
            return self.handle_run()
        if self.path == "/api/remove":
            return self.handle_remove()
        return self.send_text(404, "not found")

    def find_record(self, repo: str, number: int) -> dict | None:
        """記録を読む。repo 名にパス区切りなどが入っていたり、記録が無かったりしたら None。"""
        path = m.record_path(repo, number)
        if path.parent != m.REVIEWS_DIR or not path.exists():
            return None
        return m.load_record(path)

    def handle_run(self) -> None:
        """今すぐレビューする。指定が無ければ定期実行と同じこと、repo と number ならその PR だけ、
        url（PR の URL）なら、一覧に無い PR も個別にレビューする PR として足してレビューする。"""
        bad_request = {"message": "リクエストの形が正しくありません"}
        try:
            req = self.read_json()
        except ValueError:  # json.JSONDecodeError も含む
            return self.send_json(400, bad_request)
        if "url" in req:
            try:
                target = m.parse_pr_ref(str(req["url"]))
            except ValueError as e:
                return self.send_json(400, {"message": str(e)})  # そのまま読める説明になっている
        elif "repo" in req:
            try:
                target = (str(req["repo"]), int(req["number"]))
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, bad_request)
        else:
            target = None
        # URL のときは、一覧にあるかどうかに関係なく --add で起動する。pr_review.py が GitHub から取り直した状態で
        # 立場を決める（依頼中・自分の PR ならその扱いのまま、無い・外れた PR は個別レビューとして足す）。
        # サーバーが持っている記録は古いことがあり（依頼が外れた直後など）、それで決めると対象外として終わってしまう
        add = "url" in req
        if target and not add:
            rec = self.find_record(*target)
            if rec is None:
                return self.send_json(404, {"message": "この PR の記録がありません"})
            if not rec.get("active"):
                return self.send_json(409, {"message": "依頼が外れた PR・クローズした PR はレビューしません"})
        name = f"{target[0]}#{target[1]}" if target else None
        try:
            pid = start_run(name, add=add)
        except OSError as e:
            m.log(f"今すぐ実行を起動できませんでした: {e}")
            return self.send_json(500, {"message": f"起動できませんでした: {e}"})
        if pid is None:
            # PR を指定したときは、実行中のほうがその PR をレビューし直すとは限らない（定期実行は、レビュー済みの PR を飛ばす）。
            # 依頼を受け付けなかったことをはっきり伝える
            message = ("レビューの実行中のため、この PR のレビューは受け付けませんでした。終わってからもう一度押してください"
                       if target else "レビューの実行中です。終わったら結果を読み込みます")
            return self.send_json(409, {"message": message, "run": run_state()})
        return self.send_json(202, {"run": run_state(), "pid": pid})

    def handle_remove(self) -> None:
        """個別に頼んだ PR を一覧から外す。依頼された PR・自分の PR は、GitHub の状態で決まるので外さない。"""
        try:
            req = self.read_json()
            rec = self.find_record(str(req["repo"]), int(req["number"]))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self.send_json(400, {"message": "リクエストの形が正しくありません"})
        if rec is None:
            return self.send_json(404, {"message": "この PR の記録がありません"})
        try:
            result = remove_manual(rec["repo"], rec["number"])
        except (m.StepError, OSError) as e:
            m.log(f"個別レビューから外せませんでした: {rec['repo']}#{rec['number']}: {e}")
            return self.send_json(500, {"message": f"外せませんでした: {e}"})
        if result == "missing":
            return self.send_json(404, {"message": "この PR の記録がありません"})
        if result == "not_manual":
            return self.send_json(409, {"message": "一覧から外せるのは、URL を貼って個別にレビューした PR だけです"})
        if result == "busy":
            return self.send_json(409, {"message": "レビューの実行中か、この PR の質問に答えている途中です。終わってからもう一度押してください"})
        return self.send_json(200, {"removed": f"{rec['repo']}#{rec['number']}"})

    def handle_ask(self) -> None:
        try:
            req = self.read_json()
            repo, number, question = str(req["repo"]), int(req["number"]), str(req["question"]).strip()
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self.send_text(400, "リクエストの形が正しくありません")
        if not question or len(question) > MAX_QUESTION_CHARS:
            return self.send_text(400, f"質問は 1〜{MAX_QUESTION_CHARS} 文字で入力してください")

        rec = self.find_record(repo, number)
        if rec is None:  # 記録の無い PR には答えない
            return self.send_text(404, "この PR の記録がありません")

        # 一覧から外す処理と同じキーで排他する（記録の正式なリポジトリ名を使う）
        lock = pr_lock(f"{rec['repo']}#{rec['number']}")
        if not lock.acquire(blocking=False):
            return self.send_text(409, "この PR への前の質問に答えている途中です")
        try:
            self.answer(rec, question)
        finally:
            lock.release()
        return None

    # ---- 回答 ----

    def answer(self, rec: dict, question: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        connected = True

        def emit(obj: dict) -> None:
            # ページを閉じられても、答えは最後まで受け取って保存する
            nonlocal connected
            if not connected:
                return
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                connected = False

        try:
            self.reply(rec, question, emit)
        except Exception as e:  # noqa: BLE001 — ヘッダーを送った後なので、何が起きてもページに理由を返す
            # 想定外の例外は、起きた場所が分からないと直せないので traceback ごと残す
            m.log(f"回答中に想定外のエラー: {rec['repo']}#{rec['number']}\n{traceback.format_exc()}")
            emit({"type": "error", "message": f"答えられませんでした（想定外のエラー）: {type(e).__name__}: {e}"})

    def reply(self, rec: dict, question: str, emit: Callable[[dict], None]) -> None:
        """質問に答える（answer の本体）。途中経過と結果は emit でページに送る。"""
        repo, number, sha = rec["repo"], rec["number"], rec["head_sha"]
        m.log(f"質問: {repo}#{number} ({sha[:7]}) {question[:80]!r}")
        with _concurrency:
            try:
                emit({"type": "status", "text": "PR のファイルを準備しています"})
                work = prepare_qa_workdir(rec)
            except (m.StepError, OSError, tarfile.TarError, subprocess.SubprocessError, json.JSONDecodeError) as e:
                # 想定外の例外は answer で traceback ごとログに残す
                m.log(f"質問の準備に失敗: {repo}#{number}: {e}")
                emit({"type": "error", "message": f"PR のファイルを準備できませんでした: {e}"})
                return

            qa = load_qa(repo, number)
            conv = next((c for c in reversed(qa["conversations"]) if c["sha"] == sha), None)
            if conv is None:
                conv = {"sha": sha, "session_id": None, "started_at": m.now_iso(), "messages": []}
                qa["conversations"].append(conv)

            # claude の会話は作業ディレクトリごとに保存される。場所が変わっていたら続きにせず、新しい会話として始める
            resume = bool(conv["session_id"]) and conv.get("cwd") == str(work)
            review_key = latest_review_key(rec)
            prompt = question
            if resume and conv.get("review_key") != review_key:
                # 前の質問のあとに自動レビューが更新された（ボタンでのレビューし直し・再依頼）。
                # 続きの会話は前に読んだ結果を覚えているので、読み直すよう Claude にだけ伝える（保存する質問文には足さない）
                prompt = "（自動レビューの結果が更新されました。pr/auto_review.json を読み直してから答えてください）\n\n" + question

            cmd = [
                "claude", "-p", prompt,
                "--tools", "Read,Grep,Glob",
                "--strict-mcp-config",
                "--setting-sources", "project",
                "--permission-mode", "dontAsk",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--append-system-prompt", QA_PROMPT_FILE.read_text(encoding="utf-8").replace("{{LOGIN}}", login()),
                "--max-turns", "60",
            ]
            if resume:
                cmd += ["--resume", conv["session_id"]]
            if m.MODEL:
                cmd += ["--model", m.MODEL]

            emit({"type": "status", "text": "考えています"})
            proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            timer = threading.Timer(QA_TIMEOUT_SEC, proc.kill)
            timer.start()
            session_id, final, failed = conv["session_id"], None, None
            try:
                for line in proc.stdout:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = ev.get("type")
                    if kind == "system" and ev.get("subtype") == "init":
                        session_id = ev.get("session_id") or session_id
                    elif kind == "stream_event":
                        inner = ev.get("event") or {}
                        if inner.get("type") == "message_start":
                            emit({"type": "turn"})
                        elif inner.get("type") == "content_block_delta" and (inner.get("delta") or {}).get("type") == "text_delta":
                            emit({"type": "text", "text": inner["delta"]["text"]})
                    elif kind == "assistant":
                        for block in (ev.get("message") or {}).get("content") or []:
                            if block.get("type") == "tool_use":
                                emit({"type": "status", "text": describe_tool(block)})
                    elif kind == "result":
                        session_id = ev.get("session_id") or session_id
                        if ev.get("subtype") == "success" and not ev.get("is_error"):
                            final = ev.get("result") or ""
                        else:
                            failed = f"Claude が失敗しました（{ev.get('subtype')}）: {str(ev.get('result'))[:300]}"
                proc.wait()
            finally:
                timer.cancel()

            if final is None:
                stderr = (proc.stderr.read() or "")[-500:]
                message = failed or f"Claude が答えを返しませんでした (exit={proc.returncode}) {stderr}"
                m.log(f"回答失敗: {repo}#{number}: {message}")
                emit({"type": "error", "message": message})
                return

            at = m.now_iso()
            conv["session_id"] = session_id
            conv["cwd"] = str(work)
            conv["review_key"] = review_key  # この会話がどの自動レビューの結果を前提にしているか
            conv["messages"] += [
                {"role": "user", "text": question, "at": at},
                {"role": "assistant", "text": final, "at": at},
            ]
            m.write_json(qa_path(repo, number), qa)
            m.log(f"回答: {repo}#{number} {len(final)} 文字")
            emit({"type": "done", "answer": final, "at": at})


def main() -> int:
    if "--launchd" in sys.argv[1:]:
        # launchd から起動したときはログファイルに書く。10MB を超えていたら 1 世代だけ残して切り替える
        log_file = m.LOG_DIR / "server.log"
        if log_file.exists() and log_file.stat().st_size > 10 * 1024 * 1024:
            log_file.replace(log_file.with_suffix(".log.1"))
        m.redirect_output_to_log(log_file)
    if not m.ORG:
        m.log(f"PR_REVIEW_ORG が設定されていません。{m.CONFIG_FILE} に書いてください（例は config/env.example）")
        return 2
    for d in (m.QA_DATA_DIR, m.QA_WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
    m.cleanup_qa_workdirs()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    m.log(f"起動しました: http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
