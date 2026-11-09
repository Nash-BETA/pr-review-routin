#!/usr/bin/env python3
"""PR 自動レビューのページを配信し、ページからの質問に Claude が答えるローカルサーバー。

  GET  /          結果ページ（out/index.html）に、この起動の合言葉（トークン）を埋め込んで返す
  GET  /api/qa    保存済みの会話を返す
  POST /api/ask   質問を受け取り、Claude の答えを 1 行 1 件の JSON で流しながら返す

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

import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    if ready.exists():
        return work
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        m.prepare_workdir(work, rec, login())
        previous = work / "pr" / "previous_review.json"
        if previous.exists():
            previous.rename(work / "pr" / "auto_review.json")
        ready.touch()
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work


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
# HTTP
# ---------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pr-review-routine"

    def log_message(self, fmt: str, *args) -> None:
        m.log(f"{self.address_string()} {fmt % args}")

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

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        if not self.host_ok():
            return self.send_text(403, "forbidden host")
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not m.OUT_HTML.exists():
                return self.send_text(503, "まだページがありません。レビューの初回実行を待ってください。")
            html = m.OUT_HTML.read_text(encoding="utf-8")
            html = html.replace("/*__QA__*/null", json.dumps({"token": TOKEN}))
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
        return self.send_text(404, "not found")

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        if not self.host_ok() or not self.token_ok():
            return self.send_text(403, "forbidden")
        if self.path != "/api/ask":
            return self.send_text(404, "not found")

        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(length, 100_000)))
            repo, number, question = str(req["repo"]), int(req["number"]), str(req["question"]).strip()
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self.send_text(400, "リクエストの形が正しくありません")
        if not question or len(question) > MAX_QUESTION_CHARS:
            return self.send_text(400, f"質問は 1〜{MAX_QUESTION_CHARS} 文字で入力してください")

        path = m.record_path(repo, number)
        # repo 名にパス区切りなどが入っていないこと・記録があることを確かめる（記録の無い PR には答えない）
        if path.parent != m.REVIEWS_DIR or not path.exists():
            return self.send_text(404, "この PR の記録がありません")
        rec = m.load_record(path)

        lock = pr_lock(f"{repo}#{number}")
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

        repo, number, sha = rec["repo"], rec["number"], rec["head_sha"]
        m.log(f"質問: {repo}#{number} ({sha[:7]}) {question[:80]!r}")
        with _concurrency:
            try:
                emit({"type": "status", "text": "PR のファイルを準備しています"})
                work = prepare_qa_workdir(rec)
            except (m.StepError, OSError, subprocess.SubprocessError) as e:
                emit({"type": "error", "message": f"PR のファイルを準備できませんでした: {e}"})
                return

            qa = load_qa(repo, number)
            conv = next((c for c in reversed(qa["conversations"]) if c["sha"] == sha), None)
            if conv is None:
                conv = {"sha": sha, "session_id": None, "started_at": m.now_iso(), "messages": []}
                qa["conversations"].append(conv)

            cmd = [
                "claude", "-p", question,
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
            # claude の会話は作業ディレクトリごとに保存される。場所が変わっていたら続きにせず、新しい会話として始める
            if conv["session_id"] and conv.get("cwd") == str(work):
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
