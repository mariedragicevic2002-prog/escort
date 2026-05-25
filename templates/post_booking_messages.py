"""
Post-booking message templates - POST_BOOKING state client-facing SMS strings.
"""

# All fields collected; moving straight to availability check
DETAILS_COMPLETE_CHECKING = "Great! I have your details. Let me check my availability!"

# Some fields still missing after extraction; still collecting
DETAILS_STILL_NEED = "Great! I still need: {missing_prompt}"

# All fields collected in the post-booking re-book flow; moving to availability check
REBOOK_DETAILS_COMPLETE_CHECKING = "I have your details. Let me check my availability!"

# Some fields still missing in the post-booking re-book flow
REBOOK_STILL_NEED = "I still need: {missing_prompt} I'll then check my availability!"

# Client expressed gratitude / positive sentiment after a booking
THANK_YOU_RESPONSE = "Thank you! That means so much! \U0001F495\n\nI'd love to see you again. Would you like to book?"

# Client says goodbye after a booking
GOODBYE_RESPONSE = "Take care! Hope to see you again soon! \U0001F495"
