"""Git repository fetcher (design §8.3).

Full bare clone when the repository is small enough (or of unknown size), shallow
``--depth N --single-branch`` otherwise or when the full clone fails or times out. The
bare repository is packed into ``repository.git.tar.gz``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from alexandria.config import Settings
from alexandria.fetchers.base import (
    FetchError,
    PermanentFetchError,
    SnapshotResult,
    http_client,
)
from alexandria.models import Artifact, Kind, Resource
from alexandria.security import BlockedURL, check_url, pinned_address
from alexandria.storage.files import describe_file
from alexandria.urls import Detected, detect

log = logging.getLogger(__name__)

ARCHIVE_NAME = "repository.git.tar.gz"
_README_NAMES = ("readme.md", "readme.markdown", "readme.rst", "readme.txt", "readme")
_NOT_FOUND_MARKERS = ("repository not found", "not found", "does not appear to be a git")


@dataclass
class CloneResult:
    path: Path
    history: str  # full | shallow
    depth: int | None


@dataclass
class ArchivedRepo:
    path: Path
    clone_url: str
    history: str  # full | shallow


class GitError(FetchError):
    def __init__(self, message: str, permanent: bool = False, too_large: bool = False) -> None:
        super().__init__(message, permanent=permanent or too_large)
        self.too_large = too_large


class GitRepoFetcher:
    def __init__(self, settings: Settings, allowed_protocols: tuple[str, ...] = ("https",)):
        self.settings = settings
        # Tests add "file" to clone local fixtures; production is https only.
        self.allowed_protocols = allowed_protocols

    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult:
        url = resource.source_url or resource.canonical_url
        assert url
        if url.startswith("file://") and "file" in self.allowed_protocols:
            detected = Detected(kind=Kind.GIT_REPO, canonical_url=url, clone_url=url)
        else:
            detected = detect(url, resource.kind)
        clone_url = detected.clone_url or url
        meta: dict[str, Any] = {}
        artifacts: list[Artifact] = []
        title: str | None = None
        size_kb: int | None = None
        readme_found = False

        if detected.forge == "github" and detected.owner and detected.repo:
            meta["forge"] = "github"
            info = await self._github(detected.owner, detected.repo, staging_dir)
            if info is not None:
                meta.update(_github_meta(info))
                size_kb = info.get("size")
                title = info.get("full_name")
                clone_url = info.get("clone_url") or clone_url
                artifacts.append(describe_file(staging_dir / "github.json", "github"))
            readme = staging_dir / "README.md"
            if readme.exists():
                artifacts.append(describe_file(readme, "readme"))
                readme_found = True

        pin = await self._guard(clone_url)
        work = staging_dir / "work"
        work.mkdir()
        repo_meta: dict[str, Any] = {}
        size_mb = 0.0
        clone: CloneResult | None = None
        try:
            clone = await self._clone(clone_url, work, size_kb, pin)
        except GitError as exc:
            if not exc.too_large:
                raise
            meta["archive_skipped"] = "too_large"  # keep metadata and README (design §8.3)

        if clone is not None:
            repo_meta = await self._inspect(clone.path)
            if not meta.get("default_branch"):
                meta["default_branch"] = repo_meta.get("default_branch")
            if repo_meta.get("lfs"):
                meta["lfs"] = True
            if not readme_found and repo_meta.get("readme_name"):
                readme = staging_dir / "README.md"
                text = await self._git_show(clone.path, repo_meta["readme_name"])
                if text is not None:
                    readme.write_text(text, encoding="utf-8")
                    artifacts.append(describe_file(readme, "readme"))
                    readme_found = True
            size_mb = _dir_size(clone.path) / 1_000_000
            if size_mb > self.settings.git.max_archive_mb:
                meta["archive_skipped"] = "too_large"
            else:
                archive = staging_dir / ARCHIVE_NAME
                await asyncio.to_thread(_make_tarball, clone.path, archive)
                artifacts.insert(
                    0,
                    describe_file(
                        archive,
                        "repository",
                        history=clone.history,
                        depth=clone.depth,
                        head_commit=repo_meta.get("head_commit"),
                        commit_count=repo_meta.get("commit_count"),
                        default_branch=repo_meta.get("default_branch"),
                    ),
                )
        shutil.rmtree(work)

        fetch_json = staging_dir / "fetch.json"
        fetch_json.write_text(
            json.dumps(
                {
                    "clone_url": clone_url,
                    "history": clone.history if clone else None,
                    "depth": clone.depth if clone else None,
                    "bare_size_mb": round(size_mb, 2),
                    **{k: v for k, v in repo_meta.items() if k != "readme_name"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts.append(describe_file(fetch_json, "fetch"))
        if title is None:
            title = _title_from_url(detected.canonical_url)
        return SnapshotResult(
            artifacts=artifacts,
            meta=meta,
            title=title,
            text_role="readme" if readme_found else None,
        )

    async def archive(self, url: str, staging_dir: Path) -> ArchivedRepo:
        """Clone ``url`` (a repository page or ``.git`` URL) into ``<name>.git.tar.gz``.

        Used to attach a repository to another resource: the same bare clone, limits and
        guards as a fetch, without the GitHub metadata or README.
        """
        if url.startswith("file://") and "file" in self.allowed_protocols:
            detected = Detected(kind=Kind.GIT_REPO, canonical_url=url, clone_url=url)
        else:
            detected = detect(url, Kind.GIT_REPO)
        clone_url = detected.clone_url or url
        if detected.owner and detected.repo:
            name = f"{detected.owner}-{detected.repo}"
        else:
            name = _title_from_url(detected.canonical_url).replace("/", "-")
        pin = await self._guard(clone_url)
        work = staging_dir / "work"
        work.mkdir()
        try:
            clone = await self._clone(clone_url, work, None, pin)
            if _dir_size(clone.path) / 1_000_000 > self.settings.git.max_archive_mb:
                raise GitError("repository is larger than git.max_archive_mb", too_large=True)
            archive = staging_dir / f"{name}.git.tar.gz"
            await asyncio.to_thread(_make_tarball, clone.path, archive)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return ArchivedRepo(archive, clone_url, clone.history)

    # -- GitHub API ----------------------------------------------------------------

    async def _github(self, owner: str, repo: str, staging: Path) -> dict[str, Any] | None:
        api = self.settings.github.api_url.rstrip("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github.token:
            headers["Authorization"] = f"Bearer {self.settings.github.token}"
        try:
            await check_url(api, self.settings.security.allow_private_hosts)
        except BlockedURL as exc:
            raise PermanentFetchError(f"blocked by SSRF guard: {exc}") from exc
        async with http_client(self.settings) as client:
            try:
                response = await client.get(f"{api}/repos/{owner}/{repo}", headers=headers)
            except httpx.HTTPError as exc:
                log.warning("GitHub API unreachable for %s/%s: %s", owner, repo, exc)
                return None
            if response.status_code == 404:
                raise PermanentFetchError(f"GitHub repository {owner}/{repo} not found")
            if response.status_code != 200:
                # Rate limit or outage: clone anyway, without metadata.
                log.warning("GitHub API returned %s for %s/%s", response.status_code, owner, repo)
                return None
            info: dict[str, Any] = response.json()
            (staging / "github.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
            try:
                readme = await client.get(
                    f"{api}/repos/{owner}/{repo}/readme",
                    headers=headers | {"Accept": "application/vnd.github.raw+json"},
                )
                if (
                    readme.status_code == 200
                    and len(readme.content) <= self.settings.fetch.max_bytes
                ):
                    (staging / "README.md").write_bytes(readme.content)
            except httpx.HTTPError as exc:
                log.warning("README fetch failed for %s/%s: %s", owner, repo, exc)
        return info

    # -- git ----------------------------------------------------------------------

    async def _guard(self, clone_url: str) -> str | None:
        """Check the clone URL; returns git's ``http.curloptResolve`` entry pinning the host
        to the checked address, so git cannot resolve it again to another (DNS rebinding).
        """
        scheme = clone_url.split(":", 1)[0].lower()
        if scheme not in self.allowed_protocols:
            raise PermanentFetchError(f"git protocol {scheme!r} is not allowed")
        if scheme == "file":
            return None
        try:
            address = await pinned_address(
                clone_url, self.settings.security.allow_private_hosts, ("https",)
            )
        except BlockedURL as exc:
            raise PermanentFetchError(f"blocked by SSRF guard: {exc}") from exc
        if address is None:
            return None
        parts = urlsplit(clone_url)
        ip = f"[{address}]" if ":" in address else address
        return f"{parts.hostname}:{parts.port or 443}:{ip}"

    async def _clone(
        self, url: str, work: Path, size_kb: int | None, pin: str | None = None
    ) -> CloneResult:
        target = work / "repository.git"
        cfg = self.settings.git
        too_big = size_kb is not None and size_kb / 1024 > cfg.full_history_max_mb
        if not too_big:
            try:
                await self._git(
                    ["clone", "--bare", url, str(target)],
                    timeout=cfg.clone_timeout_s,
                    watch=target,
                    pin=pin,
                )
                return CloneResult(target, "full", None)
            except GitError as exc:
                if exc.permanent or exc.too_large:
                    raise
                log.warning("full clone of %s failed (%s); falling back to shallow", url, exc)
                shutil.rmtree(target, ignore_errors=True)
        await self._git(
            [
                "clone",
                "--bare",
                "--depth",
                str(cfg.shallow_depth),
                "--single-branch",
                url,
                str(target),
            ],
            timeout=cfg.clone_timeout_s,
            watch=target,
            pin=pin,
        )
        return CloneResult(target, "shallow", cfg.shallow_depth)

    async def _inspect(self, repo: Path) -> dict[str, Any]:
        info: dict[str, Any] = {}
        head = await self._git(["rev-parse", "HEAD"], cwd=repo, check=False)
        if head is None:
            return info  # empty repository
        info["head_commit"] = head.strip()
        count = await self._git(["rev-list", "--count", "HEAD"], cwd=repo, check=False)
        info["commit_count"] = int(count.strip()) if count else None
        branch = await self._git(["symbolic-ref", "--short", "HEAD"], cwd=repo, check=False)
        info["default_branch"] = branch.strip() if branch else None
        refs = await self._git(["for-each-ref", "--format=%(refname)"], cwd=repo, check=False)
        lines = (refs or "").split()
        info["branch_count"] = sum(1 for r in lines if r.startswith("refs/heads/"))
        info["tag_count"] = sum(1 for r in lines if r.startswith("refs/tags/"))
        attributes = await self._git_show(repo, ".gitattributes")
        if attributes and "filter=lfs" in attributes:
            info["lfs"] = True
        names = await self._git(["ls-tree", "--name-only", "HEAD"], cwd=repo, check=False)
        for name in (names or "").splitlines():
            if name.lower() in _README_NAMES:
                info["readme_name"] = name
                break
        return info

    async def _git_show(self, repo: Path, path: str) -> str | None:
        return await self._git(["show", f"HEAD:{path}"], cwd=repo, check=False)

    async def _git(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: float = 120,
        check: bool = True,
        watch: Path | None = None,
        pin: str | None = None,
    ) -> str | None:
        """Run git. With ``watch``, kill it once that directory outgrows max_archive_mb.
        ``pin`` is a ``host:port:address`` entry for ``http.curloptResolve``."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd or Path.cwd()),
            "GIT_ALLOW_PROTOCOL": ":".join(self.allowed_protocols),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        # A redirect would send git to a host the SSRF guard never saw.
        config = [("http.followRedirects", "false")]
        if pin:
            config.append(("http.curloptResolve", pin))
        token = self.settings.github.token
        if token and args[:1] == ["clone"] and any("github.com" in a for a in args):
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            config.append(("http.https://github.com/.extraheader", f"Authorization: Basic {basic}"))
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for n, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{n}"] = key
            env[f"GIT_CONFIG_VALUE_{n}"] = value
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        limit = self.settings.git.max_archive_mb * 1_000_000
        exceeded = False

        async def watchdog() -> None:
            nonlocal exceeded
            while watch is not None:
                await asyncio.sleep(1)
                size = await asyncio.to_thread(_dir_size, watch) if watch.exists() else 0
                if size > limit:
                    exceeded = True
                    proc.kill()
                    return

        guard = asyncio.create_task(watchdog())
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            raise GitError(f"git {args[0]} timed out after {timeout:.0f}s") from exc
        finally:
            guard.cancel()
            if proc.returncode is None:  # timeout, cancellation (shutdown), any error
                proc.kill()
                await proc.wait()
        if exceeded:
            raise GitError(
                f"git {args[0]} stopped: repository exceeds {self.settings.git.max_archive_mb} MB",
                too_large=True,
            )
        if proc.returncode != 0:
            if not check:
                return None
            message = stderr.decode(errors="replace").strip().splitlines()
            text = message[-1] if message else f"exit code {proc.returncode}"
            permanent = any(m in text.lower() for m in _NOT_FOUND_MARKERS)
            raise GitError(f"git {args[0]} failed: {text}", permanent=permanent)
        return stdout.decode(errors="replace")


def _github_meta(info: dict[str, Any]) -> dict[str, Any]:
    license_info = info.get("license") or {}
    return {
        "description": info.get("description"),
        "topics": info.get("topics") or [],
        "license": license_info.get("spdx_id") if isinstance(license_info, dict) else None,
        "stars": info.get("stargazers_count"),
        "default_branch": info.get("default_branch"),
        "size_kb": info.get("size"),
        "language": info.get("language"),
        "archived": info.get("archived"),
        "pushed_at": info.get("pushed_at"),
    }


def _make_tarball(source: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=source.name)


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _title_from_url(url: str) -> str:
    parts = [p for p in url.rstrip("/").split("/") if p]
    name = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return name.removesuffix(".git")
