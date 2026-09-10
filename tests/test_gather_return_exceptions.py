
def test_skill_gather_return_exceptions() -> None:
    """Issue #1468: return_exceptions=True prevents single failure cascade."""
    import asyncio

    async def _ok(slug: str) -> str:
        return slug

    async def _fail(slug: str) -> str:
        raise ConnectionError(slug)

    async def _run():
        results = await asyncio.gather(
            _ok("a"), _fail("b"), _ok("c"),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, str)]
        assert successes == ["a", "c"], f"Got: {successes}"
        assert any(isinstance(r, ConnectionError) for r in results)

    asyncio.run(_run())
