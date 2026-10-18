"""Read adapter contract tests; no connected accounts or network required."""
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from integrations.composio import messages
from integrations.composio.models import IntegrationError, source_key


@asynccontextmanager
async def budget():
    yield


def result(data, log_id="log-fixture"):
    return SimpleNamespace(data=data, log_id=log_id)


def mime_part(text, mime="text/plain", **extra):
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {"mimeType": mime, "body": {"data": encoded}, **extra}


@pytest.fixture(autouse=True)
def no_live_sdk(monkeypatch):
    monkeypatch.setattr(messages.runtime, "read_budget", budget)
    monkeypatch.setattr(
        messages.connections,
        "require_connection",
        AsyncMock(return_value=SimpleNamespace(account_id="ca-test", settings={})),
    )
    monkeypatch.setattr(
        messages.runtime,
        "execute",
        AsyncMock(side_effect=AssertionError("Unexpected provider call")),
    )


def test_inline_plain_body_preferred_and_attachments_not_decoded():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            mime_part("<p>HTML alternative</p>", "text/html"),
            mime_part("Review in BYAC 110. Bring your notes."),
            {"mimeType": "text/plain", "filename": "private.txt", "body": {"data": "INVALID"}},
        ],
    }
    body, truncated = messages._mime_body(payload)
    assert body == "Review in BYAC 110. Bring your notes."
    assert truncated is False


def test_html_only_strips_scripts_and_remote_assets():
    body, truncated = messages._mime_body(
        mime_part('<script>steal()</script><p>Office hours <b>moved</b>.</p><img src="https://tracker.test">', "text/html")
    )
    assert "Office hours" in body and "moved" in body
    assert "steal" not in body and "tracker" not in body
    assert not truncated


def test_body_limit_is_explicit():
    body, truncated = messages._mime_body(mime_part("x" * 13000))
    assert len(body) == 12000
    assert truncated


def test_invalid_mime_encoding_is_typed_not_silently_empty():
    with pytest.raises(IntegrationError) as error:
        messages._mime_body({"mimeType": "text/plain", "body": {"data": "%%%"}})
    assert error.value.code == "SCHEMA_MISMATCH"


def test_slack_timestamps_preserve_microseconds_and_native_string():
    stamp = "1792339200.000001"
    parsed = messages._slack_date(stamp)
    assert parsed.microsecond == 1
    assert messages._epoch_string(parsed) == stamp
    with pytest.raises(IntegrationError):
        messages._slack_date(float(stamp))


def test_slack_mentions_and_link_labels_are_kept_as_plain_evidence():
    body, _ = messages._slack_text("<@U123> meet in <#C123|project>? <https://example.test|Agenda>")
    assert body == "@U123 meet in #project? Agenda"


def native_mail(message_id="m1", body="Bring notes to BYAC 110.", stamp="1792339200000", labels=None):
    return {
        "id": message_id,
        "threadId": "thread-1",
        "internalDate": stamp,
        "labelIds": labels if labels is not None else ["INBOX"],
        "payload": {
            **mime_part(body),
            "headers": [
                {"name": "Subject", "value": "Midterm review"},
                {"name": "From", "value": "TA <ta@example.edu>"},
                # Internal receipt time takes precedence over the untrusted header.
                {"name": "Date", "value": "Tue, 01 Jan 2030 00:00:00 +0000"},
            ],
        },
    }


def test_gmail_native_ids_headers_and_source_are_normalized():
    raw = gmail_mail(body="See https://attacker.test")
    raw.pop("display_url")
    message = messages._gmail_message(raw, "ca-alex")
    assert message.message_id == "m1"
    assert message.thread_id == "thread-1"
    assert message.account_id == "ca-alex"
    assert message.date.startswith("2026-10-18")
    assert message.subject == "Midterm review"
    assert message.source_url is None  # A body link never becomes provenance.


def test_slack_edits_use_original_identity_and_current_text():
    message = messages._slack_message(
        {"subtype": "message_changed", "ts": "1792339300.000099", "message": {"ts": "1792339200.000001", "text": "Meeting moved to 4 PM", "user": "U1"}},
        "ca-1", "T1", "C1",
    )
    assert message.message_id == "1792339200.000001"
    assert message.body == "Meeting moved to 4 PM"
    assert message.channel_id == "C1"


@pytest.mark.parametrize("parts", ["native-legacy-id", '["other"]', '["ca",42]', '{"account":"ca"}'])
def test_source_key_rejects_unqualified_or_malformed_ids(parts):
    with pytest.raises(IntegrationError) as error:
        messages._source_parts({"user_id": "alex", "source": "gmail", "source_id": parts}, "alex")
    assert error.value.code == "SOURCE_NOT_FOUND"


