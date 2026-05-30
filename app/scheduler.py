import logging
from datetime import date, datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler()
_app = None  # stored at init so reschedule_payout_job() needs no argument


def get_period(cadence: str, today: date) -> str | None:
    if cadence == 'daily':
        return today.isoformat()
    if cadence == 'weekly':
        iso = today.isocalendar()
        return f'{iso.year}-W{iso.week:02d}'
    if cadence == 'monthly':
        return f'{today.year}-{today.month:02d}'
    return None


def assign_recurring_chores(app):
    with app.app_context():
        from .models import AssignedChore, ChoreInstance
        from . import db

        today = date.today()

        # Iterate active recurring configs (one per child+chore combo)
        configs = (
            AssignedChore.query
            .filter_by(is_recurring=True, is_active=True)
            .all()
        )

        for ac in configs:
            cadence = ac.recurrence_cadence
            rec_day = ac.recurrence_day
            if not cadence:
                continue

            # Gate: only create on the configured day
            if cadence == 'weekly':
                target_dow = rec_day if rec_day is not None else 0
                if today.weekday() != target_dow:
                    continue
            elif cadence == 'monthly':
                target_dom = rec_day if rec_day is not None else 1
                if today.day != target_dom:
                    continue

            period = get_period(cadence, today)
            if period is None:
                continue

            # Skip if an instance already exists for this period
            exists = ChoreInstance.query.filter_by(
                assigned_chore_id=ac.id, period=period
            ).first()
            if exists:
                continue

            # Expire any unsubmitted instances from previous periods
            old_assigned = ChoreInstance.query.filter(
                ChoreInstance.assigned_chore_id == ac.id,
                ChoreInstance.period != period,
                ChoreInstance.status == 'assigned',
            ).all()
            for old in old_assigned:
                old.status = 'expired'
                old.terminal_date = datetime.now()
                logger.info(
                    'Expired chore %s for child %s (period %s)',
                    ac.chore_id, ac.child_id, old.period,
                )

            db.session.add(ChoreInstance(
                assigned_chore_id=ac.id,
                status='assigned',
                period=period,
                assigned_date=datetime.now(),
            ))
            logger.info(
                'Auto-assigned chore %s to child %s for %s',
                ac.chore_id, ac.child_id, period,
            )

        db.session.commit()


def _compute_next_payout(cadence, hour, minute, dow=0, dom=1) -> datetime:
    """Return the next future datetime when a payout should fire."""
    now = datetime.now()
    today = now.date()

    if cadence == 'daily':
        candidate = datetime(today.year, today.month, today.day, hour, minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if cadence == 'weekly':
        days_ahead = (dow - today.weekday()) % 7
        if days_ahead == 0:
            candidate = datetime(today.year, today.month, today.day, hour, minute)
            if candidate <= now:
                days_ahead = 7
        if days_ahead:
            target = today + timedelta(days=days_ahead)
            candidate = datetime(target.year, target.month, target.day, hour, minute)
        return candidate

    if cadence == 'monthly':
        import calendar as _cal
        def _try(year, month):
            last_day = _cal.monthrange(year, month)[1]
            d = min(dom, last_day)
            return datetime(year, month, d, hour, minute)

        candidate = _try(today.year, today.month)
        if candidate <= now:
            next_month = today.month % 12 + 1
            next_year = today.year + (1 if next_month == 1 else 0)
            candidate = _try(next_year, next_month)
        return candidate

    raise ValueError(f'Unknown cadence: {cadence}')


def _save_next_payout(app_settings_module, db_module, cadence, hour, minute, dow=0, dom=1):
    """Compute and persist next_payout_at in AppSettings."""
    AppSettings = app_settings_module
    db = db_module
    nxt = _compute_next_payout(cadence, hour, minute, dow, dom)
    setting = AppSettings.query.get('next_payout_at')
    if setting:
        setting.value = nxt.isoformat()
    else:
        db.session.add(AppSettings(key='next_payout_at', value=nxt.isoformat()))
    return nxt


def process_scheduled_payouts(app):
    """Process approved_pending chore instances — fired by APScheduler at the configured time."""
    with app.app_context():
        from .models import ChoreInstance, BalanceTransaction, AppSettings
        from . import db

        cadence_s = AppSettings.query.get('payout_cadence')
        cadence = cadence_s.value if cadence_s else 'instant'
        if cadence == 'instant':
            return

        pending = ChoreInstance.query.filter_by(status='approved_pending').all()
        for inst in pending:
            amount = inst.actual_payout
            inst.status = 'approved'
            inst.terminal_date = datetime.now()
            inst.child.balance += amount
            partial_note = (
                f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})'
                if inst.is_partial else ''
            )
            db.session.add(BalanceTransaction(
                child_id=inst.child_id,
                amount=amount,
                description=f'Scheduled payout: {inst.effective_name}{partial_note}',
                chore_instance_id=inst.id,
            ))

        # Advance next_payout_at
        time_s = AppSettings.query.get('payout_time')
        hour, minute = (int(p) for p in (time_s.value if time_s else '18:00').split(':'))
        dow = int(AppSettings.query.get('payout_day_of_week').value) if AppSettings.query.get('payout_day_of_week') else 0
        dom = int(AppSettings.query.get('payout_day_of_month').value) if AppSettings.query.get('payout_day_of_month') else 1
        nxt = _save_next_payout(AppSettings, db, cadence, hour, minute, dow, dom)

        db.session.commit()
        if pending:
            logger.info('Processed %d scheduled payouts — next payout at %s', len(pending), nxt)
            from .utils import backup_database
            backup_database()


