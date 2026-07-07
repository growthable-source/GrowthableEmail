import pytest

from app.services.renderer import RenderError, render_batch


async def test_render_batch_personalizes_each_contact():
    results = await render_batch("welcome", [
        {"firstName": "Ada", "unsubUrl": "https://x.io/u/t1"},
        {"firstName": "Bob", "unsubUrl": "https://x.io/u/t2"},
    ])
    assert len(results) == 2
    assert "Ada" in results[0].html and "https://x.io/u/t1" in results[0].html
    assert "Bob" in results[1].html and "Bob" in results[1].text
    assert results[0].hash != results[1].hash
    assert len(results[0].hash) == 64


async def test_unknown_template_raises():
    with pytest.raises(RenderError):
        await render_batch("nope-not-real", [{}])
