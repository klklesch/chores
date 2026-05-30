import calendar as cal_module
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, session, redirect, url_for, flash, request, make_response
from ..models import Child, AssignedChore, ChoreInstance, BalanceTransaction, WishlistItem, BookCheckout, BookAssignment
from ..utils import get_payout_period_info, get_or_create_book_chore
from .. import db

child_bp = Blueprint('child', __name__)


@child_bp.route('/')
def select():
    children = Child.query.order_by(Child.name).all()
    return render_template('child/select.html', children=children, inactivity_timeout_ms=0)


@child_bp.route('/<int:child_id>')
def dashboard(child_id):
    child = Child.query.get_or_404(child_id)
    session['child_id'] = child_id

    sort = request.args.get('sort', 'date')

    assigned = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'assigned',
        )
        .all()
    )
    if sort == 'name':
        assigned.sort(key=lambda inst: inst.effective_name.lower())
    elif sort == 'value':
        assigned.sort(key=lambda inst: inst.effective_value, reverse=True)
    elif sort == 'cadence':
        assigned.sort(key=lambda inst: (inst.recurrence_cadence or '', inst.effective_name.lower()))
    else:
        assigned.sort(key=lambda inst: inst.assigned_date, reverse=True)

    submitted = (
        ChoreInstance.query
        .join(AssignedChore)
        .filter(
            AssignedChore.child_id == child_id,
            ChoreInstance.status == 'submitted',
        )
        .order_by(ChoreInstance.submitted_date.desc())
        .all()
    )

    period = get_payout_period_info()
    cadence = period['cadence']

    if cadence == 'instant':
        period_chores = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status == 'approved',
                ChoreInstance.approved_date >= period['period_start'],
            )
            .order_by(ChoreInstance.approved_date.desc())
            .all()
        )
    else:
        period_chores = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status == 'approved_pending',
            )
            .order_by(ChoreInstance.approved_date.desc())
            .all()
        )

    period_total = sum(inst.actual_payout for inst in period_chores)

    return render_template(
        'child/dashboard.html',
        child=child,
        assigned=assigned,
        submitted=submitted,
        sort=sort,
        period=period,
        period_chores=period_chores,
        period_total=period_total,
    )


@child_bp.route('/<int:child_id>/history')
def history(child_id):
    child = Child.query.get_or_404(child_id)

    month_str = request.args.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = (int(p) for p in month_str.split('-'))
        date(year, month, 1)
    except (ValueError, AttributeError):
        year, month = date.today().year, date.today().month

    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    day_str = request.args.get('day')
    selected_day = None
    if day_str:
        try:
            selected_day = date.fromisoformat(day_str)
        except ValueError:
            pass

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
        activity_days.setdefault(inst.approved_date.date(), {})['completed'] = True
    for tx in txns_in_month:
        activity_days.setdefault(tx.transaction_date.date(), {})['transaction'] = True
    for inst in expired_in_month:
        activity_days.setdefault(inst.assigned_date.date(), {})['missed'] = True

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

        day_expired = (
            ChoreInstance.query
            .join(AssignedChore)
            .filter(
                AssignedChore.child_id == child_id,
                ChoreInstance.status == 'expired',
                db.func.date(ChoreInstance.assigned_date) == selected_day.isoformat(),
            ).all()
        )

    earned_month_total = sum(inst.actual_payout for inst in approved_in_month)
    missed_month_total = sum(inst.effective_value for inst in expired_in_month)
    earned_week_total = 0.0
    missed_week_total = 0.0
    earned_day_total = 0.0
    missed_day_total = 0.0
    if selected_day:
        week_start = selected_day - timedelta(days=selected_day.weekday())
        week_end = week_start + timedelta(days=7)
        earned_week_total = sum(
            inst.actual_payout for inst in approved_in_month
            if week_start <= inst.approved_date.date() < week_end
        )
        missed_week_total = sum(
            inst.effective_value for inst in expired_in_month
            if week_start <= inst.assigned_date.date() < week_end
        )
        earned_day_total = sum(inst.actual_payout for inst in day_completed)
        missed_day_total = sum(inst.effective_value for inst in day_expired)

    prev_month = f'{year-1}-12' if month == 1 else f'{year}-{month-1:02d}'
    next_month = f'{year+1}-01' if month == 12 else f'{year}-{month+1:02d}'

    return render_template(
        'child/history.html',
        child=child,
        year=year,
        month=month,
        month_name=month_start.strftime('%B %Y'),
        cal_grid=cal_grid,
        selected_day=selected_day,
        day_completed=day_completed,
        day_transactions=day_transactions,
        day_expired=day_expired,
        earned_day_total=earned_day_total,
        earned_week_total=earned_week_total,
        earned_month_total=earned_month_total,
        missed_day_total=missed_day_total,
        missed_week_total=missed_week_total,
        missed_month_total=missed_month_total,
        prev_month=prev_month,
        next_month=next_month,
    )


@child_bp.route('/<int:child_id>/wishlist')
def wishlist(child_id):
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
    return render_template('child/wishlist.html', child=child, active=active, purchased=purchased)