def reschedule_payout_job():
    """Read payout settings and rebuild the APScheduler cron job to match."""
    if _app is None:
        return

    with _app.app_context():
        from .models import AppSettings

        cadence_s = AppSettings.query.get('payout_cadence')
        cadence = cadence_s.value if cadence_s else 'instant'

        if _scheduler.get_job('scheduled_payouts'):
            _scheduler.remove_job('scheduled_payouts')

        if cadence == 'instant':
            logger.info('Payout cadence is instant — no scheduled job needed')
            return

        time_s = AppSettings.query.get('payout_time')
        time_str = time_s.value if time_s else '18:00'
        hour, minute = (int(p) for p in time_str.split(':'))

        cron_kwargs: dict = {'hour': hour, 'minute': minute}

        if cadence == 'weekly':
            dow_s = AppSettings.query.get('payout_day_of_week')
            cron_kwargs['day_of_week'] = int(dow_s.value) if dow_s else 0
        elif cadence == 'monthly':
            dom_s = AppSettings.query.get('payout_day_of_month')
            cron_kwargs['day'] = int(dom_s.value) if dom_s else 1

        _scheduler.add_job(
            process_scheduled_payouts,
            trigger='cron',
            args=[_app],
            id='scheduled_payouts',
            replace_existing=True,
            **cron_kwargs,
        )

        with _app.app_context():
            from .models import AppSettings
            from . import db
            dow = cron_kwargs.get('day_of_week', 0)
            dom = cron_kwargs.get('day', 1)
            nxt = _save_next_payout(AppSettings, db, cadence, hour, minute, dow, dom)
            db.session.commit()

        logger.info('Payout job scheduled: cadence=%s kwargs=%s next=%s', cadence, cron_kwargs, nxt)


def check_missed_payout(app):
    """At startup, fire a payout only if now is past the stored next_payout_at time."""
    with app.app_context():
        from .models import AppSettings

        cadence_s = AppSettings.query.get('payout_cadence')
        if not cadence_s or cadence_s.value == 'instant':
            return

        next_s = AppSettings.query.get('next_payout_at')
        if not next_s:
            return

        next_payout = datetime.fromisoformat(next_s.value)
        now = datetime.now()

        if now >= next_payout:
            logger.info('Startup: payout was due at %s — processing now.', next_payout)
            process_scheduled_payouts(app)
        else:
            logger.info('Startup: next payout not due until %s — skipping.', next_payout)


def init_scheduler(app):
    global _app
    _app = app

    _scheduler.add_job(
        assign_recurring_chores,
        trigger='cron',
        hour=0, minute=1,
        args=[app],
        id='recurring_chores',
        replace_existing=True,
    )

    if not _scheduler.running:
        _scheduler.start()

    reschedule_payout_job()
    assign_recurring_chores(app)
    check_missed_payout(app)
