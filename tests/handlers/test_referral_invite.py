"""Regression for #634720 — referral invite links must survive tap-to-copy.

The invite is shown inside a <blockquote> with "tap to copy". Telegram keeps
<code> content in the clipboard but DROPS auto-linked raw URLs when copying a
quote, so plain-URL bot/cabinet links fell out of the copied text. The fix
wraps the whole invitation in <code>.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.referral as ref


async def test_create_invite_message_wraps_links_in_code(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard, **kwargs):
        captured['text'] = text

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    # get_*_referral_link are methods on the Settings class — patch on the class.
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(
        type(ref.settings), 'get_cabinet_referral_link', lambda self, code: 'https://cab.example/?ref=X&u=1'
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)

    db_user = SimpleNamespace(referral_code='X', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.create_invite_message(callback, db_user, None)

    html = captured['text']
    # INVOXY: one code block copies the complete invitation, including both links.
    assert html.count('<code>') == html.count('</code>') == 1
    code = html.split('<code>', 1)[1].split('</code>', 1)[0]
    assert 'https://t.me/bot?start=ref_X' in code
    assert 'https://cab.example/?ref=X&amp;u=1' in code
    assert '&lt;code&gt;' not in html
    assert 'Присоединяйся' in code
