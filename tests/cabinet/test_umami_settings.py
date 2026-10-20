from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.cabinet.routes import branding


def test_umami_id_accepts_uuid_or_disable_and_rejects_scripts():
    uuid = '94db1cb1-74f4-4a40-ad6c-962362670409'
    assert branding.AnalyticsCountersUpdate(umami_website_id=uuid).umami_website_id == uuid
    assert branding.AnalyticsCountersUpdate(umami_website_id='').umami_website_id == ''
    with pytest.raises(ValidationError):
        branding.AnalyticsCountersUpdate(umami_website_id='https://evil.test/script.js')


@pytest.mark.asyncio
async def test_public_settings_return_website_id(monkeypatch):
    uuid = '94db1cb1-74f4-4a40-ad6c-962362670409'
    monkeypatch.setattr(branding, 'get_setting_value', AsyncMock(side_effect=lambda db, key: uuid if key == branding.UMAMI_WEBSITE_ID_KEY else ''))
    assert (await branding.get_analytics_counters(db=object())).umami_website_id == uuid
