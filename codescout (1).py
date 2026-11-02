"""CodeScout - starter multi-agent code scout.

Usage:  python codescout.py "image resizer language:python"
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import requests

API = "https://api.github.com/search/repositories"
TOKEN = os.getenv("GITHUB_TOKEN")  # optional, avoids rate limits
PERMISSIVE = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense"}


class SearchAgent:
    """Finds candidate repos on GitHub."""

    def run(self, query, n=5):
        headers = {"Accept": "application/vnd.github+json"}
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
        r = requests.get(
            API,
            headers=headers,
            timeout=20,
            params={"q": query, "sort": "stars", "per_page": n},
        )
        r.raise_for_status()
        return r.json()["items"]


class QualityAgent:
    """Scores a repo 0-100 from popularity, freshness and basic hygiene."""

    def run(self, repo):
        stars = min(40, math.log10(repo["stargazers_count"] + 1) * 10)
        pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - pushed).days
        fresh = 30 if days < 90 else 20 if days < 365 else 10 if days < 730 else 0
        has_desc = 10 if repo.get("description") else 0
        active = 0 if repo.get("archived") else 20
        return round(stars + fresh + has_desc + active)


class LicenseAgent:
    """Checks whether the license allows easy reuse."""

    def run(self, repo):
        spdx = ((repo.get("license") or {}).get("spdx_id") or "").lower()
        if spdx in PERMISSIVE:
            return f"OK ({spdx})"
        return f"CHECK MANUALLY ({spdx or 'no license'})"


class SandboxAgent:
    """Runs the repo's tests inside a throwaway Docker container."""

    def run(self, repo):
        if not (shutil.which("docker") and shutil.which("git")):
            return "not tested (docker/git missing)"
        if repo.get("language") != "Python":
            return "not tested (Python repos only for now)"
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", repo["clone_url"], tmp],
                check=True, capture_output=True, timeout=60,
            )
            script = (
                "pip install -q pytest; "
                "[ -f requirements.txt ] && pip install -q -r requirements.txt; "
                "pytest -q"
            )
            result = subprocess.run(
                ["docker", "run", "--rm", "--memory", "512m", "--cpus", "1",
                 "-v", f"{tmp}:/app", "-w", "/app",
                 "python:3.12-slim", "sh", "-c", script],
                capture_output=True, timeout=240,
            )
            if result.returncode == 0:
                return "PASSED"
            if result.returncode == 5:
                return "no tests found"
            return "FAILED"
        except subprocess.TimeoutExpired:
            return "not tested (timed out)"
        except Exception as e:
            return f"not tested ({type(e).__name__})"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SummaryAgent:
    """Turns the findings into a readable summary (plug an LLM in here later)."""

    def run(self, item):
        r = item["repo"]
        return (
            f"{r['full_name']}  |  score {item['score']}/100\n"
            f"  {r.get('description') or 'No description'}\n"
            f"  stars: {r['stargazers_count']}  license: {item['license']}\n"
            f"  sandbox: {item['sandbox']}\n"
            f"  {r['html_url']}\n"
        )


class Coordinator:
    """Runs the agents and ranks the results."""

    def __init__(self):
        self.search = SearchAgent()
        self.quality = QualityAgent()
        self.license = LicenseAgent()
        self.sandbox = SandboxAgent()
        self.summary = SummaryAgent()

    def scout(self, query, top_n=3):
        repos = self.search.run(query)
        items = [
            {"repo": r, "score": self.quality.run(r), "license": self.license.run(r)}
            for r in repos
        ]
        items.sort(key=lambda i: i["score"], reverse=True)
        for item in items[:top_n]:  # only sandbox the top candidates
            item["sandbox"] = self.sandbox.run(item["repo"])
            if item["sandbox"] == "PASSED":
                item["score"] += 20
            elif item["sandbox"] == "FAILED":
                item["score"] -= 20
        for item in items[top_n:]:
            item["sandbox"] = "skipped"
        items.sort(key=lambda i: i["score"], reverse=True)
        return [self.summary.run(i) for i in items]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Usage: python codescout.py "what you need"')
    for block in Coordinator().scout(" ".join(sys.argv[1:])):
        print(block)
