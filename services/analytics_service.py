"""
Analytics Service
Provides booking funnel analytics, revenue analytics, and client analytics.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from services.database_service import DatabaseService

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Service for analytics and reporting."""
    
    def __init__(self, db_service: DatabaseService):
        self.db = db_service
    
    def get_booking_funnel_analytics(self, days: int = 30) -> dict[str, Any]:
        """Get booking funnel conversion rates.
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Dict with funnel metrics
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            event_rows = []
            try:
                event_rows = self.db.execute_query(
                    """
                    SELECT to_state, COUNT(*) AS count, COUNT(DISTINCT phone_number) AS unique_clients
                    FROM conversation_events
                    WHERE event_type = 'state_transition'
                      AND created_at >= %s
                      AND to_state IS NOT NULL
                    GROUP BY to_state
                    """,
                    (cutoff_date,),
                    fetch=True,
                ) or []
            except Exception as event_err:
                logger.warning("Event-sourced funnel query failed; using snapshot fallback: %s", event_err)
                event_rows = []

            has_event_states = any((row.get("to_state") or "").strip() for row in event_rows)
            if event_rows and has_event_states:
                event_counts = {row.get('to_state'): int(row.get('count') or 0) for row in event_rows}
                total_new = event_counts.get('NEW', 0)
                total_collecting = event_counts.get('COLLECTING', 0)
                total_checking = event_counts.get('CHECKING_AVAILABILITY', 0)
                total_confirmed = event_counts.get('CONFIRMED', 0)
                total_deposit_required = event_counts.get('DEPOSIT_REQUIRED', 0)

                conversion_rates = {}
                if total_new > 0:
                    conversion_rates['new_to_collecting'] = round((total_collecting / total_new) * 100, 2)
                if total_collecting > 0:
                    conversion_rates['collecting_to_checking'] = round((total_checking / total_collecting) * 100, 2)
                if total_checking > 0:
                    conversion_rates['checking_to_confirmed'] = round((total_confirmed / total_checking) * 100, 2)

                drop_offs = {
                    'at_collecting': max(0, total_new - total_collecting),
                    'at_checking': max(0, total_collecting - total_checking),
                    'at_deposit': max(0, total_checking - total_confirmed - total_deposit_required),
                    'at_confirmed': max(0, total_deposit_required - total_confirmed),
                }
                return {
                    'period_days': days,
                    'funnel': {
                        'NEW': total_new,
                        'COLLECTING': total_collecting,
                        'CHECKING_AVAILABILITY': total_checking,
                        'DEPOSIT_REQUIRED': total_deposit_required,
                        'CONFIRMED': total_confirmed,
                    },
                    'conversion_rates': conversion_rates,
                    'drop_offs': drop_offs,
                    'total_clients': sum(int(row.get('unique_clients') or 0) for row in event_rows),
                    'snapshot_based': False,
                    'notes': 'Funnel is derived from append-only conversation_events state_transition telemetry.',
                }

            # Snapshot fallback
            result = self.db.execute_query("""
                SELECT
                    current_state,
                    COUNT(*) as count,
                    COUNT(DISTINCT phone_number) as unique_clients
                FROM conversation_states
                WHERE updated_at >= %s
                GROUP BY current_state
            """, (cutoff_date,), fetch=True) or []

            state_counts = {row['current_state']: row['count'] for row in result}
            total_new = state_counts.get('NEW', 0)
            total_collecting = state_counts.get('COLLECTING', 0)
            total_checking = state_counts.get('CHECKING_AVAILABILITY', 0)
            total_confirmed = state_counts.get('CONFIRMED', 0)
            total_deposit_required = state_counts.get('DEPOSIT_REQUIRED', 0)

            conversion_rates = {}
            if total_new > 0:
                conversion_rates['new_to_collecting'] = round((total_collecting / total_new) * 100, 2)
            if total_collecting > 0:
                conversion_rates['collecting_to_checking'] = round((total_checking / total_collecting) * 100, 2)
            if total_checking > 0:
                conversion_rates['checking_to_confirmed'] = round((total_confirmed / total_checking) * 100, 2)

            drop_offs = {
                'at_collecting': max(0, total_new - total_collecting),
                'at_checking': max(0, total_collecting - total_checking),
                'at_deposit': max(0, total_checking - total_confirmed - total_deposit_required),
                'at_confirmed': max(0, total_deposit_required - total_confirmed),
            }

            return {
                'period_days': days,
                'funnel': {
                    'NEW': total_new,
                    'COLLECTING': total_collecting,
                    'CHECKING_AVAILABILITY': total_checking,
                    'DEPOSIT_REQUIRED': total_deposit_required,
                    'CONFIRMED': total_confirmed
                },
                'conversion_rates': conversion_rates,
                'drop_offs': drop_offs,
                'total_clients': sum(row['unique_clients'] for row in result),
                'snapshot_based': True,
                'notes': 'Funnel is based on current-state snapshot in conversation_states (events unavailable).'
            }
        except Exception as e:
            logger.error(f"Error calculating funnel analytics: {e}")
            return {
                'period_days': days,
                'funnel': {},
                'conversion_rates': {},
                'drop_offs': {},
                'total_clients': 0,
                'error': str(e)
            }
    
    def get_revenue_analytics(self, days: int = 30) -> dict[str, Any]:
        """Get revenue analytics.
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Dict with revenue metrics
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            # Get confirmed bookings with duration
            result = self.db.execute_query("""
                SELECT 
                    duration,
                    confirmed_at,
                    incall_outcall,
                    experience_type
                FROM conversation_states
                WHERE confirmed_at >= %s
                AND confirmed_at IS NOT NULL
                AND duration IS NOT NULL
            """, (cutoff_date,), fetch=True)
            
            from templates.confirmations import calculate_price
            
            total_revenue = 0
            booking_count = 0
            bookings_by_type = {}
            bookings_by_duration = {}
            bookings_by_time = {}
            rows = result or []
            
            for row in rows:
                duration = row['duration'] or 60
                price = calculate_price(duration)
                total_revenue += price
                booking_count += 1
                
                # By type
                booking_type = row['incall_outcall'] or 'unknown'
                bookings_by_type[booking_type] = bookings_by_type.get(booking_type, 0) + 1
                
                # By duration
                duration_key = f"{duration}min"
                bookings_by_duration[duration_key] = bookings_by_duration.get(duration_key, 0) + 1
                
                # By time (hour)
                if row['confirmed_at']:
                    hour = row['confirmed_at'].hour
                    bookings_by_time[hour] = bookings_by_time.get(hour, 0) + 1
            
            avg_booking_value = total_revenue / booking_count if booking_count > 0 else 0
            
            return {
                'period_days': days,
                'total_revenue': total_revenue,
                'booking_count': booking_count,
                'average_booking_value': round(avg_booking_value, 2),
                'bookings_by_type': bookings_by_type,
                'bookings_by_duration': bookings_by_duration,
                'peak_hours': dict(sorted(bookings_by_time.items(), key=lambda x: x[1], reverse=True)[:5])
            }
        except Exception as e:
            logger.error(f"Error calculating revenue analytics: {e}")
            return {
                'period_days': days,
                'total_revenue': 0,
                'booking_count': 0,
                'average_booking_value': 0,
                'error': str(e)
            }
    
    def get_client_analytics(self, days: int = 30) -> dict[str, Any]:
        """Get client analytics.
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Dict with client metrics
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            # Get client booking counts
            result = self.db.execute_query("""
                SELECT 
                    phone_number,
                    COUNT(*) as booking_count,
                    MAX(confirmed_at) as last_booking_date,
                    MIN(created_at) as first_contact_date
                FROM conversation_states
                WHERE confirmed_at >= %s
                AND confirmed_at IS NOT NULL
                GROUP BY phone_number
            """, (cutoff_date,), fetch=True)
            
            new_clients = 0
            returning_clients = 0
            vip_clients = 0
            frequent_clients = 0
            rows = result or []
            
            for row in rows:
                booking_count = row['booking_count']

                if booking_count == 1:
                    new_clients += 1
                else:
                    returning_clients += 1
                
                if booking_count >= 10:
                    vip_clients += 1
                elif booking_count >= 5:
                    frequent_clients += 1
            
            total_clients = len(rows)
            
            return {
                'period_days': days,
                'total_clients': total_clients,
                'new_clients': new_clients,
                'returning_clients': returning_clients,
                'vip_clients': vip_clients,
                'frequent_clients': frequent_clients,
                'repeat_booking_rate': round((returning_clients / total_clients * 100) if total_clients > 0 else 0, 2)
            }
        except Exception as e:
            logger.error(f"Error calculating client analytics: {e}")
            return {
                'period_days': days,
                'total_clients': 0,
                'new_clients': 0,
                'returning_clients': 0,
                'error': str(e)
            }
    
    def get_operational_metrics(self, days: int = 7) -> dict[str, Any]:
        """Get operational metrics.
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Dict with operational metrics
        """
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            
            # Deposit success rate
            deposit_result = self.db.execute_query("""
                SELECT 
                    COUNT(*) FILTER (WHERE deposit_paid = TRUE) as paid_count,
                    COUNT(*) FILTER (WHERE deposit_required = TRUE) as required_count
                FROM conversation_states
                WHERE deposit_required = TRUE
                AND updated_at >= %s
            """, (cutoff_date,), fetch=True)
            
            deposit_data = deposit_result[0] if deposit_result else {}
            deposit_success_rate = 0
            if deposit_data.get('required_count', 0) > 0:
                deposit_success_rate = round(
                    (deposit_data.get('paid_count', 0) / deposit_data.get('required_count', 1)) * 100, 2
                )
            
            # Calendar conflicts
            conflict_result = self.db.execute_query("""
                SELECT COUNT(*) as conflict_count
                FROM booking_analytics
                WHERE event_type = 'calendar_conflict'
                AND created_at >= %s
            """, (cutoff_date,), fetch=True)
            
            conflict_count = conflict_result[0]['conflict_count'] if conflict_result else 0
            
            return {
                'period_days': days,
                'deposit_success_rate': deposit_success_rate,
                'deposit_required_count': deposit_data.get('required_count', 0),
                'deposit_paid_count': deposit_data.get('paid_count', 0),
                'calendar_conflicts': conflict_count
            }
        except Exception as e:
            logger.error(f"Error calculating operational metrics: {e}")
            return {
                'period_days': days,
                'error': str(e)
            }
