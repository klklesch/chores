import calendar as cal_module
import os
import bcrypt
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, current_app, make_response, send_file
from werkzeug.utils import secure_filename
from ..models import Child, Chore, AssignedChore, ChoreInstance, BalanceTransaction, AppSettings, WishlistItem, Book, BookCheckout, BookAssignment
from .. import db

parent_bp = Blueprint('parent', __name__)


@parent_bp.context_processor
def inject_nav_children():
    """Make all children available in every parent template for the quick-switch bar."""
    if not session.get('parent_authenticated'):
        return {}
    children = Child.query.order_by(Child.name).all()
    return {'nav_children': children}


def parent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('parent_authenticated'):
            return redirect(url_for('parent.login'))
        return f(*args, **kwargs)
    return decorated


# ── Auth ─────────────────────────────────────────────────────────────────────

@parent_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pin = request.form.get('pin', '').encode()
        setting = AppSettings.query.get('parent_pin')
        if setting and bcrypt.checkpw(pin, setting.value.encode()):
            session['parent_authenticated'] = True
            session.permanent = True
            return redirect(url_for('parent.dashboard'))
        flash('Incorrect PIN. Please try again.', 'error')
    return render_template('parent/login.html')


@parent_bp.route('/logout')
def logout():
    session.pop('parent_authenticated', None)
    return redirect(url_for('main.home'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@parent_bp.route('/')
@parent_required
def dashboard():
    children = Child.query.order_by(Child.name).all()
    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(ChoreInstance.status == 'submitted')
        .order_by(ChoreInstance.submitted_date)
        .all()
    )
    active_checkouts = BookCheckout.query.filter(BookCheckout.returned_date == None).all()  # noqa: E711
    books_active_count = len(active_checkouts)
    books_overdue_count = sum(1 for co in active_checkouts if co.is_overdue)
    return render_template(
        'parent/dashboard.html',
        children=children,
        pending_reviews=pending_reviews,
        books_active_count=books_active_count,
        books_overdue_count=books_overdue_count,
    )


# ── Child detail ──────────────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>')
@parent_required
def child_detail(child_id):
    from ..scheduler import get_period
    from ..utils import next_recurrence_date

    child = Child.query.get_or_404(child_id)
    today = date.today()

    # Priority order: submitted > assigned > upcoming > expired > pending payout > approved
    def _row_priority(instances, is_recurring, cadence):
        statuses = {inst.status for inst in instances}
        if 'submitted' in statuses:
            return 0
        if 'assigned' in statuses:
            return 1
        if is_recurring and cadence:
            current_p = get_period(cadence, today)
            if current_p and not any(i.period == current_p for i in instances):
                return 2  # upcoming
        if 'expired' in statuses:
            return 3
        if 'approved_pending' in statuses:
            return 4
        return 5

    chore_rows = []

    # 1. Recurring chores — one row per active config
    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=True, is_active=True
    ).all():
        cadence = ac_config.recurrence_cadence
        rec_day = ac_config.recurrence_day
        if not cadence:
            continue

        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        current_period = get_period(cadence, today)
        is_upcoming = bool(current_period and not any(i.period == current_period for i in instances))

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  True,
            'is_upcoming':   is_upcoming,
            'is_dimmed':     False,
            'cadence':       cadence,
            'rec_day':       rec_day,
            'next_date':     next_recurrence_date(cadence, rec_day, today),
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, True, cadence),
        })

    # 2. Non-recurring — one row per active config (with its instances)
    #    • Active/submitted → show normally.
    #    • Terminal (approved/approved_pending/expired) assigned today → show
    #      dimmed (grey) so the parent can still see it briefly.
    #    • Terminal from a previous day → hide entirely.
    _active_statuses    = {'assigned', 'submitted'}
    _terminal_statuses  = {'approved', 'approved_pending', 'expired'}

    for ac_config in AssignedChore.query.filter_by(
        child_id=child_id, is_recurring=False, is_active=True
    ).all():
        instances = (
            ChoreInstance.query
            .filter_by(assigned_chore_id=ac_config.id)
            .order_by(ChoreInstance.assigned_date.desc())
            .limit(5)
            .all()
        )

        has_active = any(i.status in _active_statuses for i in instances)

        if not has_active:
            # Keep only if a terminal instance became terminal today.
            # Use terminal_date (set at transition time); fall back to
            # approved_date for pre-migration rows that lack terminal_date.
            def _became_terminal_today(i):
                td = i.terminal_date or (
                    i.approved_date if i.status in ('approved', 'approved_pending') else None
                )
                if td is None:
                    return False
                d = td.date() if hasattr(td, 'date') else td
                return d == today

            if not any(
                i.status in _terminal_statuses and _became_terminal_today(i)
                for i in instances
            ):
                continue
            is_dimmed = True
        else:
            is_dimmed = False

        chore_rows.append({
            'config':        ac_config,
            'instances':     instances,
            'is_recurring':  False,
            'is_upcoming':   False,
            'is_dimmed':     is_dimmed,
            'cadence':       None,
            'rec_day':       None,
            'next_date':     None,
            'chore_id':      ac_config.chore_id,
            'pending_count': sum(1 for i in instances if i.status in ('submitted', 'assigned')),
            'priority':      _row_priority(instances, False, None),
        })

    _valid_sorts = {'status', 'name', 'value', 'cadence'}
    sort = request.args.get('sort')          # explicit URL param takes priority
    save_cookie = sort in _valid_sorts       # only save when explicitly chosen
    if sort not in _valid_sorts:
        sort = request.cookies.get('chore_sort', 'status')  # fall back to cookie

    if sort == 'name':
        chore_rows.sort(key=lambda r: r['config'].effective_name.lower())
    elif sort == 'value':
        chore_rows.sort(key=lambda r: -r['config'].effective_value)
    elif sort == 'cadence':
        chore_rows.sort(key=lambda r: (r['cadence'] or 'zzz', r['config'].effective_name.lower()))
    else:  # 'status' — priority first, then alphabetical
        chore_rows.sort(key=lambda r: (r['priority'], r['config'].effective_name.lower()))

    pending_reviews = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'submitted',
        )
        .order_by(ChoreInstance.submitted_date)
        .all()
    )

    all_chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    wishlist_active = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )

    # ── Ledger: payout events + purchases, newest first, paginated ────────────
    from collections import defaultdict

    LEDGER_PAGE_SIZE = 10
    ledger_page = request.args.get('ledger_page', 1, type=int)

    # Positive balance transactions → payout events grouped by calendar date
    pos_txns = (
        BalanceTransaction.query
        .filter(
            BalanceTransaction.child_id == child_id,
            BalanceTransaction.amount > 0,
        )
        .order_by(BalanceTransaction.transaction_date)
        .all()
    )
    payout_by_date = defaultdict(list)
    for t in pos_txns:
        d = t.transaction_date.date() if hasattr(t.transaction_date, 'date') else t.transaction_date
        payout_by_date[d].append(t)

    payout_events = [
        {
            'type':         'payout',
            'date':         d,
            'amount':       sum(t.amount for t in txns),
            'transactions': txns,  # already in ascending order from the query
        }
        for d, txns in payout_by_date.items()
    ]

    # Purchased wishlist items → purchase events
    wishlist_purchased = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .all()
    )
    purchase_events = []
    for item in wishlist_purchased:
        d = item.purchased_date
        d = (d.date() if hasattr(d, 'date') else d) if d else today
        purchase_events.append({
            'type':   'purchase',
            'date':   d,
            'amount': item.price,
            'item':   item,
        })

    all_ledger = sorted(payout_events + purchase_events, key=lambda e: e['date'], reverse=True)

    ledger_total_pages = max(1, (len(all_ledger) + LEDGER_PAGE_SIZE - 1) // LEDGER_PAGE_SIZE)
    ledger_page = max(1, min(ledger_page, ledger_total_pages))
    ledger_events = all_ledger[(ledger_page - 1) * LEDGER_PAGE_SIZE: ledger_page * LEDGER_PAGE_SIZE]

    resp = make_response(render_template(
        'parent/child_detail.html',
        child=child,
        chore_rows=chore_rows,
        sort=sort,
        pending_reviews=pending_reviews,
        all_chores=all_chores,
        wishlist_active=wishlist_active,
        ledger_events=ledger_events,
        ledger_page=ledger_page,
        ledger_total_pages=ledger_total_pages,
        today=today,
    ))
    if save_cookie:
        resp.set_cookie('chore_sort', sort, max_age=365 * 24 * 3600, samesite='Lax')
    return resp


@parent_bp.route('/child/<int:child_id>/history')
@parent_required
def child_history(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)

    month_str = request.args.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = (int(p) for p in month_str.split('-'))
        date(year, month, 1)
    except (ValueError, AttributeError):
        year, month = date.today().year, date.today().month

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)

    days_in_month = [month_start + timedelta(days=i) for i in range((month_end - month_start).days)]

    day_str = request.args.get('day')
    selected_day = None
    if day_str:
        try:
            selected_day = date.fromisoformat(day_str)
        except ValueError:
            pass

    # Approved instances in this month
    approved_in_month = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status.in_(['approved', 'approved_pending']),
            ChoreInstance.approved_date >= datetime.combine(month_start, datetime.min.time()),
            ChoreInstance.approved_date < datetime.combine(month_end, datetime.min.time()),
        ).all()
    )

    txns_in_month = BalanceTransaction.query.filter(
        BalanceTransaction.child_id == child_id,
        BalanceTransaction.transaction_date >= datetime.combine(month_start, datetime.min.time()),
        BalanceTransaction.transaction_date < datetime.combine(month_end, datetime.min.time()),
    ).all()

    periods_in_month = set()
    for d in days_in_month:
        for cadence in ('daily', 'weekly', 'monthly'):
            p = get_period(cadence, d)
            if p:
                periods_in_month.add(p)

    pending_recurring = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status.in_(['assigned', 'submitted']),
            AssignedChore.is_recurring == True,  # noqa: E712
            ChoreInstance.period.in_(periods_in_month),
        ).all()
    )

    expired_in_month = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'expired',
            ChoreInstance.assigned_date >= datetime.combine(month_start, datetime.min.time()),
            ChoreInstance.assigned_date < datetime.combine(month_end, datetime.min.time()),
        ).all()
    )

    activity_days = {}
    for inst in approved_in_month:
        d = inst.approved_date.date()
        activity_days.setdefault(d, {})['completed'] = True
    for tx in txns_in_month:
        d = tx.transaction_date.date()
        activity_days.setdefault(d, {})['transaction'] = True
    for inst in pending_recurring:
        for d in days_in_month:
            if inst.period == get_period(inst.recurrence_cadence, d):
                activity_days.setdefault(d, {})['pending'] = True
    for inst in expired_in_month:
        d = inst.assigned_date.date()
        activity_days.setdefault(d, {})['missed'] = True

    cal_grid = []
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day_num in week:
            if day_num == 0:
                row.append(None)
            else:
                d = date(year, month, day_num)
                row.append({
                    'date': d,
                    'day': day_num,
                    'activity': activity_days.get(d, {}),
                    'is_today': d == date.today(),
                    'is_selected': d == selected_day,
                })
        cal_grid.append(row)

    day_completed = []
    day_transactions = []
    day_pending = []
    day_expired = []

    if selected_day:
        day_completed = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status.in_(['approved', 'approved_pending']),
                db.func.date(ChoreInstance.approved_date) == selected_day.isoformat(),
            ).all()
        )

        day_transactions = BalanceTransaction.query.filter(
            BalanceTransaction.child_id == child_id,
            db.func.date(BalanceTransaction.transaction_date) == selected_day.isoformat(),
        ).all()

        day_periods = {get_period(c, selected_day) for c in ('daily', 'weekly', 'monthly')} - {None}
        day_pending = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status.in_(['assigned', 'submitted']),
                AssignedChore.is_recurring == True,  # noqa: E712
                ChoreInstance.period.in_(day_periods),
            ).all()
        )

        day_expired = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status == 'expired',
                db.func.date(ChoreInstance.assigned_date) == selected_day.isoformat(),
            ).all()
        )

    prev_month = f'{year-1}-12' if month == 1 else f'{year}-{month-1:02d}'
    next_month = f'{year+1}-01' if month == 12 else f'{year}-{month+1:02d}'

    cadence_setting = AppSettings.query.get('payout_cadence')
    payout_cadence = cadence_setting.value if cadence_setting else 'instant'

    return render_template(
        'parent/child_history.html',
        child=child,
        year=year,
        month=month,
        month_name=month_start.strftime('%B %Y'),
        cal_grid=cal_grid,
        selected_day=selected_day,
        day_completed=day_completed,
        day_transactions=day_transactions,
        day_pending=day_pending,
        day_expired=day_expired,
        prev_month=prev_month,
        next_month=next_month,
        today=date.today(),
        payout_cadence=payout_cadence,
    )


