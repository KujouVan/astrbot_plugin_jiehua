import re

DEFAULTS = dict(enabled=True, wait_for_followup=True, wait_seconds=8,
                fast_questions=True, timeout_reply=True, clarify_incomplete=True,
                respect_wait=True, quiet_closing=True, update_unsent=False,
                context_linking=True, max_wait_seconds=30)


def options(config):
    result = {key: config.get(key, value) for key, value in DEFAULTS.items()}
    for key, low, high in [('wait_seconds', 1, 30), ('max_wait_seconds', 8, 120)]:
        try:
            result[key] = min(high, max(low, float(result[key])))
        except (ValueError, TypeError):
            result[key] = DEFAULTS[key]
    result['max_wait_seconds'] = max(result['max_wait_seconds'], result['wait_seconds'])
    return result


def classify(text):
    text = text.strip()
    compact = re.sub(r'[\s，。！!、~～]+', '', text)
    if re.fullmatch(r'(行了|就这样吧|先这样吧|不用回了|不用回复了|不聊了|再见|拜拜)', compact):
        return 'closing'
    # Only explicit instructions to wait; generic sentences containing 等 are not enough.
    if re.fullmatch(r'(稍等|等等|等一下|先别回|先别回复|等我一下|我还没说完)[。！!，\s]*', text):
        return 'defer'
    if re.search(r'(等我|等到).{0,80}(再说|再回|再评价|再回答|再讨论)', text):
        return 'defer'
    if re.search(r'(我(待会|等会|稍后).{0,40}(写完|发完|弄完|做完)|我给你发一下|还有(一点|一个问题)|我想问(一下)?|我要说的是|比如|因为|然后|但是)[，,：:\s。]*$', text):
        return 'incomplete'
    if re.search(r'[？?]$|^(怎么|如何|为什么|什么是|请问|帮我|请帮我)', text):
        return 'question'
    return 'ordinary'


def decision(text, config):
    kind = classify(text)
    if kind == 'closing' and config['quiet_closing']:
        return kind, 0
    if kind == 'defer' and config['respect_wait']:
        return kind, 0
    if kind in ('closing', 'defer'):
        kind = 'ordinary'
    if not config['wait_for_followup']:
        return 'reply', 0
    if kind == 'question' and config['fast_questions']:
        return 'reply', 0
    return kind, config['wait_seconds']


TURN_RULES = '''【接话：理解连续发言】
把当前发言和同一发送者的前文结合理解。后一句可能是补充、条件或纠正，不要把每条短消息都当成独立问题逐条回复。
“等我写完，你再说感受”是延后要求，不是现在索要感受。没有依据时不要声称对方已经做完。
只回答当前仍有效的整体意思；收尾不扩展话题，不额外催吃饭或睡觉。不要声称自己无法结合连续消息。
以上只决定本轮理解与回应时机，不改角色文风或人物关系。'''
