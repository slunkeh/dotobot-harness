"""Server history is the transcript (AppModel.mergeHistory).

Live-only rows (optimistic /stop, an open secret box) overlay in their
neighbour slot. Matching keys keep the live identity. Keep this file in
lockstep with `clients/mac/Sources/HarnessShared/AppModel.swift` `mergeHistory`.
"""


def _key(msg: dict) -> str:
    if msg.get("kind") == "card":
        return "card:" + str(msg.get("card_id") or "")
    if msg.get("kind") == "choice":
        return "choice:" + str(msg.get("choice_id") or "")
    if msg.get("kind") == "secretRequest" and msg.get("secret_name"):
        return "secret:" + str(msg["secret_name"])
    if msg.get("kind") in {"user", "assistant"} or msg.get("voice_call_id"):
        mid = msg.get("message_id") or msg.get("messageId")
        if mid:
            return "message:" + str(mid)
    return _legacy_key(msg)


def _legacy_key(msg: dict) -> str:
    stop = msg.get("kind") == "assistant" and msg.get("text") == "Stopped."
    origin = "" if stop and not msg.get("voice_call_id") else msg.get("origin") or ""
    return (
        f"{msg.get('author') or 'user'}\x1e{msg.get('text') or ''}"
        f"\x1eimg:{len(msg.get('images') or [])}\x1e{origin}"
    )


def merge_history(existing: list[dict], mapped: list[dict]) -> list[dict]:
    if not existing:
        return list(mapped)
    if not mapped:
        return list(existing)

    existing = [dict(row) for row in existing]
    mapped = [dict(row) for row in mapped]
    mapped_ids = {row.get("message_id") or row.get("messageId") for row in mapped} - {None, ""}
    paired = set()
    for hist in mapped:
        if hist.get("kind") not in {"user", "assistant"}:
            continue
        mid = hist.get("message_id") or hist.get("messageId")
        if mid and any(
            row.get("kind") == hist.get("kind")
            and (row.get("message_id") or row.get("messageId")) == mid
            for row in existing
        ):
            continue
        for index, live in enumerate(existing):
            live_mid = live.get("message_id") or live.get("messageId")
            if (
                index not in paired
                and live.get("kind") == hist.get("kind")
                and (not live_mid or not mid)
                and live_mid not in mapped_ids
                and _legacy_key(live) == _legacy_key(hist)
            ):
                paired.add(index)
                if mid:
                    live["message_id"] = mid
                elif live_mid:
                    hist["message_id"] = live_mid
                break

    # One live row per history row, in order: a run of identical bubbles
    # must not collapse onto one live identity (duplicate ids).
    live_by_key: dict[str, list[dict]] = {}
    for m in existing:
        live_by_key.setdefault(_key(m), []).append(m)
    mapped_keys = [_key(m) for m in mapped]
    mapped_set = set(mapped_keys)

    page: list[dict] = []
    for hist in mapped:
        key = _key(hist)
        queue = live_by_key.get(key)
        if queue:
            old = queue.pop(0)
            row = dict(old)
            if hist.get("kind") in {"user", "assistant"}:
                for field in ("text", "author", "images", "origin"):
                    if field in hist:
                        row[field] = hist[field]
                    else:
                        row.pop(field, None)
            if hist.get("completed"):
                row["completed"] = True
                if hist.get("selection"):
                    row["selection"] = hist["selection"]
            # Progress state lives in the card payload, not `completed`.
            if hist.get("card") is not None:
                row["card"] = hist["card"]
                if hist.get("card_type"):
                    row["card_type"] = hist["card_type"]
            mid = hist.get("message_id") or hist.get("messageId")
            if mid:
                row["message_id"] = mid
            page.append(row)
        else:
            page.append(dict(hist))

    existing_keys = [_key(m) for m in existing]
    prefix: list[dict] = []
    placed: set[str] = set()
    first_overlap = next((k for k in mapped_keys if k in existing_keys), None)
    if first_overlap is not None:
        for msg in existing:
            key = _key(msg)
            if key == first_overlap:
                break
            if key not in mapped_set:
                prefix.append(msg)
                placed.add(key)

    result = prefix + page

    def result_keys() -> list[str]:
        return [_key(m) for m in result]

    for i, ov in enumerate(existing):
        key = _key(ov)
        if key in mapped_set or key in placed:
            continue
        at = len(result)
        pred = None
        for j in range(i - 1, -1, -1):
            pk = _key(existing[j])
            if pk in result_keys():
                pred = pk
                break
        if pred is not None:
            at = result_keys().index(pred) + 1
        else:
            for j in range(i + 1, len(existing)):
                sk = _key(existing[j])
                keys = result_keys()
                if sk in keys:
                    at = keys.index(sk)
                    break
        result.insert(at, ov)
        placed.add(key)
    return result


