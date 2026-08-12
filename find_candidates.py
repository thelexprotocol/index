"""
LexProtocol GitHub Candidate Finder (v2 — inbound-only redesign)
=================================================================
This REPLACES bot.py's approach of searching for repos and opening
unsolicited promotional issues on them. That pattern is unsolicited bulk
outreach on other people's repos — the kind of thing GitHub's Acceptable
Use Policies treat as spam/abuse regardless of how polite the copy is,
and it risks the token/account being rate-limited or banned.

What this script does instead:
    1. Searches GitHub ISSUES (not repos) for people who are ALREADY
       asking for something LexProtocol solves — e.g. "how do I audit
       what my agent did", "need a trust score for my AI agent", etc.
    2. Writes candidates + a *draft* reply to a local review queue file.
       It NEVER posts anything itself.
    3. You (a human) read review_queue.md, delete/edit anything that
       doesn't fit, and only approved issues get posted — by
       post_reply.py, one at a time, with a hard daily cap.

This turns "cold spam 10 repos/hour forever" into "occasionally reply,
with a real answer, to people who already asked the question" — which
is normal, welcome participation in a community, not solicitation.

Setup:
    export GITHUB_TOKEN=ghp_your_token_here
    pip install requests
    python find_candidates.py

Output:
    ~/.lexprotocol/review_queue.md   — human-readable, for you to review
    ~/.lexprotocol/review_queue.jsonl — same data, structured (used by post_reply.py)
    ~/.lexprotocol/candidates.db      — dedupe store so re-runs don't re-surface issues you've already seen
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# How far back to look for issues (avoid resurfacing ancient/stale threads)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))

# Cap how many NEW candidates we surface per run — this is a review queue,
# not a firehose. Keep it small enough that a human can actually read it.
MAX_CANDIDATES_PER_RUN = int(os.environ.get("MAX_CANDIDATES_PER_RUN", "15"))

DB_PATH = Path(os.environ.get("BOT_DB", "~/.lexprotocol/candidates.db")).expanduser()
QUEUE_MD_PATH = Path(os.environ.get("QUEUE_MD", "~/.lexprotocol/review_queue.md")).expanduser()
QUEUE_JSONL_PATH = Path(os.environ.get("QUEUE_JSONL", "~/.lexprotocol/review_queue.jsonl")).expanduser()

# Searches are for issues where someone is asking for exactly this,
# not just "uses an agent framework". Specific > broad.
SEARCH_QUERIES = [
    '"audit trail" agent in:title,body',
    '"trust score" AI agent in:title,body',
    'how to verify what my agent did in:body',
    '"agent accountability" in:title,body',
    '"attestation" AI agent in:title,body',
    'log every agent action verifiable in:body',
]

EXCLUDE_IF_MENTIONS = ["lexprotocol"]  # skip issues that already mention us


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen_issues (
            issue_url TEXT PRIMARY KEY,
            seen_at   TEXT NOT NULL,
            status    TEXT DEFAULT 'pending'  -- pending | approved | rejected | posted
        )"""
    )
    conn.commit()
    return conn


def already_seen(conn: sqlite3.Connection, issue_url: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_issues WHERE issue_url = ?", (issue_url,)).fetchone()
    return bool(row)


def mark_seen(conn: sqlite3.Connection, issue_url: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_issues VALUES (?, ?, 'pending')",
        (issue_url, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def search_issues(self, query: str, since: str) -> dict:
        r = self.session.get(
            f"{self.BASE}/search/issues",
            params={
                "q": f"{query} is:issue is:open created:>={since}",
                "sort": "created",
                "order": "desc",
                "per_page": 20,
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


def iter_candidates(gh: GitHubClient, queries: list[str], since: str) -> Iterator[dict]:
    for query in queries:
        try:
            result = gh.search_issues(query, since)
            for item in result.get("items", []):
                yield item
            time.sleep(2)  # be polite to the search rate limit
        except requests.HTTPError as exc:
            if exc.response.status_code == 422:
                continue  # query syntax GitHub didn't like — skip it
            raise


def draft_reply(issue: dict) -> str:
    """A short, specific, non-salesy reply anchored to what THEY asked."""
    title = issue.get("title", "")
    return f"""Saw this while looking through issues on "{title}" — LexProtocol might be relevant here.

It's a small library that signs and timestamps every action an agent takes, so you get a verifiable record after the fact instead of just internal logs. 3-line integration (`@attest` decorator), and there's a LangChain callback handler too if that's your stack.

Docs: https://thelexprotocol.com — happy to answer questions if useful, and no worries if it's not a fit for what you're building.

(I'm the person behind LexProtocol — flagging that up front. Feel free to tell me to buzz off if this isn't welcome here.)"""


def main():
    if not GITHUB_TOKEN:
        raise SystemExit("GITHUB_TOKEN is required. export GITHUB_TOKEN=ghp_...")

    gh = GitHubClient(GITHUB_TOKEN)
    conn = init_db(DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    new_candidates = []
    seen_this_run: set[str] = set()

    for issue in iter_candidates(gh, SEARCH_QUERIES, since):
        url = issue.get("html_url", "")
        if not url or url in seen_this_run:
            continue
        seen_this_run.add(url)

        if already_seen(conn, url):
            continue

        body_lower = (issue.get("body") or "").lower()
        title_lower = (issue.get("title") or "").lower()
        if any(term in body_lower or term in title_lower for term in EXCLUDE_IF_MENTIONS):
            mark_seen(conn, url)  # record it so we don't keep re-checking it
            continue

        # Skip pull requests that show up in issue search
        if "pull_request" in issue:
            continue

        mark_seen(conn, url)
        new_candidates.append(issue)
        if len(new_candidates) >= MAX_CANDIDATES_PER_RUN:
            break

    QUEUE_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_MD_PATH.open("a") as md, QUEUE_JSONL_PATH.open("a") as jl:
        md.write(f"\n\n## Run at {datetime.now(timezone.utc).isoformat()} — {len(new_candidates)} new candidate(s)\n")
        if not new_candidates:
            md.write("(none this run)\n")
        for issue in new_candidates:
            reply = draft_reply(issue)
            md.write(
                f"\n### [{issue['title']}]({issue['html_url']})\n"
                f"Repo: {issue['repository_url'].replace('https://api.github.com/repos/', '')} | "
                f"Opened: {issue.get('created_at', '')}\n\n"
                f"**Draft reply (NOT posted — review before approving):**\n\n> "
                + reply.replace("\n", "\n> ")
                + "\n\n---\n"
            )
            jl.write(
                json.dumps(
                    {
                        "issue_url": issue["html_url"],
                        "title": issue["title"],
                        "repo": issue["repository_url"].replace("https://api.github.com/repos/", ""),
                        "draft_reply": reply,
                        "status": "pending",
                    }
                )
                + "\n"
            )

    print(f"Found {len(new_candidates)} new candidate(s).")
    print(f"Review: {QUEUE_MD_PATH}")
    print("Nothing was posted. Use post_reply.py to post specific, approved issues by hand.")


if __name__ == "__main__":
    main()
