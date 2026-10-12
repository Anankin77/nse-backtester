"""The private paper-log repo client, against a fake GitHub (no network)."""
import base64

import pytest

from backtester.github_file import Conflict, GitHubError, GitHubFile


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body


class FakeGitHub:
    """Just enough of the contents API: one repo, files keyed by path, sha = version number."""

    def __init__(self, token="good", repo="me/log", files=None):
        self.token, self.repo, self.files = token, repo, dict(files or {})

    def _auth(self, headers):
        return headers["Authorization"] == f"Bearer {self.token}"

    def get(self, url, headers, timeout):
        if not self._auth(headers):
            return Resp(401)
        if url.endswith(f"/repos/{self.repo}"):
            return Resp(200)
        if f"/repos/{self.repo}/contents/" not in url:
            return Resp(404)
        path = url.split("/contents/")[1]
        if path not in self.files:
            return Resp(404)
        text, ver = self.files[path]
        return Resp(200, {"content": base64.b64encode(text.encode()).decode(), "sha": str(ver)})

    def put(self, url, headers, json, timeout):
        if not self._auth(headers):
            return Resp(401)
        path = url.split("/contents/")[1]
        current = self.files.get(path)
        if current and json.get("sha") is None:
            return Resp(422)                   # GitHub: "sha wasn't supplied"
        if current and json["sha"] != str(current[1]):
            return Resp(409)                   # someone saved in between
        ver = current[1] + 1 if current else 1
        self.files[path] = (base64.b64decode(json["content"]).decode(), ver)
        return Resp(201 if ver == 1 else 200, {"content": {"sha": str(ver)}})


def test_new_file_then_update():
    gh = FakeGitHub()
    f = GitHubFile("me/log", "paper_trades.csv", "good", session=gh)
    assert f.read() == (None, None)
    sha = f.write("date,stock\n", None, "first")
    assert f.read() == ("date,stock\n", sha)
    sha2 = f.write("date,stock\n2026-10-12,TCS\n", sha, "second")
    assert f.read()[0].endswith("TCS\n") and sha2 != sha


def test_save_from_two_devices_is_a_conflict_not_an_overwrite():
    gh = FakeGitHub(files={"paper_trades.csv": ("v1", 1)})
    phone = GitHubFile("me/log", "paper_trades.csv", "good", session=gh)
    laptop = GitHubFile("me/log", "paper_trades.csv", "good", session=gh)
    _, sha_phone = phone.read()
    _, sha_laptop = laptop.read()
    laptop.write("laptop edit", sha_laptop, "laptop")
    with pytest.raises(Conflict):
        phone.write("phone edit", sha_phone, "phone")
    assert gh.files["paper_trades.csv"][0] == "laptop edit"


@pytest.mark.parametrize("token, repo, words", [
    ("bad", "me/log", "token"),        # expired or mistyped
    ("good", "me/other", "find"),      # wrong repo name
])
def test_clear_errors(token, repo, words):
    f = GitHubFile(repo, "paper_trades.csv", token, session=FakeGitHub())
    with pytest.raises(GitHubError, match=words):
        f.read()
