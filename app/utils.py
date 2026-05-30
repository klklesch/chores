"""Shared helpers for payout period calculations."""
import calendar as _cal
import logging
import os
import sqlite3
from datetime import datetime, date, timedelta

logger = logging.getLogger(__name__)

CHORE_ICONS = [
    ('🛏️', 'Make Bed'),
    ('🍽️', 'Dishes'),
    ('🧹', 'Sweep/Vacuum'),
    ('👕', 'Laundry'),
    ('🗑️', 'Trash'),
    ('🚿', 'Bathroom'),
    ('🌿', 'Lawn/Garden'),
    ('🐾', 'Feed Pet'),
    ('🍴', 'Set Table'),
    ('🧽', 'Wipe/Clean'),
    ('📚', 'Homework'),
    ('📖', 'Reading'),
    ('🪟', 'Windows'),
    ('📦', 'Organize'),
    ('🍳', 'Cooking'),
    ('🛒', 'Groceries'),
    ('♻️', 'Recycling'),
    ('🚗', 'Car'),
    ('🪴', 'Plants'),
    ('🐕', 'Walk Dog'),
    ('❄️', 'Freezer/Fridge'),
    ('🪣', 'Mopping'),
]


def backup_database() -> str | None:
    """
    Copy the SQLite database to a *_backup.db file in the same directory.
    Uses sqlite3's online-backup API so the copy is always consistent even
    under concurrent reads/writes.  The backup file is overwritten each call.
    Returns the backup path on success, or None if skipped (non-SQLite or
    source file not found).  Must be called inside a Flask app context.
    """
    from flask import current_app

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:'):
        logger.debug('backup_database: skipped (non-SQLite URI)')
        return None

    # sqlite:///relative  → relative to instance folder
    # sqlite:////absolute → absolute path
    rel_path = uri[len('sqlite:///'):]
    if os.path.isabs(rel_path):
        db_path = rel_path
    else:
        db_path = os.path.join(current_app.instance_path, rel_path)

    if not os.path.exists(db_path):
        logger.warning('backup_database: source not found at %s', db_path)
        return None

    stem, ext = os.path.splitext(db_path)
    backup_path = f'{stem}_backup{ext or ".db"}'

    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        logger.info('Database backed up → %s', backup_path)
        return backup_path
    except Exception:
        logger.exception('backup_database: failed to back up %s', db_path)
        return None


def next_recurrence_date(cadence: str, rec_day, after_date: date) -> date:
    """Return the next calendar date this recurring chore will be scheduled, strictly after after_date."""
    if cadence == 'daily':
        return after_date + timedelta(days=1)

    if cadence == 'weekly':
        target_dow = rec_day if rec_day is not None else 0
        days_ahead = (target_dow - after_date.weekday()) % 7 or 7
        return after_date + timedelta(days=days_ahead)

    if cadence == 'monthly':
        target_dom = rec_day if rec_day is not None else 1
        # Try this month if the day is still in the future
        last_this = _cal.monthrange(after_date.year, after_date.month)[1]
        d_this = min(target_dom, last_this)
        if d_this > after_date.day:
            return date(after_date.year, after_date.month, d_this)
        # Otherwise next month
        y, m = (after_date.year + 1, 1) if after_date.month == 12 else (after_date.year, after_date.month + 1)
        return date(y, m, min(target_dom, _cal.monthrange(y, m)[1]))

    return after_date + timedelta(days=1)


def _fmt_date(d: date) -> str:
    """Return e.g. 'April 23' without zero-padding (cross-platform)."""
    return d.strftime('%B ') + str(d.day)


def _fmt_time(hour: int, minute: int) -> str:
    """Return e.g. '6:00 PM'."""
    dt = datetime(2000, 1, 1, hour, minute)
    return dt.strftime('%I:%M %p').lstrip('0')


def get_payout_period_info() -> dict:
    """
    Returns a dict describing the current payout period:
      cadence, period_label, period_start (datetime),
      next_payout_str, payout_time_str
    Must be called inside a Flask app context.
    """
    from .models import AppSettings

    def _s(key, default):
        row = AppSettings.query.get(key)
        return row.value if row else default

    cadence        = _s('payout_cadence',      'instant')
    time_str       = _s('payout_time',         '18:00')
    dow_val        = int(_s('payout_day_of_week',  '0'))   # 0 = Monday
    dom_val        = int(_s('payout_day_of_month', '1'))

    payout_hour, payout_minute = (int(p) for p in time_str.split(':'))
    today = date.today()

    DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                 'Friday', 'Saturday', 'Sunday']

    if cadence == 'instant':
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = f"Today, {_fmt_date(today)}"
        next_payout    = "Immediately on approval"

    elif cadence == 'daily':
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = f"Today, {_fmt_date(today)}"
        next_payout    = f"Tomorrow at {_fmt_time(payout_hour, payout_minute)}"

    elif cadence == 'weekly':
        week_start     = today - timedelta(days=today.weekday())
        period_start   = datetime.combine(week_start, datetime.min.time())
        period_label   = f"Week of {_fmt_date(week_start)}"
        days_ahead     = (dow_val - today.weekday()) % 7
        if days_ahead == 0:
            payout_dt = datetime.combine(today, datetime.min.time().replace(hour=payout_hour, minute=payout_minute))
            if datetime.now() >= payout_dt:
                days_ahead = 7
        next_day       = today + timedelta(days=days_ahead)
        next_payout    = (f"{DAY_NAMES[dow_val]}, {_fmt_date(next_day)}"
                          f" at {_fmt_time(payout_hour, payout_minute)}")

    elif cadence == 'monthly':
        period_start   = datetime.combine(today.replace(day=1), datetime.min.time())
        period_label   = today.strftime('%B %Y')
        if today.day < dom_val:
            next_day   = today.replace(day=dom_val)
        else:
            y, m       = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            next_day   = date(y, m, dom_val)
        next_payout    = f"{_fmt_date(next_day)} at {_fmt_time(payout_hour, payout_minute)}"

    else:
        period_start   = datetime.combine(today, datetime.min.time())
        period_label   = _fmt_date(today)
        next_payout    = "—"

    return {
        'cadence':       cadence,
        'period_label':  period_label,
        'period_start':  period_start,
        'next_payout':   next_payout,
        'payout_time':   time_str,
    }


def get_or_create_book_chore():
    from .models import Chore
    from . import db
    chore = Chore.query.filter_by(name='Read a Book').first()
    if not chore:
        chore = Chore(name='Read a Book', description='Finish a library book',
                      icon='📚', default_value=0.0, is_active=False)
        db.session.add(chore)
        db.session.flush()
    return chore