@parent_bp.route('/child/<int:child_id>/assign', methods=['POST'])
@parent_required
def assign_chore(child_id):
    from ..scheduler import get_period

    child = Child.query.get_or_404(child_id)
    chore_id = request.form.get('chore_id', type=int)
    custom_value_raw = request.form.get('custom_value', '').strip()
    custom_value = float(custom_value_raw) if custom_value_raw else None
    is_recurring = request.form.get('is_recurring') == 'on'
    recurrence_cadence = request.form.get('recurrence_cadence', '').strip() if is_recurring else None
    recurrence_day = None
    if is_recurring and recurrence_cadence in ('weekly', 'monthly'):
        try:
            recurrence_day = int(request.form.get('recurrence_day', 0 if recurrence_cadence == 'weekly' else 1))
        except (ValueError, TypeError):
            recurrence_day = 0 if recurrence_cadence == 'weekly' else 1

    today = date.today()
    if is_recurring and recurrence_cadence == 'weekly' and recurrence_day is not None:
        days_since = (today.weekday() - recurrence_day) % 7
        ref_date = today - timedelta(days=days_since)
    elif is_recurring and recurrence_cadence == 'monthly' and recurrence_day is not None:
        if today.day >= recurrence_day:
            ref_date = today.replace(day=recurrence_day)
        else:
            prev_month = today.month - 1 or 12
            prev_year = today.year if today.month > 1 else today.year - 1
            ref_date = date(prev_year, prev_month, recurrence_day)
    else:
        ref_date = today

    period = get_period(recurrence_cadence, ref_date) if is_recurring and recurrence_cadence else None

    chore = Chore.query.get_or_404(chore_id)

    # Create config row
    ac_config = AssignedChore(
        child_id=child_id,
        chore_id=chore_id,
        custom_value=custom_value,
        is_recurring=is_recurring,
        recurrence_cadence=recurrence_cadence,
        recurrence_day=recurrence_day,
        is_active=True,
    )
    db.session.add(ac_config)
    db.session.flush()  # get ac_config.id

    # Create first instance
    db.session.add(ChoreInstance(
        assigned_chore_id=ac_config.id,
        status='assigned',
        period=period,
        assigned_date=datetime.now(),
    ))
    db.session.commit()

    cadence_label = f' (🔁 {recurrence_cadence})' if is_recurring else ''
    flash(f'"{chore.name}"{cadence_label} assigned to {child.name}!', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/adjust-balance', methods=['POST'])
@parent_required
def adjust_balance(child_id):
    """Unified balance adjustment: positive = credit, negative = penalty."""
    child = Child.query.get_or_404(child_id)
    amount = request.form.get('amount', type=float)
    reason = request.form.get('reason', '').strip()

    if amount is None or amount == 0:
        flash('Please enter a non-zero amount.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    if amount < 0:
        description = f'Penalty: {reason}' if reason else 'Penalty'
        flash(f'${abs(amount):.2f} penalty applied to {child.name}\'s balance.', 'success')
    else:
        description = reason or 'Manual adjustment'
        flash(f'${amount:.2f} added to {child.name}\'s balance.', 'success')

    child.balance += amount
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=amount,
        description=description,
    ))
    db.session.commit()
    return redirect(url_for('parent.child_detail', child_id=child_id))


