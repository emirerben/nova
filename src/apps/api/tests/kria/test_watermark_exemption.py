import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.device_render import DeviceRenderCapabilities, WatermarkExemption
from app.routes.creation_threads import capabilities


def test_watermark_exemption_defaults_to_branded_and_is_account_only(monkeypatch):
    owner, other = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "phone_watermark_exempt_user_ids", [owner])
    assert settings.phone_watermark_exempt_for(owner)
    assert not settings.phone_watermark_exempt_for(other)
    assert not settings.phone_watermark_exempt_for(None)
    assert DeviceRenderCapabilities().watermark_exemption is None
    assert (
        WatermarkExemption.for_account(owner).owner_hash
        == hashlib.sha256(str(owner).encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_capabilities_do_not_exempt_other_accounts_or_web(monkeypatch):
    owner, other = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "phone_watermark_exempt_user_ids", [owner])
    result = await capabilities(SimpleNamespace(id=owner), native_client=True)
    assert (
        result["phone_rendering"]["watermark_exemption"]["owner_hash"]
        == WatermarkExemption.for_account(owner).owner_hash
    )
    assert (
        "watermark_exemption"
        not in (await capabilities(SimpleNamespace(id=other), native_client=True))[
            "phone_rendering"
        ]
    )
    assert (
        "watermark_exemption"
        not in (await capabilities(SimpleNamespace(id=owner), native_client=False))[
            "phone_rendering"
        ]
    )
