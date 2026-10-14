"""Associate bridge conversations with native desktop projects over RPC."""
import ntpath
import uuid
from pathlib import Path

from .codex import RpcError
from .remote import same_cwd, windows


async def project_id(codex, cwd, config):
    if not config.get('desktop_project_sync', False):
        return None
    cursor = None
    matches = set()
    while True:
        try:
            result = await codex.rpc('project/list', {'cursor': cursor, 'limit': 100})
        except RpcError as exc:
            if exc.code == -32601:
                return None  # Older servers can still handle ordinary chat.
            raise
        for project in result['data']:
            if any(same_cwd(root['path'], cwd, config) for root in project.get('roots', [])):
                matches.add(project['id'])
        cursor = result.get('nextCursor')
        if not cursor:
            break
    if len(matches) > 1:
        return None  # A shared folder does not identify a unique desktop project.
    if matches:
        return next(iter(matches))
    identity = ntpath.normcase(ntpath.normpath(cwd)) if windows(config) else cwd
    result = await codex.rpc('project/create', {
        'idempotencyKey': str(uuid.uuid5(uuid.NAMESPACE_URL, 'codex-chat-project:' + identity)),
        'name': config.get('project_name') or (ntpath.basename(cwd) if windows(config) else Path(cwd).name),
        'roots': [{'path': cwd}],
    })
    return result['project']['id']


async def attach(codex, thread, cwd, config):
    if (not config.get('desktop_project_sync', False) or thread.get('ephemeral')
            or thread.get('projectId') or not thread.get('cwd')
            or not same_cwd(thread['cwd'], cwd, config)):
        return False
    ident = await project_id(codex, cwd, config)
    if not ident:
        return False
    await codex.rpc('thread/metadata/update', {'threadId': thread['id'], 'projectId': ident})
    return True
