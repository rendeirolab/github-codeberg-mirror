#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
#     "tomli",
# ]
# ///
"""
Mirror GitHub organization repositories to Codeberg.

This script:
1. Fetches all repos from a GitHub organization (including private)
2. Creates corresponding repos on Codeberg if they don't exist
3. Mirrors using git push --mirror (excluding PR refs)

Usage:
    uv run mirror.py
    uv run mirror.py --config /path/to/config.toml
    uv run mirror.py --dry-run
    uv run mirror.py --debug
"""

import argparse
import subprocess
import sys
import time
import tomli
import httpx
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Repo:
    name: str
    clone_url: str
    ssh_url: str
    private: bool
    description: str | None
    default_branch: str


DEBUG = False
REQUEST_DELAY = 1.0
MAX_RETRIES = 5
RETRY_BASE_DELAY = 5


def main():
    global DEBUG

    parser = argparse.ArgumentParser(description="Mirror GitHub repos to Codeberg")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config.toml",
        help="Path to config file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument("--repo", type=str, help="Mirror only this specific repository")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument(
        "--check-token",
        action="store_true",
        help="Only check token permissions, don't mirror",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip repos that already exist on Codeberg (useful for initial sync)",
    )
    args = parser.parse_args()

    DEBUG = args.debug

    config = load_config(args.config)

    github_org = config["github"]["organization"]
    github_token = config["github"]["token"]
    codeberg_org = config["codeberg"]["organization"]
    codeberg_token = config["codeberg"]["token"]
    work_dir = Path(config["mirror"]["work_directory"]).expanduser()

    log("Checking GitHub token...")
    check_github_token_scopes(github_token)
    check_org_membership(github_org, github_token)

    if args.check_token:
        log("Token check complete")
        return

    work_dir.mkdir(parents=True, exist_ok=True)

    log(f"Fetching repositories from GitHub organization: {github_org}")
    github_repos = get_github_repos(github_org, github_token)
    log(f"Found {len(github_repos)} total repositories on GitHub")

    if DEBUG:
        for repo in github_repos:
            log(f"  - {repo.name} (private={repo.private})", "DEBUG")

    if args.repo:
        github_repos = [r for r in github_repos if r.name == args.repo]
        if not github_repos:
            log(f"Repository '{args.repo}' not found in organization", "ERROR")
            sys.exit(1)

    log(f"Fetching existing repositories from Codeberg organization: {codeberg_org}")
    existing_codeberg = get_codeberg_repos(codeberg_org, codeberg_token)
    log(f"Found {len(existing_codeberg)} existing repositories on Codeberg")

    if args.skip_existing:
        before_count = len(github_repos)
        github_repos = [r for r in github_repos if r.name not in existing_codeberg]
        log(
            f"Skipping {before_count - len(github_repos)} existing repos, {len(github_repos)} remaining"
        )

    success_count = 0
    fail_count = 0

    for i, repo in enumerate(github_repos):
        log(
            f"[{i+1}/{len(github_repos)}] Processing: {repo.name} (private={repo.private})"
        )

        if i > 0:
            time.sleep(REQUEST_DELAY)

        if repo.name not in existing_codeberg:
            if args.dry_run:
                log(
                    f"[DRY-RUN] Would create {repo.name} on Codeberg (private={repo.private})"
                )
            else:
                log(f"Creating {repo.name} on Codeberg")
                if not create_codeberg_repo(
                    codeberg_org,
                    codeberg_token,
                    repo.name,
                    repo.private,
                    repo.description,
                ):
                    fail_count += 1
                    continue
                time.sleep(REQUEST_DELAY)
        else:
            if not args.dry_run:
                update_codeberg_repo_visibility(
                    codeberg_org, codeberg_token, repo.name, repo.private
                )
                time.sleep(REQUEST_DELAY)

        if mirror_repo(
            repo,
            github_org,
            codeberg_org,
            work_dir,
            github_token,
            codeberg_token,
            args.dry_run,
        ):
            success_count += 1
        else:
            fail_count += 1

    log(f"Mirroring complete: {success_count} successful, {fail_count} failed")

    if fail_count > 0:
        sys.exit(1)


