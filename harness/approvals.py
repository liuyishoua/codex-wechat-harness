"""Explicit, one-command decisions for app-server approval requests."""
import json


def command_question(params):
    command = params.get('command') or ''
    reason = params.get('reason') or '此命令需要额外执行权限。'
    cwd = params.get('cwd') or '未提供'
    text = 'Codex 等待命令授权\n原因：' + reason + '\n目录：' + cwd + '\n命令：\n' + command
    # Do not approve an action whose full command cannot be reviewed here.
    reviewable = bool(command.strip()) and len(text) <= 1900 and params.get('kind', 'command') == 'command'
    available = params.get('availableDecisions')
    if available is None:
        available = ['accept', 'decline', 'cancel']
    options, decisions = [], []
    for decision, label in [('accept', '允许本次'), ('decline', '拒绝本次'), ('cancel', '拒绝并停止本轮')]:
        if decision in available and (decision != 'accept' or reviewable):
            options.append(label)
            decisions.append(decision)
    if not reviewable:
        text = text[:1600] + '\n\n手机无法完整展示此操作，请回 Codex 桌面核对；此处仅支持拒绝。'
    if not options:
        raise ValueError('没有可以安全展示的授权选项')
    return {'id': 'approval', 'question': text, 'options': options, 'decisions': decisions}


def native_result(row, answers):
    if row['kind'] != 'command_approval':
        return {'answers': answers}
    question = json.loads(row['questions'])[0]
    value = answers[question['id']]['answers'][0]
    try:
        index = question['options'].index(value)
    except ValueError:
        raise ValueError('命令授权请回复卡片中的选项序号或完整选项文字') from None
    decision = question['decisions'][index]
    if decision not in ('accept', 'decline', 'cancel'):
        raise ValueError('不支持此授权范围')
    return {'decision': decision}