# ── Chore instance actions ─────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/approve', methods=['POST'])
@parent_required
def approve_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.approved_date = inst.submitted_date or datetime.now()

    awarded_raw = request.form.get('awarded_value', '').strip()
    try:
        awarded = round(float(awarded_raw), 2)
        if awarded < 0:
            awarded = 0.0
    except (ValueError, TypeError):
        awarded = None

    if awarded is not None and awarded != inst.effective_value:
        inst.awarded_value = awarded
    else:
        inst.awarded_value = None

    amount = inst.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    if not cadence or cadence.value == 'instant':
        inst.status = 'approved'
        inst.terminal_date = datetime.now()
        inst.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Chore completed: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {inst.child.name}\'s balance. 🎉', 'success')
    else:
        inst.status = 'approved_pending'
        inst.terminal_date = datetime.now()
        flash(f'Approved{partial_note}! Will be paid out on next {cadence.value} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/deny', methods=['POST'])
@parent_required
def deny_chore(ac_id):
    from ..scheduler import get_period
    inst = ChoreInstance.query.get_or_404(ac_id)
    notes = request.form.get('notes', '').strip()

    period_has_passed = (
        inst.is_recurring
        and inst.recurrence_cadence
        and inst.period
        and get_period(inst.recurrence_cadence, date.today()) != inst.period
    )

    book_assignment = BookAssignment.query.filter_by(chore_instance_id=inst.id).first()

    if period_has_passed:
        inst.status = 'expired'
        inst.terminal_date = datetime.now()
        inst.denial_notes = notes or None
        if book_assignment:
            book_assignment.status = 'unfinished'
        db.session.commit()
        flash(f'Period already passed — {inst.effective_name} marked as expired.', 'info')
    else:
        inst.status = 'assigned'
        inst.submitted_date = None
        inst.denial_notes = notes or None
        if book_assignment:
            book_assignment.status = 'unfinished'
        db.session.commit()
        flash(f'Chore returned to {inst.child.name} for another try.', 'info')

    return redirect(request.referrer or url_for('parent.dashboard'))


@parent_bp.route('/chore/<int:ac_id>/approve-retroactive', methods=['POST'])
@parent_required
def retroactive_approve(ac_id):
    from ..scheduler import get_period as _get_period
    inst = ChoreInstance.query.get_or_404(ac_id)
    payout_mode = request.form.get('payout_mode', 'immediate')
    approved_date_str = request.form.get('approved_date', '').strip()
    try:
        approved_date = datetime.combine(
            date.fromisoformat(approved_date_str),
            datetime.min.time().replace(hour=12),
        )
    except (ValueError, AttributeError):
        approved_date = datetime.now()
    inst.approved_date = approved_date
    amount = inst.actual_payout
    partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''

    cadence = AppSettings.query.get('payout_cadence')
    payout_cadence = cadence.value if cadence else 'instant'

    if payout_mode == 'auto' and payout_cadence != 'instant':
        current_period = _get_period(payout_cadence, date.today())
        approved_period = _get_period(payout_cadence, approved_date.date())
        payout_mode = 'pending' if approved_period == current_period else 'immediate'

    if payout_mode == 'immediate' or payout_cadence == 'instant':
        inst.status = 'approved'
        inst.terminal_date = datetime.now()
        inst.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Chore completed: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        flash(f'Approved! ${amount:.2f} added to {inst.child.name}\'s balance.', 'success')
    else:
        inst.status = 'approved_pending'
        inst.terminal_date = datetime.now()
        flash(f'Approved{partial_note}! Will be paid out on next {payout_cadence} payout.', 'success')

    db.session.commit()
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/reactivate', methods=['POST'])
@parent_required
def reactivate_chore(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'assigned'
    db.session.commit()
    flash(f'"{inst.effective_name}" reactivated.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/not-done', methods=['POST'])
@parent_required
def mark_not_done(ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    inst.status = 'expired'
    inst.terminal_date = datetime.now()
    db.session.commit()
    flash(f'"{inst.effective_name}" marked as not done.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


@parent_bp.route('/chore/<int:ac_id>/mark-incomplete', methods=['POST'])
@parent_required
def mark_incomplete(ac_id):
    from ..scheduler import get_period as _get_period
    inst = ChoreInstance.query.get_or_404(ac_id)

    if inst.status not in ('approved', 'approved_pending'):
        flash('This chore cannot be marked incomplete.', 'error')
        return redirect(request.referrer or url_for('parent.dashboard'))

    was_paid = inst.status == 'approved'

    if was_paid:
        amount = inst.actual_payout
        inst.child.balance -= amount
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=-amount,
            description=f'Chore reversed: {inst.effective_name}',
            chore_instance_id=inst.id,
        ))

    if inst.is_recurring and inst.recurrence_cadence and inst.period:
        current_period = _get_period(inst.recurrence_cadence, date.today())
        new_status = 'assigned' if inst.period == current_period else 'expired'
    else:
        new_status = 'assigned'

    inst.status = new_status
    inst.approved_date = None
    inst.terminal_date = datetime.now() if new_status == 'expired' else None
    inst.awarded_value = None

    book_assignment = BookAssignment.query.filter_by(chore_instance_id=inst.id).first()
    if book_assignment:
        book_assignment.status = 'unfinished'

    db.session.commit()

    if was_paid:
        flash(f'${amount:.2f} reversed from {inst.child.name}\'s balance — chore marked incomplete.', 'info')
    else:
        flash(f'"{inst.effective_name}" removed from pending payout — marked incomplete.', 'info')

    return redirect(request.referrer or url_for('parent.child_detail', child_id=inst.child_id))


# ── Chore config actions ───────────────────────────────────────────────────────

@parent_bp.route('/chore/<int:ac_id>/edit-value', methods=['POST'])
@parent_required
def edit_chore_value(ac_id):
    """Edit config for an AssignedChore: name, value, description, and recurrence."""
    from ..scheduler import get_period

    ac = AssignedChore.query.get_or_404(ac_id)
    was_recurring = ac.is_recurring

    custom_value_raw = request.form.get('custom_value', '').strip()
    ac.custom_value = float(custom_value_raw) if custom_value_raw else None
    ac.override_name = request.form.get('override_name', '').strip() or None
    ac.override_description = request.form.get('override_description', '').strip() or None

    # Recurrence fields
    is_recurring = request.form.get('is_recurring') == 'on'
    ac.is_recurring = is_recurring

    if is_recurring:
        cadence = request.form.get('recurrence_cadence', '').strip()
        if cadence in ('daily', 'weekly', 'monthly'):
            ac.recurrence_cadence = cadence
        try:
            ac.recurrence_day = int(request.form.get('recurrence_day', '')) if cadence in ('weekly', 'monthly') else None
        except (ValueError, TypeError):
            ac.recurrence_day = None

        # If this was just converted from one-time → recurring, stamp the
        # current period on any existing 'assigned' instance so the scheduler
        # doesn't create a duplicate.
        if not was_recurring and ac.recurrence_cadence:
            today = date.today()
            period = get_period(ac.recurrence_cadence, today)
            existing = ChoreInstance.query.filter_by(
                assigned_chore_id=ac.id, status='assigned'
            ).first()
            if existing and period:
                existing.period = period
    else:
        ac.recurrence_cadence = None
        ac.recurrence_day = None

    db.session.commit()
    flash('Chore updated.', 'success')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=ac.child_id))


