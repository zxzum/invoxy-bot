"""Telegram side of the Invoxy VPN client: app page and login confirmation."""

from aiogram import Dispatcher, F, types

from app.database.models import User, UserStatus
from app.services import app_client_service as svc


PLATFORM_NAMES = {'android': 'Android', 'ios': 'iPhone / iPad', 'macos': 'macOS', 'windows': 'Windows'}


def build_app_connect_payload(user: User) -> tuple[str, types.InlineKeyboardMarkup]:
    links = svc.public_links()
    rows: list[list[types.InlineKeyboardButton]] = []
    downloads = [
        types.InlineKeyboardButton(text=f'📥 {PLATFORM_NAMES[platform]}', url=url)
        for platform, url in links['downloads'].items()
        if url
    ]
    for i in range(0, len(downloads), 2):
        rows.append(downloads[i : i + 2])
    if links['app_page']:
        rows.append([types.InlineKeyboardButton(text='ℹ️ Как подключиться', url=links['app_page'])])
    rows.append([types.InlineKeyboardButton(text='« В главное меню', callback_data='back_to_menu')])

    availability = (
        'Скачайте приложение для своей системы кнопкой ниже.'
        if downloads
        else 'Ссылки на установку появятся здесь, как только выйдет сборка для вашей системы.'
    )
    text = (
        '📱 <b>Приложение Invoxy VPN</b>\n\n'
        f'{availability}\n\n'
        '<b>Вход без копирования ключа:</b>\n'
        '1. Откройте приложение и нажмите «Войти через Telegram».\n'
        '2. Подтвердите вход в этом чате.\n'
        '3. Вернитесь в приложение и нажмите «Подключить».'
    )
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


async def show_app_login_prompt(message: types.Message, request_id: str) -> bool:
    pending = await svc.get_pending_login(request_id)
    if pending is None:
        await message.answer('❌ Запрос на вход устарел. Начните вход в приложении заново.')
        return True
    platform = PLATFORM_NAMES.get(pending.get('platform') or '', 'неизвестное устройство')
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text='✅ Да, войти', callback_data=f'applogin:ok:{request_id}'),
                types.InlineKeyboardButton(text='❌ Нет', callback_data=f'applogin:no:{request_id}'),
            ]
        ]
    )
    await message.answer(
        f'🔐 Войти в приложение Invoxy VPN на устройстве <b>{platform}</b>?\n\n'
        'Подтверждайте, только если вход начали вы сами на своём устройстве.',
        reply_markup=keyboard,
        parse_mode='HTML',
    )
    return True


async def process_app_login_decision(callback: types.CallbackQuery, db_user: User | None = None) -> None:
    await callback.answer()
    if not isinstance(callback.message, types.Message):
        return
    _, decision, request_id = (callback.data or '').split(':', 2)
    if not db_user or db_user.status != UserStatus.ACTIVE.value:
        await callback.message.edit_text('❌ Учётная запись неактивна.')
        return
    approve = decision == 'ok'
    if not await svc.decide_login(request_id, user_id=db_user.id, approve=approve):
        await callback.message.edit_text('❌ Запрос на вход устарел. Начните вход в приложении заново.')
        return
    if not approve:
        await callback.message.edit_text('Вход отменён.')
        return
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='📲 Вернуться в приложение', url=svc.return_url())]]
    )
    await callback.message.edit_text(
        '✅ Вход подтверждён. Вернитесь в приложение Invoxy VPN — подписка подключится автоматически.',
        reply_markup=keyboard,
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(process_app_login_decision, F.data.regexp(r'^applogin:(ok|no):[A-Za-z0-9_-]{32,64}$'))
