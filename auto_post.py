"""
LexProtocol GitHub Auto-Poster (unattended mode, for scheduled CI runs)
========================================================================
This is the unattended counterpart to post_reply.py. Running the bot
hands-off on a schedule means the "human reads review_queue.md and
approves each one before it posts" step can't happen in the moment.
To keep the same safety rails without a human in the loop for the CI
run itself:

  - Uses the exact same MAX_POSTS_PER_DAY cap as post_reply.py.
  - Uses the exact same dedupe store, so nothing gets posted twice.
  - Only posts to issues find_candidates.py already matched against
    narrow, high-intent search queries (people already asking for this
    kind of thing) -- it never posts to a repo cold.
  - Every post is logged to posted_log.jsonl, which gets committed back
    to the repo by the workflow, so there's always a durable, reviewable
    record of what it did -- you're reviewing after the fact instead of
    before, not flying blind.

If you ever want to go back to human-approve-each-one mode, just delete
the "Auto-post" step from .github/workflows/lex-github-bot.yml and run
post_reply.py by hand instead -- nothing else about the setup changes.
"""

from __future__ import annotations

from post_reply import (
    GITHUB_TOKEN,
    MAX_POSTS_PER_DAY,
    load_queue,
    save_queue,
    posts_today,
    log_post,
    post_comment,
)

try:
    import lexprotocol

    lexprotocol.configure(agent_name="lexprotocol-github-reply-bot")
    _LEX_ENABLED = True
except Exception:
    _LEX_ENABLED = False


def main():
    if not GITHUB_TOKEN:
        raise SystemExit("GITHUB_TOKEN is required (set as the LEX_BOT_TOKEN repo secret).")

    queue = load_queue()
    pending = [q for q in queue if q.get("status") == "pending"]

    remaining = MAX_POSTS_PER_DAY - posts_today()
    if remaining <= 0:
        print(f"Already posted {posts_today()} today (cap={MAX_POSTS_PER_DAY}). Nothing to do.")
        return

    to_post = pending[:remaining]
    if not to_post:
        print("No pending candidates to post.")
        return

    for item in to_post:
        try:
            comment_url = post_comment(item["issue_url"], item["draft_reply"])
        except Exception as e:
            print(f"FAILED to post {item['issue_url']}: {e}")
            continue

        item["status"] = "posted"
        log_post(item["issue_url"], comment_url)

        if _LEX_ENABLED:
            try:
                lexprotocol.record(
                    "GITHUB.POST_REPLY",
                    result=comment_url,
                    description=f"Auto-posted reply to {item['issue_url']}",
                    metadata={"issue_url": item["issue_url"]},
                )
            except Exception:
                pass

        print(f"Posted: {comment_url}")

    save_queue(queue)


if __name__ == "__main__":
    main()
