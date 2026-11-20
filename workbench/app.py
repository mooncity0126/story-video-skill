"""Loopback workbench for reviewing materials and controlling the production queue."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import importlib.util
import json
import mimetypes
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from scripts.media import skill_directory
from workbench import preparation, production

PROJECT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).parent / 'static'
ACTIVE = {'submitting', 'submission_unknown', 'submitted', 'waiting_remote'}


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else default


class Brief(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=100)
    idea: str = Field(default='', max_length=10000)
    script: str = Field(default='', max_length=100000)
    max_duration_seconds: int = Field(default=300, ge=5, le=3600)
    aspect_ratio: Literal['9:16', '16:9', '1:1'] = '9:16'
    visual_style: str = Field(default='写实', max_length=200)
    budget_credits: int | None = Field(default=None, ge=0, le=10000000)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shot_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approved', 'changes_requested']
    note: str = Field(default='', max_length=3000)


class Decisions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Decision] = Field(min_length=1, max_length=200)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    authorized_budget_credits: int | None = Field(default=None, gt=0, le=10000000, strict=True)


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runs = self.root / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        self.skill = skill_directory(self.root)
        spec = importlib.util.spec_from_file_location('workbench_media_tools', self.skill / 'scripts/media_tools.py')
        self.ops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ops)
        # This resolves source paths only; it does not read keys or instantiate a media client.
        self.defaults = self.ops.load_settings(self.root / 'config/local-settings.json')
        self.defaults.setdefault('workflow_preferences_file', str(self.root / 'config/workflow-preferences.local.json'))
        from video_harness.util import run_lock
        self.lock = run_lock

    def run(self, project_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', project_id):
            raise HTTPException(404, '项目不存在')
        path = (self.runs / project_id).resolve()
        if not path.is_relative_to(self.runs.resolve()) or not (path / 'project.json').is_file():
            raise HTTPException(404, '项目不存在')
        return path

    def save(self, path: Path, value):
        self.ops.save(path, value)

    def events(self, run: Path, kind: str, text: str):
        path = run / 'workbench-events.json'
        events = read_json(path, [])
        events.append({'at': time.time(), 'kind': kind, 'text': text})
        self.save(path, events[-300:])

    def media_url(self, run: Path, value) -> str | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = run / path
        path = path.resolve()
        if not path.is_relative_to(self.runs.resolve()) or not path.is_file():
            return None
        relative = path.relative_to(self.runs.resolve())
        return f'/api/projects/{quote(relative.parts[0])}/media?path={quote(Path(*relative.parts[1:]).as_posix())}&v={path.stat().st_mtime_ns}'

    def project_list(self):
        result = []
        for path in self.runs.iterdir():
            if not path.is_dir() or not (path / 'project.json').is_file():
                continue
            try:
                project = read_json(path / 'project.json', {})
                board = read_json(path / 'storyboard.json', {})
                brief = read_json(path / 'creative-brief.json', {})
                result.append({'id': path.name, 'title': brief.get('title') or project.get('title') or board.get('title') or path.name,
                               'created_at': project.get('created_at', path.stat().st_ctime),
                               'status': 'paused_by_user' if (path / 'PAUSE_REQUESTED').exists() else project.get('status', 'planning'),
                               'shot_count': len(board.get('shots', [])), 'planned_duration_ms': board.get('total_duration_ms', 0)})
            except (ValueError, OSError):
                continue
        return sorted(result, key=lambda p: p['created_at'], reverse=True)

    def context(self, run: Path):
        config = self.ops.load_settings(run / 'settings.json')
        board = read_json(run / 'storyboard.json', {'shots': []})
        requests = {}
        for path in sorted((run / 'requests').glob('*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name)):
            value = read_json(path)
            if isinstance(value, dict) and value.get('role') == 'video' and value.get('shot_id'):
                requests[value['shot_id']] = (path, value)
        jobs = [read_json(p) for p in sorted((run / 'jobs').glob('*.json'))]
        return config, board, requests, jobs

    def bundle(self, run: Path, shot: dict, config: dict, requests: dict, jobs: list, accepted_id=None, *,
               graph=None, ledger=None, verified=None):
        sid = shot['shot_id']
        related = sorted([j for j in jobs if j.get('recipe', {}).get('role') == 'video' and j['recipe'].get('shot_id') == sid],
                         key=lambda j: j.get('submitted_at') or j.get('created_at') or 0)
        current_request = requests.get(sid)
        by_id = {job['recipe']['job_id']: job for job in related}
        selected_id = self.ops.selected_video(current_request[1]['job_id'] if current_request else None, accepted_id, by_id)
        selected = by_id.get(selected_id) if selected_id else (related[-1] if related else None)
        recipe = None
        error = None
        # Completed candidates use their retained payload; unfinished work uses its actual request.
        if selected and selected['status'] == 'succeeded':
            recipe = selected['recipe']
        elif sid in requests:
            path, request = requests[sid]
            try:
                recipe = self.ops.request_data(config, request, path.parent)
            except (ValueError, KeyError, OSError):
                error = '素材或提示词尚未齐备，请先完成准备。'
            if recipe and not selected:
                try:
                    self.ops.require_prechecks(config, run, recipe)
                    self.ops.require_continuity(config, run, recipe, graph=graph, verified=verified,
                                                ledger=ledger if ledger is not None else self.ops.current_videos(run, jobs, requests))
                except ValueError as exc:
                    error = str(exc)
        if recipe is None and selected:
            recipe = selected['recipe']
        frames = read_json(run / 'frames' / (sid + '.json'), {})
        first = (recipe or {}).get('inputs', {}).get('input_image', [])
        last = (recipe or {}).get('inputs', {}).get('input_last_frame', [])
        first_path = first[0] if first else frames.get('first', {}).get('path')
        target = frames.get('target_end') or {}
        last_path = last[0] if last else target.get('path')
        prompt = (recipe or {}).get('prompt', '')
        if not prompt:
            for key in ('video_prompt_file', 'source_prompt_file'):
                value = shot.get(key)
                if value:
                    path = (run / value).resolve()
                    if path.is_relative_to(run) and path.is_file() and path.suffix == '.txt':
                        prompt = path.read_text(encoding='utf-8-sig')
                        break
        ready = bool(recipe and first_path and prompt and not error)
        completed = bool(selected and selected['status'] == 'succeeded')
        status = self.ops.material_review_status(config, run, recipe) if recipe else {'state': 'pending', 'fingerprint': None, 'note': '', 'reviewed_at': None}
        if error and status['state'] == 'approved':
            status = {**status, 'state': 'outdated'}
        if recipe is None:
            previous = read_json(run / 'material-approvals.json', {}).get('shots', {}).get(sid, {})
            if previous.get('decision') == 'changes_requested':
                status.update(state='changes_requested', note=previous.get('note', ''), reviewed_at=previous.get('reviewed_at'))
        audio = shot.get('dialogue_audio', {})
        dialogue = audio.get('dialogue', []) if isinstance(audio, dict) else []
        return {'id': sid, 'title': shot.get('title') or shot.get('narrative_purpose') or shot.get('action') or sid,
                'purpose': shot.get('narrative_purpose', ''), 'scene': shot.get('scene_unit_id') or shot.get('scene_id', '未分场'),
                'duration_ms': (recipe or {}).get('duration_ms') or shot.get('duration_ms', 0), 'start_state': shot.get('start_state', ''),
                'action': shot.get('action') or shot.get('narrative_purpose', ''), 'end_state': shot.get('end_state', ''),
                'camera': shot.get('camera', ''), 'dialogue': dialogue, 'timing_reason': shot.get('timing_reason', ''),
                'internal_shots': shot.get('internal_shots', []), 'prompt': prompt,
                'first_image': self.media_url(run, first_path), 'last_image': self.media_url(run, last_path),
                'video': self.media_url(run, (selected or {}).get('asset', {}).get('path')),
                'job_status': (selected or {}).get('status'), 'job_id': (selected or {}).get('recipe', {}).get('job_id'),
                'material_review': status, 'reviewable': ready and not completed and (selected or {}).get('status') not in ACTIVE,
                'completed': completed, 'preparation_error': error, '_recipe': recipe}

    def detail(self, project_id: str):
        run = self.run(project_id)
        config, board, requests, jobs = self.context(run)
        project = read_json(run / 'project.json', {})
        brief = read_json(run / 'creative-brief.json', {})
        inbox = read_json(run / 'workflow-inbox.json', {})
        queue = read_json(run / 'queue.json', {})
        accepted = queue.get('accepted', {})
        waiting = {row['shot_id']: row.get('reason') for row in queue.get('waiting', [])
                   if isinstance(row, dict) and row.get('shot_id')}
        graph_error = None
        try:
            graph = self.ops.shot_dependencies(run, board)
        except ValueError as exc:
            graph, graph_error = {}, str(exc)
        ledger = self.ops.current_videos(run, jobs, requests)
        verified = set()
        raw_shots = [self.bundle(run, s, config, requests, jobs, accepted.get(s['shot_id']), graph=graph, ledger=ledger, verified=verified)
                     for s in board.get('shots', [])]
        shots = [{k: v for k, v in s.items() if not k.startswith('_')} for s in raw_shots]
        for shot in shots:
            if not shot['completed'] and not shot['reviewable']:
                shot['preparation_error'] = graph_error or shot['preparation_error'] or waiting.get(shot['id'])
        paused = (run / 'PAUSE_REQUESTED').exists()
        approvals = sum(s['material_review']['state'] == 'approved' for s in shots)
        completed = sum(s['completed'] for s in shots)
        pending = sum(not s['completed'] and s['material_review']['state'] != 'approved' for s in shots)
        reviewable = sum(s['reviewable'] and s['material_review']['state'] != 'approved' for s in shots)
        first_frames = sum(bool(s['first_image']) for s in shots)
        duration = sum(s['duration_ms'] for s in shots) or board.get('total_duration_ms', 0)
        visual_count = sum(max(1, len(s['internal_shots'])) for s in shots)
        issues = []
        if graph_error:
            issues.append({'kind': 'assets', 'title': '分镜依赖需要修正', 'detail': graph_error})
        if paused:
            issues.append({'kind': 'pause', 'title': '本次制作已暂停', 'detail': '可以检查素材和保存需求；确认素材不会恢复生成。'})
        if project.get('last_service_error', {}).get('http') == 403:
            issues.append({'kind': 'service', 'title': '平台 API 权限待恢复', 'detail': '上次连接返回需要 VIP 会员。这里显示已有记录，没有重新调用平台。'})
        control = production.controls(self, run, any(j['status'] in ACTIVE for j in jobs))
        control['action'] = 'start'
        if graph_error:
            control.update(action='wait', can_start=False, message=graph_error)
        elif inbox.get('status') == 'pending_planning' or not shots:
            control.update(action='wait', can_start=False, message='等待剧本与分镜规划完成。')
        elif control['phase'] == 'waiting_budget':
            control.update(action='budget', can_start=True)
        elif reviewable and (control['running'] or (
                not control['running'] and not any(s['material_review']['state'] == 'approved' and not s['completed'] for s in shots)
                and not any(j['status'] in ACTIVE for j in jobs) and not (
                    (run / preparation.PLAN).is_file() and first_frames < len(shots)))):
            control.update(action='review', can_start=True,
                           message=(control['message'] + ' ' if control['running'] and control['phase'] != 'waiting_approval' else '')
                           + f'{reviewable} 段素材可以检查。' + ('确认后可继续生成。' if not control['running'] else '确认后将自动提交对应视频。'))
        elif control['can_start'] and not requests and not any(j['recipe']['role'] == 'video' for j in jobs):
            if preparation.eligible(self, run, board, requests, jobs):
                control.update(action='prepare', message='可以开始制作本片参考图与分镜首帧。')
            else:
                control.update(action='wait', can_start=False, message='等待补齐分镜首帧、机位或对应的视频提示词。')
        needs_budget = (config['execution'].get('budget_credits') is None
                        or read_json(run / 'budget-authorization.json', {}).get('status') == 'pending'
                        or control['phase'] == 'waiting_budget')
        if needs_budget:
            issues.append({'kind': 'budget', 'title': '本轮预算待确认', 'detail': '点击“开始生成”或“继续生成”可授权本项目的积分上限。'})
        if first_frames < len(shots):
            issues.append({'kind': 'assets', 'title': f'分镜首帧已准备 {first_frames}/{len(shots)}',
                           'detail': '首帧和提示词就绪后，可在分镜页检查该段素材。'})
        if reviewable:
            issues.append({'kind': 'review', 'title': f'{reviewable} 个片段可以检查素材', 'detail': '查看首帧、对白、时长和提示词后确认，也可标记需要修改。'})
        if inbox.get('status') == 'pending_planning':
            issues.append({'kind': 'input', 'title': '制作需求已保存，等待 AI 规划', 'detail': '在对话中让我继续处理这些需求后，剧本和分镜会更新。'})
        stages = [
            {'id':'input','label':'制作需求','state':'done' if brief or board.get('shots') else 'waiting'},
            {'id':'script','label':'剧本与分镜','state':'done' if shots else 'waiting'},
            {'id':'assets','label':'素材准备','state':'done' if shots and all(s['first_image'] and s['prompt'] for s in shots) else ('active' if control['phase'] in {'preparing_assets','waiting_prompts'} else 'waiting')},
            {'id':'approval','label':'人工确认','state':'done' if shots and pending == 0 else ('current' if reviewable else 'waiting')},
            {'id':'generation','label':'视频生成','state':'done' if shots and completed == len(shots) else ('active' if any(j['recipe']['role'] == 'video' and j['status'] in ACTIVE for j in jobs) else 'waiting')},
            {'id':'delivery','label':'检查与成片','state':'done' if (run/'exports/final.mp4').is_file() else 'waiting'}]
        bible = read_json(run / 'bible.json', {})
        references = []
        index = read_json(run / 'reference-index.json', {})
        if isinstance(index, dict):
            for key, value in index.items():
                if isinstance(value, dict):
                    url = self.media_url(run, value.get('path'))
                    if url:
                        references.append({'id': key, 'image': url, 'reused': bool(value.get('reused'))})
        scene_names = {str(s['scene_unit_id']): s.get('title') or s.get('name') or f"场次 {s['scene_unit_id']}"
                       for s in board.get('scene_plans', []) if isinstance(s, dict) and s.get('scene_unit_id')}
        costs = read_json(run / 'cost-estimate.json', {})
        public_jobs = [{'id': j.get('recipe', {}).get('job_id'), 'role': j.get('recipe', {}).get('role'),
                        'shot_id': j.get('recipe', {}).get('shot_id'), 'status': j.get('status'), 'local_edit': bool(j.get('local_edit')),
                        'credits': j.get('reserved_credits', 0), 'created_at': j.get('created_at'),
                        'updated_at': j.get('updated_at'), 'media': self.media_url(run, j.get('asset', {}).get('path'))} for j in jobs]
        return {'id': project_id, 'title': brief.get('title') or project.get('title') or board.get('title') or project_id,
                'status': 'paused_by_user' if paused else project.get('status', 'planning'), 'paused': paused,
                'control': control, 'shots': shots,
                'script': (run/'script.md').read_text(encoding='utf-8-sig') if (run/'script.md').is_file() else '',
                'brief': brief, 'input_pending': inbox.get('status') == 'pending_planning', 'bible': bible,
                'references': references, 'scene_names': scene_names,
                'production': {'aspect_ratio': config['production']['aspect_ratio'], 'visual_style': config['production']['visual_style'],
                               'max_duration_seconds': (config['production'].get('max_duration_ms') or 300000)/1000,
                               'budget_credits': config['execution'].get('budget_credits'), 'needs_budget': needs_budget, 'model': config['models']['video']['model_cls'],
                               'native_audio': True, 'direct_prompt': self.ops.prompt_mode(config) == 'direct',
                               'prompt_mode': self.ops.prompt_mode(config),
                               'scheduling': config['production'].get('scheduling', 'dependency_parallel')},
                'stats': {'planned_duration_ms': duration, 'visual_shot_count': visual_count, 'segment_count': len(shots),
                          'approved_count': approvals, 'pending_count': pending, 'reviewable_count': reviewable,
                          'first_frame_count': first_frames, 'completed_count': completed,
                          'reused_image_count': len({s['first_image'] for s in shots if s['first_image']}),
                          'active_count': sum(j['status'] in ACTIVE for j in jobs),
                          'reserved_credits': sum(j.get('reserved_credits',0) for j in jobs),
                          'estimated_credits': costs.get('base_estimate_credits', costs.get('estimated_video_credits'))},
                'issues': issues, 'stages': stages, 'jobs': public_jobs,
                'events': read_json(run/'workbench-events.json', []), 'source_run': Path(project['source_run']).name if project.get('source_run') else None,
                'final_video': self.media_url(run, 'exports/final.mp4'), 'updated_at': time.time()}

    def decide(self, project_id: str, decisions: Decisions):
        run = self.run(project_id)
        with production.acquire_lock(self, run):
            config, board, requests, jobs = self.context(run)
            shot_map = {s['shot_id']: s for s in board.get('shots', [])}
            accepted = []
            for item in decisions.items:
                if item.shot_id not in shot_map:
                    raise HTTPException(404, '分镜不存在')
                bundle = self.bundle(run, shot_map[item.shot_id], config, requests, jobs)
                if not bundle['reviewable']:
                    raise HTTPException(409, bundle['preparation_error'] or '这个片段尚未备齐素材，或已生成，请刷新后查看。')
                if item.fingerprint != bundle['material_review']['fingerprint']:
                    raise HTTPException(409, '素材已发生变化，请刷新并检查当前版本后再确认。')
                if item.decision == 'changes_requested' and not item.note.strip():
                    raise HTTPException(422, '请写明希望修改的内容。')
                accepted.append(item)
            path = run / 'material-approvals.json'
            record = read_json(path, {'schema_version': 1, 'shots': {}})
            for item in accepted:
                record['shots'][item.shot_id] = {'fingerprint': item.fingerprint, 'decision': item.decision,
                                               'note': item.note.strip(), 'actor': 'user', 'source': 'local_workbench', 'reviewed_at': time.time()}
            self.save(path, record)
            count = len(accepted)
            self.events(run, 'material_review', f'保存了 {count} 个片段的素材确认 / 修改意见。')
        return {'saved': count, 'paused': (run / 'PAUSE_REQUESTED').exists(), 'submitted_jobs': 0}

    def save_brief(self, project_id: str, brief: Brief):
        run = self.run(project_id)
        with production.acquire_lock(self, run):
            value = {**brief.model_dump(), 'saved_at': time.time(), 'source': 'user_workbench'}
            self.save(run / 'creative-brief.json', value)
            self.save(run / 'workflow-inbox.json', {'status': 'pending_planning', 'created_at': time.time(),
                'brief_file': 'creative-brief.json', 'instruction': '按用户保存的需求更新剧本、分镜与素材；保持现有暂停状态，生成前仍需素材确认与预算授权。'})
            self.events(run, 'brief_saved', '制作需求已保存，等待 AI 更新创作文件。')
        return {'saved': True, 'status': 'pending_planning', 'paused': (run / 'PAUSE_REQUESTED').exists()}

    def create_project(self, brief: Brief):
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        project_id = f'story-{stamp}-{secrets.token_hex(2)}'
        run = self.runs / project_id
        config = json.loads(json.dumps(self.defaults))
        config['production'].update(max_duration_ms=brief.max_duration_seconds*1000, target_duration_ms=None,
                                    aspect_ratio=brief.aspect_ratio, visual_style=brief.visual_style)
        config['execution']['budget_credits'] = None
        config['review']['require_material_approval'] = True
        self.ops.init_run(config, run)
        project = read_json(run/'project.json')
        project.update(title=brief.title, status='draft_waiting_planning', source='user_workbench')
        self.save(run/'project.json', project)
        if brief.script.strip():
            (run/'script.md').write_text(brief.script,encoding='utf-8')
        self.save_brief(project_id,brief)
        return {'id':project_id,'status':'draft_waiting_planning'}


def create_app(project_root: Path = PROJECT):
    workspace = Workspace(project_root)
    app = FastAPI(title='剧情视频工作台', docs_url=None, redoc_url=None)
    app.state.workspace = workspace
    app.state.write_token = secrets.token_urlsafe(32)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def local_writes(request: Request, call_next):
        if request.method not in {'GET','HEAD','OPTIONS'}:
            token = request.headers.get('x-workbench-token', '')
            origin = request.headers.get('origin')
            if not secrets.compare_digest(token, app.state.write_token) or (origin and origin.rstrip('/') != str(request.base_url).rstrip('/')):
                return JSONResponse({'detail':'请从本机工作台页面提交操作。'},status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        elif request.url.path in {'/', '/index.html', '/app.js', '/style.css'}:
            response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.exception_handler(ValueError)
    def value_error(request: Request, error: ValueError):
        return JSONResponse({'detail':'项目记录无法读取，请检查文件格式。'},status_code=409)

    @app.exception_handler(workspace.ops.SetupRequired)
    def missing_setup(request: Request, error: ValueError):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.get('/api/workflow-settings')
    def workflow_settings() -> dict:
        return workspace.ops.setup_status(workspace.defaults)

    @app.put('/api/workflow-settings')
    def workflow_save(values: dict[str, str]) -> dict:
        try:
            return workspace.ops.save_setup(workspace.defaults, values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/session')
    def session() -> dict:
        return {'write_token': app.state.write_token, 'mode':'local', 'app':'story-video-workbench', 'generation_enabled':True}

    @app.exception_handler(production.CannotStart)
    def control_error(request: Request, error: production.CannotStart):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.post('/api/projects/{project_id}/start')
    def start_generation(project_id: str, command: StartRequest) -> dict:
        return production.start(workspace, workspace.run(project_id), command.authorized_budget_credits)

    @app.post('/api/projects/{project_id}/pause')
    def pause_generation(project_id: str) -> dict:
        return production.pause(workspace, workspace.run(project_id))

    @app.get('/api/projects')
    def projects() -> list[dict]:
        return workspace.project_list()

    @app.get('/api/projects/{project_id}')
    def project(project_id: str) -> dict:
        return workspace.detail(project_id)

    @app.post('/api/projects', status_code=201)
    def create(brief: Brief) -> dict:
        return workspace.create_project(brief)

    @app.put('/api/projects/{project_id}/brief')
    def save_brief(project_id: str, brief: Brief) -> dict:
        return workspace.save_brief(project_id, brief)

    @app.post('/api/projects/{project_id}/reviews')
    def reviews(project_id: str, decisions: Decisions) -> dict:
        return workspace.decide(project_id, decisions)

    @app.get('/api/projects/{project_id}/media')
    def media(project_id: str, path: Annotated[str, Query(max_length=500)]):
        run = workspace.run(project_id)
        file = (run / path).resolve()
        allowed = {'.png','.jpg','.jpeg','.webp','.image','.mp4','.webm','.wav','.mp3','.m4a'}
        if not file.is_relative_to(run) or not file.is_file() or file.suffix.lower() not in allowed:
            raise HTTPException(404, '媒体文件不存在')
        media_type = 'image/png' if file.suffix.lower() == '.image' else mimetypes.guess_type(file.name)[0]
        return FileResponse(file, media_type=media_type, headers={'Cache-Control':'private, max-age=60'})

    app.frontend('/', directory=STATIC)
    return app


app = create_app()