def log(msg: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if DEBUG:
        log(msg, "DEBUG")


def retry_on_error(func):
    def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_exception = e
                delay = RETRY_BASE_DELAY * (2**attempt)
                log(
                    f"Connection error (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay}s: {e}",
                    "WARNING",
                )
                time.sleep(delay)
        log(f"All {MAX_RETRIES} attempts failed", "ERROR")
        raise last_exception

    return wrapper


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        log(f"Config file not found: {config_path}", "ERROR")
        log("Copy config.example.toml to config.toml and fill in your tokens", "ERROR")
        sys.exit(1)

    with open(config_path, "rb") as f:
        return tomli.load(f)


def check_github_token_scopes(token: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.get("https://api.github.com/user", headers=headers)

        scopes = response.headers.get("x-oauth-scopes", "")
        log(f"GitHub token scopes: {scopes}")

        if response.status_code == 200:
            user_data = response.json()
            log(f"Authenticated as: {user_data.get('login')}")
        else:
            log(f"Token validation failed: {response.status_code}", "ERROR")
            log(f"Response: {response.text}", "ERROR")

        rate_limit = response.headers.get("x-ratelimit-remaining", "unknown")
        log(f"GitHub API rate limit remaining: {rate_limit}")


def check_org_membership(org: str, token: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"https://api.github.com/orgs/{org}", headers=headers)
        if response.status_code == 200:
            org_data = response.json()
            log(f"Organization: {org_data.get('login')}")
            log(f"  Public repos: {org_data.get('public_repos')}")
            log(
                f"  Total private repos: {org_data.get('total_private_repos', 'N/A (need admin)')}"
            )
            log(
                f"  Owned private repos: {org_data.get('owned_private_repos', 'N/A (need admin)')}"
            )


def get_github_repos(org: str, token: str) -> list[Repo]:
    repos = []
    page = 1
    per_page = 100

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    with httpx.Client(timeout=30.0) as client:
        while True:
            url = f"https://api.github.com/orgs/{org}/repos"
            params = {
                "page": page,
                "per_page": per_page,
                "type": "all",
            }

            debug(f"Fetching page {page}: {url}")

            response = client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                log(f"GitHub API error: {response.status_code}", "ERROR")
                log(f"Response: {response.text}", "ERROR")
                sys.exit(1)

            data = response.json()
            if not data:
                break

            for repo_data in data:
                debug(
                    f"  Found repo: {repo_data['name']} (private={repo_data['private']})"
                )
                repos.append(
                    Repo(
                        name=repo_data["name"],
                        clone_url=repo_data["clone_url"],
                        ssh_url=repo_data["ssh_url"],
                        private=repo_data["private"],
                        description=repo_data["description"],
                        default_branch=repo_data["default_branch"],
                    )
                )

            page += 1

            if len(data) < per_page:
                break

    public_count = sum(1 for r in repos if not r.private)
    private_count = sum(1 for r in repos if r.private)
    log(f"Found {len(repos)} repos: {public_count} public, {private_count} private")

    return repos


@retry_on_error
def get_codeberg_repos(org: str, token: str) -> set[str]:
    repos = set()
    page = 1

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
    }

    with httpx.Client(timeout=60.0) as client:
        while True:
            url = f"https://codeberg.org/api/v1/orgs/{org}/repos"
            params = {"page": page, "limit": 50}

            response = client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                log(f"Codeberg API error: {response.status_code}", "ERROR")
                log(f"Response: {response.text}", "ERROR")
                sys.exit(1)

            data = response.json()
            if not data:
                break

            for repo_data in data:
                repos.add(repo_data["name"])

            page += 1
            time.sleep(REQUEST_DELAY)

            if len(data) < 50:
                break

    return repos


@retry_on_error
def create_codeberg_repo(
    org: str, token: str, name: str, private: bool, description: str | None
) -> bool:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "name": name,
        "private": private,
        "description": description or "",
        "auto_init": False,
    }

    with httpx.Client(timeout=60.0) as client:
        url = f"https://codeberg.org/api/v1/orgs/{org}/repos"
        response = client.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            return True
        elif response.status_code == 409:
            return True
        else:
            log(
                f"Failed to create repo {name}: {response.status_code} {response.text}",
                "ERROR",
            )
            return False


