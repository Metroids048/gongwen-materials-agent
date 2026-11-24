"""Scope of an explicitly dated remote-work notice."""

import re


REMOTE_WORK = re.compile(r"(?:居家|远程|在家)办公")
WHOLE_EVENT = re.compile(r"(?:会议|会期|开会|(?:APEC|峰会))\s*(?:举办|召开|举行)?期间|会期内|整个会期", re.I)


def claims_whole_event(sentence):
    """Distinguish an introductory purpose from the period of the work instruction."""
    parts = re.split(r"[，,]", sentence, maxsplit=1)
    if (len(parts) == 2 and re.match(r"^\s*(?:为(?:了)?|鉴于|考虑到)", parts[0])
            and not REMOTE_WORK.search(parts[0])):
        remainder = parts[1].strip()
        announcement = re.fullmatch(r"(?:现)?(?:就|将)[^，,；;。]{0,40}(?:通知|说明)如下[：:]?", remainder)
        separate_date = re.search(r"(?:会前|会后|开始前|开幕前|结束后|闭幕后)一[天日]", remainder)
        if announcement or separate_date:
            sentence = remainder
    return bool(WHOLE_EVENT.search(sentence) and REMOTE_WORK.search(sentence))


def is_salutation(text):
    """A heading such as a comma-separated list of recipients can open a notice."""
    line = text.strip()
    return bool(re.fullmatch(r"[^。！？；：:\n]{1,100}[：:]", line)
                and not line[:-1].endswith(("如下", "说明", "要求", "安排")))
