from app.pipeline.text_overlay import _FONT_REGISTRY, _registry_ass_bold, _registry_ass_name


def test_registry_lookup_for_lyric_default_font() -> None:
    ass_name = _registry_ass_name("Inter Tight")
    assert ass_name == "Inter Tight"
    entry = _FONT_REGISTRY["fonts"]["Inter Tight"]
    assert entry.get("deprecated") is not True
    assert _registry_ass_bold("Inter Tight") == 0