def mirror_repo(
    github_repo: Repo,
    github_org: str,
    codeberg_org: str,
    work_dir: Path,
    github_token: str,
    codeberg_token: str,
    dry_run: bool = False,
) -> bool:
    """Mirror a single repository from GitHub to Codeberg."""
    repo_dir = work_dir / github_repo.name

    github_url = f"https://x-access-token:{github_token}@github.com/{github_org}/{github_repo.name}.git"
    codeberg_url = f"https://mirror:{codeberg_token}@codeberg.org/{codeberg_org}/{github_repo.name}.git"

    if dry_run:
        log(
            f"[DRY-RUN] Would mirror {github_repo.name} (private={github_repo.private})"
        )
        return True

    max_retries = MAX_RETRIES

    for attempt in range(max_retries):
        try:
            if repo_dir.exists():
                log(f"Updating mirror for {github_repo.name}")
                result = subprocess.run(
                    ["git", "remote", "update", "--prune"],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log(f"Remote update failed: {result.stderr}", "WARNING")
            else:
                log(f"Cloning {github_repo.name} as bare mirror")
                result = subprocess.run(
                    ["git", "clone", "--bare", "--mirror", github_url, str(repo_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    log(
                        f"Clone failed for {github_repo.name}: {result.stderr}", "ERROR"
                    )
                    return False

            log(f"Pushing {github_repo.name} to Codeberg")

            # Get all refs, excluding GitHub's internal PR refs
            result = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                log(f"Failed to list refs: {result.stderr}", "ERROR")
                return False

            all_refs = result.stdout.strip().split("\n")

            # Filter out PR refs and other internal GitHub refs
            refs_to_push = [
                ref for ref in all_refs if ref and not ref.startswith("refs/pull/")
            ]

            debug(
                f"Pushing {len(refs_to_push)} refs (excluded {len(all_refs) - len(refs_to_push)} PR refs)"
            )

            if not refs_to_push:
                log(f"{github_repo.name}: No refs to push")
                return True

            # Push all refs except PR refs using refspec
            # We push heads, tags, and other refs separately
            push_specs = [
                "refs/heads/*:refs/heads/*",  # All branches
                "refs/tags/*:refs/tags/*",  # All tags
            ]

            result = subprocess.run(
                ["git", "push", "--force", codeberg_url] + push_specs,
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                if "Everything up-to-date" in result.stderr:
                    log(f"{github_repo.name}: Already up-to-date")
                    return True
                elif (
                    "Could not connect" in result.stderr
                    or "Connection refused" in result.stderr
                ):
                    raise ConnectionError(result.stderr)
                else:
                    log(f"Push failed for {github_repo.name}: {result.stderr}", "ERROR")
                    return False
            else:
                # Check if anything was actually pushed
                if result.stderr.strip():
                    debug(f"Push output: {result.stderr.strip()}")

            return True

        except (subprocess.CalledProcessError, ConnectionError) as e:
            delay = RETRY_BASE_DELAY * (2**attempt)
            log(
                f"Connection error for {github_repo.name} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s",
                "WARNING",
            )
            time.sleep(delay)

    log(f"All {max_retries} attempts failed for {github_repo.name}", "ERROR")
    return False


@retry_on_error
def update_codeberg_repo_visibility(
    org: str,
    token: str,
    name: str,
    private: bool,
) -> bool:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "private": private,
    }

    with httpx.Client(timeout=60.0) as client:
        url = f"https://codeberg.org/api/v1/repos/{org}/{name}"
        response = client.patch(url, headers=headers, json=payload)

        if response.status_code == 200:
            return True
        else:
            log(
                f"Failed to update visibility for {name}: {response.status_code}",
                "WARNING",
            )
            return False


if __name__ == "__main__":
    main()
