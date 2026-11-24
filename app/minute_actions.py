"""Extract action rows from complete, explicit operational decisions in meeting minutes."""

import re


EXECUTION_PHASES = (
    ("准备阶段", r"准备"),
    ("切换当晚", r"切换当晚"),
    ("恢复或回退", r"恢复|回退"),
    ("上线后跟踪", r"上线后|跟踪"),
)


def execution_phases(request, decisions, actions):
    if not re.search(r"准备.{0,12}切换当晚.{0,12}恢复或回退.{0,12}上线后跟踪", request):
        return {}
    phases = {title: {} for title, _ in EXECUTION_PHASES}
    for anchor, quote in decisions.items():
        rows = [row for row in actions if row["anchors"][0] == anchor]
        if any(re.search(r"恢复窗口|回退", row["action"]) for row in rows):
            phase = "恢复或回退"
        elif any(re.search(r"次日|恢复运行后|连续", row["timing"]) for row in rows):
            phase = "上线后跟踪"
        elif any("当天" in row["timing"] or "当日" in row["timing"] for row in rows):
            phase = "切换当晚"
        elif any(row["deliverable"] and row["timing"].endswith("前") for row in rows):
            phase = "准备阶段"
        else:
            continue
        phases[phase][anchor] = quote
    return phases if all(phases.values()) else {}


def action_rows(blocks):
    """Return covered source lines and their actions. Unknown syntax stays with the editor."""
    assignments = {}
    authority = {}
    actions = {}
    for key, line in blocks.items():
        clauses = re.split("[，,]", line.strip().rstrip("。"))
        matches = [re.fullmatch(r"([^，：]{2,24}?)负责(.+)", clause) for clause in clauses]
        if all(matches):
            assignments[key] = [(m[1], m[2]) for m in matches]
            for name, duties in assignments[key]:
                for duty in re.split("、|和|及|与", duties):
                    authority.setdefault(duty, []).append((name, key))

    actors = {name for duties in assignments.values() for name, _ in duties}
    if not actors:
        return {}
    named = "|".join(re.escape(n) for n in sorted(actors, key=len, reverse=True))

    def row(key, action, lead="", timing="", deliverable="", acceptance="", also=()):
        if lead and timing and deliverable and not acceptance:
            acceptance = "核对是否按时收到" + deliverable + "（仅核收件，纪要未列内容验收标准）"
        return dict(action=action, lead=lead, timing=timing, deliverable=deliverable, acceptance=acceptance,
                    support="", status="confirmed", anchors=[key, *also])

    def named_action(clause):
        part = re.fullmatch(r"(?:由)?(" + named + ")(.+)", clause)
        return (part[1], part[2]) if part else ("", clause)

    for key, line in blocks.items():
        if key in assignments:
            continue
        if re.search("建议|拟安排|可能|是否由|另议|待定|作废|未确定|不再执行", line):
            continue
        parts = [s.strip() for s in re.split(r"[；;]", line.strip().rstrip("。"))]
        rows = []
        for part in parts:
            clauses = re.split("[，,]", part)
            first, *rest = clauses
            window = re.fullmatch(r"(.+?)窗口(?:已经定为|定为|为)(.+)", first)
            joint = re.fullmatch(r"(.+?)(?:共同|联合)(.+)", rest[0]) if len(rest) == 3 else None
            branch = next((i for i,c in enumerate(clauses) if re.match("^(?:就|则|应当)", c)), None)
            after = re.fullmatch(r"(.+?)由(" + named + r")在(.+?后)(.+)", first)
            timed = re.fullmatch(r"((?:当天|当日|次日|\d+月\d+日)?\s*\d{1,2}[:：]\d{2}(?:前|后|起)?)(.*)", first)
            if window and not rest:
                authority_for_action = authority.get(window[1], [])
                lead, ref = authority_for_action[0] if len(authority_for_action) == 1 else ("", "")
                rows.append(row(key, window[1], lead, window[2], also=[ref] if ref else []))
            elif joint and first.endswith("后") and "才" in rest[2]:
                owners, action = joint.groups()
                if not set(re.split("与|和|及|、", owners)) <= actors:
                    break
                condition, next_action = rest[2].split("才", 1)
                rows += [row(key, action, owners, first, acceptance=rest[1]),
                         row(key, next_action, timing=rest[1] + "，" + condition)]
            elif branch is not None and branch > 0:
                condition = "，".join(clauses[:branch])
                triggered = [re.sub("^(?:就|则|应当)", "", clauses[branch]), *clauses[branch+1:]]
                for clause in triggered:
                    lead, action = named_action(clause)
                    if not lead and clause.startswith("由"):
                        break
                    rows.append(row(key, action, lead, condition))
                else:
                    continue
                break
            elif after and not rest:
                item, owner, when, action = after.groups()
                rows.append(row(key, item + action, owner, when))
            elif timed and (timed[2] or rest):
                when = timed[1]
                operations = ([timed[2]] if timed[2] else []) + rest
                for clause in operations:
                    handover = re.fullmatch(r"([^，]{1,20}?)(提交|报送|移交|交付|交)(.+)", clause)
                    if handover:
                        actor, verb, deliverable = handover.groups()
                        rows.append(row(key, verb + deliverable, actor, when, deliverable))
                    elif clause.startswith(("保留", "并保留")) and rows:
                        rows[-1]["action"] += "，" + clause
                    else:
                        owner, action = named_action(clause)
                        recipient = re.fullmatch(r"汇总(.+?)给(.+)", action)
                        rows.append(row(key, action, owner, when, recipient[1] if recipient else ""))
            elif not rest:
                owner, action = named_action(first)
                duration = re.fullmatch(r"连续(.+?)([一二三四五六七八九十百\d]+个?(?:工作日|自然日|小时|月|周|天))", action)
                if not (owner and duration):
                    break
                rows.append(row(key, duration[1], owner, "连续" + duration[2]))
            else:
                break
        else:
            if rows:
                actions[key] = rows

    if not actions:
        return {}
    for key, duties in assignments.items():
        rows = []
        for owner, work in duties:
            untimed = []
            for action in re.split("、|和|及|与", work):
                stage = re.fullmatch(r"(.+?后)的(.+)", action)
                if stage:
                    rows.append(row(key, stage[2], owner, stage[1]))
                elif not any(r["lead"] == owner and r["action"] == action
                             for group in actions.values() for r in group):
                    untimed.append(action)
            if untimed:
                rows.append(row(key, "、".join(untimed), owner))
        actions[key] = rows
    return {anchor: actions[anchor] for anchor in blocks if anchor in actions}
