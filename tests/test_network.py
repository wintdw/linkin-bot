from linkedin_bot.network import (
    _card_identity,
    _classify_button_state,
    _label_name,
    _next_unseen,
)


class _Button:
    """Fake Connect button carrying componentkey + aria-label like the live DOM."""

    def __init__(self, label: str | None, member: int | None = None):
        self.label = label
        self.componentkey = (
            f"ConnectButtonstate:invitation:urn:li:member:{member}_connect"
            if member is not None
            else None
        )

    def get_attribute(self, name, timeout=None):
        if name == "componentkey":
            return self.componentkey
        if name == "aria-label":
            if self.label is None:
                raise RuntimeError("detached")
            return self.label
        return None


def test_picks_first_unseen_and_records_identity():
    seen: set[str] = set()
    a, b = _Button("Invite A to connect", member=1), _Button("Invite B to connect", member=2)
    assert _next_unseen([a, b], seen, 1000) is a
    assert seen == {"urn:li:member:1"}


def test_skips_already_sent_cards():
    seen = {"urn:li:member:1"}
    a, b = _Button("Invite A to connect", member=1), _Button("Invite B to connect", member=2)
    assert _next_unseen([a, b], seen, 1000) is b


def test_identity_dedupes_even_when_label_is_missing():
    # The member URN is stable, so a card is never re-clicked just because its
    # aria-label could not be read (the old aria-label-only key would re-offer it).
    seen: set[str] = set()
    first = _Button(None, member=7)
    assert _next_unseen([first], seen, 1000) is first
    assert seen == {"urn:li:member:7"}
    assert _next_unseen([_Button("Invite X to connect", member=7)], seen, 1000) is None


def test_unidentifiable_button_is_skipped():
    # No componentkey and no label -> skip (those clicks only ever no-op'd),
    # instead of returning the same button on every pass.
    seen: set[str] = set()
    stale = _Button(None)
    assert _next_unseen([stale], seen, 1000) is None
    assert seen == set()


def test_returns_none_when_every_card_is_seen():
    seen = {"urn:li:member:1"}
    assert _next_unseen([_Button("Invite A to connect", member=1)], seen, 1000) is None


def test_card_identity_prefers_member_urn_then_label():
    assert _card_identity(_Button("Invite A to connect", member=9), 1000) == "urn:li:member:9"
    assert _card_identity(_Button("Invite A to connect"), 1000) == "Invite A to connect"
    assert _card_identity(_Button(None), 1000) is None


def test_label_name_extracts_person():
    assert _label_name("Invite Nu To to connect") == "Nu To"
    assert _label_name("invite Lauren Luu to connect") == "Lauren Luu"
    assert _label_name(None) == "?"
    assert _label_name("Connect") == "Connect"


def test_classify_detached_node_is_a_send():
    # Playwright returns stale text for a detached node; isConnected is the signal.
    state = {"connected": False, "text": "Connect", "label": "Invite A to connect"}
    assert _classify_button_state(state, "Invite A to connect") == "SENT_PENDING"


def test_classify_pending_text_is_a_send():
    state = {"connected": True, "text": "Pending", "label": "Invite A to connect"}
    assert _classify_button_state(state, "Invite A to connect") == "SENT_PENDING"


def test_classify_changed_label_is_a_send():
    state = {"connected": True, "text": "Connect", "label": "Invite B to connect"}
    assert _classify_button_state(state, "Invite A to connect") == "SENT_PENDING"


def test_classify_unchanged_button_is_inconclusive():
    state = {"connected": True, "text": "Connect", "label": "Invite A to connect"}
    assert _classify_button_state(state, "Invite A to connect") is None


def test_classify_unreadable_handle_counts_as_sent():
    # A handle that cannot be read (disposed/detached) is treated conservatively.
    assert _classify_button_state(None, "Invite A to connect") == "SENT_PENDING"