SINCE = "2026-10-18T00:00:00Z"
UNTIL = "2026-10-19T00:00:00Z"


def gmail_mail(message_id="m1", **kwargs):
    native = native_mail(message_id, **kwargs)
    return {
        "messageId": native["id"], "threadId": native["threadId"],
        "labelIds": native["labelIds"], "messageTimestamp": native["internalDate"],
        "payload": native["payload"], "sender": "TA <ta@example.edu>",
        "subject": "Midterm review", "display_url": "https://mail.google.com/mail/u/0/#all/abc",
    }


def slack_selected(monkeypatch, channels=None):
    monkeypatch.setattr(messages.connections, "require_connection", AsyncMock(return_value=SimpleNamespace(
        account_id="ca-test", settings={"workspace_id": "T1", "channel_ids": channels or ["C1", "C2"]},
    )))


def channel_listing():
    return result({"ok": True, "channels": [
        {"id": "C1", "name": "project", "context_team_id": "T1"},
        {"id": "C2", "name": "club", "context_team_id": "T1"},
    ]})


def search_listing(matches=None, pages=1):
    return result({"ok": True, "messages": {"matches": matches or [], "paging": {"pages": pages}}})


@pytest.mark.asyncio
async def test_gmail_pages_read_archived_labels_dedup_and_half_open_window(monkeypatch):
    rows = [gmail_mail("read"), gmail_mail("archived", labels=["IMPORTANT"]), gmail_mail("sent-only", labels=["SENT"]), gmail_mail("spam", labels=["SPAM"])]
    html = gmail_mail("html")
    html["payload"] = mime_part("<p>Dinner is <b>$15</b>.</p>", "text/html")
    async def execute(user_id, provider, operation, arguments, **kwargs):
        assert user_id == "alex" and provider == "gmail" and operation == "gmail_list"
        assert kwargs["expected_account_id"] == "ca-test"
        assert "is:unread" not in arguments["query"]
        assert "label_ids" not in arguments  # archived mail is included
        assert arguments["include_spam_trash"] is False
        if arguments.get("page_token") == "page-2":
            return result({"messages": [rows[0], html, gmail_mail("at-end", stamp="1792368000000")], "nextPageToken": ""}, "log-page-2")
        return result({"messages": rows, "nextPageToken": "page-2"}, "log-page-1")
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert {record.message_id for record in batch.records} == {"read", "archived", "html"}
    assert batch.complete
    assert batch.log_ids == ["log-page-1", "log-page-2"]
    assert next(record for record in batch.records if record.message_id == "html").body == "Dinner is $15."
    assert batch.records[0].sender == "TA <ta@example.edu>"


@pytest.mark.asyncio
async def test_gmail_null_payload_fetches_full_message_in_same_account(monkeypatch):
    metadata = gmail_mail()
    metadata["payload"] = None
    async def execute(user_id, provider, operation, arguments, **kwargs):
        if operation == "gmail_list":
            return result({"messages": [metadata], "nextPageToken": ""})
        assert operation == "gmail_get"
        assert arguments == {"message_id": "m1", "user_id": "me", "format": "full"}
        assert kwargs["expected_account_id"] == "ca-test"
        return result(gmail_mail())
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert batch.complete and len(batch.records) == 1


@pytest.mark.asyncio
async def test_gmail_cap_and_failed_page_are_not_empty_success(monkeypatch):
    monkeypatch.setattr(messages, "MAX_GMAIL_MESSAGES", 2)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(return_value=result({"messages": [gmail_mail("m1"), gmail_mail("m2")], "nextPageToken": "more"})))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert len(batch.records) == 2 and not batch.complete
    assert batch.issues[0].code == "PARTIAL_RESULT"
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=IntegrationError("PERMISSION_DENIED", "gmail")))
    failed = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert failed.records == [] and not failed.complete
    assert failed.issues[0].code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_gmail_empty_success_and_repeating_cursor(monkeypatch):
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(return_value=result({"messages": [], "nextPageToken": ""})))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert batch.complete and batch.records == []
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(return_value=result({"messages": [gmail_mail()], "nextPageToken": "same"})))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert len(batch.records) == 1 and not batch.complete
    assert messages.runtime.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gmail", "slack"])
