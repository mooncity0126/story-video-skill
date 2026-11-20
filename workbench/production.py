"""Persistent controls for image preparation, material review and video production."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import subprocess
import sys
import time
import uuid
from workbench import preparation


POLLABLE = {'submitted', 'waiting_remote'}
AMBIGUOUS = {'submitting', 'submission_unknown'}
DEFAULT = {'desired': 'idle', 'phase': 'idle', 'message': '等待开始生成', 'revision': ''}


class CannotStart(Exception):
    pass


def read(ws, path, default):
    return ws.ops.read(path) if path.is_file() else default


def state(ws, run):
    return read(ws, run / 'workbench-production.json', DEFAULT.copy())


def command_lock(ws, run):
    """A pause can be recorded even while the media lock is held by an HTTP call."""
    directory = run / '.workbench' / 'commands'
    directory.mkdir(parents=True, exist_ok=True)
    return acquire_lock(ws, directory, 3, '另一个操作正在保存，请稍后重试。')


@contextmanager
def acquire_lock(ws, directory, seconds=12, message='平台正在同步这部片的任务，请稍后重试。'):
    from video_harness.util import HarnessError
    deadline = time.monotonic() + seconds
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(ws.lock(directory))
                break
            except HarnessError:
                if time.monotonic() >= deadline:
                    raise CannotStart(message) from None
                time.sleep(.05)
        yield


def worker_alive(ws, run):
    from video_harness.util import HarnessError
    owner = run / '.workbench' / 'worker'
    if not owner.is_dir():
        return False
    try:
        with ws.lock(owner):
            return False
    except HarnessError:
        return True


def delivered(run):
    return (run / 'exports/final.mp4').is_file() and (run / 'exports/assembly.json').is_file()


def controls(ws, run, active_jobs=False):
    saved = state(ws, run)
    paused = (run / 'PAUSE_REQUESTED').is_file()
    running = saved['desired'] == 'running'
    starting = saved['phase'] == 'starting' and time.time() - saved.get('at', 0) < 12
    live = running and not paused and (starting or worker_alive(ws, run))
    phase, message = saved['phase'], saved['message']
    if delivered(run):
        phase, message = 'completed', '成片已就绪'
    elif paused:
        phase, message = 'paused', '已暂停后续提交，已受理的任务继续在平台运行。'
    elif running and not live:
        phase, message = 'interrupted', '后台队列已停止，可以继续已有任务。'
    return {'phase': phase, 'message': message, 'running': live,
            'can_start': not live and phase != 'completed',
            'can_pause': phase not in {'paused', 'completed'} and (live or active_jobs or phase != 'idle')}


def launch(ws, run):
    directory = run / '.workbench'
    directory.mkdir(exist_ok=True)
    with (directory / 'worker.log').open('ab') as log:
        subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'workbench.production', str(ws.root), run.name],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, close_fds=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )


def write_state(ws, run, saved, phase, message, **updates):
    saved = {**saved, **updates, 'phase': phase, 'message': message, 'at': time.time()}
    ws.save(run / 'workbench-production.json', saved)
    return saved


def sync_metadata(ws, run, saved):
    from video_harness.util import HarnessError
    try:
        with ws.lock(run):
            paused = (run / 'PAUSE_REQUESTED').exists()
            project = read(ws, run / 'project.json', {})
            queue = read(ws, run / 'queue.json', {})
            status = 'paused_by_user' if paused else saved['phase']
            stages = {'preparing_assets': ('prepare_visual_references', 'write_video_prompts'),
                      'waiting_prompts': ('write_video_prompts', 'material_approval'),
                      'waiting_approval': ('material_approval', 'video_generation'),
                      'generating': ('video_generation', 'video_review'),
                      'waiting_review': ('video_review', 'assembly'), 'completed': ('completed', None)}
            stage = stages.get(status)
            changed_stage = stage and (project.get('current_stage'), project.get('next_stage')) != stage
            if project.get('status') != status or project.get('resume_requires_explicit_user_instruction') != paused or changed_stage:
                project.update(status=status, resume_requires_explicit_user_instruction=paused)
                if stage:
                    project['current_stage'], project['next_stage'] = stage
                ws.save(run / 'project.json', project)
            current_jobs = (ws.ops.read(path) for path in sorted((run / 'jobs').glob('*.json')))
            in_flight = [j['recipe']['job_id'] for j in current_jobs if j['status'] in POLLABLE | AMBIGUOUS]
            if queue.get('paused') != paused or queue.get('next_actions') != [saved['message']] or queue.get('in_flight') != in_flight:
                queue.update(paused=paused, next_actions=[saved['message']], in_flight=in_flight)
                ws.save(run / 'queue.json', queue)
    except HarnessError:
        # The marker and durable command already apply; the worker reconciles metadata after I/O.
        pass


def pause(ws, run):
    with command_lock(ws, run):
        if delivered(run) or (run / 'PAUSE_REQUESTED').is_file():
            return controls(ws, run)
        (run / 'PAUSE_REQUESTED').touch()
        saved = write_state(ws, run, state(ws, run), 'paused', '已暂停后续提交，已受理的任务继续在平台运行。',
                            desired='paused', revision=uuid.uuid4().hex)
        if not worker_alive(ws, run) and any(
                ws.ops.read(p).get('status') in POLLABLE for p in (run / 'jobs').glob('*.json')):
            launch(ws, run)
        sync_metadata(ws, run, saved)
    return controls(ws, run)


def start(ws, run, budget=None):
    with command_lock(ws, run):
        if delivered(run) or (controls(ws, run)['running'] and state(ws, run)['phase'] != 'waiting_budget'):
            return controls(ws, run)
        config, board, requests, jobs = ws.context(run)
        if read(ws, run / 'workflow-inbox.json', {}).get('status') == 'pending_planning':
            raise CannotStart('制作输入仍待应用，请先完成这版分镜规划。')
        if not board.get('shots'):
            raise CannotStart('等待剧本与分镜规划完成。')
        try:
            graph = ws.ops.shot_dependencies(run, board)
        except ValueError as exc:
            raise CannotStart(str(exc)) from exc
        prepare = preparation.eligible(ws, run, board, requests, jobs)
        if not (prepare or requests or any(j['recipe']['role'] == 'video' or j['status'] in POLLABLE | AMBIGUOUS for j in jobs)):
            raise CannotStart('请先补齐分镜的起始画面、机位和人物场景设定，或接入已有素材。')
        if not prepare and not jobs and not any(
                s['shot_id'] in requests and ws.bundle(run, s, config, requests, jobs)['material_review']['state'] == 'approved'
                for s in board['shots']
        ) and config['review'].get('require_material_approval', True):
            raise CannotStart('请先在分镜页确认待提交的素材。')
        authorization = read(ws, run / 'budget-authorization.json', {})
        existing = config['execution']['budget_credits']
        waiting_budget = state(ws, run)['phase'] == 'waiting_budget'
        if existing is None or authorization.get('status') == 'pending' or waiting_budget:
            if budget is None:
                raise CannotStart('请填写并授权本项目的素材与视频总积分上限。')
            if waiting_budget and existing is not None and budget <= existing:
                raise CannotStart('新的项目额度需高于已用额度和当前上限。')
            with acquire_lock(ws, run):
                if budget < sum(j.get('reserved_credits', 0) for j in jobs):
                    raise CannotStart('本项目已预留的积分超过填写的额度。')
                settings = read(ws, run / 'settings.json', {})
                settings['execution']['budget_credits'] = budget
                ws.save(run / 'settings.json', settings)
                ws.save(run / 'budget-authorization.json', {**authorization, 'scope': run.name, 'run_id': run.name,
                        'status': 'authorized', 'budget_credits': budget, 'authorized_at': time.time(),
                        'source': 'user_workbench'})
        elif budget is not None and budget != existing:
            raise CannotStart('已授权额度发生变化，请刷新后查看。')
        if prepare and not (run / preparation.PLAN).is_file():
            ws.save(run / preparation.PLAN, preparation.make_plan(ws, run, config, board))
        dependency_path = run / 'dependencies.json'
        stored = read(ws, dependency_path, {'shots': []})
        rows = stored.get('shots', []) if isinstance(stored, dict) else stored
        recorded = {row['shot_id'] for row in rows}
        missing = [decision for sid, decision in graph.items() if sid not in recorded]
        updated = list(rows)
        if config['production'].get('scheduling') == 'serial':
            updated = []
            for row in rows:
                decision = graph.get(row['shot_id'])
                if decision and row != {**row, **decision}:
                    original = {'authored_reason': row['reason']} if row.get('reason') and 'authored_reason' not in row else {}
                    row = {**row, **original, **decision}
                updated.append(row)
        if missing or updated != rows:
            combined = [*updated, *missing]
            ws.save(dependency_path, {**stored, 'shots': combined} if isinstance(stored, dict) else combined)
        # Spawn while holding the command lock. The child reads the command after it is committed.
        if not worker_alive(ws, run):
            launch(ws, run)
        saved = write_state(ws, run, state(ws, run), 'starting', '正在接续生成队列',
                            desired='running', revision=uuid.uuid4().hex)
        (run / 'PAUSE_REQUESTED').unlink(missing_ok=True)
        sync_metadata(ws, run, saved)
    return controls(ws, run)


def cycle(ws, run):
    """One pass through the actual ledger. Each submission uses the skill's paid-operation guards."""
    config, board, requests, jobs = ws.context(run)
    ops = ws.ops
    for job in jobs:
        if job['status'] in POLLABLE:
            ops.poll_job(config, run, job['recipe']['job_id'])
    config, board, requests, jobs = ws.context(run)
    active = any(j['status'] in POLLABLE for j in jobs)
    if (run / 'PAUSE_REQUESTED').exists():
        return 'paused', '已暂停后续提交，已受理的任务继续在平台运行。', not active
    if delivered(run):
        return 'completed', '成片已就绪', True
    unsettled = [j['recipe']['job_id'] for j in jobs if j['status'] in AMBIGUOUS]
    if unsettled:
        # A director may be submitting a corrected still while the queue polls.
        # The submitter holds the media lock until the platform receipt is durable.
        from video_harness.util import HarnessError
        try:
            with ws.lock(run):
                unresolved = any(ops.read(run / 'jobs' / (jid + '.json'))['status'] in AMBIGUOUS
                                 for jid in unsettled)
        except HarnessError:
            return 'synchronizing', '正在同步新提交的素材或视频。', False
        if unresolved:
            return 'attention', '有提交结果待核实，请先核实平台任务记录。', not active
        return 'synchronizing', '已收到平台任务号，正在接续队列。', False
    if read(ws, run / 'workflow-inbox.json', {}).get('status') == 'pending_planning':
        return 'waiting_input', '等待应用新的制作输入。', not active
    if config['execution']['budget_credits'] is None:
        return 'waiting_budget', '等待授权本项目的积分上限。', not active
    if state(ws, run)['phase'] == 'waiting_budget':
        return 'waiting_budget', '余下积分不足以提交下一段。已受理的任务会继续同步。', not active
    prep = None
    if (run / preparation.PLAN).is_file():
        try:
            prep = preparation.advance(ws, run, config, board, requests, jobs)
        except ValueError as exc:
            if str(exc) != 'Media credit budget exceeded':
                raise
            active = any(j['status'] in POLLABLE for j in ws.context(run)[3])
            return 'waiting_budget', '余下积分不足以提交下一项素材。已受理的任务继续同步。', not active
        if prep and prep[2]:
            return prep
        config, board, requests, jobs = ws.context(run)
    by_id, chosen = ops.current_videos(run, jobs, requests)
    active = any(j['status'] in POLLABLE for j in jobs)
    video_active = any(j['recipe']['role'] == 'video' and j['status'] in POLLABLE for j in jobs)
    queue = read(ws, run / 'queue.json', {})
    ready = set(queue['ready']) if 'ready' in queue else None
    waiting = {item.get('shot_id') if isinstance(item, dict) else item for item in queue.get('waiting', [])}
    graph = ops.shot_dependencies(run, board)
    slots = ops.concurrency_status(config, jobs)['available']['video']
    selections = {}
    missing, unapproved, awaiting_review, needs_repair = [], [], [], []
    pending_checks = []
    for shot in board.get('shots', []):
        if (run / 'PAUSE_REQUESTED').exists():
            return 'paused', '已暂停后续提交，已受理的任务继续在平台运行。', False
        sid = shot['shot_id']
        entry = requests.get(sid)
        candidate = chosen.get(sid)
        job = by_id.get(candidate)
        if job is None:
            if not entry or sid in waiting or (ready is not None and sid not in ready):
                missing.append(sid)
                continue
            if slots <= 0:
                continue
            path, request = entry
            try:
                recipe = ops.request_data(config, request, path.parent)
            except FileNotFoundError:
                missing.append(sid)
                continue
            try:
                ops.require_continuity(config, run, recipe, graph=graph, ledger=(by_id, chosen))
            except ValueError as exc:
                missing.append(f'{sid}（{exc}）')
                continue
            try:
                ops.require_prechecks(config, run, recipe)
            except ValueError as exc:
                pending_checks.append(str(exc))
                continue
            if (config['review'].get('require_material_approval', True)
                    and ops.material_review_status(config, run, recipe)['state'] != 'approved'):
                unapproved.append(sid)
                continue
            try:
                ops.submit_job(config, run, request, path.parent, execute=True)
            except ValueError as exc:
                if str(exc) != 'Media credit budget exceeded':
                    raise
                return 'waiting_budget', '余下积分不足以提交下一段。已受理的任务会继续同步。', not active
            slots -= 1
            active = video_active = True
        elif job['status'] == 'succeeded':
            report = read(ws, run / 'reviews' / (candidate + '.json'), {})
            if report.get('source_sha256') == job['asset']['sha256'] and report.get('outcome') in {'pass','uncertain'}:
                try:
                    ops.require_continuity(config, run, job['recipe'], graph=graph, ledger=(by_id, chosen))
                    selections[sid] = candidate
                except ValueError as exc:
                    missing.append(f'{sid}（{exc}）')
            elif report.get('outcome') == 'serious' and report.get('source_sha256') == job['asset']['sha256']:
                needs_repair.append(sid)
            else:
                packet = read(ws, run / 'inspection' / candidate / 'packet.json', {})
                if packet.get('source_sha256') != job['asset']['sha256']:
                    ops.inspect_video(config, run, candidate)
                awaiting_review.append(sid)
        elif job['status'] not in POLLABLE:
            needs_repair.append(sid)
    if video_active:
        return 'generating', '正在生成并下载已提交的片段。', False
    if needs_repair:
        return 'waiting_repair', '等待处理候选片段 ' + '、'.join(needs_repair), False
    if unapproved:
        return 'waiting_approval', f'{len(unapproved)} 段素材等待你确认，确认后的分镜将自动提交视频。', False
    if pending_checks:
        return 'waiting_review', ' '.join(pending_checks), False
    if awaiting_review:
        return 'waiting_review', '等待候选内容检查 ' + '、'.join(awaiting_review), False
    if prep:
        return prep
    if active:
        return 'preparing_assets', '正在生成并下载已提交的图片。', False
    if missing:
        return 'waiting_materials', '等待分镜素材或依赖就绪 ' + '、'.join(missing), False
    order = board.get('playback_order') or [s['shot_id'] for s in board['shots']]
    if not order or len(order) != len(selections) or set(order) != set(selections):
        return 'attention', '请核对分镜播放顺序。', True
    timeline = [{'job_id': selections[sid]} for sid in order]
    ops.pause_check(run)
    ws.save(run / 'timeline.json', timeline)
    ops.assemble(config, run, timeline)
    with ws.lock(run):
        queue = read(ws, run / 'queue.json', {})
        queue.update(ready=[], in_flight=[], accepted=selections)
        ws.save(run / 'queue.json', queue)
    return 'completed', '成片已就绪', True