@parent_bp.route('/child/<int:child_id>/recurring/<int:config_id>/cancel', methods=['POST'])
@parent_required
def cancel_recurring_chore(child_id, config_id):
    """Deactivate a recurring chore — stops new instances from being created."""
    ac = AssignedChore.query.filter_by(id=config_id, child_id=child_id).first_or_404()
    ac.is_active = False
    db.session.commit()
    flash(f'"{ac.effective_name}" recurring schedule cancelled.', 'info')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/recurring/<int:config_id>/edit', methods=['POST'])
@parent_required
def edit_recurring_chore(child_id, config_id):
    """Edit the config of a recurring chore (cadence, day, value, name, description)."""
    ac = AssignedChore.query.filter_by(id=config_id, child_id=child_id).first_or_404()

    new_cadence = request.form.get('recurrence_cadence')
    new_day     = request.form.get('recurrence_day', type=int)
    new_value   = request.form.get('custom_value', '').strip()
    new_name    = request.form.get('override_name', '').strip() or None
    new_desc    = request.form.get('override_description', '').strip() or None

    if new_cadence in ('daily', 'weekly', 'monthly'):
        ac.recurrence_cadence = new_cadence
    ac.recurrence_day = new_day  # None is fine for daily
    ac.custom_value = float(new_value) if new_value else None
    ac.override_name = new_name
    ac.override_description = new_desc

    db.session.commit()
    flash(f'"{ac.effective_name}" recurring schedule updated.', 'info')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/chore/<int:ac_id>/delete', methods=['POST'])
@parent_required
def delete_assigned_chore(ac_id):
    """Delete an AssignedChore config and all its instances (cascade)."""
    ac = AssignedChore.query.get_or_404(ac_id)
    child_id = ac.child_id
    db.session.delete(ac)
    db.session.commit()
    flash('Chore removed.', 'info')
    return redirect(request.referrer or url_for('parent.child_detail', child_id=child_id))


# ── Wishlist management ───────────────────────────────────────────────────────

@parent_bp.route('/child/<int:child_id>/wishlist')
@parent_required
def child_wishlist(child_id):
    child = Child.query.get_or_404(child_id)
    active = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )
    purchased = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='purchased')
        .order_by(WishlistItem.purchased_date.desc())
        .all()
    )
    return render_template('parent/child_wishlist.html', child=child, active=active, purchased=purchased)


@parent_bp.route('/child/<int:child_id>/wishlist/add', methods=['POST'])
@parent_required
def parent_add_wish(child_id):
    child = Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if not name or not price_raw:
        flash('Item name and price are required.', 'error')
        return redirect(url_for('parent.child_wishlist', child_id=child_id))

    max_order = db.session.query(db.func.max(WishlistItem.sort_order)).filter_by(
        child_id=child_id, status='active'
    ).scalar() or 0

    db.session.add(WishlistItem(
        child_id=child_id,
        name=name,
        description=request.form.get('description', '').strip() or None,
        price=float(price_raw),
        url=request.form.get('url', '').strip() or None,
        sort_order=max_order + 1,
    ))
    db.session.commit()
    flash(f'"{name}" added to {child.name}\'s wishlist!', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/quick-purchase', methods=['POST'])
@parent_required
def quick_purchase(child_id):
    """Record a purchase directly without going through the wishlist."""
    child = Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    try:
        price = round(float(request.form.get('price', '')), 2)
        if price <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('A valid price greater than zero is required.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    if not name:
        flash('Item name is required.', 'error')
        return redirect(url_for('parent.child_detail', child_id=child_id))

    db.session.add(WishlistItem(
        child_id=child_id,
        name=name,
        description=description or None,
        price=price,
        status='purchased',
        purchased_date=datetime.now(),
    ))
    child.balance -= price
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-price,
        description=f'Purchased: {name}',
    ))
    db.session.commit()
    flash(f'Purchase recorded — ${price:.2f} deducted from {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_detail', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/purchase', methods=['POST'])
@parent_required
def purchase_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    child = item.child

    if child.balance < item.price:
        flash(
            f'Not enough balance — {child.name} has ${child.balance:.2f} but needs ${item.price:.2f}.',
            'error',
        )
        return redirect(url_for('parent.child_wishlist', child_id=child_id))

    item.status = 'purchased'
    item.purchased_date = datetime.now()
    child.balance -= item.price
    db.session.add(BalanceTransaction(
        child_id=child_id,
        amount=-item.price,
        description=f'Purchased: {item.name}',
    ))
    db.session.commit()
    flash(f'"{item.name}" purchased! ${item.price:.2f} deducted from {child.name}\'s balance.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/edit', methods=['POST'])
@parent_required
def edit_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if name:
        item.name = name
    if price_raw:
        item.price = float(price_raw)
    item.description = request.form.get('description', '').strip() or None
    item.url = request.form.get('url', '').strip() or None
    db.session.commit()
    flash('Wishlist item updated.', 'success')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


@parent_bp.route('/child/<int:child_id>/wishlist/<int:item_id>/delete', methods=['POST'])
@parent_required
def parent_delete_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    name = item.name
    db.session.delete(item)
    db.session.commit()
    flash(f'"{name}" removed from wishlist.', 'info')
    return redirect(url_for('parent.child_wishlist', child_id=child_id))


# ── Chore library ─────────────────────────────────────────────────────────────

@parent_bp.route('/chores')
@parent_required
def chore_library():
    from ..utils import CHORE_ICONS
    chores = Chore.query.filter_by(is_active=True).order_by(Chore.name).all()
    return render_template('parent/chore_library.html', chores=chores, chore_icons=CHORE_ICONS)


@parent_bp.route('/chores/add', methods=['POST'])
@parent_required
def add_chore():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Chore name is required.', 'error')
        return redirect(url_for('parent.chore_library'))

    default_value_raw = request.form.get('default_value', '').strip()

    db.session.add(Chore(
        name=name,
        description=request.form.get('description', '').strip(),
        icon=request.form.get('icon', '').strip() or None,
        default_value=float(default_value_raw) if default_value_raw else 1.0,
    ))
    db.session.commit()
    flash(f'"{name}" added to the chore library!', 'success')
    return redirect(url_for('parent.chore_library'))


@parent_bp.route('/chores/<int:chore_id>/edit', methods=['POST'])
@parent_required
def edit_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    name = request.form.get('name', '').strip()
    if name:
        chore.name = name
    chore.description = request.form.get('description', '').strip()
    chore.icon = request.form.get('icon', '').strip() or None
    default_value_raw = request.form.get('default_value', '').strip()
    if default_value_raw:
        chore.default_value = float(default_value_raw)
    db.session.commit()
    flash(f'"{chore.name}" updated.', 'success')
    return redirect(url_for('parent.chore_library'))


@parent_bp.route('/chores/<int:chore_id>/delete', methods=['POST'])
@parent_required
def delete_chore(chore_id):
    chore = Chore.query.get_or_404(chore_id)
    chore.is_active = False
    db.session.commit()
    flash(f'"{chore.name}" removed from library.', 'success')
    return redirect(url_for('parent.chore_library'))


# ── Payout summary ───────────────────────────────────────────────────────────

@parent_bp.route('/payouts')
@parent_required
def payouts():
    from ..utils import get_payout_period_info

    period = get_payout_period_info()
    cadence = period['cadence']
    children = Child.query.order_by(Child.name).all()

    child_data = []
    grand_total = 0.0

    for child in children:
        if cadence == 'instant':
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved',
                    ChoreInstance.approved_date >= period['period_start'],
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )
        else:
            instances = (
                ChoreInstance.query
                .join(AssignedChore)
                .filter(
                    AssignedChore.child_id == child.id,
                    ChoreInstance.status == 'approved_pending',
                )
                .order_by(ChoreInstance.approved_date.desc())
                .all()
            )

        subtotal = sum(inst.actual_payout for inst in instances)
        grand_total += subtotal
        child_data.append({'child': child, 'chores': instances, 'subtotal': subtotal})

    return render_template(
        'parent/payouts.html',
        child_data=child_data,
        grand_total=grand_total,
        period=period,
    )


