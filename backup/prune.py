"""
Retention for the weekly backups (one GitHub Release per week, tagged `backup-YYYY-MM-DD`,
in the private backups repo). Run by the backup workflow right after publishing.

Nothing is ever deleted while the total size of all backup assets is within the budget.
Once it's over, past months are thinned oldest-first down to ONE backup each (the last one
of that month, since it holds the most data), stopping as soon as the total is back under
budget. The current month is never thinned, and no month ever loses its last backup - if
that still isn't enough, this exits non-zero so the workflow alerts instead of deleting more.

GitHub doesn't cap total release size, so the budget is self-imposed: 1 GB by default,
GitHub's recommended max repo size.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo uv run backup/prune.py [--budget-mb N] [--dry-run]
"""

import argparse
import logging
import os
import re
import sys
from datetime import date

import requests

TAG_RE = re.compile(r"^backup-(\d{4}-\d{2}-\d{2})$")


def list_backups(session: requests.Session, repo: str) -> list[dict]:
    """[{id, tag, day, size}] for every backup release, oldest first."""
    backups, url = [], f"https://api.github.com/repos/{repo}/releases?per_page=100"
    while url:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        for rel in resp.json():
            m = TAG_RE.match(rel["tag_name"])
            if m:
                backups.append({
                    "id": rel["id"],
                    "tag": rel["tag_name"],
                    "day": date.fromisoformat(m.group(1)),
                    "size": sum(a["size"] for a in rel["assets"]),
                })
        url = resp.links.get("next", {}).get("url")
    return sorted(backups, key=lambda b: b["day"])


def pick_deletions(backups: list[dict], budget: int, today: date) -> tuple[list[dict], int]:
    """Which backups to delete (in order) to get under budget, and the resulting total."""
    total = sum(b["size"] for b in backups)
    deletions = []
    by_month: dict[str, list[dict]] = {}
    for b in backups:
        by_month.setdefault(b["day"].strftime("%Y-%m"), []).append(b)
    for month in sorted(by_month):
        if total <= budget or month == today.strftime("%Y-%m"):
            break
        for b in by_month[month][:-1]:  # oldest first, always keeping the month's last
            if total <= budget:
                break
            deletions.append(b)
            total -= b["size"]
    return deletions, total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-mb", type=float, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    budget = int(args.budget_mb * 1e6)

    repo = os.environ["GITHUB_REPOSITORY"]
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    })

    backups = list_backups(session, repo)
    total = sum(b["size"] for b in backups)
    logging.info("%d backups, %.1f MB total, budget %.1f MB", len(backups), total / 1e6, budget / 1e6)

    deletions, total_after = pick_deletions(backups, budget, date.today())
    for b in deletions:
        logging.info("%sdeleting %s (%.1f MB)", "[dry run] " if args.dry_run else "", b["tag"], b["size"] / 1e6)
        if args.dry_run:
            continue
        session.delete(f"https://api.github.com/repos/{repo}/releases/{b['id']}", timeout=30).raise_for_status()
        session.delete(f"https://api.github.com/repos/{repo}/git/refs/tags/{b['tag']}", timeout=30).raise_for_status()

    if total_after > budget:
        logging.error(
            "still %.1f MB over budget with every past month down to one backup - raise "
            "--budget-mb or prune by hand", (total_after - budget) / 1e6,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
