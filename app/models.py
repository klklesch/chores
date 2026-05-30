from datetime import datetime
from . import db


class Child(db.Model):
    __tablename__ = 'children'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    avatar_color = db.Column(db.String(7), default='#6366f1')
    avatar_filename = db.Column(db.String(200))
    balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    assigned_chores = db.relationship(
        'AssignedChore', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )
    transactions = db.relationship(
        'BalanceTransaction', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )
    wishlist_items = db.relationship(
        'WishlistItem', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )
    book_assignments = db.relationship(
        'BookAssignment', back_populates='child', lazy=True, cascade='all, delete-orphan'
    )

    @property
    def pending_submission_count(self):
        return sum(1 for ac in self.assigned_chores for inst in ac.instances if inst.status == 'submitted')

    @property
    def active_chore_count(self):
        return sum(1 for ac in self.assigned_chores for inst in ac.instances if inst.status == 'assigned')


class Chore(db.Model):
    __tablename__ = 'chores'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    icon = db.Column(db.String(10))
    default_value = db.Column(db.Float, default=1.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    assignments = db.relationship('AssignedChore', back_populates='chore', lazy=True)


class AssignedChore(db.Model):
    """Scheduling / config row — one per unique chore assignment for a child.
    Recurring chores share a single config row; ChoreInstance rows track each period.
    """
    __tablename__ = 'assigned_chores'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    chore_id = db.Column(db.Integer, db.ForeignKey('chores.id'), nullable=False)
    custom_value = db.Column(db.Float)          # overrides chore.default_value when set
    override_name = db.Column(db.String(100))
    override_description = db.Column(db.Text)
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_cadence = db.Column(db.String(20))   # 'daily', 'weekly', 'monthly'
    recurrence_day = db.Column(db.Integer)           # weekly: 0=Mon…6=Sun; monthly: 1–31
    is_active = db.Column(db.Boolean, default=True)  # False = cancelled / stopped
    created_at = db.Column(db.DateTime, default=datetime.now)

    child = db.relationship('Child', back_populates='assigned_chores')
    chore = db.relationship('Chore', back_populates='assignments')
    instances = db.relationship(
        'ChoreInstance', back_populates='assigned_chore',
        cascade='all, delete-orphan', lazy=True,
    )

    @property
    def effective_name(self):
        return self.override_name or (self.chore.name if self.chore else '')

    @property
    def effective_description(self):
        return self.override_description or (self.chore.description if self.chore else '')

    @property
    def effective_value(self):
        if self.custom_value is not None:
            return self.custom_value
        return self.chore.default_value if self.chore else 0.0

    @property
    def recurrence_label(self):
        if not self.is_recurring or not self.recurrence_cadence:
            return None
        if self.recurrence_cadence == 'daily':
            return 'Daily'
        if self.recurrence_cadence == 'weekly':
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            day = day_names[self.recurrence_day] if self.recurrence_day is not None else 'Monday'
            return f'Weekly · Every {day}'
        if self.recurrence_cadence == 'monthly':
            d = self.recurrence_day if self.recurrence_day is not None else 1
            suffix = 'th' if 11 <= d <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')
            return f'Monthly · {d}{suffix} of each month'
        return self.recurrence_cadence.capitalize()


class ChoreInstance(db.Model):
    """One occurrence of an AssignedChore being worked on and completed.
    Tracks the mutable per-period state: status, dates, partial credit, denial notes.
    """
    __tablename__ = 'chore_instances'
    id = db.Column(db.Integer, primary_key=True)
    assigned_chore_id = db.Column(db.Integer, db.ForeignKey('assigned_chores.id'), nullable=False)
    status = db.Column(db.String(20), default='assigned')
    # Status flow: assigned → submitted → approved | denied → assigned
    #              assigned → expired  (missed period)
    #              approved → approved_pending (waiting for scheduled payout)
    period = db.Column(db.String(20))       # '2024-04-15' | '2024-W17' | '2024-04'
    assigned_date = db.Column(db.DateTime, default=datetime.now)
    submitted_date = db.Column(db.DateTime)
    approved_date = db.Column(db.DateTime)
    terminal_date = db.Column(db.DateTime)  # set whenever status becomes terminal (approved/approved_pending/expired)
    denial_notes = db.Column(db.Text)
    awarded_value = db.Column(db.Float)     # set when partial credit is given at approval

    assigned_chore = db.relationship('AssignedChore', back_populates='instances')

    # ── Proxy properties (delegate to config for read convenience) ────────────
    @property
    def child(self):
        return self.assigned_chore.child

    @property
    def chore(self):
        return self.assigned_chore.chore

    @property
    def child_id(self):
        return self.assigned_chore.child_id

    @property
    def chore_id(self):
        return self.assigned_chore.chore_id

    @property
    def is_recurring(self):
        return self.assigned_chore.is_recurring

    @property
    def recurrence_cadence(self):
        return self.assigned_chore.recurrence_cadence

    @property
    def recurrence_day(self):
        return self.assigned_chore.recurrence_day

    @property
    def recurrence_label(self):
        return self.assigned_chore.recurrence_label

    @property
    def effective_name(self):
        return self.assigned_chore.effective_name

    @property
    def effective_description(self):
        return self.assigned_chore.effective_description

    @property
    def effective_value(self):
        return self.assigned_chore.effective_value

    @property
    def actual_payout(self):
        """Amount actually credited — may be less than effective_value for partial credit."""
        if self.awarded_value is not None:
            return self.awarded_value
        return self.effective_value

    @property
    def is_partial(self):
        return self.awarded_value is not None and self.awarded_value < self.effective_value


class BalanceTransaction(db.Model):
    __tablename__ = 'balance_transactions'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200))
    transaction_date = db.Column(db.DateTime, default=datetime.now)
    chore_instance_id = db.Column(db.Integer, db.ForeignKey('chore_instances.id'))

    child = db.relationship('Child', back_populates='transactions')


