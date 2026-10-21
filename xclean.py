#!/usr/bin/env python3
"""
xclean — 用 X 官方 API 批量删除自己账号的帖子。

只走官方 v2 接口，遵守平台限速，不做任何绕过：
  GET    /2/users/me                          身份确认
  GET    /2/users/{id}/tweets                 枚举自己的帖子（含回复与转推）
  DELETE /2/tweets/{id}                       删除原创 / 回复 / 引用
  DELETE /2/users/{id}/retweets/{tweet_id}    撤销转推

认证为 OAuth 2.0 Authorization Code + PKCE（用户上下文令牌）。
app-only 的 Bearer Token 无权删除任何内容。

删除不可恢复。默认 dry-run，真实执行需 --yes 并输入确认词。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator

API = os.environ.get("XCLEAN_API_BASE", "https://api.x.com/2").rstrip("/")
AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = f"{API}/oauth2/token"
SCOPES = "tweet.read tweet.write users.read offline.access"

HOME = Path(os.environ.get("XCLEAN_HOME", Path.home() / ".xclean"))
TOKEN_FILE = HOME / "token.json"
STATE_FILE = HOME / "state.json"
AUDIT_FILE = HOME / "audit.jsonl"
BACKUP_DIR = HOME / "backups"

USER_AGENT = "xclean/1.0 (+official-api)"

# 官方单价（docs.x.com/x-api/getting-started/pricing，2026-10 核对）
PRICE_OWNED_READ = 0.001      # 读自己的帖子，Owned Reads
PRICE_CONTENT_MANAGE = 0.005  # 删除帖子的计费类别 Content: Manage
# 同一资源 24 小时内重复读取不重复计费，所以一次完整清理的读取成本按条数计一次。

# 删除端点限速：50 次 / 15 分钟。留一点余量，不贴着上限跑。
DELETE_WINDOW_SECONDS = 15 * 60
DELETE_WINDOW_BUDGET = 50
DELETE_SAFETY_MARGIN = 2


class XCleanError(Exception):
    """面向用户的、可读的错误。"""


# ───────────────────────────── 基础 IO ─────────────────────────────


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for path in (HOME, BACKUP_DIR):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_private(path: Path, data: str) -> None:
    """写入含敏感信息的文件，权限收紧到 0600。"""
    ensure_home()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


# ───────────────────────────── HTTP ─────────────────────────────


@dataclass
class Response:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, str] | None = None,
    timeout: int = 30,
) -> Response:
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return Response(resp.status, _parse(raw), dict(resp.headers))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return Response(exc.code, _parse(raw), dict(exc.headers or {}))
    except urllib.error.URLError as exc:
        raise XCleanError(f"网络请求失败：{exc.reason}") from exc


def _parse(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:500]}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def describe_error(payload: dict[str, Any]) -> str:
    """从 API 错误响应里提取人话。"""
    if "detail" in payload:
        return str(payload["detail"])
    if "title" in payload:
        return str(payload["title"])
    if "error" in payload:
        return str(payload["error"])
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("detail") or first)
        return str(first)
    if "_raw" in payload:
        return str(payload["_raw"])
    return json.dumps(payload, ensure_ascii=False)[:200]


# ───────────────────────────── OAuth 2.0 PKCE ─────────────────────────────


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    error: str | None = None
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        cls = type(self)
        cls.error = (params.get("error") or [None])[0]
        cls.code = (params.get("code") or [None])[0]
        cls.state = (params.get("state") or [None])[0]

        ok = bool(cls.code) and not cls.error
        body = (
            "<html><body style='font-family:system-ui;padding:40px'>"
            "<h2>授权完成</h2><p>可以关闭这个页面，回到终端继续。</p></body></html>"
            if ok
            else "<html><body style='font-family:system-ui;padding:40px'>"
            "<h2>授权未完成</h2><p>请回到终端查看提示。</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        cls.done.set()

    def log_message(self, *args: Any) -> None:  # 静音默认访问日志
        return


def cmd_login(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("XCLEAN_CLIENT_ID")
    if not client_id:
        raise XCleanError(
            "缺少 client_id。请在 X 开发者后台创建应用后，用 --client-id 传入，"
            "或设置环境变量 XCLEAN_CLIENT_ID。"
        )

    port = args.port
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    url = f"{AUTH_URL}?{query}"

    print("请在浏览器中完成授权。如果浏览器没有自动打开，手动访问：\n")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"等待回调（127.0.0.1:{port}）…")
    if not _CallbackHandler.done.wait(timeout=300):
        server.shutdown()
        raise XCleanError("等待授权超时（5 分钟）。请重新运行 login。")

    server.shutdown()

    if _CallbackHandler.error:
        raise XCleanError(f"授权被拒绝或出错：{_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise XCleanError("没有收到授权码。")
    if _CallbackHandler.state != state:
        raise XCleanError("state 校验失败，已中止。请重新登录。")

    resp = request(
        "POST",
        TOKEN_URL,
        body={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if not resp.ok:
        raise XCleanError(f"换取令牌失败（{resp.status}）：{describe_error(resp.payload)}")

    store_tokens(resp.payload, client_id)
    print("\n已保存令牌。")
    return cmd_whoami(argparse.Namespace())


def store_tokens(payload: dict[str, Any], client_id: str) -> None:
    access = payload.get("access_token")
    if not access:
        raise XCleanError("令牌响应里没有 access_token。")
    expires_in = int(payload.get("expires_in") or 7200)
    record = {
        "access_token": access,
        "refresh_token": payload.get("refresh_token"),
        "client_id": client_id,
        "expires_at": int(time.time()) + expires_in,
        "scope": payload.get("scope", SCOPES),
    }
    write_private(TOKEN_FILE, json.dumps(record, indent=2))
    print(f"令牌已写入 {TOKEN_FILE}（权限 600）")


def load_tokens() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise XCleanError("尚未登录。先运行：xclean.py login --client-id <你的 Client ID>")
    data = read_json(TOKEN_FILE, {})
    if not data.get("access_token"):
        raise XCleanError("令牌文件损坏或缺少 access_token，请重新 login。")
    return data


def refresh_tokens(tokens: dict[str, Any]) -> dict[str, Any]:
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise XCleanError("令牌已过期且没有 refresh_token，请重新 login。")

    resp = request(
        "POST",
        TOKEN_URL,
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": tokens.get("client_id", ""),
        },
    )
    if not resp.ok:
        raise XCleanError(
            f"刷新令牌失败（{resp.status}）：{describe_error(resp.payload)}。请重新 login。"
        )
    store_tokens(resp.payload, tokens.get("client_id", ""))
    return load_tokens()


def access_token() -> str:
    """返回可用令牌，必要时先刷新。"""
    tokens = load_tokens()
    if time.time() < int(tokens.get("expires_at", 0)) - 60:
        return tokens["access_token"]
    print("令牌即将过期，正在刷新…")
    return refresh_tokens(tokens)["access_token"]


def api(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, str] | None = None,
) -> Response:
    return request(method, f"{API}{path}", token=token, body=body)


# ───────────────────────────── 身份 ─────────────────────────────


def fetch_me(token: str) -> dict[str, Any]:
    resp = api("GET", "/users/me", token=token)
    if resp.status == 401:
        raise XCleanError("认证失败（401）。令牌可能已失效，请重新 login。")
    if not resp.ok:
        raise XCleanError(f"读取账号信息失败（{resp.status}）：{describe_error(resp.payload)}")
    data = resp.payload.get("data") or {}
    if not data.get("id"):
        raise XCleanError("账号信息里没有 id，无法继续。")
    return data


def cmd_whoami(args: argparse.Namespace) -> int:
    token = access_token()
    me = fetch_me(token)
    print(f"账号：@{me.get('username')}  (id {me.get('id')})")
    print(f"名称：{me.get('name', '—')}")
    return 0


# ───────────────────────────── 枚举 ─────────────────────────────


@dataclass
class Candidate:
    id: str                 # 删除动作要用的 id
    kind: str               # original | reply | quote | repost
    text: str
    created_at: str
    source_id: str | None = None   # 转推时指向原帖 id

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "created_at": self.created_at,
            "source_id": self.source_id,
        }


def classify(item: dict[str, Any]) -> Candidate:
    refs = item.get("referenced_tweets") or []
    ref_types = {r.get("type") for r in refs if isinstance(r, dict)}

    if "retweeted" in ref_types:
        source = next(r.get("id") for r in refs if r.get("type") == "retweeted")
        return Candidate(
            id=str(item.get("id")),
            kind="repost",
            text=item.get("text", ""),
            created_at=item.get("created_at", ""),
            source_id=str(source) if source else None,
        )
    if "replied_to" in ref_types:
        kind = "reply"
    elif "quoted" in ref_types:
        kind = "quote"
    else:
        kind = "original"

    return Candidate(
        id=str(item.get("id")),
        kind=kind,
        text=item.get("text", ""),
        created_at=item.get("created_at", ""),
    )


def iter_timeline(
    token: str,
    user_id: str,
    *,
    exclude: set[str],
    max_items: int | None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> Iterator[Candidate]:
    """按游标翻页枚举自己的帖子。"""
    seen: set[str] = set()
    pagination: str | None = None
    emitted = 0
    page = 0

    while True:
        params: list[tuple[str, str]] = [
            ("max_results", "100"),
            ("tweet.fields", "created_at,text,referenced_tweets"),
        ]
        if exclude:
            params.append(("exclude", ",".join(sorted(exclude))))
        if start_time:
            params.append(("start_time", start_time))
        if end_time:
            params.append(("end_time", end_time))
        if pagination:
            params.append(("pagination_token", pagination))

        path = f"/users/{user_id}/tweets?{urllib.parse.urlencode(params)}"
        resp = api("GET", path, token=token)
        page += 1

        if resp.status == 429:
            wait = rate_limit_wait(resp.headers)
            print(f"  读取被限速，等待 {wait:.0f}s 后继续…")
            time.sleep(wait)
            continue

        if resp.status == 401:
            raise XCleanError("读取时间线时认证失败（401），请重新 login。")
        if not resp.ok:
            raise XCleanError(
                f"读取时间线失败（{resp.status}）：{describe_error(resp.payload)}"
            )

        items = resp.payload.get("data") or []
        for item in items:
            cand = classify(item)
            if cand.id in seen:
                continue
            seen.add(cand.id)
            yield cand
            emitted += 1
            if max_items is not None and emitted >= max_items:
                return

        meta = resp.payload.get("meta") or {}
        pagination = meta.get("next_token")
        if not pagination or not items:
            return


def rate_limit_wait(headers: dict[str, str]) -> float:
    """根据响应头算出还要等多久，封顶一个窗口。"""
    lowered = {k.lower(): v for k, v in headers.items()}
    reset = lowered.get("x-rate-limit-reset")
    if reset:
        try:
            wait = int(reset) - int(time.time()) + 2
            return float(max(3, min(wait, DELETE_WINDOW_SECONDS)))
        except ValueError:
            pass
    return 60.0


# ───────────────────────────── 状态与审计 ─────────────────────────────


def load_state() -> dict[str, Any]:
    return read_json(STATE_FILE, {"deleted": {}, "failed": {}})


def save_state(state: dict[str, Any]) -> None:
    ensure_home()
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def audit(record: dict[str, Any]) -> None:
    ensure_home()
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def backup(candidates: Iterable[Candidate], account: str) -> Path:
    ensure_home()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{account}-{stamp}.jsonl"
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for cand in candidates:
            fh.write(json.dumps(cand.as_dict(), ensure_ascii=False) + "\n")
            count += 1
    os.chmod(path, 0o600)
    print(f"已备份 {count} 条到 {path}")
    return path


# ───────────────────────────── 删除闸门 ─────────────────────────────


class RateGate:
    """按平台返回的限速头节流，不主动越过窗口额度。"""

    def __init__(self, window: int = DELETE_WINDOW_SECONDS, budget: int = DELETE_WINDOW_BUDGET):
        self.window = window
        self.budget = budget
        self.stamps: list[float] = []
        self.blocked_until = 0.0

    def wait(self) -> None:
        now = time.time()
        if now < self.blocked_until:
            time.sleep(self.blocked_until - now)
            now = time.time()

        self.stamps = [s for s in self.stamps if now - s < self.window]
        if len(self.stamps) >= self.budget - DELETE_SAFETY_MARGIN:
            oldest = self.stamps[0]
            sleep_for = self.window - (now - oldest) + 2
            if sleep_for > 0:
                print(f"  本地额度用尽，等待 {sleep_for:.0f}s 进入下一个窗口…")
                time.sleep(sleep_for)

    def mark(self) -> None:
        self.stamps.append(time.time())

    def block(self, seconds: float) -> None:
        self.blocked_until = time.time() + seconds


# ───────────────────────────── 删除执行 ─────────────────────────────


def delete_one(
    token: str,
    user_id: str,
    cand: Candidate,
    *,
    max_retries: int = 3,
) -> tuple[bool, str]:
    """
    返回 (成功与否, 说明)。
    转推必须走 undo retweet，删原帖 ID 不会撤销转推。
    遇 429 按服务器给出的 reset 时间退避后重试。
    """
    attempt = 0
    while True:
        attempt += 1
        if cand.kind == "repost":
            if not cand.source_id:
                return False, "转推缺少原帖 id，无法撤销"
            resp = api("DELETE", f"/users/{user_id}/retweets/{cand.source_id}", token=token)
        else:
            resp = api("DELETE", f"/tweets/{cand.id}", token=token)

        if resp.ok:
            deleted = (resp.payload.get("data") or {}).get("deleted")
            return True, "ok" if deleted in (True, None) else "ok(未确认)"

        if resp.status == 429:
            if attempt > max_retries:
                return False, "限速"
            wait = rate_limit_wait(resp.headers)
            print(f"    被限速，等待 {wait:.0f}s 后重试（第 {attempt} 次）…")
            time.sleep(wait)
            continue

        if resp.status == 404:
            return False, "已不存在或不可见"
        if resp.status == 403:
            return False, f"无权限：{describe_error(resp.payload)}"
        return False, f"{resp.status}: {describe_error(resp.payload)}"


@dataclass
class Plan:
    account: str
    user_id: str
    candidates: list[Candidate] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.candidates:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    def earliest(self) -> str:
        dates = [c.created_at for c in self.candidates if c.created_at]
        return min(dates) if dates else "—"

    def estimate(self, *, skip_reads: bool = False) -> tuple[float, float]:
        """返回 (读取成本, 删除成本)。归档模式跳过读取计费。"""
        n = len(self.candidates)
        read_cost = 0.0 if skip_reads else n * PRICE_OWNED_READ
        delete_cost = n * PRICE_CONTENT_MANAGE
        return read_cost, delete_cost


KIND_LABEL = {
    "original": "原创",
    "reply": "回复",
    "quote": "引用",
    "repost": "转推",
}


def load_archive_plan(args: argparse.Namespace) -> Plan:
    """从本地清单构建计划：只调一次 users/me 确认身份，不读时间线。"""
    token = access_token()
    me = fetch_me(token)

    if args.account and args.account.lstrip("@").lower() != str(me.get("username", "")).lower():
        raise XCleanError(
            f"登录账号是 @{me.get('username')}，与指定的 @{args.account.lstrip('@')} 不一致，已停止。"
        )

    path = Path(args.from_archive)
    if not path.exists():
        raise XCleanError(f"清单文件不存在：{path}")

    plan = Plan(account=str(me.get("username")), user_id=str(me["id"]))
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            kind = row.get("kind", "original")
            if kind not in KIND_LABEL:
                bad += 1
                continue
            if kind == "reply" and (args.exclude_replies or not args.include_replies):
                continue
            if kind == "repost" and (args.exclude_retweets or not args.include_retweets):
                continue
            if args.kind and kind not in args.kind:
                continue
            cand = Candidate(
                id=str(row.get("id", "")),
                kind=kind,
                text=str(row.get("text", "")),
                created_at=str(row.get("created_at", "")),
                source_id=row.get("source_id"),
            )
            if not cand.id:
                bad += 1
                continue
            if args.contains and args.contains.lower() not in cand.text.lower():
                continue
            if args.before and cand.created_at and cand.created_at > args.before:
                continue
            if args.after and cand.created_at and cand.created_at < args.after:
                continue
            plan.candidates.append(cand)
            if args.limit and len(plan.candidates) >= args.limit:
                break

    if bad:
        print(f"跳过 {bad} 行无法识别的记录")
    print(f"从清单载入：{len(plan.candidates)} 条（未调用时间线接口，无读取费用）")
    return plan


def build_plan(args: argparse.Namespace) -> Plan:
    if getattr(args, "from_archive", None):
        return load_archive_plan(args)

    token = access_token()
    me = fetch_me(token)

    if args.account and args.account.lstrip("@").lower() != str(me.get("username", "")).lower():
        raise XCleanError(
            f"登录账号是 @{me.get('username')}，与指定的 @{args.account.lstrip('@')} 不一致，已停止。"
        )

    exclude: set[str] = set()
    if not args.include_replies:
        exclude.add("replies")
    if not args.include_retweets:
        exclude.add("retweets")
    if args.exclude_replies:
        exclude.add("replies")
    if args.exclude_retweets:
        exclude.add("retweets")

    print(f"账号：@{me.get('username')}")
    print("正在枚举帖子…")

    plan = Plan(account=str(me.get("username")), user_id=str(me["id"]))
    for cand in iter_timeline(
        token,
        plan.user_id,
        exclude=exclude,
        max_items=None,
        start_time=args.after,
        end_time=args.before,
    ):
        if args.kind and cand.kind not in args.kind:
            continue
        if args.contains and args.contains.lower() not in cand.text.lower():
            continue
        plan.candidates.append(cand)
        if args.limit and len(plan.candidates) >= args.limit:
            break
        if len(plan.candidates) % 200 == 0:
            print(f"  已枚举 {len(plan.candidates)} 条…")

    print(f"枚举完成：{len(plan.candidates)} 条")
    return plan


def print_plan(plan: Plan, *, skip_reads: bool = False) -> None:
    counts = plan.counts()
    read_cost, delete_cost = plan.estimate(skip_reads=skip_reads)
    per_hour = DELETE_WINDOW_BUDGET * (3600 / DELETE_WINDOW_SECONDS)
    hours = len(plan.candidates) / per_hour

    print()
    print("─" * 52)
    print(f"  账号    @{plan.account}")
    print(f"  条数    {len(plan.candidates)}")
    if counts:
        detail = "  ".join(f"{KIND_LABEL.get(k, k)} {v}" for k, v in sorted(counts.items()))
        print(f"  构成    {detail}")
    print(f"  最早    {plan.earliest()}")
    print("─" * 52)
    if skip_reads:
        print("  读取    用本地归档，不计费")
    else:
        print(f"  读取    ${read_cost:.3f}（$0.001/条，同日重复读取不重复计费）")
    print(f"  删除    ${delete_cost:.3f}（$0.005/条）")
    print(f"  合计    ${read_cost + delete_cost:.3f}")
    print(f"  耗时    约 {hours:.1f} 小时（限速 50 次/15 分钟）")
    print("─" * 52)
    print("  删除不可恢复。X 已取消 API 免费档，删除请求无法归零。")
    print("─" * 52)
    print()


def cmd_list(args: argparse.Namespace) -> int:
    plan = build_plan(args)
    print_plan(plan)
    for cand in plan.candidates:
        stamp = cand.created_at[:10] or "—"
        text = " ".join(cand.text.split())[:70]
        print(f"{cand.id}  {stamp}  {KIND_LABEL.get(cand.kind, cand.kind):<2}  {text}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    skip_reads = bool(getattr(args, "from_archive", None))
    plan = build_plan(args)
    print_plan(plan, skip_reads=skip_reads)

    if not plan.candidates:
        print("没有符合条件的内容，未执行任何删除。")
        return 0

    state = load_state()
    already = state.get("deleted", {})
    pending = [c for c in plan.candidates if c.id not in already]
    skipped = len(plan.candidates) - len(pending)
    if skipped:
        print(f"其中 {skipped} 条在之前的运行里已经删过，本次跳过。")

    if not pending:
        print("没有剩余待删内容。")
        return 0

    # 预算护栏：按删除单价估算，超了直接停，不发请求。
    if args.budget is not None:
        projected = len(pending) * PRICE_CONTENT_MANAGE
        print(f"本次预计花费 ${projected:.3f}，预算上限 ${args.budget:.3f}")
        if projected > args.budget:
            allowed = int(args.budget // PRICE_CONTENT_MANAGE)
            raise XCleanError(
                f"预计 ${projected:.3f} 超过预算 ${args.budget:.3f}。"
                f"按预算最多能删 {allowed} 条，可加 --limit {allowed}，或调高 --budget。"
            )

    path = backup(plan.candidates, plan.account)

    if not args.yes:
        print("当前是演练模式（dry-run），没有发出任何删除请求。")
        print(f"确认无误后加 --yes 执行，共 {len(pending)} 条。")
        print(f"待删清单已存到：{path}")
        return 0

    confirm_word = f"DELETE {len(pending)}"
    print(f"这会真实删除 {len(pending)} 条内容，无法撤销。")
    typed = input(f"输入 {confirm_word} 继续，其它任意输入取消： ").strip()
    if typed != confirm_word:
        print("已取消，没有删除任何内容。")
        return 1

    token = access_token()
    gate = RateGate()
    ok_count = success = failed = 0
    failures: dict[str, int] = {}

    print()
    try:
        for idx, cand in enumerate(pending, start=1):
            gate.wait()
            ok, note = delete_one(token, plan.user_id, cand)
            gate.mark()

            if ok:
                success += 1
                state.setdefault("deleted", {})[cand.id] = {
                    "at": int(time.time()),
                    "kind": cand.kind,
                }
                audit({"action": "delete", "id": cand.id, "kind": cand.kind, "result": "ok"})
            elif note == "限速":
                gate.block(rate_limit_wait({}))
                failed += 1
                failures["限速（重试后仍失败）"] = failures.get("限速（重试后仍失败）", 0) + 1
                state.setdefault("failed", {})[cand.id] = {"at": int(time.time()), "reason": note}
                audit({"action": "delete", "id": cand.id, "kind": cand.kind, "result": "rate_limited"})
            else:
                failed += 1
                failures[note] = failures.get(note, 0) + 1
                state.setdefault("failed", {})[cand.id] = {"at": int(time.time()), "reason": note}
                audit(
                    {
                        "action": "delete",
                        "id": cand.id,
                        "kind": cand.kind,
                        "result": "failed",
                        "reason": note,
                    }
                )

            if idx % 10 == 0 or idx == len(pending):
                save_state(state)
                print(f"  进度 {idx}/{len(pending)}  成功 {success}  失败 {failed}")

    except KeyboardInterrupt:
        save_state(state)
        print("\n已中断。进度已保存，重新运行会接着删剩下的。")
        return 130
    finally:
        save_state(state)
        print()
        print("─" * 52)
        print(f"  已删除 {success} 条，失败 {failed} 条")
        for reason, n in sorted(failures.items(), key=lambda kv: -kv[1]):
            print(f"    {reason}：{n}")
        print(f"  审计日志 {AUDIT_FILE}")
        print("─" * 52)

    return 0 if failed == 0 else 2


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    deleted = state.get("deleted", {})
    failed = state.get("failed", {})
    print(f"累计已删：{len(deleted)} 条")
    print(f"记录失败：{len(failed)} 条")
    if deleted:
        latest = max(v.get("at", 0) for v in deleted.values())
        print(f"最近一次删除：{time.strftime('%Y-%m-%d %H:%M', time.localtime(latest))}")
    if failed:
        reasons: dict[str, int] = {}
        for v in failed.values():
            reasons[v.get("reason", "未知")] = reasons.get(v.get("reason", "未知"), 0) + 1
        print("失败原因：")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {reason}：{n}")
    print(f"状态文件：{STATE_FILE}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("已删除本地令牌。")
    else:
        print("本来就没有保存令牌。")
    return 0


# ───────────────────────────── CLI ─────────────────────────────


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", help="校验登录账号是否为这个 @用户名，防止误删")
    parser.add_argument("--limit", type=int, help="最多处理多少条")
    parser.add_argument("--before", help="只处理此时间之前（ISO8601，如 2024-01-01T00:00:00Z）")
    parser.add_argument("--after", help="只处理此时间之后（ISO8601）")
    parser.add_argument("--contains", help="只处理正文含该关键词的条目")
    parser.add_argument(
        "--kind",
        action="append",
        choices=["original", "reply", "quote", "repost"],
        help="只处理指定类型，可重复使用",
    )
    parser.add_argument("--include-replies", action="store_true", help="包含回复")
    parser.add_argument("--include-retweets", action="store_true", help="包含转推")
    parser.add_argument("--exclude-replies", action="store_true", help="排除回复")
    parser.add_argument("--exclude-retweets", action="store_true", help="排除转推")
    parser.add_argument(
        "--from-archive",
        metavar="FILE",
        help="用 archive_import.py 生成的清单删除，跳过读取计费（最省钱）",
    )
    parser.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="本次最多花多少美元，超出即停止（按 $0.005/条估算）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xclean",
        description="用 X 官方 API 批量删除自己账号的帖子（默认演练，不真删）。",
        epilog="删除不可恢复。所有操作走官方 v2 接口并遵守平台限速。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="通过浏览器授权登录")
    p_login.add_argument("--client-id", help="X 应用的 OAuth 2.0 Client ID")
    p_login.add_argument("--port", type=int, default=8721, help="本地回调端口，默认 8721")
    p_login.set_defaults(func=cmd_login)

    p_who = sub.add_parser("whoami", help="显示当前登录账号")
    p_who.set_defaults(func=cmd_whoami)

    p_list = sub.add_parser("list", help="列出将处理的内容（不删除）")
    add_filter_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="删除符合条件的内容")
    add_filter_args(p_del)
    p_del.add_argument("--yes", action="store_true", help="真实执行；不加只做演练")
    p_del.set_defaults(func=cmd_delete)

    p_status = sub.add_parser("status", help="查看历史进度")
    p_status.set_defaults(func=cmd_status)

    p_logout = sub.add_parser("logout", help="删除本地保存的令牌")
    p_logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except XCleanError as exc:
        print(f"\n出错了：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
