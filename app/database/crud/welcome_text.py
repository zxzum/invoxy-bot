import html
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, WelcomeText


logger = structlog.get_logger(__name__)

WELCOME_TEXT_KEY = 'welcome_text'


async def get_active_welcome_text(db: AsyncSession) -> str | None:
    result = await db.execute(
        select(WelcomeText)
        .where(WelcomeText.is_active == True)
        .where(WelcomeText.is_enabled == True)
        .order_by(WelcomeText.updated_at.desc())
    )
    welcome_text = result.scalar_one_or_none()

    if welcome_text:
        return welcome_text.text_content

    return None


async def get_current_welcome_text_settings(db: AsyncSession) -> dict:
    result = await db.execute(
        select(WelcomeText).where(WelcomeText.is_active == True).order_by(WelcomeText.updated_at.desc())
    )
    welcome_text = result.scalar_one_or_none()

    if welcome_text:
        return {'text': welcome_text.text_content, 'is_enabled': welcome_text.is_enabled, 'id': welcome_text.id}

    return {'text': await get_current_welcome_text_or_default(), 'is_enabled': True, 'id': None}


async def get_welcome_text_by_id(db: AsyncSession, welcome_text_id: int) -> WelcomeText | None:
    result = await db.execute(select(WelcomeText).where(WelcomeText.id == welcome_text_id))
    return result.scalar_one_or_none()


async def list_welcome_texts(
    db: AsyncSession,
    *,
    include_inactive: bool = True,
    limit: int = 50,
    offset: int = 0,
):
    query = select(WelcomeText).order_by(WelcomeText.updated_at.desc())
    if not include_inactive:
        query = query.where(WelcomeText.is_active == True)

    result = await db.execute(query.limit(limit).offset(offset))
    return result.scalars().all()


async def count_welcome_texts(db: AsyncSession, *, include_inactive: bool = True) -> int:
    query = select(func.count(WelcomeText.id))
    if not include_inactive:
        query = query.where(WelcomeText.is_active == True)

    result = await db.execute(query)
    return result.scalar()


async def toggle_welcome_text_status(db: AsyncSession, admin_id: int) -> bool:
    try:
        result = await db.execute(
            select(WelcomeText).where(WelcomeText.is_active == True).order_by(WelcomeText.updated_at.desc())
        )
        welcome_text = result.scalar_one_or_none()

        if welcome_text:
            welcome_text.is_enabled = not welcome_text.is_enabled
            welcome_text.updated_at = datetime.now(UTC)

            await db.commit()
            await db.refresh(welcome_text)

            status = 'включен' if welcome_text.is_enabled else 'отключен'
            logger.info('Приветственный текст администратором', status=status, admin_id=admin_id)
            return welcome_text.is_enabled
        default_text = await get_current_welcome_text_or_default()
        new_welcome_text = WelcomeText(text_content=default_text, is_active=True, is_enabled=True, created_by=admin_id)

        db.add(new_welcome_text)
        await db.commit()
        await db.refresh(new_welcome_text)

        logger.info('Создан и включен дефолтный приветственный текст администратором', admin_id=admin_id)
        return True

    except Exception as e:
        logger.error('Ошибка при переключении статуса приветственного текста', error=e)
        await db.rollback()
        return False


async def set_welcome_text(db: AsyncSession, text_content: str, admin_id: int) -> bool:
    try:
        current_settings = await get_current_welcome_text_settings(db)
        current_enabled_status = current_settings.get('is_enabled', True)

        await db.execute(update(WelcomeText).values(is_active=False))

        new_welcome_text = WelcomeText(
            text_content=text_content, is_active=True, is_enabled=current_enabled_status, created_by=admin_id
        )

        db.add(new_welcome_text)
        await db.commit()
        await db.refresh(new_welcome_text)

        logger.info('Установлен новый приветственный текст администратором', admin_id=admin_id)
        return True

    except Exception as e:
        logger.error('Ошибка при установке приветственного текста', error=e)
        await db.rollback()
        return False


async def create_welcome_text(
    db: AsyncSession,
    *,
    text_content: str,
    created_by: int | None = None,
    is_enabled: bool = True,
    is_active: bool = True,
) -> WelcomeText:
    resolved_creator = created_by

    if created_by is not None:
        result = await db.execute(select(User.id).where(User.id == created_by))
        resolved_creator = result.scalar_one_or_none()

    if is_active:
        await db.execute(update(WelcomeText).values(is_active=False))

    welcome_text = WelcomeText(
        text_content=text_content,
        is_active=is_active,
        is_enabled=is_enabled,
        created_by=resolved_creator,
    )

    db.add(welcome_text)
    await db.commit()
    await db.refresh(welcome_text)

    logger.info(
        '✅ Создан приветственный текст',
        welcome_text_id=welcome_text.id,
        is_active=welcome_text.is_active,
        is_enabled=welcome_text.is_enabled,
    )
    return welcome_text


async def update_welcome_text(
    db: AsyncSession,
    welcome_text: WelcomeText,
    *,
    text_content: str | None = None,
    is_enabled: bool | None = None,
    is_active: bool | None = None,
) -> WelcomeText:
    if is_active:
        await db.execute(update(WelcomeText).where(WelcomeText.id != welcome_text.id).values(is_active=False))

    if text_content is not None:
        welcome_text.text_content = text_content

    if is_enabled is not None:
        welcome_text.is_enabled = is_enabled

    if is_active is not None:
        welcome_text.is_active = is_active

    welcome_text.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(welcome_text)

    logger.info(
        '📝 Обновлен приветственный текст',
        welcome_text_id=welcome_text.id,
        is_active=welcome_text.is_active,
        is_enabled=welcome_text.is_enabled,
    )
    return welcome_text