def _subsequence(needle: list[dict], haystack: list[dict]) -> bool:
    """`needle` rows appear in order inside `haystack` (identity by id)."""
    ids = [m["id"] for m in haystack]
    pos = -1
    for row in needle:
        try:
            pos = ids.index(row["id"], pos + 1)
        except ValueError:
            return False
    return True


def _u(i, text):  # noqa: E741
    return {"id": f"u{i}", "kind": "user", "text": text, "author": None}


def _a(i, text):
    return {"id": f"a{i}", "kind": "assistant", "text": text, "author": "atlas"}


def test_empty_live_uses_history():
    mapped = [_u(1, "hi"), _a(1, "hello")]
    assert merge_history([], mapped) == mapped


def test_stop_stays_above_stopped():
    # Live already shows the send, then the ack. History logged the ack
    # against the previous turn and never saw /stop.
    live = [_u(1, "run the scan"), _u(2, "/stop"), _a(1, "Stopped.")]
    history = [_u(1, "run the scan"), _a(1, "Stopped.")]
    out = merge_history(live, history)
    assert [m["text"] for m in out] == ["run the scan", "/stop", "Stopped."]
    assert _subsequence(live, out)


def test_stop_without_ack_yet_appends_history_reply():
    live = [_u(1, "run the scan"), _u(2, "/stop")]
    history = [_u(1, "run the scan"), _a(1, "Stopped.")]
    out = merge_history(live, history)
    assert [m["text"] for m in out] == ["run the scan", "/stop", "Stopped."]
    assert _subsequence(live, out)


def test_secret_box_stays_where_it_was():
    secret = {"id": "s1", "kind": "secretRequest", "text": "need a key", "author": "atlas"}
    live = [_u(1, "hi"), secret, _a(1, "thanks")]
    history = [_u(1, "hi"), _a(1, "thanks")]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "s1", "a1"]


def test_history_skipped_secret_settles_live_box():
    """History-only catch-up after a timeout must close the live box."""
    secret = {
        "id": "s1",
        "kind": "secretRequest",
        "text": "need a key",
        "author": "atlas",
        "secret_name": "TOKEN",
    }
    hist_secret = {**secret, "id": "hist-s", "completed": True}
    live = [_u(1, "sign in"), secret]
    history = [_u(1, "sign in"), hist_secret]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "s1"]
    assert out[1]["completed"] is True


def test_history_skipped_return_card_settles_live_card():
    live_card = {
        "id": "live-r",
        "kind": "card",
        "text": "",
        "card_id": "ret-1",
        "card_type": "control_return",
        "author": "atlas",
    }
    hist_card = {**live_card, "id": "hist-r", "completed": True}
    live = [_u(1, "take over"), live_card]
    history = [_u(1, "take over"), hist_card]
    out = merge_history(live, history)
    assert out[1]["id"] == "live-r"
    assert out[1]["completed"] is True


def test_missed_card_slides_in_without_moving_live_rows():
    card = {"id": "c1", "kind": "card", "text": "", "card_id": "chart-1", "author": "atlas"}
    live = [_u(1, "plot it"), _a(1, "done")]
    history = [_u(1, "plot it"), card, _a(1, "done")]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "c1", "a1"]
    assert _subsequence(live, out)


def test_older_history_is_prefixed():
    live = [_u(5, "later"), _a(5, "ok")]
    history = [_u(1, "first"), _a(1, "yes"), _u(5, "later"), _a(5, "ok")]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "a1", "u5", "a5"]
    assert _subsequence(live, out)


def test_later_server_row_is_not_dropped():
    """A phone that only saw the send still picks up /stop and the next turn."""
    live = [_u(1, "do a dry run")]
    progress = {
        "id": "p1",
        "kind": "card",
        "text": "",
        "card_id": "prog-1",
        "author": "atlas",
    }
    history = [
        _u(1, "do a dry run"),
        progress,
        _u(2, "/stop"),
        _a(1, "Stopped."),
        _a(2, "Hour is up."),
    ]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "p1", "u2", "a1", "a2"]


def test_live_progress_keeps_identity_when_history_catches_up():
    progress = {
        "id": "live-p",
        "kind": "card",
        "text": "",
        "card_id": "prog-1",
        "author": "atlas",
        "card": {"title": "Dry run", "state": "running"},
    }
    hist_progress = {
        "id": "hist-p",
        "kind": "card",
        "text": "",
        "card_id": "prog-1",
        "author": "atlas",
        # Progress cards have no prompt resolution, so `completed` stays
        # false. The coalesced GET payload is how the client learns done.
        "card": {
            "title": "Dry run",
            "state": "done",
            "steps": [{"label": "Login", "status": "done"}],
        },
    }
    live = [_u(1, "go"), progress]
    history = [_u(1, "go"), hist_progress, _a(1, "done")]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "live-p", "a1"]
    assert out[1]["card"]["state"] == "done"
    assert out[1]["card"]["steps"] == [{"label": "Login", "status": "done"}]


