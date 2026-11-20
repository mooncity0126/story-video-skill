"""Exercise the local workbench against isolated media; no user approvals or cloud calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.app import app as production_app, create_app
from workbench import production
from scripts.media import skill_directory


def test_workbench_and_media_share_the_portable_skill_location(tmp_path, monkeypatch):
    monkeypatch.delenv('STORY_VIDEO_SKILL_DIR', raising=False)
    assert skill_directory(tmp_path) == tmp_path / '.agents/skills/produce-story-video'
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/skill-location.json').write_text('{"skill_root":"installed/skill"}')
    assert skill_directory(tmp_path) == tmp_path / 'installed/skill'
    monkeypatch.setenv('STORY_VIDEO_SKILL_DIR', str(tmp_path / 'external'))
    assert skill_directory(tmp_path) == tmp_path / 'external'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Live network is forbidden in workbench tests')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', blocked)
    root = tmp_path / 'workspace'
    (root / 'config').mkdir(parents=True)
    config = copy.deepcopy(production_app.state.workspace.defaults)
    config.pop('credentials_file', None)
    config['workflow_preferences_file'] = str(root / 'config/workflow-preferences.local.json')
    config['execution']['budget_credits'] = None
    (root / 'config/local-settings.json').write_text(json.dumps(config), encoding='utf-8')
    (root / 'config/skill-location.json').write_text(
        json.dumps({'skill_root': str(production_app.state.workspace.skill)}), encoding='utf-8')
    app = create_app(root)
    ws = app.state.workspace
    run = root / 'runs/fixture'
    legacy = dict(config)
    legacy.pop('workflow_preferences_file')
    ws.ops.init_run(legacy, run)
    (run / 'requests').mkdir()
    Image.new('RGB', (108, 192), 'navy').save(run / 'assets/first.png')
    shots = []
    for sid in ('S001', 'S002'):
        ws.save(run / f'requests/{sid}.json', {'job_id': 'video_'+sid, 'role': 'video', 'shot_id': sid,
            'duration_ms': 5000, 'prompt': 'Fixture dialogue only.', 'inputs': {'input_image': ['../assets/first.png']}})
        shots.append({'shot_id': sid, 'duration_ms': 5000, 'scene_id': 'room',
                      'generation_plan': {'relation': 'independent', 'confidence': 'high',
                                          'reason': '独立插叙，不承接另一镜的动作或道具。'},
                      'dialogue_audio': {'dialogue': [{'speaker_id': 'P1', 'text': '测试台词'}]}})
    ws.save(run / 'storyboard.json', {'title': '测试素材', 'shots': shots})
    ws.save(run / 'bible.json', {'characters': {'P1': {'name': '测试人物'}},
                                 'scenes': [{'id': 'room', 'description': '窗边一张窄桌'}]})
    (run / 'script.md').write_text('Original script', encoding='utf-8')
    (run / 'PAUSE_REQUESTED').write_text('user pause', encoding='utf-8')
    with TestClient(app) as client:
        client.headers['X-Workbench-Token'] = client.get('/api/session').json()['write_token']
        yield ws, run, client


def choice(client, sid='S001', **changes):
    data = client.get('/api/projects/fixture').json()
    shot = next(s for s in data['shots'] if s['id'] == sid)
    return dict(shot_id=sid, fingerprint=shot['material_review']['fingerprint'], decision='approved', note='', **changes)


def test_workbench_and_skill_share_first_use_without_modifying_legacy(workspace):
    ws, legacy, client = workspace
    original = (legacy/'settings.json').read_bytes()
    endpoint = '/api/workflow-settings'
    prefs = client.get(endpoint).json()
    assert not prefs['configured']
    assert client.post('/api/projects', json={'title':'等待设置'}).status_code == 409
    assert len(list(ws.runs.iterdir())) == 1
    assert client.get('/api/projects/fixture').status_code == 200
    assert TestClient(create_app(ws.root)).put(endpoint, json=prefs['values']).status_code == 403
    assert client.put(endpoint, json={**prefs['values'], 'prompt_mode':'both'}).status_code == 422
    assert not ws.ops.setup_status(ws.defaults)['configured']
    values = {**prefs['values'], 'prompt_mode':'online', 'frame_check':'always'}
    assert client.put(endpoint, json=values).status_code == 200
    cli = ['--config', str(ws.root/'config/local-settings.json')]
    assert ws.ops.main([*cli,'setup'])['values'] == values
    created = client.post('/api/projects', json={'title':'在线优化样片'}).json()['id']
    data = client.get('/api/projects/'+created).json()
    assert data['production']['prompt_mode'] == 'online' and data['production']['scheduling'] == 'serial'
    assert not data['production']['direct_prompt'] and data['production']['budget_credits'] is None
    assert not list((ws.run(created)/'jobs').iterdir())
    assert (legacy/'settings.json').read_bytes() == original


def test_enabled_frame_and_prompt_checks_block_only_their_current_candidate(workspace):
    ws, run, client = workspace
    config, board, requests, _ = ws.context(run)
    config['production']['scheduling'] = 'dependency_parallel'
    config['review'].update(frame_check='always', prompt_check='always')
    ws.save(run/'settings.json', config)
    first = run/'assets/first.png'
    ws.save(run/'frames/S001.json', {'first': {'path':str(first), 'sha256':ws.ops.sha(first)}})
    endpoint = '/api/projects/fixture'
    assert not client.get(endpoint).json()['shots'][0]['reviewable']
    frame = ws.ops.main(['--run', str(run), 'check-inputs', '--shot', 'S001'])
    assert frame == {'frame': {'first':ws.ops.sha(first), 'target_end':None}}
    def record(stage, hashes):
        ws.save(run/'checks'/('S001-'+stage+'.json'), {'round':1,'enabled':True,'outcome':'pass','source_hashes':hashes})
    record('frame', frame['frame'])
    assert '提示词' in client.get(endpoint).json()['shots'][0]['preparation_error']
    reqpath, req = requests['S001']
    expected = ws.ops.main(['--run', str(run), 'check-inputs', '--request', str(reqpath)])
    record('prompt', expected['prompt'])
    assert [s['reviewable'] for s in client.get(endpoint).json()['shots']] == [True, False]
    ws.save(reqpath, {**req,'prompt':'replacement'})
    assert '提示词' in client.get(endpoint).json()['shots'][0]['preparation_error']


def test_workbench_local_rewrite_reuses_and_invalidates_sources(workspace, monkeypatch):
    from workbench import preparation
    ws, run, client = workspace
    (run/'PAUSE_REQUESTED').unlink()
    config, board, requests, _ = ws.context(run)
    for path, _ in requests.values():
        path.unlink()
    config['rewriter'].update(mode='local', enabled=True)
    config['production']['scheduling'] = 'dependency_parallel'
    ws.save(run/'settings.json', config)
    for shot in board['shots']:
        shot.update(start_state='窗边', camera='中景')
    ws.save(run/'storyboard.json', board)
    ws.save(run/preparation.PLAN, preparation.make_plan(ws, run, config, board))
    (run/'prompts').mkdir()
    first = run/'assets/first.png'
    for shot in board['shots']:
        sid = shot['shot_id']
        ws.save(run/'frames'/f'{sid}.json', {'first': {'path':str(first), 'sha256':ws.ops.sha(first)}})
        (run/'prompts'/f'{sid}-h3.txt').write_text('authored', encoding='utf-8')
    calls = []
    def stub(cfg, directory, prompt, ms, opening, ending):
        source = prompt.read_text(encoding='utf-8')
        calls.append(source)
        cache = run/'prompt_rewrites'/ws.ops.fingerprint([source, prompt.name])
        ws.save(cache/'input.json', {'source_prompt':source, 'first_frame_sha256':ws.ops.sha(opening),
            'duration_seconds':ms/1000,'aspect_ratio':cfg['production']['aspect_ratio']})
        ws.save(cache/'model-output.json', {'mock':True})
        output = cache/'minimax-prompt.txt'
        output.write_text(source+' rewritten', encoding='utf-8')
        record = cache/'record.json'
        ws.save(record, {'prompt':source+' rewritten', 'raw_output_sha256':ws.ops.sha(cache/'model-output.json'),
            'prompt_file_sha256':ws.ops.sha(output)})
        return {'record_path':str(record), 'prompt_file':str(output)}
    monkeypatch.setattr(ws.ops, 'rewrite', stub)
    def prepare():
        c, b, reqs, jobs = ws.context(run)
        preparation.advance(ws, run, c, b, reqs, jobs)
    prepare()
    prepare()
    assert len(calls) == 2
    endpoint = '/api/projects/fixture'
    shot = client.get(endpoint).json()['shots'][0]
    assert shot['reviewable'] and shot['prompt'] == 'authored rewritten'
    assert client.post(endpoint+'/reviews', json={'items':[choice(client)]}).status_code == 200
    (run/'prompts/S001-h3.txt').write_text('updated', encoding='utf-8')
    assert not client.get(endpoint).json()['shots'][0]['reviewable']
    prepare()
    shot = client.get(endpoint).json()['shots'][0]
    assert len(calls) == 3 and shot['prompt'] == 'updated rewritten' and shot['material_review']['state'] == 'outdated'


def test_read_only_real_shape_and_media(workspace):
    ws, run, client = workspace
    before = {p.name: p.read_bytes() for p in run.glob('*.json')}
    data = client.get('/api/projects/fixture').json()
    assert data['stats']['pending_count'] == 2 and data['paused']
    assert data['bible']['characters']['P1']['name'] == '测试人物'
    assert client.get(data['shots'][0]['first_image']).headers['content-type'] == 'image/png'
    assert client.get('/').status_code == 200
    assert {p.name:p.read_bytes() for p in run.glob('*.json')} == before
    assert 'credentials_file' not in json.dumps(data)


def test_ui_decision_is_consumed_by_submission_guard_without_resuming(workspace):
    ws, run, client = workspace
    response = client.post('/api/projects/fixture/reviews', json={'items':[choice(client)]})
    assert response.status_code == 200 and response.json()['submitted_jobs'] == 0
    cfg = ws.ops.load_settings(run/'settings.json')
    req = ws.ops.read(run/'requests/S001.json')
    recipe = ws.ops.request_data(cfg, req, run/'requests')
    ws.ops.require_material_approval(cfg, run, recipe)
    assert (run/'PAUSE_REQUESTED').is_file()
    assert not list((run/'jobs').iterdir())
    assert cfg['execution']['budget_credits'] is None
    Image.new('RGB', (108,192),'orange').save(run/'assets/first.png')
    assert client.get('/api/projects/fixture').json()['shots'][0]['material_review']['state'] == 'outdated'


def test_stale_batch_is_atomic_and_changes_request_requires_note(workspace):
    ws, run, client = workspace
    first, second = choice(client), choice(client,'S002')
    second['fingerprint'] = '0'*64
    assert client.post('/api/projects/fixture/reviews',json={'items':[first,second]}).status_code == 409
    assert not (run/'material-approvals.json').exists()
    first['decision'] = 'changes_requested'
    assert client.post('/api/projects/fixture/reviews',json={'items':[first]}).status_code == 422
    first['note'] = '请调整首帧位置'
    assert client.post('/api/projects/fixture/reviews',json={'items':[first]}).status_code == 200
    assert client.get('/api/projects/fixture').json()['shots'][0]['material_review']['state'] == 'changes_requested'


def test_new_candidate_after_completed_job_remains_reviewable(workspace):
    ws,run,client=workspace
    config=ws.ops.load_settings(run/'settings.json')
    request=ws.ops.read(run/'requests/S001.json')
    recipe=ws.ops.request_data(config,request,run/'requests')
    ws.save(run/'jobs/video_S001.json',{'recipe':recipe,'status':'succeeded','submitted_at':1})
    assert client.get('/api/projects/fixture').json()['shots'][0]['completed']
    request['job_id']='video_S001_v2'
    request['prompt']='A corrected performance.'
    ws.save(run/'requests/S001.json',request)
    shot=client.get('/api/projects/fixture').json()['shots'][0]
    assert shot['reviewable'] and not shot['completed']
    assert shot['prompt']=='A corrected performance.'
    assert client.post('/api/projects/fixture/reviews',json={'items':[choice(client)]}).status_code==200


def test_new_input_preserves_original_files_and_blocks_old_plan(workspace):
    ws, run, client = workspace
    client.post('/api/projects/fixture/reviews',json={'items':[choice(client)]})
    before = {p: (run/p).read_bytes() for p in ('storyboard.json','script.md','settings.json','PAUSE_REQUESTED')}
    brief = {'title':'新片名','idea':'更明确的冲突','script':'新的剧本','max_duration_seconds':180,'budget_credits':6000}
    assert client.put('/api/projects/fixture/brief',json=brief).status_code == 200
    assert all((run/p).read_bytes()==raw for p,raw in before.items())
    cfg=ws.ops.load_settings(run/'settings.json')
    recipe=ws.ops.request_data(cfg,ws.ops.read(run/'requests/S001.json'),run/'requests')
    with pytest.raises(ValueError,match='await planning'):
        ws.ops.require_material_approval(cfg,run,recipe)
    assert client.get('/api/projects/fixture').json()['input_pending']
    assert ws.ops.read(run/'creative-brief.json')['budget_credits']==6000
    assert cfg['execution']['budget_credits'] is None


def test_create_project_is_only_local_draft_and_persists(workspace):
    ws, run, client = workspace
    prefs = client.get('/api/workflow-settings').json()
    assert client.put('/api/workflow-settings', json=prefs['values']).status_code == 200
    response=client.post('/api/projects',json={'title':'新项目','idea':'一个误会','script':'草稿','max_duration_seconds':120})
    assert response.status_code==201
    project_id=response.json()['id']
    reopened=TestClient(create_app(ws.root))
    data=reopened.get('/api/projects/'+project_id).json()
    assert data['title']=='新项目' and data['script']=='草稿' and data['input_pending']
    assert data['production']['budget_credits'] is None and data['stats']['segment_count']==0
    assert not list((ws.runs/project_id/'jobs').iterdir())
    reopened.close()


@pytest.mark.parametrize('path',['../fixture/settings.json','settings.json','../../config/local-settings.json','C:/Windows/win.ini'])
def test_media_endpoint_cannot_return_non_media_or_outside_paths(workspace,path):
    ws,run,client=workspace
    assert client.get('/api/projects/fixture/media',params={'path':path}).status_code==404


def test_cross_origin_or_missing_token_cannot_write(workspace):
    ws,run,client=workspace
    body={'items':[choice(client)]}
    assert client.post('/api/projects/fixture/reviews',json=body,headers={'Origin':'https://unrelated.example'}).status_code==403
    assert client.post('/api/projects/fixture/reviews',json=body,headers={'X-Workbench-Token':''}).status_code==403
    assert not (run/'material-approvals.json').exists()


def test_controls_require_approval_and_express_budget_before_start(workspace, monkeypatch):
    ws, run, client = workspace
    launches = []
    monkeypatch.setattr(production, 'launch', lambda workspace, project: launches.append(project))
    url = '/api/projects/fixture/start'
    assert client.post(url, json={'authorized_budget_credits':750}).status_code == 409
    assert ws.ops.read(run/'settings.json')['execution']['budget_credits'] is None
    client.post('/api/projects/fixture/reviews', json={'items':[choice(client)]})
    assert client.post(url, json={}).status_code == 409
    assert client.post(url, json={'authorized_budget_credits':750}, headers={'X-Workbench-Token':''}).status_code == 403
    assert (run/'PAUSE_REQUESTED').is_file()
    assert client.post(url, json={'authorized_budget_credits':750}).json()['running']
    assert ws.ops.read(run/'settings.json')['execution']['budget_credits'] == 750
    assert ws.ops.read(run/'budget-authorization.json')['scope'] == 'fixture'
    assert client.post(url, json={}).json()['running']
    assert launches == [run]


def test_board_only_start_prepares_stills_and_waits_for_user_before_video(workspace, monkeypatch):
    ws, run, client = workspace
    endpoint = '/api/projects/fixture'
    for p in (run / 'requests').glob('*.json'):
        p.unlink()
    board = ws.ops.read(run / 'storyboard.json')
    for shot in board['shots']:
        shot.update(start_state='人物站在窗边', camera='固定中景', characters=['P1'])
    ws.save(run / 'storyboard.json', board)
    ws.save(run / 'queue.json', {'ready': [], 'waiting': [{'shot_id': s['shot_id']} for s in board['shots']]})
    monkeypatch.setattr(production, 'launch', lambda *_: None)
    calls = []

    def submit(config, directory, req, base, *, execute):
        assert execute and config['execution']['budget_credits'] == 900
        recipe = ws.ops.request_data(config, req, base)
        if req['role'] == 'video':
            ws.ops.require_material_approval(config, directory, recipe)
        calls.append(req['job_id'])
        job = {'recipe': recipe, 'status': 'submitted', 'reserved_credits': 1}
        ws.save(directory / 'jobs' / (req['job_id'] + '.json'), job)
        return job

    def poll(config, directory, jid):
        path = directory / 'jobs' / (jid + '.json')
        job = ws.ops.read(path)
        if job['recipe']['role'] != 'video':
            target = directory / 'assets' / (jid + '.png')
            Image.new('RGB', (108, 192), '#' + ws.ops.fingerprint(jid)[:6]).save(target)
            ws.save(path, {**job, 'status': 'succeeded', 'asset': {
                'path': target.relative_to(directory).as_posix(), 'sha256': ws.ops.sha(target)}})

    monkeypatch.setattr(ws.ops, 'submit_job', submit)
    monkeypatch.setattr(ws.ops, 'poll_job', poll)
    initial = client.get(endpoint).json()
    assert initial['control']['action'] == 'prepare' and initial['stats']['reviewable_count'] == 0
    assert client.post(endpoint + '/start', json={}).status_code == 409
    assert client.post(endpoint + '/start', json={'authorized_budget_credits': 900}).status_code == 200
    foundations = ['prep-scene-' + ws.ops.fingerprint('room')[:12], 'prep-cast-' + ws.ops.fingerprint('P1')[:12]]
    assert production.cycle(ws, run)[0] == 'preparing_assets' and calls == foundations
    client.post(endpoint + '/pause', json={})
    assert production.cycle(ws, run)[0] == 'paused' and calls == foundations
    assert client.post(endpoint + '/start', json={}).status_code == 200
    assert production.cycle(ws, run)[0] == 'preparing_assets'
    assert production.cycle(ws, run)[0] == 'waiting_prompts'
    assert client.get(endpoint).json()['stats']['first_frame_count'] == 2
    prompts = run / 'prompts'
    prompts.mkdir()
    for s in board['shots']:
        (prompts / (s['shot_id'] + '-h3.txt')).write_text('Final fixture performance', encoding='utf-8')
    assert production.cycle(ws, run)[0] == 'waiting_approval'
    assert production.cycle(ws, run)[0] == 'waiting_approval'
    assert calls == foundations + ['prep-S001-v1', 'prep-S002-v1']
    edit = ws.ops.read(run / 'jobs/prep-S001-v1.json')['recipe']
    hashes = [ws.ops.read(run / f'jobs/{jid}.json')['asset']['sha256'] for jid in foundations]
    assert edit['input_hashes'][edit['model']['media_field']] == hashes
    assert ws.ops.read(run / 'queue.json')['ready'] == ['S001', 'S002']
    assert client.get(endpoint).json()['stats']['reviewable_count'] == 2
    assert client.post(endpoint + '/reviews', json={'items': [choice(client)]}).status_code == 200
    assert production.cycle(ws, run)[0] == 'generating'
    assert production.cycle(ws, run)[0] == 'generating'
    assert calls[4:] == ['video-S001-v1']


def test_pause_during_media_lock_then_resume_continues_existing_jobs(workspace, monkeypatch):
    ws, run, client = workspace
    client.post('/api/projects/fixture/reviews',json={'items':[choice(client), choice(client,'S002')]})
    monkeypatch.setattr(production,'launch',lambda *args:None)
    assert client.post('/api/projects/fixture/start',json={'authorized_budget_credits':750}).status_code == 200
    first = ws.ops.read(run/'requests/S001.json')
    cfg = ws.ops.load_settings(run/'settings.json')
    cfg['production']['maximum_parallel_video_jobs'] = 1
    ws.save(run/'settings.json',cfg)
    recipe = ws.ops.request_data(cfg,first,run/'requests')
    ws.save(run/'jobs/video_S001.json',{'recipe':recipe,'status':'submitted','task_id':'known-1','reserved_credits':70})
    with ws.lock(run):
        result=client.post('/api/projects/fixture/pause',json={})
        assert result.status_code == 200 and result.json()['phase']=='paused'
        assert (run/'PAUSE_REQUESTED').is_file()
    polled=[]
    monkeypatch.setattr(ws.ops,'poll_job',lambda config,project,jid:polled.append(jid))
    charged=[]
    def submit(config, directory, request, parent, *,execute):
        ws.ops.require_material_approval(config,directory,ws.ops.request_data(config,request,parent))
        charged.append(request['job_id'])
        ws.save(directory/'jobs'/(request['job_id']+'.json'),{'recipe':ws.ops.request_data(config,request,parent),'status':'submitted','task_id':'known-2','reserved_credits':70})
    monkeypatch.setattr(ws.ops,'submit_job',submit)
    assert production.cycle(ws,run)[0]=='paused'
    assert polled==['video_S001'] and charged==[]
    assert client.post('/api/projects/fixture/start',json={}).status_code == 200
    assert not (run/'PAUSE_REQUESTED').exists()
    assert production.cycle(ws,run)[0]=='generating'
    assert charged==[]  # First task still occupies the only slot.
    job=ws.ops.read(run/'jobs/video_S001.json')
    job.update(status='succeeded',asset={'sha256':'first'})
    ws.save(run/'jobs/video_S001.json',job)
    ws.save(run/'reviews/video_S001.json',{'source_sha256':'first','outcome':'pass'})
    production.cycle(ws,run)
    production.cycle(ws,run)
    assert charged==['video_S002']


def test_worker_waits_for_reviews_before_assembling_in_playback_order(workspace,monkeypatch):
    ws,run,client=workspace
    cfg=ws.ops.load_settings(run/'settings.json')
    cfg['execution']['budget_credits']=800
    ws.save(run/'settings.json',cfg)
    (run/'PAUSE_REQUESTED').unlink()
    board=ws.ops.read(run/'storyboard.json')
    board['playback_order']=['S002','S001']
    ws.save(run/'storyboard.json',board)
    for sid in board['playback_order']:
        req=ws.ops.read(run/f'requests/{sid}.json')
        jid=req['job_id']
        ws.save(run/f'jobs/{jid}.json',{'recipe':ws.ops.request_data(cfg,req,run/'requests'),'status':'succeeded','asset':{'sha256':sid}})
        ws.save(run/f'inspection/{jid}/packet.json',{'source_sha256':sid})
    assembled=[]
    monkeypatch.setattr(ws.ops,'assemble',lambda config,path,timeline:assembled.append(timeline))
    assert production.cycle(ws,run)[0]=='waiting_review'
    assert not assembled
    # A resumed legacy run can still use an already downloaded candidate after archiving its request.
    (run/'requests/S001.json').unlink()
    for sid in board['playback_order']:
        ws.save(run/f'reviews/video_{sid}.json',{'source_sha256':sid,'outcome':'pass'})
    assert production.cycle(ws,run)[0]=='completed'
    assert assembled==[[{'job_id':'video_S002'},{'job_id':'video_S001'}]]


def test_only_matching_source_edit_and_reviewed_tail_can_propagate(workspace):
    ws, run, _ = workspace
    ops = ws.ops
    cfg = ops.load_settings(run / 'settings.json')
    video = run / 'assets/source.mp4'
    video.write_bytes(b'local fixture video')
    source_hash = ops.sha(video)
    job = {'status': 'succeeded', 'recipe': {'job_id': 'video_1', 'role': 'video', 'shot_id': 'S001'},
           'asset': {'sha256': source_hash, 'path': str(video)}}
    jobs = {'video_1': job, 'local': {'local_edit': {'source_job_id': 'video_1', 'source_sha256': source_hash}}}
    assert ops.selected_video('video_1', 'local', jobs) == 'local'
    assert ops.selected_video('video_next', 'local', jobs) == 'video_next'
    extracted = run/'assets/video_1-tail-full.png'
    Image.new('RGB', (64, 96), 'olive').save(extracted)
    pin = {'kind': 'actual_tail', 'source_id': 'S001', 'source_job_id': 'video_1', 'source_sha256': source_hash,
           'retention_policy': 'full_source', 'path': str(extracted), 'sha256': ops.sha(extracted)}
    ws.save(run/'dependencies.json', {'shots': [{'shot_id': 'S002', 'relation': 'continuous', 'requires': [pin]}]})
    graph = ops.shot_dependencies(run)
    ledger = (jobs, {'S001': 'video_1'})
    recipe = {'role': 'video', 'shot_id': 'S002', 'input_hashes': {'input_image': [pin['sha256']]}}
    def require():
        ops.require_continuity(cfg, run, recipe, graph=graph, ledger=ledger)
    with pytest.raises(ValueError, match='完成内容检查'):
        require()
    ws.save(run/'reviews/video_1.json', {'source_sha256': source_hash, 'outcome': 'pass'})
    require()
    jobs['video_1']['asset']['sha256'] = 'new'
    with pytest.raises(ValueError, match='完成内容检查'):
        require()
    assert ops.selected_video('video_1', 'local', jobs) == 'video_1'


def test_budget_exhaustion_drains_received_jobs_without_more_quotes(workspace, monkeypatch):
    ws,run,client = workspace
    client.post('/api/projects/fixture/reviews',json={'items':[choice(client),choice(client,'S002')]})
    cfg=ws.ops.load_settings(run/'settings.json')
    cfg['execution']['budget_credits']=100
    ws.save(run/'settings.json',cfg)
    (run/'PAUSE_REQUESTED').unlink()
    first=ws.ops.request_data(cfg,ws.ops.read(run/'requests/S001.json'),run/'requests')
    file=run/'jobs/video_S001.json'
    job={'recipe':first,'status':'submitted','task_id':'in-flight','reserved_credits':70}
    ws.save(file,job)
    calls=[]
    monkeypatch.setattr(ws.ops,'poll_job',lambda *args:calls.append('poll'))
    def over_budget(*args, **kwargs):
        calls.append('quote')
        raise ValueError('Media credit budget exceeded')
    monkeypatch.setattr(ws.ops,'submit_job',over_budget)
    phase,message,stop=production.cycle(ws,run)
    assert phase=='waiting_budget' and not stop
    production.write_state(ws,run,production.state(ws,run),phase,message,desired='running')
    assert not production.cycle(ws,run)[2]
    assert calls==['poll','quote','poll']
    ws.save(file,{**job,'status':'succeeded','asset':{'sha256':'fixture'}})
    assert production.cycle(ws,run)[2]
    assert calls==['poll','quote','poll']


def test_concurrent_material_submission_is_polled_without_resubmitting(workspace, monkeypatch):
    ws, run, _ = workspace
    (run / 'PAUSE_REQUESTED').unlink()
    pending = {'recipe': {'job_id': 'corrected-frame', 'role': 'edit', 'shot_id': 'S001'},
               'status': 'submitting', 'task_id': None, 'reserved_credits': 40}
    job = run / 'jobs/corrected-frame.json'
    with ws.lock(run):
        ws.save(job, pending)
        phase, _, stop = production.cycle(ws, run)
        assert phase == 'synchronizing' and not stop
    assert production.cycle(ws, run)[0] == 'attention'  # An abandoned submission requires reconciliation.
    ws.save(job, {**pending, 'status': 'submitted', 'task_id': 'receipt'})
    polled = []
    monkeypatch.setattr(ws.ops, 'poll_job', lambda _, directory, jid: polled.append(jid))
    production.cycle(ws, run)
    assert polled == ['corrected-frame']


def test_graph_preserves_authored_and_object_dependencies_across_an_insert(workspace):
    ws, run, _ = workspace
    independent = {'relation': 'independent', 'confidence': 'high', 'reason': '另一个时间的独立画面'}
    def prop(start, end):
        return {'prop_id': 'CARD', 'start_state_id': start, 'end_state_id': end,
                'start_description': start, 'end_description': end}
    board = {'playback_order': ['A', 'C', 'B', 'D'], 'shots': [
        {'shot_id': 'A', 'scene_id': 'room', 'objects': [prop('hand', 'desk')]},
        {'shot_id': 'B', 'scene_id': 'room', 'objects': [prop('desk', 'desk')], 'generation_plan': independent},
        {'shot_id': 'C', 'scene_id': 'hall', 'generation_plan': independent},
        {'shot_id': 'D', 'scene_id': 'room', 'generation_plan': {**independent, 'confidence': 'low'}}]}
    ws.save(run/'dependencies.json', {'shots': [{'shot_id': 'D', 'relation': 'independent', 'reason': '旧版计划'},
                                               {'shot_id': 'retired', 'relation': 'independent'}]})
    graph = ws.ops.shot_dependencies(run, board)
    assert list(graph) == board['playback_order']
    assert not graph['C']['requires']
    assert [(r['source_id'], r['kind']) for r in graph['B']['requires']] == [('A', 'actual_tail')]
    assert [r['source_id'] for r in graph['D']['requires']] == ['B']
    board['shots'][2]['generation_plan'] = {'confidence': 'high', 'relation': 'continuous', 'reason': 'bad ref',
        'references': [{'kind': 'tail', 'shot_id': 'D', 'reason': '未来镜头'}]}
    with pytest.raises(ValueError, match='更早'):
        ws.ops.shot_dependencies(run, board)


def test_serial_branch_waits_for_actual_tail_while_an_independent_shot_can_proceed(workspace, monkeypatch):
    ws, run, client = workspace
    ops = ws.ops
    for path in (run / 'requests').glob('*.json'):
        path.unlink()
    board = ops.read(run/'storyboard.json')
    a, b = board['shots']
    a.update(start_state='卡在手上', camera='中景', characters=['P1', 'P2'])
    b.update(start_state='卡放在桌上', camera='反打', characters=['P2', 'P1'],
             target_end_frame={'enabled': True, 'prompt': '卡被拿起'})
    b.pop('generation_plan')  # The board from the reported regression omitted all dependency declarations.
    def dependent(sid):
        return {'relation': 'continuous', 'confidence': 'high', 'reason': '接回同一张桌上的卡',
                'references': [{'shot_id': sid, 'kind': 'tail', 'reason': '持卡人变化'}]}
    board['shots'] += [dict(a, shot_id='S003'), dict(a, shot_id='S004', generation_plan=dependent('S001')),
                       dict(a, shot_id='S005', generation_plan=dependent('S002'))]
    ws.save(run/'storyboard.json', board)
    ws.save(run/'dependencies.json', {'shots': [{'shot_id': 'S001', 'relation': 'shared_reference',
            'reason': '复用办公室布景', 'requires': [{'kind': 'reference_image', 'source_id': 'room'}]}]})
    bible = ops.read(run/'bible.json')
    bible['characters']['P2'] = {'name': '另一位人物'}
    for entry, name, color in [(bible['scenes'][0], 'room', 'tan'), (bible['characters']['P1'], 'cast', 'navy'),
                               (bible['characters']['P2'], 'cast-two', 'maroon')]:
        path = run/'assets'/f'{name}.png'
        Image.new('RGB', (64, 96), color).save(path)
        entry['reference_images'] = [str(path)]
    ws.save(run/'bible.json', bible)
    original_submit = ops.submit_job
    calls = []
    def submit(cfg, directory, req, base, *, execute):
        recipe = ops.request_data(cfg, req, base)
        ops.require_continuity(cfg, directory, recipe)
        job = {'recipe': recipe, 'status': 'submitted', 'reserved_credits': 1}
        jid = req['job_id']
        if req['role'] == 'edit':
            path = run/'assets'/f'{jid}.png'
            Image.new('RGB', (64, 96), '#' + ops.fingerprint(jid)[:6]).save(path)
            job.update(status='succeeded', asset={'path': str(path), 'sha256': ops.sha(path)})
        else:
            assert req['role'] == 'video'
            ops.require_material_approval(cfg, run, recipe)
        calls.append(jid)
        ws.save(run/'jobs'/f'{jid}.json', job)
        return job
    monkeypatch.setattr(production, 'launch', lambda *_: None)
    monkeypatch.setattr(ops, 'submit_job', submit)
    monkeypatch.setattr(ops, 'poll_job', lambda *_: None)
    url = '/api/projects/fixture'
    assert client.post(url + '/start', json={'authorized_budget_credits': 900}).status_code == 200
    production.cycle(ws, run)
    assert calls == ['prep-S001-v1', 'prep-S003-v1']
    cfg = ops.load_settings(run/'settings.json')
    raw = {'role': 'edit', 'shot_id': 'S002', 'job_id': 'premature', 'prompt': '反打', 'inputs': {'input_image': ['assets/room.png']}}
    with pytest.raises(ValueError, match='等待 S001'):
        original_submit(cfg, run, raw, run, execute=True)
    assert not (run/'jobs/premature.json').exists()
    (run/'prompts').mkdir()
    def write_prompt(sid):
        (run/'prompts'/f'{sid}-h3.txt').write_text('Performance from the prepared shot image.', encoding='utf-8')
    write_prompt('S001')
    write_prompt('S003')
    production.cycle(ws, run)
    assert ws.ops.read(run/'queue.json')['ready'] == ['S001', 'S003']
    assert client.post(url + '/reviews', json={'items': [choice(client)]}).status_code == 200
    production.cycle(ws, run)
    assert calls[-1] == 'video-S001-v1'
    from video_harness.media import asset_record
    clip = run/'assets/source.mp4'
    subprocess.run([cfg['media']['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=c=green:s=64x96:r=10:d=0.6', '-f', 'lavfi', '-i', 'anullsrc=d=0.6',
                    '-c:v', 'libx264', '-c:a', 'aac', str(clip)], check=True, timeout=30)
    asset = asset_record(run, clip, 'video', ops.technical(cfg))
    def downloaded(jid, sid, review=True):
        req = ops.read(run/'requests'/f'{sid}.json')
        ws.save(run/'jobs'/f'{jid}.json', {'recipe': ops.request_data(cfg, req, run/'requests'),
                                          'status': 'succeeded', 'asset': asset})
        if review:
            ws.save(run/'reviews'/f'{jid}.json', {'outcome': 'pass', 'source_sha256': asset['sha256']})
    downloaded('video-S001-v1', 'S001', review=False)
    ws.save(run/'inspection/video-S001-v1/packet.json', {'source_sha256': asset['sha256']})
    production.cycle(ws, run)
    assert calls == ['prep-S001-v1', 'prep-S003-v1', 'video-S001-v1']
    assert not (run/'assets/video-S001-v1-tail-full.png').exists()
    downloaded('video-S001-v1', 'S001')
    for _ in range(2):
        production.cycle(ws, run)
    first = ops.read(run/'frames/S002.json')['first']
    refs = {v['kind']: v for v in first['sources']}
    assert [p['character_ids'] for p in refs['cast_reference']['portrait_sources']] == [['P1'], ['P2']]
    assert refs['cast_reference']['sha256'] == next(v['sha256'] for v in ops.read(run/'frames/S001.json')['first']['sources']
                                                   if v['kind'] == 'cast_reference')
    assert refs['actual_tail']['source_job_id'] == 'video-S001-v1' and refs['actual_tail']['retention_policy'] == 'full_source'
    assert refs['actual_tail']['sha256'] == ops.sha(run/'assets/video-S001-v1-tail-full.png')
    assert refs['actual_tail']['sha256'] != first['sha256']
    edit = ops.read(run/'jobs/prep-S002-v1.json')['recipe']
    assert edit['input_hashes'][edit['model']['media_field']] == [
        refs[k]['sha256'] for k in ('actual_tail', 'scene_reference', 'cast_reference')]
    assert (run/'frames/S004.json').is_file() and not (run/'frames/S005.json').is_file()
    write_prompt('S002')
    production.cycle(ws, run)
    b_req = ops.read(run/'requests/S002.json')
    b_recipe = ops.request_data(cfg, b_req, run/'requests')
    assert b_recipe['input_hashes']['input_image'] == [first['sha256']]
    assert b_recipe['input_hashes']['input_last_frame'] == [ops.read(run/'frames/S002.json')['target_end']['sha256']]
    assert client.post(url + '/reviews', json={'items': [choice(client, 'S002')]}).status_code == 200
    production.cycle(ws, run)
    assert calls.count('video-S002-v1') == 1
    downloaded('video-S002-v1', 'S002')
    # Even identical replacement pixels are a different accepted source. Descendants must rebind.
    a_req = ops.read(run/'requests/S001.json')
    ws.save(run/'requests/S001.json', {**a_req, 'job_id': 'video-S001-v2'})
    downloaded('video-S001-v2', 'S001')
    ops.tail(cfg, run, 'video-S001-v2')
    with pytest.raises(ValueError, match='替换'):
        ops.require_continuity(cfg, run, b_recipe)
    with pytest.raises(ValueError, match='替换'):
        original_submit(cfg, run, {**b_req, 'job_id': 'outdated-video'}, run/'requests', execute=True)
    assert not (run/'jobs/outdated-video.json').exists()
    with pytest.raises(ValueError, match='替换'):
        ops.assemble(cfg, run, [{'job_id': 'video-S001-v2'}, {'job_id': 'video-S002-v1'}])
    ws.save(run/'requests/S002.json', {**b_req, 'job_id': 'outdated-video'})
    stale = next(s for s in client.get(url).json()['shots'] if s['id'] == 'S002')
    assert not stale['reviewable'] and stale['material_review']['state'] == 'outdated'
    assert '替换' in stale['preparation_error']
    ws.save(run/'requests/S002.json', b_req)
    with pytest.raises(ValueError, match='替换'):
        ops.dependency_sources(cfg, run, ops.shot_dependencies(run)['S005'], *ops.current_videos(run),
                               graph=ops.shot_dependencies(run))
    assert sum(j.startswith('video-') for j in calls) == 2
