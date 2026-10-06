#!/usr/bin/env python
"""CLI script to notify trial users who have active paid subscriptions in Lazeika VPN.

Usage:
    python -m scripts.notify_lazeika_migration --user-id 735721146
    python -m scripts.notify_lazeika_migration --all
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog
from aiogram import Bot

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.migration.trial_migration_notifier import (
    notify_all_eligible_trial_users,
    notify_trial_user_about_migration,
)


logger = structlog.get_logger(__name__)


async def main() -> int:
    parser = argparse.ArgumentParser(description='Notify trial users about Lazeika VPN migration.')
    parser.add_argument('--user-id', type=int, help='Specific Telegram ID to notify (e.g. 735721146)')
    parser.add_argument('--all', action='store_true', help='Notify all active trial users who have Lazeika subscriptions')
    args = parser.parse_args()

    if not args.user_id and not args.all:
        parser.print_help()
        return 1

    bot_token = settings.BOT_TOKEN
    if not bot_token:
        print('Error: BOT_TOKEN is not set in environment or settings.')
        return 1

    bot = Bot(token=bot_token)

    try:
        async with AsyncSessionLocal() as session:
            if args.user_id:
                print(f'Checking migration eligibility for Telegram ID: {args.user_id}...')
                sent = await notify_trial_user_about_migration(bot, session, args.user_id)
                if sent:
                    print(f'✅ Migration offer sent successfully to {args.user_id}!')
                    return 0
                else:
                    print(f'❌ User {args.user_id} is not eligible or failed to send message.')
                    return 2

            if args.all:
                print('Starting bulk notification for all active trial users...')
                stats = await notify_all_eligible_trial_users(bot, session)
                print(
                    f'Finished: Checked={stats["total_checked"]}, '
                    f'Eligible={stats["eligible_found"]}, '
                    f'Notified={stats["notified_success"]}, '
                    f'Errors={stats["errors"]}'
                )
                return 0

    finally:
        await bot.session.close()

    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
