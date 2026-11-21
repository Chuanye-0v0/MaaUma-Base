"""Match localized event options in existing OCR, without another capture/read."""
from core.event_knowledge_lookup import normalize_text, _title_match


def option_key(text):
    # OCR commonly varies terminal ellipsis/punctuation; keep all semantic text.
    return "".join(normalize_text(text).split()).rstrip("。.!！?？…·")


def owner_matches(event, trainee_id=None):
    owner = event.get("owner", {})
    return (trainee_id is None or owner.get("owner_id_namespace") != "trainee_id"
            or str(owner.get("owner_id")) == str(trainee_id))


def visible_event_options(lines, event, *, title_index=None):
    """Return complete ordered observed options, ignoring intervening background.

    Require unique full option text before the options-effect control when it is
    present. Missing, duplicated or reversed options do not establish identity.
    """
    titles = ([title_index] if title_index is not None else
              [i for i, line in enumerate(lines) if _title_match(event, normalize_text(line))])
    if len(titles) != 1:
        return None
    start = titles[0] + 1
    end = next((i for i in range(start, len(lines))
                if lines[i] in {"选项效果", "選項效果"}), len(lines))
    options = []
    previous = start - 1
    for option in sorted(event["options"], key=lambda item: item["index"]):
        keys = {option_key(text) for text in option["texts"].values()} - {""}
        positions = [i for i in range(start, end) if option_key(lines[i]) in keys]
        if len(positions) != 1 or positions[0] <= previous:
            return None
        previous = positions[0]
        options.append(lines[previous])
    return options
