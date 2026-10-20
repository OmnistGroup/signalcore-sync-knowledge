"""Keeps a SignalCore agent's knowledge in step with this repository (README.md).

Reads `.signalcore/knowledge.yml`: the agent, the globs of the files it should know, those to
leave out, and the prefix its copies go under (`repo/` by default), so they never clash with the
agent's own files. Then, in the repository's checkout:

- since the commit synced last (kept on the agent as `<prefix>.synced`), each file the globs take
  that was added or changed is put as a searchable file (the API indexes it), each removed one is
  deleted;
- with nothing synced yet, that commit gone from the repository, the config changed, or a full
  sync asked for (`full: true`, a manual run), every file the globs take is put and every copy
  under the prefix that no longer has one is deleted.

What the API can't hold is skipped with a warning, its copy removed: a binary without words to
index (PDF, Word, Excel and PowerPoint have them), a file past 50 MB, a name with characters a
file's path can't have, a link. Calls past the rate limit wait for it; a file that fails is
reported and the run fails at the end, without moving `.synced`, so the next run tries it again.
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

import yaml

# The API's rules for a file: its name, its size.
PATH = re.compile(r"^[\w .\-()]+(/[\w .\-()]+)*$")
MAX_FILE = 50_000_000
# Files with a NUL byte are binaries, except these, which have words the API reads.
WORDS = frozenset({".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"})
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml")
SYNCED = ".synced"
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # what `.synced` may hold
TRIES = 5


@dataclass(frozen=True)
class Config:
    agent: str
    prefix: str
    include: list[re.Pattern[str]]
    exclude: list[re.Pattern[str]]

    def wants(self, path: str) -> bool:
        if path == SYNCED:  # its copy would be the marker
            return False
        return any(p.fullmatch(path) for p in self.include) and not any(
            p.fullmatch(path) for p in self.exclude
        )


def load(path: Path) -> Config:
    """The config, refused with what is wrong in it."""
    if not path.is_file():
        fail(f"No {path}: it names the agent and the files it should know (README.md)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agent, prefix = data.get("agent"), data.get("prefix", "repo/")
    include, exclude = data.get("include"), data.get("exclude") or []
    if not isinstance(agent, str) or not agent.startswith("agt_"):
        fail(f"{path}: `agent` is the agent's id, agt_…")
    if not isinstance(prefix, str) or not prefix.endswith("/") or not PATH.match(prefix[:-1]):
        fail(f"{path}: `prefix` is a folder ending in /, such as repo/")
    if not _globs(include) or not include or not _globs(exclude):
        fail(f"{path}: `include` (and `exclude`) are lists of globs, such as docs/**")
    return Config(agent, prefix, [pattern(g) for g in include], [pattern(g) for g in exclude])


def _globs(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(g, str) and g for g in value)


def pattern(glob: str) -> re.Pattern[str]:
    """A glob as .gitignore reads it: `*` and `?` within a folder, `**` across folders; one
    without a `/` matches a name in any folder, one with it from the repository's root."""
    glob = glob.strip().lstrip("/")
    if "/" not in glob.rstrip("/"):
        glob = f"**/{glob}"
    if glob.endswith("/"):
        glob += "**"
    regex = ""
    for token in re.split(r"(\*\*/|\*\*|\*|\?)", glob):
        regex += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(
            token, re.escape(token)
        )
    return re.compile(regex)


class Api:
    """The public API, as the key in the repository's secrets reaches it."""

    def __init__(self, base: str, key: str, org: str, agent: str) -> None:
        if not base.startswith(("https://", "http://")):
            fail(f"`api` is an http(s) address, not {base}")
        self.base = f"{base.rstrip('/')}/agents/{quote(agent, safe='')}/files"
        self.headers = {"Authorization": f"Bearer {key}", "User-Agent": "signalcore-sync"}
        if org:
            self.headers["SignalCore-Org"] = org

    def listing(self) -> list[str]:
        return [row["path"] for row in json.loads(self._call("GET", ""))]

    def read(self, path: str) -> str | None:
        try:
            return self._call("GET", f"/{quote(path)}").decode().strip()
        except Refused as refused:
            if refused.status == 404:
                return None
            raise

    def put(self, path: str, content: bytes, media_type: str, *, searchable: bool) -> None:
        query = "?searchable=true" if searchable else ""
        self._call("PUT", f"/{quote(path)}{query}", content, media_type)

    def delete(self, path: str) -> None:
        try:
            self._call("DELETE", f"/{quote(path)}")
        except Refused as refused:
            if refused.status != 404:  # already gone is gone
                raise

    def _call(
        self, method: str, path: str, body: bytes | None = None, media_type: str = ""
    ) -> bytes:
        """The call's answer; past the rate limit, after `Retry-After`; a server error or a
        dropped connection tried again."""
        headers = {**self.headers, **({"Content-Type": media_type} if media_type else {})}
        for attempt in range(1, TRIES + 1):
            request = urllib.request.Request(  # noqa: S310 (http(s), checked in __init__)
                self.base + path, body, headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310
                    return answer.read()
            except urllib.error.HTTPError as error:
                text = error.read().decode(errors="replace")
                if attempt < TRIES and error.code == 429:
                    time.sleep(int(error.headers.get("Retry-After") or 10))
                elif attempt < TRIES and error.code >= 500:
                    time.sleep(2**attempt)
                else:
                    raise Refused(error.code, _detail(text)) from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == TRIES:
                    raise Refused(0, str(error)) from None
                time.sleep(2**attempt)
        raise AssertionError("unreachable")


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status} {detail}" if status else detail)
        self.status = status


