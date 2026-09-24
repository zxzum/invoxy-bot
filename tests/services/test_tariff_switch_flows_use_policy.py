"""Сторож: все флоу смены тарифа берут правила из одного места.

Флоу три — кабинет, Mini App и бот, — и каждый раньше считал остаток дней и
решал про обнуление трафика сам. Обе ошибки (бесплатный прыжок в последний
день и трафик с нуля при бесплатном переключении) жили в каждом флоу отдельно.
Четвёртый флоу не должен завести их заново.
"""

import re
from pathlib import Path

import pytest


APP = Path(__file__).resolve().parents[2] / 'app'
POLICY = 'app/services/tariff_switch_policy.py'

SWITCH_FLOWS = [
    'cabinet/routes/subscription_modules/tariff_switch.py',
    'webapi/routes/miniapp.py',
    'handlers/subscription/tariff_purchase.py',
]

# Остаток дней «в лоб»: (end_date - now).days вместо общего расчёта.
INLINE_REMAINING = re.compile(r'^[ \t]*(?:remaining_days|rem_days) = .*\.days', re.MULTILINE)


@pytest.mark.parametrize('rel_path', SWITCH_FLOWS)
def test_flow_computes_remaining_days_through_policy(rel_path):
    source = (APP / rel_path).read_text(encoding='utf-8')
    assert 'remaining_days_for_switch' in source, f'{rel_path} не использует общий расчёт остатка'
    leftovers = INLINE_REMAINING.findall(source)
    assert not leftovers, (
        f'{rel_path} считает остаток дней вручную: {leftovers[:2]}. '
        'Целая часть обнуляет остаток меньше суток — переключение становится бесплатным.'
    )


@pytest.mark.parametrize('rel_path', SWITCH_FLOWS)
def test_flow_asks_policy_before_resetting_traffic(rel_path):
    source = (APP / rel_path).read_text(encoding='utf-8')
    assert 'RESET_TRAFFIC_ON_TARIFF_SWITCH' not in source, (
        f'{rel_path} читает выключатель трафика напрямую — обнуление обязано идти через '
        'should_reset_used_traffic(), иначе бесплатное переключение выдаёт новую квоту.'
    )
    assert 'should_reset_used_traffic' in source, f'{rel_path} не спрашивает политику про сброс трафика'


def test_cabinet_conversion_does_not_bypass_traffic_policy():
    source = (APP / SWITCH_FLOWS[0]).read_text(encoding='utf-8')
    assert 'reset_used_traffic = should_reset_used_traffic(upgrade_cost)' in source
    assert 'should_reset_used_traffic(upgrade_cost) or is_convert_days' not in source


def test_cabinet_switch_preserves_paid_regular_and_lte_topups():
    source = (APP / SWITCH_FLOWS[0]).read_text(encoding='utf-8')
    assert '_apply_base_limit_preserving_active_purchases(' in source
    assert '_apply_whitelist_base_limit_preserving_active_purchases(' in source
    assert 'sql_delete(TrafficPurchase)' not in source


# Кто ещё вправе читать выключатель напрямую. Все — оплаченные или админские
# пути, где бесплатного прыжка быть не может.
ALLOWED_DIRECT_READERS = {
    POLICY,
    'app/config.py',
    'app/services/system_settings_service.py',  # метаданные редактора настроек
    'app/handlers/admin/pricing.py',  # экран настроек в админке
    'app/database/crud/subscription.py',  # extend_subscription: оплаченная покупка тарифа
    'app/services/subscription_auto_purchase_service.py',  # автопокупка после пополнения
    'app/cabinet/routes/admin_users.py',  # админ меняет тариф руками
    'app/cabinet/routes/admin_bulk_actions.py',  # массовая смена тарифа админом
    'app/handlers/admin/users.py',  # то же из бота
}


def test_no_new_direct_readers_of_the_traffic_switch():
    """Новый читатель выключателя обязан объясниться здесь.

    Пользовательские флоу смены тарифа читать его не должны вовсе — им нужен
    вопрос «а переключение вообще оплачено?», а не голая настройка.
    """
    readers = {
        str(path.relative_to(APP.parent))
        for path in APP.rglob('*.py')
        if 'RESET_TRAFFIC_ON_TARIFF_SWITCH' in path.read_text(encoding='utf-8')
    }
    assert readers <= ALLOWED_DIRECT_READERS, f'Новые прямые читатели: {sorted(readers - ALLOWED_DIRECT_READERS)}'
