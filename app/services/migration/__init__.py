"""Temporary migration module for seamless transition from Lazeika VPN to Invoxy VPN."""

from .migration_service import (
    MigrationCandidate,
    MigrationResult,
    check_migration_eligibility,
    execute_user_migration,
)


from .trial_migration_notifier import (
    build_migration_offer_text,
    get_migration_offer_keyboard,
    notify_all_eligible_trial_users,
    notify_trial_user_about_migration,
    send_migration_notification,
)


__all__ = [
    'MigrationCandidate',
    'MigrationResult',
    'check_migration_eligibility',
    'execute_user_migration',
    'build_migration_offer_text',
    'get_migration_offer_keyboard',
    'send_migration_notification',
    'notify_trial_user_about_migration',
    'notify_all_eligible_trial_users',
]