def test_live_confirm_copies_selection_from_history():
    live = {
        "id": "live-c",
        "kind": "card",
        "text": "",
        "card_id": "conf-1",
        "author": "atlas",
        "card": {"question": "Delete?"},
    }
    hist = {
        "id": "hist-c",
        "kind": "card",
        "text": "",
        "card_id": "conf-1",
        "author": "atlas",
        "completed": True,
        "selection": "confirm",
        "card": {"question": "Delete?"},
    }
    out = merge_history([_u(1, "rm it"), live], [_u(1, "rm it"), hist])
    assert [m["id"] for m in out] == ["u1", "live-c"]
    assert out[1]["completed"] is True
    assert out[1]["selection"] == "confirm"


def test_identical_bubbles_keep_distinct_identities():
    """Seven identical dream reports must stay seven rows with seven ids.

    The old single-entry map reused one live row for every history row
    with the same author and text, so the Mac transcript ForEach carried
    duplicate ids (shared frames, shared hover state, a lazy stack that
    could not place the rows around them — the blank-pages bug).
    """
    text = "Dream turn done. Nothing to report."
    live = [
        {"id": f"live-{i}", "kind": "assistant", "author": "atlas", "text": text} for i in range(3)
    ]
    history = [
        {"id": f"hist-{i}", "kind": "assistant", "author": "atlas", "text": text} for i in range(7)
    ]
    out = merge_history(live, history)
    ids = [m["id"] for m in out]
    assert len(out) == 7
    assert len(set(ids)) == 7, ids
    # Live identities are consumed in order, then history rows keep their own.
    assert ids[:3] == ["live-0", "live-1", "live-2"]
    assert ids[3:] == ["hist-3", "hist-4", "hist-5", "hist-6"]


def test_more_live_copies_than_history_drops_the_extras():
    text = "Dream turn done. Nothing to report."
    live = [
        {"id": f"live-{i}", "kind": "assistant", "author": "atlas", "text": text} for i in range(3)
    ]
    history = [
        {"id": "hist-0", "kind": "assistant", "author": "atlas", "text": text},
    ]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["live-0"]


def test_live_assistant_without_id_matches_history_with_id():
    """Streamed replies omit messageId; GET /history often includes it."""
    live = [
        _u(1, "hi"),
        {"id": "live-a", "kind": "assistant", "text": "hello", "author": "atlas"},
    ]
    history = [
        _u(1, "hi"),
        {
            "id": "hist-a",
            "kind": "assistant",
            "text": "hello",
            "author": "atlas",
            "message_id": "srv-1",
        },
    ]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["u1", "live-a"]


def test_history_pairs_user_rows_by_message_id_not_text():
    """The second unsent 'Try again' must not absorb the first's identity."""
    live = [
        {"id": "l1", "kind": "user", "text": "Try again", "author": None, "message_id": "one"},
        {"id": "l2", "kind": "user", "text": "Try again", "author": None, "message_id": "two"},
    ]
    history = [
        {"id": "h1", "kind": "user", "text": "Try again", "author": None, "message_id": "two"},
    ]
    out = merge_history(live, history)
    assert [m["id"] for m in out] == ["l1", "l2"]
    assert out[0]["message_id"] == "one"
    assert out[1]["message_id"] == "two"


def test_same_assistant_id_reconciles_changed_text_and_origin():
    live = {**_a(1, "Posted."), "message_id": "msg-r1"}
    history = {**_a(2, "Posted and verified."), "message_id": "msg-r1", "origin": "routine"}
    rows = merge_history([live], [history])
    assert len(rows) == 1
    assert rows[0]["id"] == live["id"]
    assert rows[0]["text"] == history["text"]
    assert rows[0]["origin"] == "routine"


def test_identical_assistant_text_with_different_ids_does_not_merge():
    first = {**_a(1, "Done"), "message_id": "one"}
    second = {**_a(2, "Done"), "message_id": "two"}
    assert merge_history([first, second], [second]) == [first, second]


def test_legacy_fallback_reserves_exact_identity_matches():
    known = {**_a(1, "Done"), "message_id": "known"}
    legacy = _a(2, "Done")
    first = {**_a(3, "Done"), "message_id": "first"}
    rows = merge_history([known, legacy], [first, known])
    assert [row["id"] for row in rows] == [legacy["id"], known["id"]]
    assert [row["message_id"] for row in rows] == ["first", "known"]
