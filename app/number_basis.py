"""Compare quantities with their units and derive only explicit per-group totals."""

from decimal import Decimal
import re
import unicodedata

CN_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
CN = "零〇一二两三四五六七八九十百千万"
VALUE = rf"(?:\d+(?:\.\d+)?|[{CN}]+)"
MEASURE = (
    r"(?:个)?工作日|分钟|小时|公斤|千克|克|千米|公里|米(?!线)|万元|元|名(?![单录称额])|人(?![民员数])|"
    r"套|双|箱|支(?=队伍|志愿队|队)|件(?!套)|天(?!然)|条(?!例)|组(?!织)|场(?!地|所)|份|页|辆|次(?!日|数)|百分比"
)
QUANTITY = re.compile(rf"(?<![\d.{CN}])(?P<count>{VALUE})[ \t]*(?P<unit>{MEASURE})")
CALENDAR_PART = re.compile(rf"([{CN}]+)(?=年|月|[日号]|楼|层)")
LIST_PREFIX = re.compile(r"(?m)^\s*(?:[（(]\s*[0-9一二三四五六七八九十]+\s*[)）]|[0-9一二三四五六七八九十]+[、．]|\d+\.(?!\d))\s*(?=[\u4e00-\u9fff])")
DOC_NO = re.compile(r"[\u4e00-\u9fff]{1,16}[〔\[［](\d{4})[〕\]］]\s*(\d+)\s*号")
BATCH = re.compile(rf"(?P<n>{VALUE})\s*(?:个|支)?(?:志愿)?(?P<class>组|班|站|批|场|队|箱)"
                   rf"(?:伍|人员|(?<=箱)(?:(?![不未非没待])[\u4e00-\u9fff]){{1,8}}(?=[，,：:]))?"
                   rf"[，,、：:\s]*(?:各|每(?:个)?(?P=class))\s*"
                   rf"(?:安排|分配|配备|领取|领|发放)?\s*(?P<m>{VALUE})\s*(?P<unit>{MEASURE})")
TOTAL_PREFIX = r"(?:合计|总共|共计|共|总计|总人数|总数量|总数)\s*(?:为|是|领取|领用|领|配备|发放|安排|分配)?\s*"
CONVERSIONS = {"个工作日": ("工作日", 1), "名": ("人", 1), "小时": ("分钟", 60),
               "千米": ("米", 1000), "公里": ("米", 1000), "千克": ("克", 1000),
               "公斤": ("克", 1000), "万元": ("元", 10000)}


def amount(value):
    if value[0].isascii():
        return Decimal(value)
    if not any(c in CN_UNITS for c in value):
        return Decimal("".join(str(CN_DIGITS[c]) for c in value))
    total = group = digit = 0
    for c in value:
        if c in CN_DIGITS:
            digit = CN_DIGITS[c]
        elif c == "万":
            total += (group + digit or 1) * CN_UNITS[c]
            group = digit = 0
        else:
            group += (digit or 1) * CN_UNITS[c]
            digit = 0
    return Decimal(total + group + digit)


def normalized_text(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(rf"^第{VALUE}场(?=[｜|])", "", text)
    text = re.sub(r"(星期|礼拜|周)([一二三四五六日天])(?=[" + CN + r"])", r"\1\2 ", text)
    return CALENDAR_PART.sub(lambda m: str(amount(m[1])), LIST_PREFIX.sub("", text))


def measure_token(value, unit):
    base, scale = CONVERSIONS.get(unit, (unit, 1))
    number = value * scale
    return (format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)) + base


def quantity_token(match):
    return measure_token(amount(match["count"]), match["unit"])


def derived_totals(text):
    return {measure_token(amount(m["n"]) * amount(m["m"]), m["unit"]) for m in BATCH.finditer(text)}


def same_batch_total(prefix, sources, unit):
    """Calculate the preceding batch only when the same count and rate occur in the sources."""
    matches = list(BATCH.finditer(prefix))
    if len(matches) != 1 or not re.fullmatch(r"[。，、,；\s]*" + TOTAL_PREFIX, prefix[matches[0].end():]):
        return None
    batch = matches[0]
    dimension, scale = CONVERSIONS.get(unit, (unit, 1))
    rate_dimension, rate_scale = CONVERSIONS.get(batch["unit"], (batch["unit"], 1))
    if dimension != rate_dimension:
        return None
    count, rate = amount(batch["n"]), measure_token(amount(batch["m"]), batch["unit"])
    if any(other["class"] == batch["class"] and amount(other["n"]) == count
           and measure_token(amount(other["m"]), other["unit"]) == rate
           for source in sources for other in BATCH.finditer(source)):
        return count * amount(batch["m"]) * rate_scale / scale
    return None


def local_quantity(match, claim, sources):
    """Use matching words around the number. A shared unit alone cannot identify a fact."""
    left = re.search(r"[\u4e00-\u9fff]{2,8}$", claim[:match.start()])
    right = re.match(r"[\u4e00-\u9fff]{2,8}", claim[match.end():])
    if not (left or right):
        return None
    candidates = set()
    for source in sources:
        for other in QUANTITY.finditer(source):
            if CONVERSIONS.get(other["unit"], (other["unit"],))[0] != CONVERSIONS.get(match["unit"], (match["unit"],))[0]:
                continue
            if left and not source[:other.start()].endswith(left[0]):
                continue
            if right and not source[other.end():].startswith(right[0]):
                continue
            candidates.add((other[0], quantity_token(other)))
    return next(iter(candidates)) if len(candidates) == 1 else None


def conflicting_rates(text):
    if not re.search(r"同一批|同一事项|同一场", text):
        return {}
    groups = {}
    for match in re.finditer(r"(每(?:组|队|站|班|人))\s*(?:限|可|应)?(领(?:取|用)?|派|交|报)\s*" + QUANTITY.pattern, text):
        basis = (match[1], "领取" if match[2].startswith("领") else match[2], match["unit"])
        groups.setdefault(basis, {})[quantity_token(match)] = match["count"] + match["unit"]
    return {scope: values for scope, values in groups.items() if len(values) > 1}
