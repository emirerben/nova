import pytest

from app.kria.brief import BriefRequirement
from app.kria.noise_reduction_ask import wants_engine_noise_reduction


@pytest.mark.parametrize(
    "text",
    [
        "Motor sesini biraz kıs, sesim öne çıksın",
        "Reduce engine noise separately from speech",
        "Lower the engine noise",
    ],
)
def test_explicit_engine_noise_request(text):
    assert wants_engine_noise_reduction(
        BriefRequirement(id="r1", kind="audio", scope="global", description=text)
    )


@pytest.mark.parametrize(
    "text",
    [
        "Don't reduce engine noise",
        "Motor sesini kısma",
        "Keep engine noise",
        "Replace engine noise with music",
        "Lower original audio",
        "Reduce wind noise",
    ],
)
def test_other_requests_do_not_authorize_engine_filter(text):
    assert not wants_engine_noise_reduction(
        BriefRequirement(id="r1", kind="audio", scope="global", description=text)
    )
