"""
GitHub REST API calls for Layer 7.
Creates pull requests using a Personal Access Token.
Includes automatic retry for transient HTTP errors (429, 500, 502, 503).
"""
from __future__ import annotations

import base64
import re
import time
import requests
import structlog

log = structlog.get_logger(__name__)

GITHUB_API = "https://api.github.com"

_RETRYABLE_STATUS = {429, 500, 502, 503}
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request with exponential backoff retry for transient errors."""
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUS:
                resp.raise_for_status()
                return resp
            # Retryable status — back off
            wait = _BACKOFF_BASE * (2 ** attempt)
            log.warning("github.retry", status=resp.status_code, url=url[:80], wait=wait, attempt=attempt + 1)
            time.sleep(wait)
            last_exc = requests.HTTPError(response=resp)
        except requests.ConnectionError as e:
            wait = _BACKOFF_BASE * (2 ** attempt)
            log.warning("github.retry.connection", url=url[:80], wait=wait, attempt=attempt + 1)
            time.sleep(wait)
            last_exc = e
    # Final attempt — let it raise
    if last_exc:
        raise last_exc
    resp = requests.request(method, url, **kwargs)
    resp.raise_for_status()
    return resp


def get_default_branch(repo_id: str, token: str) -> str:
    """Return the repo's default branch (main/master)."""
    url = f"{GITHUB_API}/repos/{repo_id}"
    resp = _request("GET", url, headers=_headers(token), timeout=15)
    return resp.json().get("default_branch", "main")


def create_pull_request(
    repo_id: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    token: str,
    draft: bool = False,
) -> dict:
    """
    Open a pull request. Returns the PR payload dict.
    Raises on HTTP error.
    """
    url = f"{GITHUB_API}/repos/{repo_id}/pulls"
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
        "draft": draft,
    }
    log.info("github.create_pr", repo=repo_id, head=head_branch, base=base_branch)
    resp = requests.post(url, json=payload, headers=_headers(token), timeout=15)  # no retry — check 422

    if resp.status_code == 422:
        # PR already exists — find it
        data = resp.json()
        errors = data.get("errors", [])
        if any("already exists" in str(e) for e in errors):
            log.warning("github.pr_already_exists", repo=repo_id, head=head_branch)
            return _find_existing_pr(repo_id, head_branch, base_branch, token)

    resp.raise_for_status()
    pr = resp.json()
    log.info("github.pr_created", number=pr["number"], url=pr["html_url"])
    return pr


def get_pr_diff(repo_id: str, pr_number: int, token: str) -> str:
    """Fetch the unified diff of a pull request."""
    url = f"{GITHUB_API}/repos/{repo_id}/pulls/{pr_number}"
    headers = _headers(token)
    headers["Accept"] = "application/vnd.github.v3.diff"
    resp = _request("GET", url, headers=headers, timeout=30)
    return resp.text


def get_pr_review_comments(repo_id: str, pr_number: int, token: str) -> list[dict]:
    """Fetch review comments (inline code comments) on a PR."""
    url = f"{GITHUB_API}/repos/{repo_id}/pulls/{pr_number}/comments"
    resp = _request("GET", url, headers=_headers(token), timeout=15)
    comments = resp.json()
    return [
        {
            "file": c.get("path", ""),
            "line": c.get("line") or c.get("original_line", 0),
            "body": c.get("body", ""),
            "author": c.get("user", {}).get("login", ""),
        }
        for c in comments
    ]


def get_pr_issue_comments(repo_id: str, pr_number: int, token: str) -> list[dict]:
    """Fetch top-level conversation comments on a PR."""
    url = f"{GITHUB_API}/repos/{repo_id}/issues/{pr_number}/comments"
    resp = _request("GET", url, headers=_headers(token), timeout=15)
    comments = resp.json()
    return [
        {
            "body": c.get("body", ""),
            "author": c.get("user", {}).get("login", ""),
        }
        for c in comments
    ]


def get_pr_info(repo_id: str, pr_number: int, token: str) -> dict:
    """Fetch basic PR info: title, body, state."""
    url = f"{GITHUB_API}/repos/{repo_id}/pulls/{pr_number}"
    resp = _request("GET", url, headers=_headers(token), timeout=15)
    pr = resp.json()
    return {
        "title": pr.get("title", ""),
        "body": pr.get("body", ""),
        "state": pr.get("state", ""),
        "base_branch": pr.get("base", {}).get("ref", "main"),
        "head_branch": pr.get("head", {}).get("ref", ""),
    }


# ---------------------------------------------------------------------------
# Image extraction from PR comments
# ---------------------------------------------------------------------------

_IMAGE_MD_RE = re.compile(
    r'!\[([^\]]*)\]\((https?://[^\s)]+\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s)]*)?)\)',
    re.IGNORECASE,
)

_IMAGE_HTML_RE = re.compile(
    r'<img[^>]+src=["\']?(https?://[^\s"\']+\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s"\']*)?)["\']?',
    re.IGNORECASE,
)

# GitHub user-attachments URLs (uploaded via drag-and-drop)
_GITHUB_ATTACHMENT_RE = re.compile(
    r'(https://github\.com/user-attachments/assets/[^\s)"\'>]+)',
    re.IGNORECASE,
)

_MIME_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def extract_image_urls(text: str) -> list[str]:
    """Extract all image URLs from markdown/HTML text."""
    urls = set()
    for _, url in _IMAGE_MD_RE.findall(text):
        urls.add(url)
    for url in _IMAGE_HTML_RE.findall(text):
        urls.add(url)
    for url in _GITHUB_ATTACHMENT_RE.findall(text):
        urls.add(url)
    return list(urls)


def download_image_as_base64(url: str, token: str = "") -> dict | None:
    """
    Download an image URL and return a Claude vision block dict.
    Returns None if download fails.
    """
    try:
        headers = {}
        if token and "github.com" in url:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "png" in content_type:
            media_type = "image/png"
        elif "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "gif" in content_type:
            media_type = "image/gif"
        elif "webp" in content_type:
            media_type = "image/webp"
        else:
            # Guess from URL extension
            ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
            media_type = _MIME_MAP.get(ext, "image/png")

        data = base64.standard_b64encode(resp.content).decode("ascii")
        log.info("github.image_downloaded", url=url[:80], size=len(resp.content))
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
    except Exception as e:
        log.warning("github.image_download_failed", url=url[:80], error=str(e))
        return None


def extract_images_from_comments(
    comments: list[dict],
    token: str = "",
    max_images: int = 5,
) -> list[dict]:
    """
    Scan comment bodies for image URLs, download them, return Claude vision blocks.
    """
    all_urls = []
    for c in comments:
        body = c.get("body", "")
        all_urls.extend(extract_image_urls(body))

    # Deduplicate, cap at max
    seen = set()
    unique_urls = []
    for url in all_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    unique_urls = unique_urls[:max_images]

    images = []
    for url in unique_urls:
        block = download_image_as_base64(url, token)
        if block:
            images.append(block)
    return images


def _find_existing_pr(repo_id: str, head_branch: str, base_branch: str, token: str) -> dict:
    url = f"{GITHUB_API}/repos/{repo_id}/pulls"
    params = {"head": f"{repo_id.split('/')[0]}:{head_branch}", "base": base_branch, "state": "open"}
    resp = _request("GET", url, params=params, headers=_headers(token), timeout=15)
    prs = resp.json()
    if prs:
        return prs[0]
    raise RuntimeError(f"PR already exists but couldn't find it for {head_branch}")
