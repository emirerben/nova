import pytest

from app.kria.brief import BriefRequirement
from app.kria.noise_reduction_ask import wants_engine_noise_reduction


def test_engine_noise_fact_on_an_audio_ask_authorizes_the_filter():
    assert wants_engine_noise_reduction(
        BriefRequirement(
            id="r1",
            kind="audio",
            scope="global",
            description="Motor sesini biraz kıs, sesim öne çıksın",
            facts={"noise_reduction": "engine"},
        )
    )


@pytest.mark.parametrize(
    ("kind", "description", "facts"),
    [
        # KRI-667: the words alone (a brief stored before v76, prod T1's r5) never authorize it.
        ("audio", "Motor sesini biraz kıs, sesim öne çıksın", {}),
        ("audio", "Reduce engine noise separately from speech", {}),
        ("audio", "Reduce wind noise", {"noise_reduction": "wind"}),
        ("audio", "Lower original audio", {"audio_intent": "source_unchanged"}),
        ("style", "Lower the engine noise", {"noise_reduction": "engine"}),
    ],
)
def test_other_requests_do_not_authorize_engine_filter(kind, description, facts):
    assert not wants_engine_noise_reduction(
        BriefRequirement(id="r1", kind=kind, scope="global", description=description, facts=facts)
    )