@parent_bp.route('/payouts/process-now', methods=['POST'])
@parent_required
def process_payout_now():
    """Immediately pay out all approved_pending chore instances."""
    pending = ChoreInstance.query.filter_by(status='approved_pending').all()
    if not pending:
        flash('No pending payouts to process.', 'info')
        return redirect(url_for('parent.payouts'))

    total_by_child: dict = {}
    for inst in pending:
        amount = inst.actual_payout
        inst.status = 'approved'
        inst.terminal_date = datetime.now()
        inst.child.balance += amount
        partial_note = f' (partial: ${amount:.2f} of ${inst.effective_value:.2f})' if inst.is_partial else ''
        db.session.add(BalanceTransaction(
            child_id=inst.child_id,
            amount=amount,
            description=f'Manual payout: {inst.effective_name}{partial_note}',
            chore_instance_id=inst.id,
        ))
        total_by_child[inst.child.name] = total_by_child.get(inst.child.name, 0) + amount

    db.session.commit()
    from ..utils import backup_database
    backup_database()
    summary = ', '.join(f'{name} +${amt:.2f}' for name, amt in total_by_child.items())
    flash(f'Payout processed! {summary}', 'success')
    return redirect(url_for('parent.payouts'))


# ── Settings ─────────────────────────────────────────────────────────────────

@parent_bp.route('/settings')
@parent_required
def settings():
    children = Child.query.order_by(Child.name).all()
    def _get(key, default):
        s = AppSettings.query.get(key)
        return s.value if s else default

    return render_template(
        'parent/settings.html',
        children=children,
        payout_cadence=_get('payout_cadence', 'instant'),
        payout_time=_get('payout_time', '18:00'),
        payout_day_of_week=_get('payout_day_of_week', '0'),
        payout_day_of_month=_get('payout_day_of_month', '1'),
        session_timeout=_get('session_timeout', '5'),
        notify_email_enabled=_get('notify_email_enabled', 'off'),
        notify_email_to=_get('notify_email_to', ''),
        notify_smtp_user=_get('notify_smtp_user', ''),
        notify_smtp_password_set=bool(_get('notify_smtp_password', '')),
        app_base_url=_get('app_base_url', ''),
        book_default_loan_days=_get('book_default_loan_days', '28'),
        google_books_api_key_set=bool(_get('google_books_api_key', '')),
        app_timezone=_get('app_timezone', 'UTC'),
    )


@parent_bp.route('/settings/update', methods=['POST'])
@parent_required
def update_settings():
    def _set(key, value):
        s = AppSettings.query.get(key)
        if s:
            s.value = value
        else:
            db.session.add(AppSettings(key=key, value=value))

    cadence = request.form.get('payout_cadence')
    if cadence in ('instant', 'daily', 'weekly', 'monthly'):
        _set('payout_cadence', cadence)

    payout_time = request.form.get('payout_time', '').strip()
    if payout_time:
        _set('payout_time', payout_time)

    dow = request.form.get('payout_day_of_week', '').strip()
    if dow.isdigit() and 0 <= int(dow) <= 6:
        _set('payout_day_of_week', dow)

    dom = request.form.get('payout_day_of_month', '').strip()
    if dom.isdigit() and 1 <= int(dom) <= 28:
        _set('payout_day_of_month', dom)

    new_pin = request.form.get('new_pin', '').strip()
    confirm_pin = request.form.get('confirm_pin', '').strip()
    if new_pin:
        if len(new_pin) < 4:
            flash('PIN must be at least 4 characters.', 'error')
            return redirect(url_for('parent.settings'))
        if new_pin != confirm_pin:
            flash('PINs do not match.', 'error')
            return redirect(url_for('parent.settings'))
        pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
        s = AppSettings.query.get('parent_pin')
        if s:
            s.value = pin_hash
        else:
            db.session.add(AppSettings(key='parent_pin', value=pin_hash))

    timeout = request.form.get('session_timeout', '').strip()
    if timeout.isdigit() and int(timeout) >= 1:
        _set('session_timeout', timeout)

    # ── Notification settings ─────────────────────────────────────────────────
    _set('notify_email_enabled', 'on' if request.form.get('notify_email_enabled') else 'off')

    notify_to = request.form.get('notify_email_to', '').strip()
    if notify_to:
        _set('notify_email_to', notify_to)

    notify_user = request.form.get('notify_smtp_user', '').strip()
    if notify_user:
        _set('notify_smtp_user', notify_user)

    # Only overwrite the password if a new one was actually typed
    notify_pw = request.form.get('notify_smtp_password', '').strip()
    if notify_pw:
        _set('notify_smtp_password', notify_pw)

    base_url = request.form.get('app_base_url', '').strip().rstrip('/')
    _set('app_base_url', base_url)

    loan_days = request.form.get('book_default_loan_days', '').strip()
    if loan_days.isdigit() and int(loan_days) >= 1:
        _set('book_default_loan_days', loan_days)

    google_key = request.form.get('google_books_api_key', '').strip()
    if google_key:
        _set('google_books_api_key', google_key)

    tz_val = request.form.get('app_timezone', '').strip()
    if tz_val:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(tz_val)
            _set('app_timezone', tz_val)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError):
            flash('Invalid timezone selected.', 'error')
            return redirect(url_for('parent.settings'))

    db.session.commit()

    from ..scheduler import reschedule_payout_job
    reschedule_payout_job()

    flash('Settings saved!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/chore-action/<token>')
