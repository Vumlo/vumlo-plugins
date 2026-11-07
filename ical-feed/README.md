# ical-feed

Serves a private iCalendar endpoint. It currently returns a valid empty feed
and declares a secret `feed_token` setting.

## Needs from core

The core has no deadline-list or assignment metadata plugin API and no deadline
event. Events are only submission, grade publication, and settings changes, so
deadline VEVENTs cannot be populated without inventing a contract.