async def delete_welcome_text(db: AsyncSession, welcome_text: WelcomeText) -> None:
    await db.delete(welcome_text)
    await db.commit()
    logger.info('🗑️ Удален приветственный текст ID', welcome_text_id=welcome_text.id)


async def get_current_welcome_text_or_default() -> str:
    return (
        'Привет, {user_name}! 🎁 3 дней VPN бесплатно! '
        'Подключайтесь за минуту и забудьте о блокировках. '
        '✅ До 1 Гбит/с скорость '
        '✅ Умный VPN — можно не отключать для большинства российских сервисов '
        '✅ Современные протоколы — максимум защиты и анонимности '
        '💉 Всего 99₽/мес за 1 устройство '
        '👇 Жмите кнопку и подключайтесь!'
    )


def replace_placeholders(text: str, user) -> str:
    first_name = getattr(user, 'first_name', None)
    username = getattr(user, 'username', None)

    first_name = first_name.strip() if first_name else None
    username = username.strip() if username else None

    user_name = html.escape(first_name or username or 'друг')
    display_first_name = html.escape(first_name or 'друг')
    display_username = f'@{html.escape(username)}' if username else html.escape(first_name or 'друг')
    clean_username = html.escape(username or first_name or 'друг')

    replacements = {
        '{user_name}': user_name,
        '{first_name}': display_first_name,
        '{username}': display_username,
        '{username_clean}': clean_username,
        'Egor': user_name,
    }

    result = text
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result


async def get_welcome_text_for_user(db: AsyncSession, user, db_user: User | None = None) -> str | None:
    welcome_text = await get_active_welcome_text(db)

    if not welcome_text:
        return None

    if isinstance(user, str):

        class SimpleUser:
            def __init__(self, name):
                self.first_name = name
                self.username = None

        user = SimpleUser(user)

    # INVOXY: prices and quotas must follow admin edits, never a static welcome caption.
    if '{tariffs}' in welcome_text or '{trial}' in welcome_text:
        from app.config import settings
        from app.database.crud.tariff import get_tariffs_for_user, get_trial_tariff
        from app.localization.texts import Texts
        from app.utils.formatters import format_days_declension

        tariffs = await get_tariffs_for_user(db, db_user.promo_group_id if db_user else None)
        lines = []
        for tariff in tariffs:
            prices = tariff.period_prices or {}
            if not tariff.is_daily and not prices:
                continue
            if tariff.is_daily:
                price = f'{settings.format_price(tariff.daily_price_kopeks)}/день'
            else:
                period = min(prices, key=int)
                price = f'{settings.format_price(prices[period])} / {format_days_declension(int(period))}'
            traffic = Texts.format_traffic(tariff.traffic_limit_gb)
            line = (
                f'<b>{html.escape(tariff.name)}</b> — {traffic}, '
                f'{Texts.format_device_limit(tariff.device_limit)} устр., {price}'
            )
            if tariff.whitelist_traffic_limit_gb:
                line += f' · LTE {tariff.whitelist_traffic_limit_gb} ГБ'
            lines.append(line)
        welcome_text = welcome_text.replace('{tariffs}', '\n'.join(lines) or 'Тарифы скоро появятся.')
        trial_text = ''
        if settings.TRIAL_DURATION_DAYS > 0 and not settings.is_trial_disabled_for_user(
            getattr(db_user, 'auth_type', 'telegram')
        ):
            trial = await get_trial_tariff(db)
            days = (trial.trial_duration_days or settings.TRIAL_DURATION_DAYS) if trial else settings.TRIAL_DURATION_DAYS
            traffic = trial.traffic_limit_gb if trial else settings.TRIAL_TRAFFIC_LIMIT_GB
            devices = trial.device_limit if trial else settings.TRIAL_DEVICE_LIMIT
            forced = settings.get_disabled_mode_device_limit() if not settings.is_devices_selection_enabled() else None
            if forced is not None:
                devices = forced
            trial_text = (
                f'🎁 Пробный период: {format_days_declension(days)}, '
                f'{Texts.format_traffic(traffic)}, {Texts.format_device_limit(devices)} устр.'
            )
            if trial and trial.whitelist_traffic_limit_gb:
                trial_text += f' · из них до {trial.whitelist_traffic_limit_gb} ГБ LTE'
            if settings.is_trial_paid_activation_enabled():
                trial_text += f' · Активация: {settings.format_price(settings.get_trial_activation_price())}'
        welcome_text = welcome_text.replace('{trial}', trial_text)

    return replace_placeholders(welcome_text, user)


def get_available_placeholders() -> dict:
    return {
        '{tariffs}': 'Актуальные тарифы, цены и лимиты из базы',
        '{trial}': 'Актуальные условия пробного периода из базы',
        '{user_name}': 'Имя или username пользователя (приоритет: имя → username → "друг")',
        '{first_name}': 'Только имя пользователя (или "друг" если не указано)',
        '{username}': 'Username с символом @ (или имя если username не указан)',
        '{username_clean}': 'Username без символа @ (или имя если username не указан)',
    }
