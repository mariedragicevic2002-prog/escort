#!/usr/bin/env python3
"""
Backfill financial fields for existing bookings.
Run this script to populate price_total, remaining_amount, and deposit_reference
for bookings that don't have these values set.
"""

import os
import sys
import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def get_db_connection():
    """Get database connection from environment variables."""
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        database=os.getenv('DB_NAME', 'adellachatbot'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
        port=os.getenv('DB_PORT', '5432')
    )

def backfill_financial_fields():
    """Backfill financial fields for existing bookings."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Update bookings where price_total is NULL but deposit_amount exists
            print("Updating bookings where price_total is NULL but deposit_amount exists...")
            cur.execute("""
                UPDATE bookings
                SET price_total = deposit_amount * 2
                WHERE price_total IS NULL
                  AND deposit_amount IS NOT NULL
                  AND deposit_amount > 0
            """)
            print(f"  Updated {cur.rowcount} rows")

            # Update bookings where remaining_amount is NULL but price_total and deposit_amount exist
            print("Updating bookings where remaining_amount is NULL...")
            cur.execute("""
                UPDATE bookings
                SET remaining_amount = price_total - deposit_amount
                WHERE remaining_amount IS NULL
                  AND price_total IS NOT NULL
                  AND deposit_amount IS NOT NULL
            """)
            print(f"  Updated {cur.rowcount} rows")

            # Set deposit_reference to empty string if NULL
            print("Setting deposit_reference to empty string if NULL...")
            cur.execute("""
                UPDATE bookings
                SET deposit_reference = ''
                WHERE deposit_reference IS NULL
            """)
            print(f"  Updated {cur.rowcount} rows")

            # For bookings with no deposit (reserved/peacock), set a default price_total
            print("Setting default price_total for bookings with no deposit...")
            cur.execute("""
                UPDATE bookings
                SET price_total = 600,
                    remaining_amount = 600
                WHERE price_total IS NULL
                  AND (deposit_status = 'not_required' OR deposit_amount IS NULL OR deposit_amount = 0)
                  AND status IN ('reserved', 'confirmed', 'reschedule-confirmed')
            """)
            print(f"  Updated {cur.rowcount} rows")

            conn.commit()
            print("\n✓ Backfill completed successfully!")

    except Exception as e:
        conn.rollback()
        print(f"\n✗ Error during backfill: {e}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    backfill_financial_fields()
