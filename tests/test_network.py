from linkedin_bot.network import _label_name, _next_unseen


class _Button:
    def __init__(self, label: str | None):
        self.label = label

    def get_attribute(self, name, timeout=None):
        if self.label is None:
            raise RuntimeError("detached")
        return self.label


def test_picks_first_unseen_and_records_it():
    seen: set[str] = set()
    a, b = _Button("Invite A to connect"), _Button("Invite B to connect")
    assert _next_unseen([a, b], seen, 1000) is a
    assert seen == {"Invite A to connect"}


def test_skips_already_sent_cards():
    seen = {"Invite A to connect"}
    a, b = _Button("Invite A to connect"), _Button("Invite B to connect")
    assert _next_unseen([a, b], seen, 1000) is b


def test_returns_none_when_every_card_is_seen():
    seen = {"Invite A to connect"}
    # a lingering sent card stays at the head on the next re-query
    assert _next_unseen([_Button("Invite A to connect")], seen, 1000) is None


def test_unidentifiable_button_is_still_offered():
    seen: set[str] = set()
    stale = _Button(None)
    assert _next_unseen([stale], seen, 1000) is stale
    assert seen == set()


def test_label_name_extracts_person():
    assert _label_name("Invite Nu To to connect") == "Nu To"
    assert _label_name("invite Lauren Luu to connect") == "Lauren Luu"
    assert _label_name(None) == "?"
    assert _label_name("Connect") == "Connect"
