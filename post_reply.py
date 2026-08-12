"""
LexProtocol GitHub Reply Poster (v2 — human-approved, one at a time)
=====================================================================
Companion to find_candidates.py. This is the ONLY script that ever posts
to GitHub, and it requires an explicit --yes confirmation per issue plus
a hard daily cap. There is no "run forever" mode — that's intentional.

Usage:
    export GITHUB_TOKEN=ghp_your_token_here

    # See what's pending review:
    python post_reply.py --list

    # Post a comment on one specific, already-reviewed issue:
    python post_reply.py --url https://github.com/owner/repo/issues/123 --yes

    # Edit the reply before posting instead of using the draft verbatim:
    python post_reply.py --url https://github.com/owner/repo/issues/123 \\
        --message "your edited reply text" --yes

Reject a candidate you don't want to post to (marks it done, skips it):
    python post_reply.py --url https://github.com/owner/repo/issues/123 --reject
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import requests

try:
    import lexprotocol
    lexprotocol.configure(agent_name="lexprotocol-github-reply-bot")
    _LEX_ENABLED = True
except Exception:
    _LEX_ENABLED = False

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
QUEUE_JSONL_PATH = Path(os.environ.get("QUEUE_JSONL", "~/.lexprotocol/review_queue.jsonl")).expanduser()
POST_LOG_PATH = Path(os.environ.get("POST_LOG", "~/.lexprotocol/posted_log.jsonl")).expanduser()

# Hard ceiling — even with --yes, this script refuses to post more than
# this many comments in a single calendar day.
MAX_POSTS_PER_DAY = int(os.environ.get("MAX_POSTS_PER_DAY", "3"))


def load_queue() -> list[dict]:
    if not QUEUE_JSONL_PATH.exists():
        return []
    items = []
    for line in QUEUE_JSONL_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def save_queue(items: list[dict]) -> None:
    with QUEUE_JSONL_PATH.open("w") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")


def posts_today() -> int:
    if not POST_LOG_PATH.exists():
        return 0
    today = date.today().isoformat()
    count = 0
    for line in POST_LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("posted_at", "").startswith(today):
            count += 1
    return count


def log_post(issue_url: str, comment_url: str) -> None:
    POST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with POST_LOG_PATH.open("a") as f:
        f.write(
            json.dumps(
                {
                    "issue_url": issue_url,
                    "comment_url": comment_url,
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )


def post_comment(issue_url: str, body: str) -> str:
    # issue_url looks like https://github.com/owner/repo/issues/123
    parts = issue_url.rstrip("/").split("/")
    owner, repo, issue_number = parts[-4], parts[-3], parts[-1]
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    r = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("html_url", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List pending candidates and exit")
    ap.add_argument("--url", help="Issue URL to act on")
    ap.add_argument("--message", help="Override the draft reply text")
    ap.add_argument("--yes", action="store_true", help="Confirm posting (required to actually post)")
    ap.add_argument("--reject", action="store_true", help="Mark this candidate rejected without posting")
    args = ap.parse_args()

    queue = load_queue()

    if args.list or not args.url:
        pending = [q for q in queue if q.get("status") == "pending"]
        if not pending:
            print("No pending candidates. Run find_candidates.py first.")
        for q in pending:
            print(f"- {q['issue_url']}  ({q['title']})")
        return

    match = next((q for q in queue if q["issue_url"] == args.url), None)
    if match is None:
        raise SystemExit(f"No queued candidate found for {args.url}. Run --list to see options.")

    if args.reject:
        match["status"] = "rejected"
        save_queue(queue)
        print(f"Marked rejected: {args.url}")
        return

    if not GITHUB_TOKEN:
        raise SystemExit("GITHUB_TOKEN is required. export GITHUB_TOKEN=ghp_...")

    if not args.yes:
        print("--- DRAFT (not posted) ---")
        print(args.message or match["draft_reply"])
        print("--- Re-run with --yes to actually post this. ---")
        return

    if posts_today() >= MAX_POSTS_PER_DAY:
        raise SystemExit(
            f"Already posted {posts_today()} comment(s) today "
            f"(MAX_POSTS_PER_DAY={MAX_POSTS_PER_DAY}). Try again tomorrow, "
            f"or raise the cap deliberately if you're sure."
        )

    body = args.message or match["draft_reply"]
    comment_url = post_comment(args.url, body)
    match["status"] = "posted"
    save_queue(queue)
    log_post(args.url, comment_url)

    # Dogfooding: LexProtocol attests its own growth-engine actions.
    if _LEX_ENABLED:
        try:
            lexprotocol.record(
                "GITHUB.POST_REPLY",
                result=comment_url,
                description=f"Posted human-approved reply to {args.url}",
                metadata={"issue_url": args.url},
            )
        except Exception:
            pass

    print(f"Posted: {comment_url}")


if __name__ == "__main__":
    main()
