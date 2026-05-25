"""
Progressive Field Collector - Collects fields one at a time for better UX.
"""

import logging
from typing import Any, Optional

from booking.field_collector import FieldCollector

logger = logging.getLogger("adella_chatbot.progressive_collector")


class ProgressiveFieldCollector:
    """Collects booking fields one at a time for better user experience."""
    
    # Field collection order (priority)
    FIELD_ORDER = ['date', 'time', 'duration', 'incall_outcall', 'experience_type', 'outcall_address']
    
    def __init__(self, field_collector: Optional['FieldCollector'] = None):
        """Initialize progressive collector.

        Args:
            field_collector: Optional FieldCollector instance (uses default if None)
        """
        if field_collector is None:
            # Avoid circular import at module import time
            import config
            from booking.field_collector import FieldCollector
            field_collector = FieldCollector(config)

        self.field_collector = field_collector
    
    def get_next_field_to_ask(self, _current_fields: dict[str, Any], missing_fields: list[str]) -> str | None:
        """
        Get the next field to ask for based on priority order.
        
        Args:
            current_fields: Current booking fields
            missing_fields: List of missing field names
            
        Returns:
            Next field name to ask for, or None if all fields collected
        """
        if not missing_fields:
            return None
        
        # Filter to only fields in our priority order
        ordered_missing = [f for f in self.FIELD_ORDER if f in missing_fields]
        
        if ordered_missing:
            return ordered_missing[0]
        
        # If field not in order, return first missing
        return missing_fields[0] if missing_fields else None
    
    def get_progress_message(self, current_fields: dict[str, Any], missing_fields: list[str]) -> str:
        """
        Get progress message showing what's been collected.
        
        Args:
            current_fields: Current booking fields
            missing_fields: List of missing fields
            
        Returns:
            Progress message string
        """
        total_fields = len(self.FIELD_ORDER)
        collected_count = total_fields - len(missing_fields)
        
        if collected_count == 0:
            return "Let's start collecting your booking details."
        
        progress = f"Progress: {collected_count} of {total_fields} fields collected.\n\n"
        
        # Show what we have
        collected = []
        if current_fields.get('date'):
            collected.append("\u2705 Date")
        if current_fields.get('time'):
            collected.append("\u2705 Time")
        if current_fields.get('duration'):
            collected.append("\u2705 Duration")
        if current_fields.get('incall_outcall'):
            collected.append("\u2705 Location type")
        if current_fields.get('experience_type'):
            collected.append("\u2705 Experience type")
        if current_fields.get('outcall_address'):
            collected.append("\u2705 Address")
        
        if collected:
            progress += "Collected:\n" + "\n".join(collected) + "\n\n"
        
        # Show what's missing
        if missing_fields:
            next_field = self.get_next_field_to_ask(current_fields, missing_fields)
            if next_field:
                from templates.field_prompts import get_field_prompt
                progress += f"Next: {get_field_prompt(next_field)}"
        
        return progress
    
    def extract_and_prioritize(self, message: str, current_fields: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Extract fields and return prioritized next step.
        
        Args:
            message: User message
            current_fields: Current booking fields
            
        Returns:
            Dict with extracted fields and next step info
        """
        fields = current_fields or {}
        collector = self.field_collector
        if collector is None:
            return {"extracted_fields": {}, "all_fields": fields, "missing_fields": [], "next_field": None}

        # Extract fields using regular collector
        extracted = collector.extract_fields(message, fields or {}) or {}
        
        # Merge with current fields
        if fields:
            merged = {**fields, **extracted}
        else:
            merged = extracted
        
        # Get missing fields
        missing = collector.get_missing_fields(merged)
        
        # Get next field to ask
        next_field = self.get_next_field_to_ask(merged, missing)
        
        return {
            'extracted_fields': extracted,
            'merged_fields': merged,
            'missing_fields': missing,
            'next_field': next_field,
            'is_complete': len(missing) == 0
        }
