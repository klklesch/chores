import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
    db_path = os.environ.get('DATABASE_URL', 'sqlite:///database/chores.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['PERMANENT_SESSION_LIFETIME'] = 3600 * 8  # 8 hours

    db.init_app(app)

    from .routes.main import main_bp
    from .routes.child import child_bp
    from .routes.parent import parent_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(child_bp, url_prefix='/child')
    app.register_blueprint(parent_bp, url_prefix='/parent')

    with app.app_context():
        db.create_all()
        _migrate_db()
        _seed_defaults()

    @app.context_processor
    def inject_timeout():
        from .models import AppSettings as _AS
        try:
            s = _AS.query.get('session_timeout')
            minutes = int(s.value) if s and s.value else 5
        except Exception:
            minutes = 5
        return {'inactivity_timeout_ms': minutes * 60 * 1000}

    from .scheduler import init_scheduler
    init_scheduler(app)

    return app


def _migrate_db():
    """Idempotently migrate the database schema as the app evolves."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    # ── Legacy column additions (kept for very old databases) ────────────────
    if 'children' in tables:
        child_cols = {c['name'] for c in inspector.get_columns('children')}
        with db.engine.connect() as conn:
            if 'avatar_filename' not in child_cols:
                conn.execute(text('ALTER TABLE children ADD COLUMN avatar_filename VARCHAR(200)'))
                conn.commit()

    if 'chores' in tables:
        chore_cols = {c['name'] for c in inspector.get_columns('chores')}
        with db.engine.connect() as conn:
            if 'icon' not in chore_cols:
                conn.execute(text('ALTER TABLE chores ADD COLUMN icon VARCHAR(10)'))
                conn.commit()

    if 'assigned_chores' in tables:
        existing = {c['name'] for c in inspector.get_columns('assigned_chores')}

        # ── Schema-split migration ────────────────────────────────────────────
        if 'status' in existing:
            # Old monolithic schema detected → migrate to two-table design
            _migrate_schema_split()
            # Re-inspect after migration
            inspector = inspect(db.engine)
            existing = {c['name'] for c in inspector.get_columns('assigned_chores')}
        else:
            # Post-split: ensure is_active column exists
            with db.engine.connect() as conn:
                if 'is_active' not in existing:
                    conn.execute(text(
                        'ALTER TABLE assigned_chores ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1'
                    ))
                    conn.commit()

    # ── Ensure chore_instance_id on balance_transactions ─────────────────────
    if 'balance_transactions' in tables:
        bt_cols = {c['name'] for c in inspector.get_columns('balance_transactions')}
        with db.engine.connect() as conn:
            if 'chore_instance_id' not in bt_cols:
                conn.execute(text(
                    'ALTER TABLE balance_transactions ADD COLUMN chore_instance_id INTEGER'
                ))
                conn.commit()

    # ── Ensure terminal_date on chore_instances ───────────────────────────────
    if 'chore_instances' in tables:
        ci_cols = {c['name'] for c in inspector.get_columns('chore_instances')}
        with db.engine.connect() as conn:
            if 'terminal_date' not in ci_cols:
                conn.execute(text(
                    'ALTER TABLE chore_instances ADD COLUMN terminal_date DATETIME'
                ))
                conn.commit()


def _migrate_schema_split():
    """
    One-time migration: split the monolithic assigned_chores table into
    assigned_chores (config/scheduling) and chore_instances (per-occurrence).

    Strategy
    --------
    • Each old row → one ChoreInstance row (same id).
    • Non-recurring: one AssignedChore config per old row (same id).
    • Recurring:     one AssignedChore config per (child_id, chore_id),
                     using min(id) of that group as the config id so that
                     all instances can reference it.
    • balance_transactions.chore_instance_id = old assigned_chore_id
      (works because chore_instances.id == old assigned_chores.id).
    """
    from sqlalchemy import text
    import logging
    logger = logging.getLogger(__name__)
    logger.info('Running schema-split migration: assigned_chores → config + chore_instances')

    with db.engine.connect() as conn:
        # ── Step 1: Populate chore_instances from old data ────────────────────
        # chore_instances table was created empty by db.create_all()
        conn.execute(text("""
            INSERT INTO chore_instances
                (id, assigned_chore_id, status, period, assigned_date,
                 submitted_date, approved_date, denial_notes, awarded_value)
            SELECT
                old.id,
                CASE
                    WHEN old.is_recurring = 1 THEN cfg.config_id
                    ELSE old.id
                END AS assigned_chore_id,
                old.status,
                old.period,
                old.assigned_date,
                old.submitted_date,
                old.approved_date,
                old.denial_notes,
                old.awarded_value
            FROM assigned_chores old
            LEFT JOIN (
                SELECT child_id, chore_id, MIN(id) AS config_id
                FROM assigned_chores
                WHERE is_recurring = 1
                GROUP BY child_id, chore_id
            ) cfg ON old.is_recurring = 1
                  AND old.child_id = cfg.child_id
                  AND old.chore_id = cfg.chore_id
        """))

        # ── Step 2: Build new assigned_chores config table ────────────────────
        conn.execute(text("""
            CREATE TABLE assigned_chores_new (
                id            INTEGER PRIMARY KEY,
                child_id      INTEGER NOT NULL REFERENCES children(id),
                chore_id      INTEGER NOT NULL REFERENCES chores(id),
                custom_value        REAL,
                override_name       VARCHAR(100),
                override_description TEXT,
                is_recurring        BOOLEAN NOT NULL DEFAULT 0,
                recurrence_cadence  VARCHAR(20),
                recurrence_day      INTEGER,
                is_active           BOOLEAN NOT NULL DEFAULT 1,
                created_at          DATETIME
            )
        """))

        # Non-recurring: one config row per old row, same id
        # Use assigned_date as created_at (created_at may not exist in old schema)
        conn.execute(text("""
            INSERT INTO assigned_chores_new
                (id, child_id, chore_id, custom_value, override_name,
                 override_description, is_recurring, recurrence_cadence,
                 recurrence_day, is_active, created_at)
            SELECT
                id, child_id, chore_id, custom_value, override_name,
                override_description, 0, NULL, NULL, 1, assigned_date
            FROM assigned_chores
            WHERE is_recurring = 0 OR is_recurring IS NULL
        """))

        # Recurring: one config per (child_id, chore_id), id = MIN(old id)
        # Pull config values from the most-recent old row for that combo
        conn.execute(text("""
            INSERT INTO assigned_chores_new
                (id, child_id, chore_id, custom_value, override_name,
                 override_description, is_recurring, recurrence_cadence,
                 recurrence_day, is_active, created_at)
            SELECT
                grp.config_id,
                grp.child_id,
                grp.chore_id,
                latest.custom_value,
                latest.override_name,
                latest.override_description,
                1,
                latest.recurrence_cadence,
                latest.recurrence_day,
                1,
                grp.earliest_date
            FROM (
                SELECT child_id, chore_id,
                       MIN(id)            AS config_id,
                       MIN(assigned_date) AS earliest_date
                FROM assigned_chores
                WHERE is_recurring = 1
                GROUP BY child_id, chore_id
            ) grp
            JOIN assigned_chores latest ON latest.id = (
                SELECT id FROM assigned_chores
                WHERE child_id = grp.child_id
                  AND chore_id = grp.chore_id
                  AND is_recurring = 1
                ORDER BY assigned_date DESC
                LIMIT 1
            )
        """))

        # ── Step 3: Add chore_instance_id to balance_transactions ─────────────
        try:
            conn.execute(text(
                'ALTER TABLE balance_transactions ADD COLUMN chore_instance_id INTEGER'
            ))
        except Exception:
            pass  # already exists

        conn.execute(text("""
            UPDATE balance_transactions
            SET chore_instance_id = assigned_chore_id
            WHERE assigned_chore_id IS NOT NULL
        """))

        # ── Step 4: Replace the table ─────────────────────────────────────────
        conn.execute(text('ALTER TABLE assigned_chores RENAME TO assigned_chores_backup'))
        conn.execute(text('ALTER TABLE assigned_chores_new RENAME TO assigned_chores'))

        conn.commit()

    logger.info('Schema-split migration complete.')


def _seed_defaults():
    from .models import AppSettings, Chore
    import bcrypt

    if not AppSettings.query.get('parent_pin'):
        pin_hash = bcrypt.hashpw(b'1234', bcrypt.gensalt()).decode()
        db.session.add(AppSettings(key='parent_pin', value=pin_hash))

    if not AppSettings.query.get('payout_cadence'):
        db.session.add(AppSettings(key='payout_cadence', value='instant'))
    if not AppSettings.query.get('payout_time'):
        db.session.add(AppSettings(key='payout_time', value='18:00'))
    if not AppSettings.query.get('payout_day_of_week'):
        db.session.add(AppSettings(key='payout_day_of_week', value='0'))
    if not AppSettings.query.get('payout_day_of_month'):
        db.session.add(AppSettings(key='payout_day_of_month', value='1'))
    if not AppSettings.query.get('session_timeout'):
        db.session.add(AppSettings(key='session_timeout', value='5'))
    if not AppSettings.query.get('notify_email_enabled'):
        db.session.add(AppSettings(key='notify_email_enabled', value='off'))
    if not AppSettings.query.get('app_base_url'):
        db.session.add(AppSettings(key='app_base_url', value=''))
    if not AppSettings.query.get('book_default_loan_days'):
        db.session.add(AppSettings(key='book_default_loan_days', value='28'))
    if not AppSettings.query.get('app_timezone'):
        db.session.add(AppSettings(key='app_timezone', value='UTC'))

    if Chore.query.count() == 0:
        defaults = [
            ('Make Bed', 'Make your bed neatly each morning', 0.50),
            ('Do Homework', 'Complete all homework assignments', 1.00),
            ('Clean Room', 'Tidy and organize your room', 1.50),
            ('Wash Dishes', 'Wash and dry the dishes', 1.00),
            ('Take Out Trash', 'Take trash cans out to the curb', 1.00),
            ('Vacuum Living Room', 'Vacuum the living room floor', 1.50),
            ('Feed Pet', 'Feed and water the pet', 0.50),
            ('Set Table', 'Set the dinner table before meals', 0.50),
            ('Sweep Floor', 'Sweep the kitchen floor', 0.75),
            ('Wipe Counters', 'Wipe down kitchen counters', 0.75),
            ('Clean Bathroom', 'Clean and scrub the bathroom', 2.00),
            ('Put Away Laundry', 'Fold and put away clean laundry', 1.00),
            ('Read for 20 Minutes', 'Read a book for at least 20 minutes', 0.50),
            ('Mow Lawn', 'Mow the front and back lawn', 5.00),
            ('Weed Garden', 'Pull weeds from the garden', 2.00),
        ]
        for name, desc, value in defaults:
            db.session.add(Chore(name=name, description=desc, default_value=value))

    # Ensure "Read a Book" chore exists (shared template for book completions)
    if not Chore.query.filter_by(name='Read a Book').first():
        db.session.add(Chore(
            name='Read a Book',
            description='Finish a library book',
            icon='📚',
            default_value=0.0,
            is_active=False,
        ))

    db.session.commit()