@pytest.mark.parametrize("since,until", [(SINCE, SINCE), (UNTIL, SINCE), ("2026-10-18T00:00:00", UNTIL)])
async def test_invalid_window_rejected_before_connection_or_provider(provider, since, until):
    function = messages.fetch_gmail_messages if provider == "gmail" else messages.fetch_slack_messages
    with pytest.raises(IntegrationError) as error:
        await function("alex", since=since, until=until)
    assert error.value.code == "INVALID_WINDOW"
    messages.connections.require_connection.assert_not_awaited()
    messages.runtime.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_selection_required_before_provider_reads():
    with pytest.raises(IntegrationError) as error:
        await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert error.value.code == "CHANNEL_SELECTION_REQUIRED"
    messages.runtime.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_selected_channels_include_replies_and_distinct_timestamp_ids(monkeypatch):
    slack_selected(monkeypatch)
    root = {"ts": "1792339200.000001", "text": "Meet after class?", "user": "U1", "reply_count": 1}
    reply = {"ts": "1792339300.000002", "text": "4 PM in BYAC 110", "user": "U2", "thread_ts": root["ts"]}
    async def execute(user_id, provider, operation, arguments, **kwargs):
        assert kwargs["expected_account_id"] == "ca-test"
        if operation == "slack_identity":
            return result({"ok": True, "team_id": "T1"})
        if operation == "slack_channels":
            return channel_listing()
        if operation == "slack_search":
            return search_listing()
        assert arguments["channel"] in {"C1", "C2"}
        assert arguments["inclusive"] is True
        if operation == "slack_replies":
            assert arguments["ts"] == root["ts"]
            return result({"ok": True, "messages": [root, reply]})
        assert operation == "slack_history"
        return result({"ok": True, "messages": [root if arguments["channel"] == "C1" else {**root, "reply_count": 0, "text": "Club dinner $15"}]})
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert batch.complete and len(batch.records) == 3
    assert len({record.source_id for record in batch.records}) == 3
    assert next(record for record in batch.records if record.message_id == reply["ts"]).thread_id == root["ts"]


@pytest.mark.asyncio
async def test_slack_inaccessible_channel_preserves_other_channel(monkeypatch):
    slack_selected(monkeypatch)
    async def execute(user_id, provider, operation, arguments, **kwargs):
        if operation == "slack_identity":
            return result({"ok": True, "team_id": "T1"})
        if operation == "slack_channels":
            return channel_listing()
        if operation == "slack_search":
            return search_listing()
        if arguments["channel"] == "C1":
            raise IntegrationError("PERMISSION_DENIED", "slack", log_id="log-denied")
        return result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Club dinner", "user": "U1"}]})
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and len(batch.records) == 1
    assert batch.records[0].channel_id == "C2"
    assert batch.issues[0].code == "PERMISSION_DENIED"
    assert "log-denied" in batch.log_ids


@pytest.mark.asyncio
async def test_slack_workspace_mismatch_blocks_history(monkeypatch):
    slack_selected(monkeypatch)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(return_value=result({"ok": True, "team_id": "T-other"})))
    with pytest.raises(IntegrationError) as error:
        await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert error.value.code == "ACCOUNT_CHANGED"
    assert messages.runtime.execute.await_count == 1


@pytest.mark.asyncio
async def test_slack_channel_discovery_paginates_without_dms_or_joining(monkeypatch):
    async def execute(user_id, provider, operation, arguments, **kwargs):
        if operation == "slack_identity":
            return result({"ok": True, "team_id": "T1"})
        assert operation == "slack_channels"
        assert arguments["types"] == "public_channel,private_channel"
        if arguments.get("cursor"):
            return result({"ok": True, "channels": [{"id": "C2", "name": "club", "context_team_id": "T1"}]})
        return result({"ok": True, "channels": [{"id": "C1", "name": "project", "context_team_id": "T1"}, {"id": "D1", "name": "dm", "is_im": True}], "response_metadata": {"next_cursor": "page-2"}})
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.list_slack_channels("alex")
    assert batch.complete and {channel.id for channel in batch.records} == {"C1", "C2"}
    assert all(channel.workspace_id == "T1" for channel in batch.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [None, {"user_id": "other", "source": "gmail", "source_id": '["ca-test","m1"]'}, {"user_id": "alex", "source": "gmail", "source_id": '["other-account","m1"]'}])
async def test_source_ownership_and_account_checked_before_provider(monkeypatch, item):
    monkeypatch.setattr(messages.repo, "get_item", AsyncMock(return_value=item))
    with pytest.raises(IntegrationError) as error:
        await messages.fetch_source("alex", "owned-item")
    assert error.value.code == "SOURCE_NOT_FOUND"
    messages.runtime.execute.assert_not_awaited()
    messages.repo.get_item.assert_awaited_once_with("alex", "owned-item")


