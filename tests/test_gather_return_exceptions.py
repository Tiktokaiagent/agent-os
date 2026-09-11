"""Test: asyncio.gather with return_exceptions=True (issue #1468)."""

import asyncio


def test_gather_returns_exceptions_for_failures() -> None:
    """A failing coro must not cancel healthy ones."""
    async def ok(slug: str) -> str:
        return slug

    async def fail(slug: str) -> str:
        raise ConnectionError(slug)

    async def run():
        results = await asyncio.gather(
            ok("a"), fail("b"), ok("c"),
            return_exceptions=True,
        )
        successes = [r for r in results if isinstance(r, str)]
        assert successes == ["a", "c"], f"Got: {successes}"
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(errors) == 1

    asyncio.run(run())