class WishlistItem(db.Model):
    __tablename__ = 'wishlist_items'
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False)
    url = db.Column(db.String(500))
    status = db.Column(db.String(20), default='active')  # 'active', 'purchased'
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    purchased_date = db.Column(db.DateTime)

    child = db.relationship('Child', back_populates='wishlist_items')


class Book(db.Model):
    __tablename__ = 'books'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    author = db.Column(db.String(200))
    isbn = db.Column(db.String(20))
    cover_url = db.Column(db.String(500))
    notes = db.Column(db.Text)

    checkouts = db.relationship('BookCheckout', back_populates='book', lazy=True, cascade='all, delete-orphan')


class BookCheckout(db.Model):
    __tablename__ = 'book_checkouts'
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('books.id'), nullable=False)
    checked_out_date = db.Column(db.DateTime, default=datetime.now)
    due_date = db.Column(db.Date)
    returned_date = db.Column(db.DateTime)

    book = db.relationship('Book', back_populates='checkouts')
    assignments = db.relationship('BookAssignment', back_populates='checkout', lazy=True, cascade='all, delete-orphan')

    @property
    def is_returned(self):
        return self.returned_date is not None

    @property
    def is_overdue(self):
        if self.returned_date or not self.due_date:
            return False
        from datetime import date
        return date.today() > self.due_date

    @property
    def days_until_due(self):
        if self.returned_date or not self.due_date:
            return None
        from datetime import date
        return (self.due_date - date.today()).days


class BookAssignment(db.Model):
    __tablename__ = 'book_assignments'
    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.Integer, db.ForeignKey('book_checkouts.id'), nullable=False)
    child_id = db.Column(db.Integer, db.ForeignKey('children.id'), nullable=False)
    reward_value = db.Column(db.Float, default=0.0, nullable=False)
    status = db.Column(db.String(20), default='unread', nullable=False)
    # Status flow: unread → reading → finished | unfinished
    assigned_date = db.Column(db.DateTime, default=datetime.now)
    started_date = db.Column(db.DateTime)
    finished_date = db.Column(db.DateTime)
    chore_instance_id = db.Column(db.Integer, db.ForeignKey('chore_instances.id'))

    checkout = db.relationship('BookCheckout', back_populates='assignments')
    child = db.relationship('Child', back_populates='book_assignments')
    chore_instance = db.relationship('ChoreInstance', foreign_keys=[chore_instance_id])


class AppSettings(db.Model):
    __tablename__ = 'app_settings'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)