@pytest.mark.asyncio
async def test_owned_gmail_source_has_sender_body_real_link_and_truncation(monkeypatch):
    monkeypatch.setattr(messages.repo, "get_item", AsyncMock(return_value={"user_id": "alex", "source": "gmail", "source_id": source_key("ca-test", "m1")}))
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(return_value=result(gmail_mail(body="x" * 13000))))
    source = await messages.fetch_source("alex", "owned-item")
    assert source.truncated and len(source.body) == 12000
    assert source.sender == "TA <ta@example.edu>"
    assert source.link.startswith("https://mail.google.com/")
    assert messages.runtime.execute.call_args.args[2] == "gmail_get"


@pytest.mark.asyncio
async def test_slack_reply_source_search_is_scoped_and_exact(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    timestamp = "1792339300.000002"
    monkeypatch.setattr(messages.repo, "get_item", AsyncMock(return_value={"user_id": "alex", "source": "slack", "source_id": source_key("ca-test", "T1", "C1", timestamp)}))
    async def execute(user_id, provider, operation, arguments, **kwargs):
        if operation == "slack_identity":
            return result({"ok": True, "team_id": "T1"})
        if operation == "slack_history":
            assert arguments["oldest"] == timestamp == arguments["latest"]
            return result({"ok": True, "messages": []})
        if operation == "slack_channels":
            return result({"ok": True, "channels": [{"id": "C1", "name": "project"}]})
        assert operation == "slack_search"
        assert arguments["query"].startswith("in:#project ")
        assert arguments["auto_paginate"] is False
        return result({"ok": True, "messages": {"paging": {"pages": 1}, "matches": [
            {"ts": timestamp, "text": "Wrong channel evidence", "channel": {"id": "C2"}},
            {"ts": timestamp, "text": "Meet in BYAC 110", "user": "U1", "team": "T1", "channel": {"id": "C1"}, "thread_ts": "1792339200.000001", "permalink": "https://margin.slack.com/archives/C1/p1792339300000002"},
        ]}})
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    source = await messages.fetch_source("alex", "owned-item")
    assert source.body == "Meet in BYAC 110" and source.channel == "project"
    assert source.thread_id == "1792339200.000001"
    assert source.link.endswith("p1792339300000002")


@pytest.mark.asyncio
async def test_gmail_account_change_mid_pagination_discards_previous_account(monkeypatch):
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"messages": [gmail_mail()], "nextPageToken": "more"}),
        IntegrationError("ACCOUNT_CHANGED", "gmail"),
    ]))
    batch = await messages.fetch_gmail_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and batch.records == []
    assert batch.issues[0].code == "ACCOUNT_CHANGED"


@pytest.mark.asyncio
async def test_slack_account_change_discards_already_read_channels(monkeypatch):
    slack_selected(monkeypatch)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}),
        channel_listing(),
        result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Old account evidence"}]}),
        search_listing(),
        IntegrationError("ACCOUNT_CHANGED", "slack"),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and batch.records == []
    assert batch.issues[0].code == "ACCOUNT_CHANGED"


@pytest.mark.asyncio
async def test_slack_page_limit_and_missing_cursor_disclose_partial(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages, "MAX_PAGES", 2)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}),
        channel_listing(),
        result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Meeting"}], "has_more": True}),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and len(batch.records) == 1
    assert batch.issues[0].code == "PARTIAL_RESULT"


@pytest.mark.asyncio
async def test_slack_source_missing_search_scope_is_explicit_error(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages.repo, "get_item", AsyncMock(return_value={"user_id": "alex", "source": "slack", "source_id": source_key("ca-test", "T1", "C1", "1792339300.000002")}))
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}),
        result({"ok": True, "messages": []}),
        result({"ok": True, "team_id": "T1"}),
        result({"ok": True, "channels": [{"id": "C1", "name": "project"}]}),
        IntegrationError("PERMISSION_DENIED", "slack", log_id="log-search-scope"),
    ]))
    with pytest.raises(IntegrationError) as error:
        await messages.fetch_source("alex", "owned-item")
    assert error.value.code == "PERMISSION_DENIED"
    assert error.value.log_id == "log-search-scope"


