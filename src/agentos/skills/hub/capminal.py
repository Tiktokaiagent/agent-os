"""Capminal skill source — browses and installs skills from Capminal.

The Capminal repository (https://github.com/Capminal/agent-skills) publishes each skill
as a directory containing ``SKILL.md`` + ``_meta.json``. This source reads the
metadata live from GitHub so users can browse and install. Downloading and installation
are delegated to :class:`GitHubSource` via the parsed identifier URL.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence

import structlog

from agentos.env import trust_env as _trust_env
from agentos.skills.hub.github import GitHubSource, _frontmatter_field, _parse_identifier
from agentos.skills.hub.source import SkillBundle, SkillMeta, SkillSource, infer_category

log = structlog.get_logger(__name__)

_DEFAULT_REPO = "Capminal/agent-skills"
_DEFAULT_REF = "main"
# Only these skills are loaded from Capminal/agent-skills.
_ALLOWED_SLUGS: tuple[str, ...] = ("capminal", "contract-interaction", "morse-launch-b20")
_CAPMINAL_EMOJI = "🤖"
_CATALOG_TTL_SECONDS = 15 * 60
_FAILURE_RETRY_SECONDS = 60
_CATALOG_CONCURRENCY = 16
# Every Capminal skill is crypto-adjacent, so "crypto" is the bucket a row falls
# back to when its slug and tags name nothing more specific. It is a fallback,
# not a constant: hard-coding it for the whole catalog collapsed the browse
# chip row into a single no-op filter.
_FALLBACK_CATEGORY = "crypto"


def _category_for(slug: str, tags: Sequence[str]) -> str:
    """Return the browse-filter bucket for a Capminal row.

    :func:`infer_category` answers ``"other"`` when nothing matches, which is a
    truthful answer for a general catalog but a useless chip here — every skill
    in this repository is crypto tooling. So "other" is rewritten to the house
    fallback while a real match ("trading", "dev", …) is kept as-is.
    """
    inferred = infer_category(slug, "Capminal", tags)
    return _FALLBACK_CATEGORY if inferred == "other" else inferred


def _matches(meta: SkillMeta, query: str) -> bool:
    q = query.strip().lower()
    if not q:
        return True
    haystack = " ".join(
        [meta.name, meta.provider, meta.category, meta.description, *meta.tags]
    ).lower()
    return q in haystack


class CapminalSource(SkillSource):
    """Skill source backed by the Capminal/agent-skills GitHub catalog."""

    def __init__(
        self,
        token: str | None = None,
        *,
        repo: str = _DEFAULT_REPO,
        ref: str = _DEFAULT_REF,
        allowlist: Sequence[str] = _ALLOWED_SLUGS,
    ) -> None:
        self._github = GitHubSource(token=token)
        self._repo = repo
        self._ref = ref
        self._allowlist = tuple(allowlist)
        self._raw_base = f"https://raw.githubusercontent.com/{repo}/{ref}"
        self._cache_metas: list[SkillMeta] | None = None
        self._cache_at = 0.0
        self._last_failure_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def source_id(self) -> str:
        return "capminal"

    @property
    def trust_level(self) -> str:
        return "community"

    def _skill_url(self, slug: str) -> str:
        return f"https://github.com/{self._repo}/tree/{self._ref}/{slug}"

    async def search(self, query: str, limit: int = 200) -> list[SkillMeta]:
        """List Capminal skills (all when query is empty; filtered otherwise)."""
        metas = await self._load_catalog()
        results = [m for m in metas if _matches(m, query)]
        return results[:limit]

    def _is_allowlisted(self, identifier: str) -> bool:
        """Return True when ``identifier`` names an allowlisted skill in this repo."""
        ref = _parse_identifier(identifier)
        if ref is None:
            return False
        if ref.repo_full.lower() != self._repo.lower():
            return False
        return ref.skill_dir.strip("/") in self._allowlist

    async def inspect(self, identifier: str) -> SkillMeta | None:
        if not self._is_allowlisted(identifier):
            log.warning("capminal.identifier_rejected", op="inspect")
            return None
        return await self._github.inspect(identifier)

    async def fetch(self, identifier: str) -> SkillBundle | None:
        if not self._is_allowlisted(identifier):
            log.warning("capminal.identifier_rejected", op="fetch")
            return None
        return await self._github.fetch(identifier)

    async def _load_catalog(self) -> list[SkillMeta]:
        async with self._lock:
            now = time.monotonic()
            if self._cache_metas is not None and (now - self._cache_at) < _CATALOG_TTL_SECONDS:
                return self._cache_metas
            # Negative cache: after a failed fetch, serve what we have without hammering GitHub.
            if (now - self._last_failure_at) < _FAILURE_RETRY_SECONDS:
                return self._cache_metas or []

            metas = await self._fetch_catalog()
            if metas is None:
                self._last_failure_at = time.monotonic()
                return self._cache_metas or []

            self._cache_metas = metas
            self._cache_at = time.monotonic()
            return metas

    async def _fetch_catalog(self) -> list[SkillMeta] | None:
        """Fetch _meta.json + SKILL.md for each allowlisted slug directly."""
        import httpx

        if not self._allowlist:
            return []

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                sem = asyncio.Semaphore(_CATALOG_CONCURRENCY)

                async def _load_one(slug: str) -> SkillMeta | None:
                    async with sem:
                        return await self._load_catalog_entry(client, slug)

                loaded = await asyncio.gather(
                    *(_load_one(s) for s in self._allowlist),
                    return_exceptions=True,
                )
        except Exception as exc:
            log.warning("capminal.fetch_failed", error=str(exc))
            return None

        # Filter out exception results from individual skill failures
        if loaded:
            loaded = [m for m in loaded if isinstance(m, SkillMeta) or m is None]
        metas = [m for m in loaded if m is not None]
        if not metas:
            return None
        metas.sort(key=lambda m: m.name)
        return metas

    async def _load_catalog_entry(self, client, slug: str) -> SkillMeta | None:
        """Fetch and parse one skill's _meta.json, then SKILL.md. Skips on error."""
        headers = self._github._headers()
        meta_url = f"{self._raw_base}/{slug}/_meta.json"
        try:
            resp = await client.get(meta_url, headers=headers)
            resp.raise_for_status()
            meta_data = json.loads(resp.content)
        except Exception as exc:
            log.warning("capminal.meta_failed", slug=slug, error=str(exc))
            return None
        if not isinstance(meta_data, dict):
            return None

        skill_md_url = f"{self._raw_base}/{slug}/SKILL.md"
        skill_md_content = await self._load_skill_md(client, slug, skill_md_url, headers)

        return self._meta_from_catalog(slug, meta_data, skill_md_content)

    async def _load_skill_md(self, client, slug: str, url: str, headers: dict) -> str:
        """Fetch SKILL.md and read its content. Empty on error."""
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("capminal.skill_md_failed", slug=slug, error=str(exc))
            return ""
        try:
            return str(resp.content.decode("utf-8"))
        except UnicodeDecodeError:
            return ""

    def _meta_from_catalog(
        self, slug: str, catalog: dict, skill_md_content: str
    ) -> SkillMeta | None:
        """Build a browse-time SkillMeta from parsed _meta.json and SKILL.md content."""
        identifier = self._skill_url(slug)
        display_name = str(catalog.get("displayName") or slug)
        name = _frontmatter_field(skill_md_content, "name") or display_name
        description = _frontmatter_field(skill_md_content, "description") or str(
            catalog.get("description") or ""
        )
        latest_release = catalog.get("latestRelease")
        version = ""
        if isinstance(latest_release, dict):
            version = str(latest_release.get("version") or "")
        if not version:
            version = _frontmatter_field(skill_md_content, "version") or ""

        author = (
            str(catalog.get("owner") or "")
            or _frontmatter_field(skill_md_content, "author")
            or "capminal"
        )

        tags_raw = _frontmatter_field(skill_md_content, "tags")
        tags = []
        if tags_raw:
            t_str = tags_raw.strip()
            if t_str.startswith("[") and t_str.endswith("]"):
                t_str = t_str[1:-1]
            tags = [t.strip() for t in t_str.split(",") if t.strip()]

        return SkillMeta(
            name=name,
            description=description,
            version=version,
            author=author,
            source_id="capminal",
            trust_level="community",
            identifier=identifier,
            homepage=identifier,
            tags=tags,
            provider="Capminal",
            logo="",
            emoji=_CAPMINAL_EMOJI,
            category=_category_for(name, tags),
        )
