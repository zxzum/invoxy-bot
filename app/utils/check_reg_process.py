from aiogram.types import CallbackQuery, TelegramObject

from app.states import RegistrationStates


def is_registration_process(event: TelegramObject, current_state: str | None) -> bool:
    registration_states = [
        RegistrationStates.waiting_for_language.state,
        RegistrationStates.waiting_for_rules_accept.state,
        RegistrationStates.waiting_for_privacy_policy_accept.state,
        RegistrationStates.waiting_for_referral_code.state,
    ]

    registration_callbacks = [
        'rules_accept',
        'rules_decline',
        'privacy_policy_accept',
        'privacy_policy_decline',
        'referral_skip',
        'lazeika_migrate:execute',
        'lazeika_migrate:skip',
    ]

    language_select_prefix = 'language_select:'
    migration_callback_prefix = 'lazeika_migrate:'

    if current_state in registration_states:
        return True

    if (
        isinstance(event, CallbackQuery)
        and event.data
        and (
            event.data in registration_callbacks
            or event.data.startswith(language_select_prefix)
            or event.data.startswith(migration_callback_prefix)
        )
    ):
        return True

    return False