@pytest.mark.asyncio
async def test_slack_recent_reply_to_old_parent_is_found_scoped_and_paginated(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    old_parent = "1792000000.000001"
    reply = {"ts": "1792339200.000001", "thread_ts": old_parent, "text": "Room changed to BYAC 110", "user": "U1", "team": "T1", "channel": {"id": "C1"}, "permalink": "https://margin.slack.com/archives/C1/p1792339200000001"}
    calls = []
    async def execute(user_id, provider, operation, arguments, **kwargs):
        calls.append(operation)
        assert kwargs["expected_account_id"] == "ca-test"
        if operation == "slack_identity":
            return result({"ok": True, "team_id": "T1"})
        if operation == "slack_channels":
            return channel_listing()
        if operation == "slack_history":
            return result({"ok": True, "messages": []})
        assert operation == "slack_search"
        assert arguments["query"] == "in:#project after:2026-10-17 before:2026-10-21"
        assert arguments["count"] == 100 and arguments["auto_paginate"] is False
        if arguments["page"] == 1:
            return search_listing([reply, {**reply, "ts": old_parent}, {**reply, "ts": "1792368000.000000"}], pages=2)
        return search_listing([
            reply,  # repeated pages do not duplicate a reply
            {**reply, "ts": "1792339300.000001", "channel": {"id": "C2"}},
            {**reply, "ts": "1792339400.000001", "team": "T-other"},
        ], pages=2)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=execute))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert batch.complete and len(batch.records) == 1
    assert batch.records[0].message_id == reply["ts"]
    assert batch.records[0].thread_id == old_parent
    assert batch.records[0].channel_name == "project"
    assert batch.records[0].source_id == source_key("ca-test", "T1", "C1", reply["ts"])
    assert batch.records[0].source_url == reply["permalink"]
    assert calls == ["slack_identity", "slack_channels", "slack_history", "slack_search", "slack_search"]


@pytest.mark.asyncio
async def test_slack_missing_search_scope_preserves_history_as_incomplete(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}), channel_listing(),
        result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Project meeting"}]}),
        IntegrationError("PERMISSION_DENIED", "slack", log_id="search-scope-missing"),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and len(batch.records) == 1
    assert batch.records[0].body == "Project meeting"
    assert batch.issues[0].code == "PERMISSION_DENIED"
    assert "search-scope-missing" in batch.log_ids


@pytest.mark.asyncio
async def test_slack_search_shares_total_page_budget_with_discovery_and_history(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages, "MAX_PAGES", 3)
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}), channel_listing(),
        result({"ok": True, "messages": []}),
        search_listing([{"ts": "1792339200.000001", "text": "Recent reply", "thread_ts": "1792000000.000001", "channel": {"id": "C1"}}], pages=2),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert len(batch.records) == 1 and not batch.complete
    assert batch.issues[0].code == "PARTIAL_RESULT"
    assert messages.runtime.execute.await_count == 4  # identity + 3 shared pages


@pytest.mark.asyncio
async def test_slack_search_shares_message_cap_and_does_not_undo_history_edits(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages, "MAX_SLACK_MESSAGES_PER_CHANNEL", 2)
    first = {"ts": "1792339200.000001", "text": "Meeting moved to 4 PM", "channel": {"id": "C1"}}
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}), channel_listing(),
        result({"ok": True, "messages": [first]}),
        search_listing([
            {**first, "text": "Meeting at 3 PM", "permalink": "https://margin.slack.com/archives/C1/p1792339200000001"},
            {**first, "ts": "1792339200.000002", "text": "Recent reply"},
            {**first, "ts": "1792339200.000003", "text": "Beyond cap"},
        ]),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert len(batch.records) == 2 and not batch.complete
    retained = next(record for record in batch.records if record.message_id == first["ts"])
    assert retained.body == "Meeting moved to 4 PM"
    assert retained.source_url.endswith("p1792339200000001")
    assert batch.issues[0].code == "PARTIAL_RESULT"


@pytest.mark.asyncio
async def test_slack_channel_discovery_failure_prevents_false_complete_search_coverage(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}),
        IntegrationError("PERMISSION_DENIED", "slack"),
        result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Readable history"}]}),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and len(batch.records) == 1
    assert [call.args[2] for call in messages.runtime.execute.await_args_list] == ["slack_identity", "slack_channels", "slack_history"]


@pytest.mark.asyncio
async def test_slack_account_change_during_search_discards_history(monkeypatch):
    slack_selected(monkeypatch, ["C1"])
    monkeypatch.setattr(messages.runtime, "execute", AsyncMock(side_effect=[
        result({"ok": True, "team_id": "T1"}), channel_listing(),
        result({"ok": True, "messages": [{"ts": "1792339200.000001", "text": "Previous account evidence"}]}),
        IntegrationError("ACCOUNT_CHANGED", "slack"),
    ]))
    batch = await messages.fetch_slack_messages("alex", since=SINCE, until=UNTIL)
    assert not batch.complete and batch.records == []
    assert batch.issues[0].code == "ACCOUNT_CHANGED"
