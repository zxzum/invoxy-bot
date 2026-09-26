"""Temporary migration module for seamless transition from Lazeika VPN to Invoxy VPN."""

from .migration_service import (
    MigrationCandidate,
    MigrationResult,
    check_migration_eligibility,
    execute_user_migration,
)


__all__ = [
    'MigrationCandidate',
    'MigrationResult',
    'check_migration_eligibility',
    'execute_user_migration',
]