def _detail(text: str) -> str:
    try:
        return str(json.loads(text).get("detail", text))
    except (ValueError, AttributeError):
        return text[:300]


def git(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 (our own arguments)
        ["git", *args],  # noqa: S607 (the runner's git)
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def has_commit(sha: str) -> bool:
    """Whether the commit is here, fetched when the checkout is shallow; False when the
    repository no longer has it (a force push)."""
    try:
        git("cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        pass
    try:
        git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", sha)
        return True
    except subprocess.CalledProcessError:
        return False


def changes(since: str) -> list[tuple[str, str]]:
    """Each file added, changed or removed since `since`, as `(status, path)`; a rename is a
    removal and an addition."""
    out = git("diff", "--no-renames", "--name-status", "-z", since, "HEAD").split("\0")
    return [(out[i][0], out[i + 1]) for i in range(0, len(out) - 1, 2)]


def payload(path: str, name: str) -> tuple[bytes, str] | str:
    """The file's bytes and type as the API takes them, or why it can't."""
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        return "a link or not a file"
    if not PATH.match(name):
        return "a name with characters a file's path can't have"
    if file.stat().st_size > MAX_FILE:
        return "bigger than 50 MB"
    content = file.read_bytes()
    guessed = mimetypes.guess_type(path, strict=False)[0] or ""
    if file.suffix.lower() in WORDS:
        return content, guessed or "application/octet-stream"
    if b"\0" in content[:8000]:
        return "a binary without words to index"
    return content, guessed if guessed.startswith(TEXT_TYPES) else "text/plain"


def main() -> None:
    config_file = Path(os.environ.get("SIGNALCORE_CONFIG") or ".signalcore/knowledge.yml")
    config = load(config_file)
    key = os.environ.get("SIGNALCORE_KEY") or fail("No key: pass `key` from the repo's secrets")
    api = Api(
        os.environ.get("SIGNALCORE_API") or "https://signalcore.ai/v1",
        key,
        os.environ.get("SIGNALCORE_ORG", ""),
        config.agent,
    )
    head = git("rev-parse", "HEAD").strip()
    marker = config.prefix + SYNCED
    synced = api.read(marker)
    if synced and not SHA.match(synced):
        synced = None  # not a commit: as if nothing was synced
    asked = os.environ.get("SIGNALCORE_FULL") == "true" or os.environ.get("GITHUB_EVENT_NAME") in (
        "workflow_dispatch",
        "schedule",
    )
    diff = changes(synced) if synced and not asked and has_commit(synced) else None
    if diff is not None and any(path == config_file.as_posix() for _, path in diff):
        diff = None  # other globs: which files the agent should have is worked out anew

    puts: list[str] = []
    removals: list[str] = []
    if diff is None:
        print(f"Full sync of {head[:7]}" + (f" (last synced {synced[:7]})" if synced else ""))
        puts = [p for p in git("ls-files", "-z").split("\0") if p and config.wants(p)]
        kept = {config.prefix + path for path in puts}
        removals = [
            path[len(config.prefix) :]
            for path in api.listing()
            if path.startswith(config.prefix) and path != marker and path not in kept
        ]
    else:
        print(f"Changes from {synced[:7]} to {head[:7]}")
        for status, path in diff:
            if config.wants(path):
                (removals if status == "D" else puts).append(path)

    counts = {"put": 0, "removed": 0, "skipped": 0, "failed": 0}
    for path in puts:
        name = config.prefix + path
        try:
            got = payload(path, name)
            if isinstance(got, str):
                print(f"::warning file={path}::Skipped: {got}")
                counts["skipped"] += 1
                api.delete(name)  # an older copy of it shouldn't stay
            else:
                api.put(name, *got, searchable=True)
                print(f"  put     {path}")
                counts["put"] += 1
        except Refused as refused:
            print(f"::error file={path}::{refused}")
            counts["failed"] += 1
    for path in removals:
        try:
            api.delete(config.prefix + path)
            print(f"  removed {path}")
            counts["removed"] += 1
        except Refused as refused:
            print(f"::error file={path}::{refused}")
            counts["failed"] += 1

    summary = ", ".join(f"{n} {what}" for what, n in counts.items() if n) or "nothing to do"
    print(summary)
    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as out:
            out.write(f"SignalCore knowledge of `{config.agent}` at {head[:7]}: {summary}\n")
    if counts["failed"]:
        sys.exit(1)
    if synced != head:
        api.put(marker, head.encode(), "text/plain", searchable=False)


def fail(message: str) -> NoReturn:
    print(f"::error::{message}")
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Refused as refused:  # the listing, the marker: nothing can be synced without them
        fail(f"SignalCore refused: {refused}")
