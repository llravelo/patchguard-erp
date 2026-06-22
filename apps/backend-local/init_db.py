"""Create tables + seed data. Run once after `docker compose up -d db`:

    python init_db.py

Seeds:
  - admin user      admin@patchguard.local / admin123
  - inspector user  inspector@patchguard.local / inspect123
  - viewer user     viewer@patchguard.local / viewer123
  - demo contractor "Acme Roads Pty Ltd" with one work record along Abercrombie St,
    Darlington (Sydney) under a 24-month guarantee — survey that street to demo the
    Action tab auto-matching.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select, text

from db import Base, SessionLocal, engine
from geo import linestring_wkt
from models_db import Contractor, Role, User, WorkRecord
from security import hash_password

# A stretch of Abercrombie Street, Darlington — handy demo street near Ultimo.
DEMO_PATH = [
    [-33.88894, 151.19737],
    [-33.89071, 151.19833],
    [-33.89234, 151.19925],
    [-33.89399, 151.20018],
]


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column migrations for existing DBs.
        await conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'worker'"))
        await conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS analyzed BOOLEAN DEFAULT TRUE"))
    print("tables created")

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.email == "admin@patchguard.local"))
        ).scalar_one_or_none()
        if existing:
            print("seed data already present — skipping")
            return

        admin = User(
            email="admin@patchguard.local",
            full_name="Admin",
            role=Role.admin,
            password_hash=hash_password("admin123"),
        )
        inspector = User(
            email="inspector@patchguard.local",
            full_name="Site Inspector",
            role=Role.inspector,
            password_hash=hash_password("inspect123"),
        )
        viewer = User(
            email="viewer@patchguard.local",
            full_name="Read Only",
            role=Role.viewer,
            password_hash=hash_password("viewer123"),
        )
        session.add_all([admin, inspector, viewer])
        await session.flush()

        acme = Contractor(
            name="Acme Roads Pty Ltd",
            abn="12 345 678 901",
            contact_email="ops@acmeroads.example",
            phone="+61 2 9000 0000",
        )
        session.add(acme)
        await session.flush()

        work_date = date.today() - timedelta(days=90)
        session.add(
            WorkRecord(
                contractor_id=acme.id,
                title="Abercrombie St resurfacing (Darlington)",
                work_date=work_date,
                cost=Decimal("184500.00"),
                hours_spent=Decimal("312.5"),
                guarantee_months=24,
                guarantee_expires=date(
                    work_date.year + 2, work_date.month, work_date.day
                ),
                path=linestring_wkt(DEMO_PATH),
                notes="Full-depth asphalt resurfacing, both lanes.",
                created_by=admin.id,
            )
        )
        await session.commit()
    print("seeded: 3 users, 1 contractor, 1 work record (Abercrombie St, 24mo guarantee)")
    print("login: admin@patchguard.local / admin123")


if __name__ == "__main__":
    asyncio.run(main())
