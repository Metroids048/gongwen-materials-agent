"""Project numbered instructions without guessing who performs an action."""

import re


ARTICLE = re.compile(r"^第[一二三四五六七八九十百零〇\d、和至第]+条[：:]\s*(?P<body>.+)$")
POLICY = re.compile(r"《[^》]*(?:指引|办法|细则|规定|规程|规范|制度|规则)[^》]*》")
DISCUSSION = re.compile(r"会前(?:讨论|沟通|会商)稿|会商稿")
USE_SOURCE = re.compile(r"按(?:照)?(?:附件|材料|原文)|照(?:附件|材料|原文)|依据(?:附件|材料|原文)")
AVOID_PROJECTION = re.compile(r"仅供参考|只作参考|不按原文|征求意见稿|讨论版|草案")
ACTOR = (r"[\u4e00-\u9fff、]{0,22}?(?:局|政府|部门|中心|管理方|实施主体|代理机构|主场|客场|"
         r"组长|成员|单位|机构|办公室|负责人|工作组)")
DUTY = (r"统筹|推进|具体组织|提供|负责|应(?=在|于|向|将|当)|定(?=两|各|评)|确定|"
        r"按(?=照|该|要求)|原则上|保管|保存|向|可(?=以|决定)|决定|配合|组织|通知|联系|委托|办理|"
        r"提交|审核|复核|登记|签署|签发|报送|核验|检查|实施")
NAMED = re.compile(rf"^(?P<lead>{ACTOR}(?:(?:或|和|及|与){ACTOR})?)(?P<action>(?:都)?(?:{DUTY}).+)$")
BY_ACTOR = re.compile(rf"^(?P<context>[^，]*?)由(?P<lead>{ACTOR})(?P<action>(?:{DUTY}).+)$")
CONDITION = re.compile(r"^(?:出现|仍|超过|如|遇到|意见).*(?:时|的|延期|故障|解除)$")
SCOPE = re.compile(r"^(?:适用范围|达到|本(?:指引|办法|细则|规定|规程)|并非|具体项目)")
SECTION = "材料明确的职责与程序"
AGENDA = "会上待定事项"
VOTE_RECORD = re.compile(r"投票记录|表决记录")
RECORD_HANDOFF = re.compile(r"交接|移交|归集|归档|留存|汇总|传递|怎么交|如何交")


def source_plan(request, materials):
    if not (DISCUSSION.search(request) and USE_SOURCE.search(request)) or AVOID_PROJECTION.search(request):
        return None
    sources = [item for item in materials if item["id"] != "U" and not item.get("reference_only")]
    if len(sources) != 1:
        return None
    blocks = sources[0]["blocks"]
    if not any(POLICY.search(line) and not AVOID_PROJECTION.search(line) for line in blocks.values()):
        return None
    articles = {anchor: line for anchor, line in blocks.items() if ARTICLE.fullmatch(line)}
    if len(articles) < 2:
        return None
    actions = [row for anchor, line in articles.items() for row in _rows(anchor, ARTICLE.fullmatch(line)["body"])]
    if not any(row["lead"] for row in actions):
        return None
    return {"source_id": sources[0]["id"], "articles": articles, "actions": actions,
            "open_record_handoff": _open_record_handoff(request, blocks, articles)}


def _open_record_handoff(request, blocks, articles):
    requested = VOTE_RECORD.search(request)
    if not requested or not RECORD_HANDOFF.search(re.split(r"[。；？！]", request[requested.end():], maxsplit=1)[0]):
        return None
    # Monitor/record duties alone do not settle responsibility for handing the records over.
    if any(VOTE_RECORD.search(sentence) and RECORD_HANDOFF.search(sentence)
           for line in blocks.values() for sentence in re.split(r"[。；]", line)):
        return None
    anchor = next((key for key, line in articles.items()
                   if re.search(r"(?:投票|表决)[^。；]*记录", line)), None)
    if anchor:
        subject = requested[0]
        return {"text": f"{subject}移交：由谁归集、向谁移交，采用什么形式？", "anchors": ["U.1", anchor]}
    return None


def _rows(anchor, body):
    for sentence in re.split(r"[。；]", body):
        if not sentence or SCOPE.match(sentence):
            continue
        pieces = [piece.strip() for piece in sentence.split("，")]
        timing, fragments = "", []
        if len(pieces) > 1 and CONDITION.fullmatch(pieces[0]):
            timing = pieces.pop(0)
        for piece in pieces:
            if fragments and not (NAMED.fullmatch(piece) or BY_ACTOR.fullmatch(piece)):
                fragments[-1] += "，" + piece
            else:
                fragments.append(piece)
        for fragment in fragments:
            lead, action, when = "", fragment, timing
            if match := BY_ACTOR.fullmatch(fragment):
                lead, action = match["lead"], match["action"]
                context = match["context"]
                when = "，".join(part for part in (timing, context) if part)
            elif match := NAMED.fullmatch(fragment):
                lead, action = match["lead"], match["action"]
                if relative := re.match(r"^应在(?P<time>[^，。]{2,20}前(?:[一二三四五六七八九十两0-9]+[周天日小时]+)?)(?=向|将|提交|办理)", action):
                    when = "，".join(part for part in (timing, relative["time"]) if part)
                    action = "应" + action[relative.end():]
            yield dict(action=action, lead=lead, support="", timing=when, deliverable="", acceptance="",
                       contact="", phone="", status="confirmed", anchors=[anchor])
