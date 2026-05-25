#!/usr/bin/env python3
"""
Standalone script to backfill financial fields for existing bookings.
Run this from the PythonAnywhere console or via a web console.
"""

import os
import sys

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
from services.database_service import get_shared_db

def backfill_financial_fields():
    """Backfill financial fields for existing bookings."""
    try:
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            print("\n✗ Database unavailable")
            return {}

        results = {}

        # Update bookings where price_total is NULL but deposit_amount exists
        print("Updating bookings where price_total is NULL but deposit_amount exists...")
        db.execute_query("""
            UPDATE bookings
            SET price_total = deposit_amount * 2
            WHERE price_total IS NULL
              AND deposit_amount IS NOT NULL
              AND deposit_amount > 0
        """)
        db.conn.commit()
        results["price_total_from_deposit"] = db.cursor.rowcount
        print(f"  Updated {db.cursor.rowcount} rows")

        # Update bookings where remaining_amount is NULL but price_total and deposit_amount exist
        print("Updating bookings where remaining_amount is NULL...")
        db.execute_query("""
            UPDATE bookings
            SET remaining_amount = price_total - deposit_amount
            WHERE remaining_amount IS NULL
              AND price_total IS NOT NULL
              AND deposit_amount IS NOT NULL
        """)
        db.conn.commit()
        results["remaining_amount"] = db.cursor.rowcount
        print(f"  Updated {db.cursor.rowcount} rows")

        # Set deposit_reference to empty string if NULL
        print("Setting deposit_reference to empty string if NULL...")
        db.execute_query("""
            UPDATE bookings
            SET deposit_reference = ''
            WHERE deposit_reference IS NULL
        """)
        db.conn.commit()
        results["deposit_reference"] = db.cursor.rowcount
        print(f"  Updated {db.cursor.rowcount} rows")

        # For bookings with no deposit (reserved/peacock), set a default price_total
        print("Setting default price_total for bookings with no deposit...")
        db.execute_query("""
            UPDATE bookings
            SET price_total = 600,
                remaining_amount = 600
            WHERE price_total IS NULL
              AND (deposit_status = 'not_required' OR deposit_amount IS NULL OR deposit_amount = 0)
              AND status IN ('reserved', 'confirmed', 'reschedule-confirmed')
        """)
        db.conn.commit()
        results["default_price_no_deposit"] = db.cursor.rowcount
        print(f"  Updated {db.cursor.rowcount} rows")

        print("\n✓ Backfill completed successfully!")
        print(f"Results: {results}")
        return results

    except Exception as e:
        print(f"\n✗ Error during backfill: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    backfill_financial_fields()
