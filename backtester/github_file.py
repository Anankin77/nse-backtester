"""Read and write one file in a GitHub repo through the GitHub API.

Used to keep the paper log in a separate private repo, so it survives app restarts
and is the same on your phone and laptop. Needs a fine-grained token with
"Contents: read and write" on that one repo only.
"""
from __future__ import annotations

import base64

import requests

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class Conflict(GitHubError):
    """The file changed since it was read (e.g. saved from another device)."""


class GitHubFile:
    def __init__(self, repo: str, path: str, token: str, session=None, api: str = API):
        self.repo, self.path, self.api = repo.strip().strip("/"), path.strip().lstrip("/"), api.rstrip("/")
        self.url = f"{self.api}/repos/{self.repo}/contents/{self.path}"
        self.http = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token.strip()}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def read(self) -> tuple[str | None, str | None]:
        """(text, sha). (None, None) if the repo exists but the file doesn't yet."""
        r = self.http.get(self.url, headers=self.headers, timeout=20)
        if r.status_code == 404:
            repo = self.http.get(f"{self.api}/repos/{self.repo}", headers=self.headers, timeout=20)
            if repo.status_code != 200:
                self._fail(repo)
            return None, None
        if r.status_code != 200:
            self._fail(r)
        body = r.json()
        return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]

    def write(self, text: str, sha: str | None, message: str) -> str:
        """Save `text`. `sha` is what read() returned (None for a new file). Returns the new sha."""
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        r = self.http.put(self.url, headers=self.headers, json=body, timeout=20)
        if r.status_code in (409, 422):
            raise Conflict("The log was changed somewhere else since you opened it. "
                           "Press Reload, then redo your edit.")
        if r.status_code not in (200, 201):
            self._fail(r)
        return r.json()["content"]["sha"]

    def _fail(self, r):
        why = {
            401: "GitHub rejected the token. It may have expired or been pasted wrong.",
            403: f"The token can't write to {self.repo}. Give it 'Contents: read and write' on that repo.",
            404: f"Can't find {self.repo}. Check the repo name, and that the token has access to it.",
        }.get(r.status_code, f"GitHub error {r.status_code}.")
        raise GitHubError(why)
