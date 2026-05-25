"""
Touring-inquiry message templates - touring inquiry and subscription flow client-facing SMS strings.
"""

# Generic fallback when the touring inquiry handler encounters an unexpected error
TOURING_INTEREST_FALLBACK = "Thanks for your interest! Check out my profile for touring schedule info."

# Confirmation sent when client successfully subscribes to touring notifications for a city
TOURING_SUBSCRIBED = "\u2705 Perfect! I'll text you as soon as I'm in {city}. See you soon! \U0001F60A"

# Fallback confirmation used when the subscribe handler hits an unexpected error
TOURING_SUBSCRIBED_FALLBACK = "Thanks! I've noted your interest \u2014 I'll let you know when I'm next in your area."