@child_bp.route('/<int:child_id>/wishlist/add', methods=['POST'])
def add_wish(child_id):
    Child.query.get_or_404(child_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    if not name or not price_raw:
        flash('Item name and price are required.', 'error')
        return redirect(url_for('child.wishlist', child_id=child_id))

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
    flash(f'"{name}" added to your wishlist! 🌟', 'success')
    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/wishlist/<int:item_id>/move', methods=['POST'])
def move_wish(child_id, item_id):
    direction = request.form.get('direction')
    item = WishlistItem.query.get_or_404(item_id)
    if item.child_id != child_id:
        return redirect(url_for('child.wishlist', child_id=child_id))

    siblings = (
        WishlistItem.query
        .filter_by(child_id=child_id, status='active')
        .order_by(WishlistItem.sort_order, WishlistItem.created_at)
        .all()
    )
    ids = [s.id for s in siblings]
    idx = ids.index(item_id)

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap_idx < len(siblings):
        other = siblings[swap_idx]
        item.sort_order, other.sort_order = other.sort_order, item.sort_order
        if item.sort_order == other.sort_order:
            item.sort_order = swap_idx
            other.sort_order = idx
        db.session.commit()

    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/wishlist/<int:item_id>/delete', methods=['POST'])
def delete_wish(child_id, item_id):
    item = WishlistItem.query.get_or_404(item_id)
    if item.child_id != child_id:
        return redirect(url_for('child.wishlist', child_id=child_id))
    db.session.delete(item)
    db.session.commit()
    flash('Item removed from wishlist.', 'info')
    return redirect(url_for('child.wishlist', child_id=child_id))


@child_bp.route('/<int:child_id>/submit/<int:ac_id>', methods=['POST'])
def submit_chore(child_id, ac_id):
    inst = ChoreInstance.query.get_or_404(ac_id)
    if inst.child_id != child_id or inst.status != 'assigned':
        flash('Cannot submit this chore right now.', 'error')
        return redirect(url_for('child.dashboard', child_id=child_id))

    inst.status = 'submitted'
    inst.submitted_date = datetime.now()
    db.session.commit()

    from ..notifications import send_chore_submitted
    send_chore_submitted(inst)

    flash('Nice work! Your chore has been sent to a parent for review. 🌟', 'success')
    return redirect(url_for('child.dashboard', child_id=child_id))


@child_bp.route('/<int:child_id>/books')
def child_books(child_id):
    child = Child.query.get_or_404(child_id)
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .all()
    )
    assignments = {
        co.id: BookAssignment.query.filter_by(checkout_id=co.id, child_id=child_id).first()
        for co in checkouts
    }
    return render_template('child/books.html', child=child, checkouts=checkouts, assignments=assignments)


@child_bp.route('/<int:child_id>/books/<int:checkout_id>/self_assign', methods=['POST'])
def self_assign_book(child_id, checkout_id):
    existing = BookAssignment.query.filter_by(checkout_id=checkout_id, child_id=child_id).first()
    if existing:
        flash('You already have this book on your reading list.', 'error')
        return redirect(url_for('child.child_books', child_id=child_id))
    db.session.add(BookAssignment(
        checkout_id=checkout_id,
        child_id=child_id,
        reward_value=0.0,
        status='unread',
    ))
    db.session.commit()
    flash('Added to your reading list!', 'success')
    return redirect(url_for('child.child_books', child_id=child_id))


@child_bp.route('/<int:child_id>/assignments/<int:assignment_id>/start', methods=['POST'])
def start_book(child_id, assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    if assignment.status != 'unread':
        flash('This book cannot be started right now.', 'error')
        return redirect(url_for('child.child_books', child_id=child_id))
    assignment.status = 'reading'
    assignment.started_date = datetime.now()
    db.session.commit()
    flash('Happy reading!', 'success')
    return redirect(url_for('child.child_books', child_id=child_id))


@child_bp.route('/<int:child_id>/assignments/<int:assignment_id>/finish', methods=['POST'])
def finish_book(child_id, assignment_id):
    assignment = BookAssignment.query.get_or_404(assignment_id)
    checkout = assignment.checkout
    book = checkout.book
    if assignment.status != 'reading':
        flash('This book cannot be finished right now.', 'error')
        return redirect(url_for('child.child_books', child_id=child_id))
    assignment.status = 'finished'
    assignment.finished_date = datetime.now()
    book_chore = get_or_create_book_chore()
    ac = AssignedChore(
        child_id=child_id,
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
    from ..notifications import send_chore_submitted
    send_chore_submitted(inst)
    db.session.commit()
    flash('Great job finishing the book!', 'success')
    return redirect(url_for('child.child_books', child_id=child_id))


@child_bp.route('/<int:child_id>/books/history')
def child_books_history(child_id):
    child = Child.query.get_or_404(child_id)
    assignments = (
        BookAssignment.query
        .filter_by(child_id=child_id)
        .order_by(BookAssignment.assigned_date.desc())
        .all()
    )
    return render_template('child/books_history_partial.html', child=child, assignments=assignments)


@child_bp.route('/<int:child_id>/books/search')
def search_child_books(child_id):
    child = Child.query.get_or_404(child_id)
    q = request.args.get('q', '').strip()
    checkouts = (
        BookCheckout.query
        .filter(BookCheckout.returned_date == None)  # noqa: E711
        .all()
    )
    if q:
        from thefuzz import process
        choices = {co.book.title: co for co in checkouts}
        results = process.extractBests(q, choices, score_cutoff=50)
        checkouts = [co for co, score in results]
    assignments = {
        co.id: BookAssignment.query.filter_by(checkout_id=co.id, child_id=child_id).first()
        for co in checkouts
    }
    tile_size = request.cookies.get('book_tile_size', 'medium')
    return render_template('child/books_tile_partial.html', checkouts=checkouts, assignments=assignments, child=child, tile_size=tile_size)


@child_bp.route('/<int:child_id>/books/tile-size', methods=['POST'])
def set_book_tile_size(child_id):
    size = request.form.get('size', 'medium')
    if size not in ('small', 'medium', 'large'):
        size = 'medium'
    resp = make_response('', 204)
    resp.set_cookie('book_tile_size', size, max_age=60 * 60 * 24 * 365)
    resp.headers['HX-Trigger'] = 'tileResized'
    return resp
