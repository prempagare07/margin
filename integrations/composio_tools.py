"""Margin's async Composio facade. Call only with an authenticated sender ID.

Raw source text is untrusted evidence for the Inbox extractor. Writes must pass
through execute_approved_action, which owns the durable execution claim.
"""

from .composio.actions import execute_approved_action
from .composio.cache import cache_items, cached_gather_result, filter_items
from .composio.calendar import auto_select_calendars, compute_free_slots, fetch_calendar_events, list_calendars
from .composio.connections import (
    connect_link, connection_status, is_connected, save_connection_settings,
    select_connected_account,
)
from .composio.messages import fetch_gmail_messages, fetch_slack_messages, fetch_source, list_slack_channels
from .composio.models import (
    CalendarOption, CalendarRecord, ChannelOption, ConnectionSettings,
    ConnectionState, IntegrationError, Provider, ReadBatch, SourceDetail,
    SourceMessage, item_id, source_key,
)
from .composio.runtime import observe_read as _observe_read

fetch_gmail_messages = _observe_read("gmail", "messages", fetch_gmail_messages)
fetch_slack_messages = _observe_read("slack", "messages", fetch_slack_messages)
fetch_calendar_events = _observe_read("gcal", "events", fetch_calendar_events)
list_calendars = _observe_read("gcal", "calendars", list_calendars)
list_slack_channels = _observe_read("slack", "channels", list_slack_channels)
fetch_source = _observe_read("composio", "source", fetch_source)

__all__ = [
    "connect_link", "connection_status", "is_connected", "select_connected_account",
    "list_calendars", "auto_select_calendars", "list_slack_channels", "save_connection_settings",
    "fetch_gmail_messages", "fetch_slack_messages", "fetch_calendar_events",
    "compute_free_slots", "fetch_source", "execute_approved_action",
    "cache_items", "cached_gather_result", "filter_items", "item_id", "source_key",
    "Provider", "ConnectionState", "ConnectionSettings", "CalendarOption",
    "ChannelOption", "CalendarRecord", "IntegrationError", "ReadBatch",
    "SourceMessage", "SourceDetail",
]
