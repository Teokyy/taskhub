import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)
APP_DB_PATH = Path(__file__).resolve().parent.parent / "taskhub.sqlite3"
VOLUME_MOUNT_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
CONFIGURED_DB_PATH = os.getenv("DB_PATH", "").strip()
DB_PATH = Path(
    CONFIGURED_DB_PATH
    or (str(Path(VOLUME_MOUNT_PATH) / "taskhub.sqlite3") if VOLUME_MOUNT_PATH else str(APP_DB_PATH))
)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _migrate_ephemeral_database_once():
    """Copy the pre-volume SQLite database to persistent storage without overwriting data."""
    if not VOLUME_MOUNT_PATH or CONFIGURED_DB_PATH:
        return
    if DB_PATH.exists():
        log.info("DATABASE_VOLUME_TARGET_EXISTS path=%s; migration skipped", DB_PATH)
        return
    if not APP_DB_PATH.is_file():
        log.warning("DATABASE_VOLUME_SOURCE_MISSING source=%s target=%s", APP_DB_PATH, DB_PATH)
        return
    temporary_path = DB_PATH.with_name(DB_PATH.name + ".migrating")
    try:
        if temporary_path.exists():
            temporary_path.unlink()
        with sqlite3.connect(f"file:{APP_DB_PATH}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary_path) as target:
                source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise sqlite3.DatabaseError(f"SQLite integrity check failed: {integrity}")
        os.replace(temporary_path, DB_PATH)
        log.warning(
            "DATABASE_VOLUME_MIGRATED source=%s target=%s bytes=%s",
            APP_DB_PATH, DB_PATH, DB_PATH.stat().st_size,
        )
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.exception("DATABASE_VOLUME_MIGRATION_FAILED source=%s target=%s", APP_DB_PATH, DB_PATH)
        raise


_migrate_ephemeral_database_once()
log.info(
    "DATABASE_CONFIG path=%s volume_mounted=%s exists=%s",
    DB_PATH, bool(VOLUME_MOUNT_PATH), DB_PATH.exists(),
)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _add_column(conn, table, definition):
    name = definition.split()[0]
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_room_links(
                room_id INTEGER PRIMARY KEY,
                chat_id INTEGER UNIQUE NOT NULL,
                chat_title TEXT NOT NULL DEFAULT '',
                connected_by INTEGER,
                connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS telegram_room_link_codes(
                room_id INTEGER PRIMARY KEY,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'helper',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                status TEXT NOT NULL DEFAULT 'active',
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stages(
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                position INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tours(
                id INTEGER PRIMARY KEY,
                stage_id INTEGER NOT NULL REFERENCES stages(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                position INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Другое',
                reward_kop INTEGER NOT NULL DEFAULT 0 CHECK(reward_kop>=0),
                status TEXT NOT NULL DEFAULT 'available',
                assignee_id INTEGER REFERENCES users(id),
                created_by INTEGER REFERENCES users(id),
                deadline TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS submissions(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                task_id INTEGER REFERENCES tasks(id),
                amount_kop INTEGER NOT NULL,
                kind TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(task_id,kind)
            );
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY,
                actor_id INTEGER,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_criteria(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                title TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 0 CHECK(weight BETWEEN 0 AND 100),
                critical INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS criterion_results(
                id INTEGER PRIMARY KEY,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                criterion_id INTEGER NOT NULL REFERENCES task_criteria(id),
                result TEXT NOT NULL CHECK(result IN ('done','partial','violated','na')),
                comment TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                UNIQUE(submission_id,criterion_id)
            );
            CREATE TABLE IF NOT EXISTS disputes(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                submission_id INTEGER REFERENCES submissions(id),
                criterion_id INTEGER REFERENCES task_criteria(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                admin_decision TEXT NOT NULL DEFAULT '',
                admin_comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payouts(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                amount_kop INTEGER NOT NULL CHECK(amount_kop>=0),
                status TEXT NOT NULL DEFAULT 'prepared',
                paid_at TEXT,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id,period_start,period_end)
            );
            """
        )
        # Additive migrations preserve the existing MVP database and its balances.
        _add_column(conn, "users", "photo_url TEXT")
        _add_column(conn, "users", "role_flags TEXT NOT NULL DEFAULT 'none'")
        _add_column(conn, "users", "onboarding_step INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "users", "onboarding_completed_at TEXT")
        _add_column(conn, "users", "onboarding_skipped_at TEXT")
        _add_column(conn, "tasks", "room_id INTEGER")
        for definition in (
            "project_id INTEGER REFERENCES projects(id)",
            "stage_id INTEGER REFERENCES stages(id)",
            "tour_id INTEGER REFERENCES tours(id)",
            "pricing_model TEXT NOT NULL DEFAULT 'fixed'",
            "norm_minutes INTEGER NOT NULL DEFAULT 0",
            "difficulty REAL NOT NULL DEFAULT 1.0",
            "urgency REAL NOT NULL DEFAULT 1.0",
            "quality_factor REAL NOT NULL DEFAULT 1.0",
            "fixed_reward_kop INTEGER NOT NULL DEFAULT 0",
            "criteria_frozen INTEGER NOT NULL DEFAULT 0",
            "budget_reserved_kop INTEGER NOT NULL DEFAULT 0",
            "moderation_status TEXT NOT NULL DEFAULT 'approved'",
            "moderation_comment TEXT NOT NULL DEFAULT ''",
            "moderated_by INTEGER REFERENCES users(id)",
            "moderated_at TEXT",
        ):
            _add_column(conn, "tasks", definition)
        conn.execute(
            "UPDATE tasks SET fixed_reward_kop=reward_kop WHERE fixed_reward_kop=0 AND reward_kop>0"
        )

        # Marketplace MVP domain schema. All tables are additive so existing
        # room data, task history and balances remain untouched.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES categories(id),
                is_hidden INTEGER NOT NULL DEFAULT 0 CHECK(is_hidden IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(parent_id,name)
            );
            CREATE TABLE IF NOT EXISTS category_requests(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                text TEXT NOT NULL CHECK(length(text)<=200),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','added','moved','rejected')),
                resolved_category_id INTEGER REFERENCES categories(id),
                moderator_id INTEGER REFERENCES users(id),
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS responses(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                executor_id INTEGER NOT NULL REFERENCES users(id),
                message TEXT NOT NULL DEFAULT '' CHECK(length(message)<=1000),
                proposed_deadline TEXT,
                status TEXT NOT NULL DEFAULT 'sent' CHECK(status IN ('sent','accepted','rejected','withdrawn')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(task_id,executor_id)
            );
            CREATE TABLE IF NOT EXISTS chats(
                id INTEGER PRIMARY KEY,
                response_id INTEGER NOT NULL UNIQUE REFERENCES responses(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL REFERENCES chats(id),
                sender_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL CHECK(length(text)<=2000),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                hidden_for_user_id INTEGER REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS message_hides(
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(message_id,user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_hides_user ON message_hides(user_id,message_id);
            CREATE TABLE IF NOT EXISTS task_history(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                changed_by INTEGER NOT NULL REFERENCES users(id),
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                author_id INTEGER NOT NULL REFERENCES users(id),
                target_id INTEGER NOT NULL REFERENCES users(id),
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                text TEXT NOT NULL DEFAULT '' CHECK(length(text)<=500),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(task_id,author_id)
            );
            CREATE TABLE IF NOT EXISTS subscriptions(
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,category_id)
            );
            CREATE TABLE IF NOT EXISTS achievements(
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                earned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,code)
            );
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT,
                channel TEXT NOT NULL DEFAULT 'telegram'
            );
            CREATE INDEX IF NOT EXISTS idx_responses_task_status ON responses(task_id,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_category ON subscriptions(category_id);
            CREATE INDEX IF NOT EXISTS idx_category_requests_status_created ON category_requests(status,created_at);
            CREATE INDEX IF NOT EXISTS idx_task_history_task_changed ON task_history(task_id,changed_at);
            CREATE INDEX IF NOT EXISTS idx_notifications_unsent ON notifications(sent_at,created_at);
            """
        )
        _add_column(conn, "tasks", "category_id INTEGER REFERENCES categories(id)")
        _add_column(conn, "tasks", "author_id INTEGER REFERENCES users(id)")
        _add_column(conn, "tasks", "dispute_status TEXT")
        _add_column(conn, "tasks", "completed_at TEXT")
        # IRL marketplace and courier delivery fields are additive migrations.
        for definition in (
            "task_mode TEXT NOT NULL DEFAULT 'online'",
            "city TEXT NOT NULL DEFAULT ''",
            "pickup_address TEXT NOT NULL DEFAULT ''",
            "delivery_address TEXT NOT NULL DEFAULT ''",
            "pickup_lat REAL",
            "pickup_lon REAL",
            "delivery_lat REAL",
            "delivery_lon REAL",
            "parcel_weight_kg REAL",
            "parcel_length_cm REAL",
            "parcel_width_cm REAL",
            "parcel_height_cm REAL",
            "parcel_count INTEGER NOT NULL DEFAULT 1",
            "delivery_distance_km REAL",
            "delivery_weather TEXT",
            "delivery_weather_factor REAL",
            "delivery_courier_pay_kop INTEGER",
        ):
            _add_column(conn, "tasks", definition)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_category_status_created ON tasks(category_id,status,created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_market_tasks_feed ON tasks(room_id,status,id DESC)")

        # Seed the two-level launch taxonomy idempotently.
        category_tree = {
            "Маркетинг": ["SMM", "Таргетированная реклама", "Копирайтинг", "SEO", "Дизайн соцсетей", "Видеомонтаж для соцсетей", "Email-рассылки", "Инфлюенс-маркетинг"],
            "Discord": ["Боты", "Оформление серверов", "Модерация серверов", "Настройка ролей и прав", "Разработка интеграций", "Дизайн эмодзи и баннеров"],
            "Игры": ["Гайды и тексты", "Моддинг", "Прокачка аккаунтов", "Дизайн игровых ассетов", "Буст и тренировки", "Тестирование и баг-репорты", "Стримы и монтаж"],
            "Разработка": ["Telegram-боты", "Сайты и лендинги", "Мобильные приложения", "Скрипты и автоматизация"],
            "Дизайн": ["Логотипы и брендинг", "Иллюстрации", "UI/UX", "Презентации"],
            "Тексты и переводы": ["Копирайтинг", "Редактура", "Переводы", "Транскрибация"],
            "Видео и аудио": ["Видеомонтаж", "Озвучка", "Motion-дизайн", "Подкасты"],
            "Образование и репетиторство": ["Языки", "Школьные предметы", "Программирование", "Подготовка к экзаменам"],
            "Бизнес и финансы": ["Бухгалтерия", "Юридическая помощь", "Бизнес-планы", "Консультации"],
            "Бытовые услуги": ["Сборка мебели", "Разборка мебели", "Мелкий бытовой ремонт", "Сантехнические работы", "Электромонтажные работы", "Уборка квартиры", "Мытьё окон", "Помощь с переездом", "Погрузка и разгрузка", "Садовые работы"],
            "Доставка и курьеры": ["Доставка посылок", "Доставка продуктов", "Забрать заказ из магазина", "Курьер на несколько поручений", "Перевозка небольших грузов"],
            "Другое": ["Прочее"],
        }
        for parent_name, children in category_tree.items():
            conn.execute("INSERT OR IGNORE INTO categories(name,parent_id) VALUES(?,NULL)", (parent_name,))
            parent_id = conn.execute("SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (parent_name,)).fetchone()["id"]
            for child_name in children:
                conn.execute("INSERT OR IGNORE INTO categories(name,parent_id) VALUES(?,?)", (child_name,parent_id))
        conn.execute("UPDATE users SET role_flags='none' WHERE onboarding_step=0 AND onboarding_completed_at IS NULL AND onboarding_skipped_at IS NULL AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE subscriptions.user_id=users.id)")
        conn.execute("UPDATE tasks SET author_id=created_by WHERE author_id IS NULL AND created_by IS NOT NULL")
        conn.execute("UPDATE tasks SET status='open' WHERE room_id IS NULL AND status='available' AND assignee_id IS NULL")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_cancel_requests(
                task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                requested_by INTEGER NOT NULL REFERENCES users(id),
                accepted_by INTEGER REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_presence(
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digest_windows(
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                window_start TEXT NOT NULL,
                sent_at TEXT,
                PRIMARY KEY(user_id,window_start)
            );
            CREATE TABLE IF NOT EXISTS market_services(
                id INTEGER PRIMARY KEY,
                seller_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                price_kop INTEGER NOT NULL CHECK(price_kop>0),
                delivery_minutes INTEGER NOT NULL DEFAULT 60,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS market_service_orders(
                id INTEGER PRIMARY KEY,
                service_id INTEGER NOT NULL REFERENCES market_services(id),
                buyer_id INTEGER NOT NULL REFERENCES users(id),
                seller_id INTEGER NOT NULL REFERENCES users(id),
                details TEXT NOT NULL DEFAULT '',
                price_kop INTEGER NOT NULL CHECK(price_kop>0),
                status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','accepted','delivered','completed','cancelled')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_market_services_active ON market_services(active,category);
            CREATE INDEX IF NOT EXISTS idx_market_service_orders_users ON market_service_orders(buyer_id,seller_id,status);
            CREATE TABLE IF NOT EXISTS market_notification_preferences(
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                categories TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id,stage_id,tour_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id,id);
            CREATE TABLE IF NOT EXISTS courier_profiles(
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                city TEXT NOT NULL DEFAULT '',
                transport TEXT NOT NULL DEFAULT 'walk' CHECK(transport IN ('walk','bicycle','car','motorcycle')),
                max_weight_kg REAL NOT NULL DEFAULT 5 CHECK(max_weight_kg>0 AND max_weight_kg<=200),
                max_length_cm REAL NOT NULL DEFAULT 40 CHECK(max_length_cm>0 AND max_length_cm<=500),
                max_width_cm REAL NOT NULL DEFAULT 30 CHECK(max_width_cm>0 AND max_width_cm<=500),
                max_height_cm REAL NOT NULL DEFAULT 30 CHECK(max_height_cm>0 AND max_height_cm<=500),
                max_items INTEGER NOT NULL DEFAULT 1 CHECK(max_items BETWEEN 1 AND 50),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_attachments(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                uploader_id INTEGER NOT NULL REFERENCES users(id),
                file_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                file_size INTEGER NOT NULL CHECK(file_size BETWEEN 1 AND 10485760),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_irl_city ON tasks(task_mode,city,status);
            CREATE INDEX IF NOT EXISTS idx_courier_profiles_city ON courier_profiles(city);
            CREATE INDEX IF NOT EXISTS idx_task_attachments_task ON task_attachments(task_id,id);
            CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
            """
        )
