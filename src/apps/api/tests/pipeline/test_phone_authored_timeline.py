

def test_authored_compile_refuses_a_creator_song_instead_of_dropping_it(monkeypatch) -> None:
    import pytest

    from app.pipeline import phone_authored_timeline as pat

    monkeypatch.setattr(
        "app.services.user_song_projection.user_song_for_variant",
        lambda *_a, **_k: {"mode": "lipsync"},
    )
    with pytest.raises(ValueError, match="own song"):
        pat.compile_phone_authored_timeline(object(), {}, object())
