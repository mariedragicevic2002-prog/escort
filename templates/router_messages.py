"""
Router message templates - fallback / no-handler-found client-facing SMS strings.
"""

# Sent when no handler is registered for the resolved (state, intent) key.
# The global (*,*) fallback handler should normally catch before this is reached.
NO_HANDLER_FOUND = (
    "Thanks for your message! If you have a specific question, "
    "reply with ENQUIRY followed by your question.\n\n"
    "Or if you'd like to make a booking, just let me know your preferred date, time, duration and experience type."
)
