#!/usr/bin/env python3
"""
从 X 账号数据归档里提取待删帖子清单，省掉 API 读取费用。

X 的「下载数据存档」是免费的，里面有完整的推文 ID 与时间。
拿它当清单，就只需要为删除付费，读取成本归零。

支持：
  - 官方归档 zip（data/tweets.js 这类格式，含 window.YTD.tweets.part0 = [...]）
  - 已经解压的目录
  - 只有 ID 的纯文本 / JSONL

用法：
  python3 archive_import.py twitter-2026-10-21-xxxx.zip -o archive.jsonl
  python3 archive_import.py ./解压目录 -o archive.jsonl --include-replies
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

TWITTER_DATE = re.compile(r"^\w{3} (\w{3}) (\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{4}) (\d{4})$")
MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def parse_twitter_date(raw: str) -> str:
    """把 'Wed Oct 10 20:19:24 +0000 2018' 转成 ISO8601。"""
    m = TWITTER_DATE.match((raw or "").strip())
    if not m:
        return ""
    mon, day, clock, tz, year = m.groups()
    month = MONTHS.get(mon)
    if not month:
        return ""
    sign = tz[0]
    offset = f"{sign}{tz[1:3]}:{tz[3:5]}"
    return f"{year}-{month}-{day}T{clock}{offset}"


def strip_js_prefix(text: str) -> str:
    """
    归档里的 .js 文件其实是 'window.YTD.tweets.part0 = [ ... ]'。
    取出等号后的 JSON 部分。
    """
    idx = text.find("=")
    if idx == -1:
        return text.strip()
    return text[idx + 1:].strip()


def iter_tweet_records(files: dict[str, str]):
    """从 {文件名: 内容} 中产出原始推文 dict。"""
    for name, content in files.items():
        base = name.replace("\\", "/")
        if not re.search(r"(^|/)(tweets(-part\d+)?)\.(js|json)$", base):
            continue

        payload = strip_js_prefix(content)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            print(f"跳过无法解析的文件：{name}", file=sys.stderr)
            continue

        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            # 官方格式是 {"tweet": {...}}；也有直接平铺的
            rec = entry.get("tweet") if isinstance(entry.get("tweet"), dict) else entry
            if rec and rec.get("id_str", rec.get("id")):
                yield rec


def classify(rec: dict) -> str:
    """判断原创 / 回复 / 引用 / 转推。"""
    if rec.get("retweeted_status_id_str") or rec.get("retweeted_status_id"):
        return "repost"
    text = str(rec.get("full_text") or rec.get("text") or "")
    if text.startswith("RT @"):
        return "repost"
    if rec.get("in_reply_to_status_id_str") or rec.get("in_reply_to_status_id"):
        return "reply"
    if rec.get("quoted_status_id_str") or rec.get("quoted_status_id"):
        return "quote"
    return "original"


def collect(files: dict[str, str], *, include_replies: bool, include_reposts: bool):
    seen: set[str] = set()
    for rec in iter_tweet_records(files):
        tid = str(rec.get("id_str") or rec.get("id"))
        if tid in seen:
            continue
        kind = classify(rec)
        if kind == "reply" and not include_replies:
            continue
        if kind == "repost" and not include_reposts:
            continue
        seen.add(tid)
        text = str(rec.get("full_text") or rec.get("text") or "")
        yield {
            "id": tid,
            "kind": kind,
            "text": text[:280],
            "created_at": parse_twitter_date(str(rec.get("created_at", ""))),
            "source_id": str(
                rec.get("retweeted_status_id_str")
                or rec.get("retweeted_status_id")
                or ""
            ) or None,
        }


def load_files(path: Path) -> dict[str, str]:
    """读 zip、目录或单个 js/json 文件，返回 {文件名: 文本}，只收推文相关文件。"""
    files: dict[str, str] = {}

    if path.is_file() and path.suffix.lower() in {".js", ".json"} and not zipfile.is_zipfile(path):
        if re.search(r"tweets(-part\d+)?\.(js|json)$", path.name):
            files[path.name] = path.read_text(encoding="utf-8", errors="replace")
            return files
        raise SystemExit(f"文件名不像推文归档：{path.name}（应含 tweets.js 或 tweets.json）")

    if path.is_dir():
        for p in path.rglob("*"):
            if p.is_file() and p.suffix.lower() in {".js", ".json"}:
                rel = str(p.relative_to(path))
                if re.search(r"(^|/)(tweets(-part\d+)?)\.(js|json)$", rel):
                    files[rel] = p.read_text(encoding="utf-8", errors="replace")
        return files

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if not name.lower().endswith((".js", ".json")):
                    continue
                if not re.search(r"(^|/)(tweets(-part\d+)?)\.(js|json)$", name):
                    continue
                try:
                    files[name] = zf.read(info).decode("utf-8", errors="replace")
                except (KeyError, UnicodeDecodeError):
                    continue
        return files

    raise SystemExit(f"无法识别：{path}（需要 X 数据归档的 zip 或解压目录）")


def main() -> int:
    ap = argparse.ArgumentParser(description="从 X 数据归档提取待删清单，免读取费用")
    ap.add_argument("archive", type=Path, help="归档 zip 或解压后的目录")
    ap.add_argument("-o", "--output", type=Path, default=Path("archive.jsonl"))
    ap.add_argument("--include-replies", action="store_true", help="包含回复")
    ap.add_argument("--include-reposts", action="store_true", help="包含转推")
    args = ap.parse_args()

    if not args.archive.exists():
        raise SystemExit(f"路径不存在：{args.archive}")

    files = load_files(args.archive)
    if not files:
        raise SystemExit("归档里没找到 tweets.js / tweets.json。请确认下载的是完整归档。")

    rows = list(
        collect(files, include_replies=args.include_replies, include_reposts=args.include_reposts)
    )
    if not rows:
        raise SystemExit("没有提取到任何帖子。")

    with open(args.output, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    label = {"original": "原创", "reply": "回复", "quote": "引用", "repost": "转推"}

    print(f"已提取 {len(rows)} 条 → {args.output}")
    print("  " + "  ".join(f"{label.get(k, k)} {v}" for k, v in sorted(counts.items())))
    dates = [r["created_at"] for r in rows if r["created_at"]]
    if dates:
        print(f"  时间范围 {min(dates)[:10]} ~ {max(dates)[:10]}")
    print()
    print("下一步（跳过读取计费）：")
    print(f"  python3 xclean.py delete --from-archive {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