def work(ws, run):
    from video_harness.util import HarnessError, safe_error
    from video_harness.client import APIError
    owner = run / '.workbench' / 'worker'
    owner.mkdir(parents=True, exist_ok=True)
    with ExitStack() as lease:
        with command_lock(ws, run):
            try:
                lease.enter_context(ws.lock(owner))
            except HarnessError:
                return
        while True:
            with command_lock(ws, run):
                current = state(ws, run)
                if current['desired'] == 'idle':
                    lease.close()
                    return
            try:
                with ws.ops.credential_environment(ws.ops.load_settings(run / 'settings.json')):
                    phase, message, stop = cycle(ws, run)
            except Exception as exc:
                if (run / 'PAUSE_REQUESTED').exists():
                    phase, message, stop = 'paused', '已暂停后续提交，已受理的任务继续在平台运行。', False
                elif isinstance(exc, HarnessError) and '另一个进程' in str(exc):
                    time.sleep(.5)
                    continue
                elif isinstance(exc, APIError) and (exc.category == 'network' or exc.status == 429 or (exc.status or 0) >= 500):
                    phase, message, stop = 'retrying', '平台暂时不可用，稍后重新查询。', False
                else:
                    detail = str(exc) if isinstance(exc, ValueError) else safe_error(exc)
                    print(f'Worker stopped: {type(exc).__name__}: {detail}', flush=True)
                    phase, message, stop = 'attention', '队列需要处理：' + detail[:240], True
            with command_lock(ws, run):
                saved = state(ws, run)
                if saved['revision'] != current['revision']:
                    continue
                if saved.get('phase') != phase or saved.get('message') != message or stop:
                    desired = ('paused' if phase == 'paused' else 'idle') if stop else saved['desired']
                    saved = write_state(ws, run, saved, phase, message, desired=desired)
                sync_metadata(ws, run, saved)
                if stop:
                    lease.close()
                    return
            delay = 16 if phase == 'retrying' else (8 if phase in {'generating','paused'} else 4)
            for _ in range(delay):
                time.sleep(1)
                if state(ws, run)['revision'] != current['revision']:
                    break


if __name__ == '__main__':
    from workbench.app import Workspace
    workspace = Workspace(Path(sys.argv[1]))
    work(workspace, workspace.run(sys.argv[2]))