def chore_action(token):
    """One-click approve/deny from an email notification link. No login required —
    the signed token is the credential."""
    from ..notifications import verify_action_token
    from ..scheduler import get_period

    data = verify_action_token(token, current_app.secret_key)
    if not data:
        return render_template('parent/chore_action.html',
                               state='error',
                               message='This link is invalid or has expired (links work for 7 days).')

    inst = ChoreInstance.query.get(data.get('id'))
    if not inst:
        return render_template('parent/chore_action.html',
                               state='error',
                               message='Chore not found — it may have been deleted.')

    action = data.get('action')
    child_name = inst.child.name
    chore_name = inst.effective_name

    # ── Already actioned ──────────────────────────────────────────────────────
    if inst.status != 'submitted':
        friendly = {
            'approved': 'already approved ✓',
            'approved_pending': 'already approved (pending payout) ✓',
            'assigned': 'already sent back for another try',
            'expired': 'already marked expired',
        }.get(inst.status, inst.status)
        return render_template('parent/chore_action.html',
                               state='already_done',
                               child_name=child_name,
                               chore_name=chore_name,
                               friendly_status=friendly)

    # ── Approve ───────────────────────────────────────────────────────────────
    if action == 'approve':
        inst.approved_date = inst.submitted_date or datetime.now()
        inst.awarded_value = None  # full value
        amount = inst.actual_payout

        cadence = AppSettings.query.get('payout_cadence')
        if not cadence or cadence.value == 'instant':
            inst.status = 'approved'
            inst.terminal_date = datetime.now()
            inst.child.balance += amount
            db.session.add(BalanceTransaction(
                child_id=inst.child_id,
                amount=amount,
                description=f'Chore completed: {chore_name}',
                chore_instance_id=inst.id,
            ))
            payout_note = f'${amount:.2f} added to their balance.'
        else:
            inst.status = 'approved_pending'
            inst.terminal_date = datetime.now()
            payout_note = f'Will be paid out on the next {cadence.value} payout.'

        db.session.commit()
        return render_template('parent/chore_action.html',
                               state='approved',
                               child_name=child_name,
                               chore_name=chore_name,
                               value=amount,
                               payout_note=payout_note)

    # ── Deny ──────────────────────────────────────────────────────────────────
    if action == 'deny':
        period_has_passed = (
            inst.is_recurring
            and inst.recurrence_cadence
            and inst.period
            and get_period(inst.recurrence_cadence, date.today()) != inst.period
        )
        if period_has_passed:
            inst.status = 'expired'
            inst.terminal_date = datetime.now()
            db.session.commit()
            return render_template('parent/chore_action.html',
                                   state='denied',
                                   child_name=child_name,
                                   chore_name=chore_name,
                                   note='The period already passed — marked as expired.')
        else:
            inst.status = 'assigned'
            inst.submitted_date = None
            db.session.commit()
            return render_template('parent/chore_action.html',
                                   state='denied',
                                   child_name=child_name,
                                   chore_name=chore_name,
                                   note='Chore sent back for another try.')

    return render_template('parent/chore_action.html',
                           state='error',
                           message='Unknown action in token.')


@parent_bp.route('/settings/test-email', methods=['POST'])
@parent_required
def test_email_notification():
    """Send a test email using the currently saved SMTP settings."""
    def _get(key):
        s = AppSettings.query.get(key)
        return s.value.strip() if s and s.value else ''

    to_addr = _get('notify_email_to')
    smtp_user = _get('notify_smtp_user')
    smtp_password = _get('notify_smtp_password')

    if not all([to_addr, smtp_user, smtp_password]):
        flash('Please save your email settings before sending a test.', 'error')
        return redirect(url_for('parent.settings'))

    from ..notifications import send_test_email
    error = send_test_email(to_addr, smtp_user, smtp_password)
    if error:
        flash(f'Test failed: {error}', 'error')
    else:
        flash(f'Test email sent to {to_addr} — check your inbox!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/add-child', methods=['POST'])
@parent_required
def add_child():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Name is required.', 'error')
        return redirect(url_for('parent.settings'))
    color = request.form.get('color', '#6366f1')
    db.session.add(Child(name=name, avatar_color=color))
    db.session.commit()
    flash(f'{name} added!', 'success')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/settings/remove-child/<int:child_id>', methods=['POST'])
@parent_required
def remove_child(child_id):
    child = Child.query.get_or_404(child_id)
    name = child.name
    db.session.delete(child)
    db.session.commit()
    flash(f'{name} removed.', 'info')
    return redirect(url_for('parent.settings'))


@parent_bp.route('/child/<int:child_id>/avatar', methods=['POST'])
@parent_required
def upload_avatar(child_id):
    child = Child.query.get_or_404(child_id)
    file = request.files.get('avatar')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('parent.settings'))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        flash('Invalid file type. Use JPG, PNG, GIF, or WebP.', 'error')
        return redirect(url_for('parent.settings'))
    filename = secure_filename(f'child_{child_id}{ext}')
    upload_dir = os.path.join(current_app.instance_path, 'avatars')
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))
    child.avatar_filename = filename
    db.session.commit()
    flash(f'Avatar updated for {child.name}!', 'success')
    return redirect(url_for('parent.settings'))


# ── Library Books ─────────────────────────────────────────────────────────────

def _book_sort_key(checkout):
    statuses = {a.status for a in checkout.assignments}
    if 'reading' in statuses:
        group = 0
    elif 'unread' in statuses:
        group = 1
    else:
        group = 2
    return (group, checkout.book.title.lower())


def _get_loan_days():
    setting = AppSettings.query.get('book_default_loan_days')
    try:
        return int(setting.value) if setting else 28
    except (ValueError, TypeError):
        return 28


@parent_bp.route('/books')
@parent_required
def books_dashboard():
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .join(Book)
        .all()
    )
    sort = request.args.get('sort', 'status')
    if sort == 'title':
        checkouts.sort(key=lambda co: co.book.title.lower())
    elif sort == 'reward':
        checkouts.sort(
            key=lambda co: max((a.reward_value for a in co.assignments), default=0.0),
            reverse=True,
        )
    else:
        sort = 'status'
        checkouts.sort(key=_book_sort_key)
    return render_template('parent/books.html', checkouts=checkouts, current_sort=sort)


@parent_bp.route('/books/add', methods=['POST'])
@parent_required
def add_book():
    title = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip() or None
    isbn = request.form.get('isbn', '').strip() or None
    cover_url = request.form.get('cover_url', '').strip() or None
    due_date_str = request.form.get('due_date', '').strip()

    book = None
    if isbn:
        book = Book.query.filter_by(isbn=isbn).first()
        if book:
            flash('Book already in library — created a new checkout.', 'success')

    if not book:
        book = Book(title=title, author=author, isbn=isbn, cover_url=cover_url)
        db.session.add(book)
        db.session.flush()

    if due_date_str:
        try:
            due_date = date.fromisoformat(due_date_str)
        except ValueError:
            due_date = date.today() + timedelta(days=_get_loan_days())
    else:
        due_date = date.today() + timedelta(days=_get_loan_days())

    checkout = BookCheckout(book_id=book.id, due_date=due_date)
    db.session.add(checkout)
    db.session.commit()
    _download_cover(book)
    flash(f'"{book.title}" checked out!', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/search-external')
@parent_required
def search_books_external():
    import json
    import re
    import urllib.request
    import urllib.error
    import urllib.parse
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return ''
    isbn_clean = re.sub(r'[-\s]', '', q)
    is_isbn = isbn_clean.isdigit() and len(isbn_clean) in (10, 13)
    api_key_row = AppSettings.query.get('google_books_api_key')
    api_key = api_key_row.value if api_key_row else ''
    if is_isbn:
        api_url = f'https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn_clean}&maxResults=1'
    else:
        api_url = (f'https://www.googleapis.com/books/v1/volumes'
                   f'?q={urllib.parse.quote(q)}&maxResults=10')
    if api_key:
        api_url += f'&key={api_key}'
    results, error = [], None
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        for item in (data.get('items') or []):
            info = item.get('volumeInfo', {})
            title = info.get('title', '')
            if not title:
                continue
            authors = info.get('authors', [])
            author = ', '.join(authors) if authors else ''
            isbn = ''
            for id_entry in info.get('industryIdentifiers', []):
                if id_entry.get('type') == 'ISBN_13':
                    isbn = id_entry['identifier']
                    break
            if not isbn:
                for id_entry in info.get('industryIdentifiers', []):
                    if id_entry.get('type') == 'ISBN_10':
                        isbn = id_entry['identifier']
                        break
            image_links = info.get('imageLinks', {})
            cover = image_links.get('thumbnail') or image_links.get('smallThumbnail') or ''
            if cover:
                cover = cover.replace('&edge=curl', '').replace('http://', 'https://')
            results.append({'title': title, 'author': author, 'isbn': isbn, 'cover': cover})
    except urllib.error.HTTPError as e:
        current_app.logger.warning('Book search HTTP %s for q=%r', e.code, q)
        error = 'Rate limited — try again in a moment.' if e.code == 429 else f'Google Books error ({e.code}).'
    except Exception as e:
        current_app.logger.warning('Book search failed for q=%r: %s', q, e)
        error = 'Could not reach Google Books.'
    return render_template('parent/book_search_results.html', results=results, error=error)


@parent_bp.route('/checkouts/<int:checkout_id>/return', methods=['POST'])
@parent_required
def return_checkout(checkout_id):
    checkout = BookCheckout.query.get_or_404(checkout_id)
    checkout.returned_date = datetime.now()
    db.session.commit()
    _maybe_delete_cover(checkout.book_id)
    flash(f'"{checkout.book.title}" marked as returned.', 'success')
    return redirect(url_for('parent.review_checkout', checkout_id=checkout_id))


@parent_bp.route('/checkouts/<int:checkout_id>/review', methods=['GET', 'POST'])
@parent_required
def review_checkout(checkout_id):
    checkout = BookCheckout.query.get_or_404(checkout_id)

    if request.method == 'POST':
        for assignment in checkout.assignments:
            new_status = request.form.get(f'status_{assignment.id}')
            reward_raw = request.form.get(f'reward_{assignment.id}', '').strip()

            if new_status in ('unread', 'reading', 'finished', 'unfinished'):
                if new_status == 'unfinished' and assignment.chore_instance_id:
                    inst = ChoreInstance.query.get(assignment.chore_instance_id)
                    if inst:
                        inst.status = 'expired'
                        inst.awarded_value = 0
                        inst.terminal_date = datetime.now()
                assignment.status = new_status

            if reward_raw:
                try:
                    new_reward = float(reward_raw)
                    assignment.reward_value = new_reward
                    if (assignment.chore_instance_id
                            and assignment.chore_instance
                            and assignment.chore_instance.status not in ('approved', 'approved_pending')):
                        ac = assignment.chore_instance.assigned_chore
                        if ac:
                            ac.custom_value = new_reward
                except (ValueError, TypeError):
                    pass

        db.session.commit()
        flash('Review saved.', 'success')
        return redirect(url_for('parent.books_dashboard'))

    return render_template('parent/book_review.html', checkout=checkout)


@parent_bp.route('/books/<int:book_id>/checkout-again', methods=['POST'])
@parent_required
def checkout_again(book_id):
    book = Book.query.get_or_404(book_id)
    due_date = date.today() + timedelta(days=_get_loan_days())
    checkout = BookCheckout(book_id=book.id, due_date=due_date)
    db.session.add(checkout)
    db.session.commit()
    flash('Checked out again!', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/history')
@parent_required
def books_history():
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date != None)  # noqa: E711
        .join(Book)
        .order_by(BookCheckout.returned_date.desc())
        .all()
    )
    return render_template('parent/book_history.html', checkouts=checkouts)


@parent_bp.route('/assignments/<int:assignment_id>/edit-reward', methods=['POST'])
@parent_required
def edit_assignment_reward(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    reward_raw = request.form.get('reward', '').strip()
    try:
        new_reward = float(reward_raw)
    except (ValueError, TypeError):
        flash('Invalid reward value.', 'error')
        return redirect(request.referrer or url_for('parent.books_dashboard'))

    assignment.reward_value = new_reward
    if (assignment.chore_instance_id
            and assignment.chore_instance
            and assignment.chore_instance.status not in ('approved', 'approved_pending', 'expired')):
        ac = assignment.chore_instance.assigned_chore
        if ac:
            ac.custom_value = new_reward
    db.session.commit()
    flash('Reward updated.', 'success')

    if request.headers.get('HX-Request'):
        from flask import render_template_string
        return render_template_string(f'<span>${new_reward:.2f} saved</span>')
    return redirect(request.referrer or url_for('parent.books_dashboard'))


@parent_bp.route('/assignments/<int:assignment_id>/update', methods=['POST'])
@parent_required
def update_assignment(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    status = request.form.get('status', '').strip()

    if status == 'unassigned':
        if assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'approved_pending', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        db.session.delete(assignment)
        db.session.commit()
        flash('Assignment removed.', 'success')
        return redirect(request.referrer or url_for('parent.books_dashboard'))

    reward_raw = request.form.get('reward', '').strip()
    try:
        new_reward = round(float(reward_raw), 2)
        if new_reward < 0:
            new_reward = 0.0
        assignment.reward_value = new_reward
        if (assignment.chore_instance_id
                and assignment.chore_instance
                and assignment.chore_instance.status not in ('approved', 'approved_pending', 'expired')):
            ac = assignment.chore_instance.assigned_chore
            if ac:
                ac.custom_value = new_reward
    except (ValueError, TypeError):
        pass

    if status in ('unread', 'reading', 'unfinished'):
        if status == 'unfinished' and assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'approved_pending', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        assignment.status = status

    db.session.commit()
    flash('Assignment updated.', 'success')
    return redirect(request.referrer or url_for('parent.books_dashboard'))


@parent_bp.route('/assignments/<int:assignment_id>/mark-finished', methods=['POST'])
@parent_required
def mark_book_finished(assignment_id):
    from ..utils import get_or_create_book_chore
    assignment = BookAssignment.query.get_or_404(assignment_id)
    book = assignment.checkout.book
    now = datetime.now()

    if not assignment.finished_date:
        assignment.finished_date = now

    inst = ChoreInstance.query.get(assignment.chore_instance_id) if assignment.chore_instance_id else None

    if inst and inst.status in ('approved', 'approved_pending'):
        assignment.status = 'approved'
        db.session.commit()
        flash('Already approved.', 'success')
        return redirect(request.referrer or url_for('parent.books_dashboard'))

    if not inst:
        book_chore = get_or_create_book_chore()
        ac = AssignedChore(
            child_id=assignment.child_id,
            chore_id=book_chore.id,
            custom_value=assignment.reward_value,
            override_name=book.title,
            is_recurring=False,
            is_active=True,
        )
        db.session.add(ac)
        db.session.flush()
        inst = ChoreInstance(
            assigned_chore_id=ac.id,
            status='submitted',
            assigned_date=now,
            submitted_date=now,
            period=date.today().isoformat(),
        )
        db.session.add(inst)
        db.session.flush()
        assignment.chore_instance_id = inst.id

    amount = assignment.reward_value
    inst.approved_date = now
    inst.terminal_date = now

    cadence = AppSettings.query.get('payout_cadence')
    if not cadence or cadence.value == 'instant':
        inst.status = 'approved'
        assignment.child.balance += amount
        db.session.add(BalanceTransaction(
            child_id=assignment.child_id,
            amount=amount,
            description=f'Book finished: {book.title}',
            chore_instance_id=inst.id,
        ))
        flash(f'Finished! ${amount:.2f} added to {assignment.child.name}\'s balance. 🎉', 'success')
    else:
        inst.status = 'approved_pending'
        flash(f'Finished! Reward queued for next {cadence.value} payout.', 'success')

    assignment.status = 'approved'
    db.session.commit()
    return redirect(request.referrer or url_for('parent.books_dashboard'))


@parent_bp.route('/assignments/<int:assignment_id>/set-status', methods=['POST'])
@parent_required
def set_assignment_status(assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    status = request.form.get('status', '').strip()
    if status not in ('unread', 'reading', 'finished', 'unfinished', 'unassigned'):
        flash('Invalid status.', 'error')
        return redirect(request.referrer or url_for('parent.books_dashboard'))

    if status == 'unassigned':
        if assignment.chore_instance_id:
            inst = ChoreInstance.query.get(assignment.chore_instance_id)
            if inst and inst.status not in ('approved', 'expired'):
                inst.status = 'expired'
                inst.awarded_value = 0
                inst.terminal_date = datetime.now()
        db.session.delete(assignment)
        db.session.commit()
        flash('Assignment removed.', 'success')
        return redirect(request.referrer or url_for('parent.books_dashboard'))

    if status == 'finished' and not assignment.chore_instance_id:
        from ..utils import get_or_create_book_chore
        from ..notifications import send_chore_submitted
        book = assignment.checkout.book
        if not assignment.finished_date:
            assignment.finished_date = datetime.now()
        book_chore = get_or_create_book_chore()
        ac = AssignedChore(
            child_id=assignment.child_id,
            chore_id=book_chore.id,
            custom_value=assignment.reward_value,
            override_name=book.title,
            is_recurring=False,
            is_active=True,
        )
        db.session.add(ac)
        db.session.flush()
        inst = ChoreInstance(
            assigned_chore_id=ac.id,
            status='submitted',
            assigned_date=datetime.now(),
            submitted_date=datetime.now(),
            period=date.today().isoformat(),
        )
        db.session.add(inst)
        db.session.flush()
        assignment.chore_instance_id = inst.id
        send_chore_submitted(inst)

    if status == 'unfinished' and assignment.chore_instance_id:
        inst = ChoreInstance.query.get(assignment.chore_instance_id)
        if inst and inst.status not in ('approved', 'expired'):
            inst.status = 'expired'
            inst.awarded_value = 0
            inst.terminal_date = datetime.now()

    assignment.status = status
    db.session.commit()
    flash('Status updated.', 'success')

    if request.headers.get('HX-Request'):
        from flask import render_template_string
        return render_template_string(f'<span>Status: {status}</span>')
    return redirect(request.referrer or url_for('parent.books_dashboard'))


@parent_bp.route('/books/search')
@parent_required
def search_books():
    from thefuzz import process as fuzz_process
    q = request.args.get('q', '').strip()
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .join(Book)
        .all()
    )
    if q:
        choices = {co.book.title: co for co in checkouts}
        results = fuzz_process.extractBests(q, choices, score_cutoff=65)
        checkouts = [co for co, score in results]
    return render_template('parent/books_list_partial.html', checkouts=checkouts)


@parent_bp.route('/checkouts/bulk-return', methods=['POST'])
@parent_required
def bulk_return_checkouts():
    checkout_ids = request.form.getlist('checkout_ids')
    book_ids = []
    for cid in checkout_ids:
        co = BookCheckout.query.get(int(cid))
        if co and not co.returned_date:
            co.returned_date = datetime.now()
            book_ids.append(co.book_id)
    db.session.commit()
    for bid in book_ids:
        _maybe_delete_cover(bid)
    flash(f'Marked {len(checkout_ids)} book(s) as returned.', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/assignments/bulk-assign', methods=['POST'])
@parent_required
def bulk_assign_books():
    checkout_ids = request.form.getlist('checkout_ids')
    child_ids = request.form.getlist('child_ids')
    reward = float(request.form.get('reward', 0) or 0)
    count = 0
    for cid in checkout_ids:
        for child_id in child_ids:
            existing = BookAssignment.query.filter_by(
                checkout_id=int(cid), child_id=int(child_id)
            ).first()
            if not existing:
                db.session.add(BookAssignment(
                    checkout_id=int(cid), child_id=int(child_id),
                    reward_value=reward, status='unread',
                ))
                count += 1
    db.session.commit()
    flash(f'Assigned {count} book(s).', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/checkouts/bulk-set-status', methods=['POST'])
@parent_required
def bulk_set_status_checkouts():
    checkout_ids = request.form.getlist('checkout_ids')
    status = request.form.get('status', '').strip()
    if status not in ('unread', 'reading', 'finished', 'unfinished'):
        flash('Invalid status.', 'error')
        return redirect(url_for('parent.books_dashboard'))

    count = 0
    for cid in checkout_ids:
        co = BookCheckout.query.get(int(cid))
        if not co:
            continue
        for a in co.assignments:
            if a.status == status:
                continue
            a.status = status
            if status == 'unfinished' and a.chore_instance_id:
                inst = ChoreInstance.query.get(a.chore_instance_id)
                if inst and inst.status not in ('approved', 'expired'):
                    inst.status = 'expired'
                    inst.awarded_value = 0
            count += 1
    db.session.commit()
    flash(f'Updated {count} assignment(s) to {status}.', 'success')
    return redirect(url_for('parent.books_dashboard'))


@parent_bp.route('/books/<int:book_id>/cover.svg')
def book_cover_svg(book_id):
    book = Book.query.get_or_404(book_id)
    local = _cover_path(book_id)
    if not os.path.exists(local) and book.cover_url:
        _download_cover(book)
    if os.path.exists(local):
        return send_file(local, mimetype='image/jpeg',
                         max_age=86400, conditional=True)
    if book.cover_url:
        return redirect(book.cover_url)
    hue = hash(book.title) % 360
    title_lines = _wrap_text(book.title, 18)
    author_line = book.author or ''
    title_y_start = 120 - (len(title_lines) - 1) * 14
    title_blocks = ''.join(
        f'<tspan x="100" dy="{0 if i == 0 else 28}">{_xml_escape(line)}</tspan>'
        for i, line in enumerate(title_lines)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 280" width="200" height="280">
  <rect width="200" height="280" fill="hsl({hue},55%,82%)"/>
  <rect x="10" y="10" width="180" height="260" rx="4" fill="none" stroke="hsl({hue},40%,60%)" stroke-width="3"/>
  <text x="100" y="{title_y_start}" text-anchor="middle" font-family="Georgia,serif" font-size="16" font-weight="bold" fill="hsl({hue},40%,25%)">{title_blocks}</text>
  {f'<text x="100" y="220" text-anchor="middle" font-family="Georgia,serif" font-size="12" fill="hsl({hue},35%,40%)">{_xml_escape(author_line)}</text>' if author_line else ''}
  <text x="100" y="260" text-anchor="middle" font-family="Georgia,serif" font-size="28" fill="hsl({hue},40%,50%)" opacity="0.4">📚</text>
</svg>'''
    from flask import Response
    return Response(svg, mimetype='image/svg+xml',
                    headers={'Cache-Control': 'public, max-age=86400'})


def _wrap_text(text, max_chars):
    words = text.split()
    lines, current = [], ''
    for word in words:
        if len(current) + len(word) + (1 if current else 0) <= max_chars:
            current = (current + ' ' + word).strip()
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text[:max_chars]]


def _xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ── Cover image local cache ───────────────────────────────────────────────────

def _covers_dir():
    d = os.path.join(current_app.instance_path, 'covers')
    os.makedirs(d, exist_ok=True)
    return d


def _cover_path(book_id):
    return os.path.join(_covers_dir(), f'{book_id}.jpg')


def _download_cover(book):
    """Download book.cover_url to the local covers cache. No-op if already cached or no URL."""
    if not book.cover_url:
        return
    path = _cover_path(book.id)
    if os.path.exists(path):
        return
    try:
        import urllib.request as _ur
        req = _ur.Request(book.cover_url, headers={'User-Agent': 'ChoreTracker/1.0'})
        with _ur.urlopen(req, timeout=8) as r:
            data = r.read()
        with open(path, 'wb') as f:
            f.write(data)
    except Exception as e:
        current_app.logger.warning('Cover download failed for book %s: %s', book.id, e)


def _maybe_delete_cover(book_id):
    """Delete cached cover if no active (unreturned) checkouts remain for this book."""
    active = BookCheckout.query.filter(
        BookCheckout.book_id == book_id,
        BookCheckout.returned_date == None  # noqa: E711
    ).first()
    if not active:
        path = _cover_path(book_id)
        try:
            os.remove(path)
        except OSError:
            pass
