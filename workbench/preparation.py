"""Prepare real set, cast and shot images in the order of the story dependencies."""
from __future__ import annotations

from pathlib import Path

PLAN = 'workbench-assets.json'
FAILED = {'remote_failed', 'rejected', 'confirmed_not_submitted'}


def eligible(ws, run, board, requests, jobs):
    if (run / PLAN).is_file():
        return True
    if requests or jobs or any((run / 'frames').glob('*.json')):
        return False
    shots = board.get('shots', [])
    return bool(shots and (run / 'bible.json').is_file() and all(
        shot.get('shot_id') and shot.get('start_state') and shot.get('camera') for shot in shots))


def indexed(items):
    if isinstance(items, dict):
        return {key: value if isinstance(value, dict) else {'description': str(value)} for key, value in items.items()}
    return {v.get('id') or v.get('scene_id') or v.get('character_id') or v.get('speaker_id'): v
            for v in items if isinstance(v, dict)}


def scene_for(shot, scenes):
    if shot.get('scene_id'):
        return shot['scene_id']
    if shot.get('scene_unit_id') in scenes:
        return shot['scene_unit_id']
    if len(scenes) == 1:
        return next(iter(scenes))
    if shot.get('scene_unit_id') and not scenes:
        return shot['scene_unit_id']
    if not scenes:
        return 'default'
    raise ValueError(f"{shot['shot_id']} 需要注明采用哪个场景的布局。")


def make_plan(ws, run, config, board):
    bible = ws.ops.read(run / 'bible.json')
    cast = indexed(bible.get('characters', {}))
    scenes = indexed(bible.get('scenes', []))
    style = config['production']['visual_style']
    portraits = {key: {'role': 'image', 'job_id': 'prep-cast-' + ws.ops.fingerprint(key)[:12],
                       'prompt': f'{style}。{person.get("name", key)}的单人定妆图，面部清晰，展示发型和服装。'
                                 f'{person.get("appearance") or person.get("description", "")}。无字幕或水印。'}
                 for key, person in cast.items()}
    edits, sets = {}, {}
    for shot in board['shots']:
        sid = ws.ops.identifier(shot['shot_id'])
        scene = scene_for(shot, scenes)
        info = scenes.get(scene, {})
        if scene not in sets:
            layout = info.get('image_prompt') or info.get('description') or str(scene)
            anchors = '、'.join(map(str, info.get('anchors') or []))
            sets[scene] = {'role': 'image', 'job_id': 'prep-scene-' + ws.ops.fingerprint(scene)[:12],
                'prompt': f'{style}。作为本场固定空间基准，拍摄同一处无人空场全景。{layout}。{anchors}。'
                          '交代家具的轮廓与尺寸比例、门窗方位、通道、光源和固定陈设。留出表演空间，活动道具的状态由各镜首帧确定。'}
        people = shot.get('characters', list(cast))
        ids = [person.get('character_id') if isinstance(person, dict) else person for person in people]
        names = [f"{key} {cast.get(key, {}).get('name', key)}" for key in ids]
        edits[sid] = {'job_id': f'prep-{sid}-v1', 'role': 'edit', 'shot_id': sid,
                      'prompt': f'{style}。制作一张连续实景首帧。依照所附场景图的家具尺寸、方位和光线，以及人物定妆。'
                      f'如附前镜实拍末帧，继承画内角色站位、物品数量、位置和归属，再转成本镜机位。'
                      f'本镜人物：{"、".join(names)}。构图：{shot["camera"]}。'
                      f'动作发生前的瞬间：{shot["start_state"]}。无拼贴、字幕、水印。'}
    return {'version': 2, 'characters': portraits, 'scenes': sets, 'shots': edits,
            'board_hash': ws.ops.fingerprint(board), 'bible_hash': ws.ops.fingerprint(bible)}


def advance(ws, run, config, board, requests, jobs):
    ops = ws.ops
    plan = ops.read(run / PLAN)
    shots = {s['shot_id']: s for s in board['shots']}
    bible = ops.read(run / 'bible.json')
    if (plan.get('version') not in {1, 2} or set(plan.get('shots', {})) != set(shots)
            or (plan.get('board_hash') and plan['board_hash'] != ops.fingerprint(board))
            or (plan.get('bible_hash') and plan['bible_hash'] != ops.fingerprint(bible))):
        return 'waiting_input', '人物、分镜或素材计划版本不一致，请先更新计划。', True
    # Existing authored prompts and IDs remain intact. Only unprepared shots use the new set references.
    if plan['version'] == 1:
        plan = {**make_plan(ws, run, config, board), 'characters': {}, **plan}
    graph = ops.shot_dependencies(run, board)
    by_id, selected = ops.current_videos(run, jobs, requests)
    available = ops.concurrency_status(config, jobs)['available']['image']
    scenes, cast = indexed(bible.get('scenes', [])), indexed(bible.get('characters', {}))
    index_path = run / 'reference-index.json'
    initial_index = ops.read(index_path) if index_path.is_file() else {}
    index = dict(initial_index)

    def ensure_image(request):
        nonlocal available
        jid = ops.identifier(request['job_id'])
        job = by_id.get(jid)
        if job and job['status'] in FAILED:
            request = {**request, 'retry_of': jid, 'job_id': jid + '-retry1'}
            job = by_id.get(request['job_id'])
            if job and job['status'] in FAILED:
                raise ValueError('图片任务连续两次失败，请检查 ' + request['job_id'])
        if job is None and available:
            ops.pause_check(run)
            job = ops.submit_job(config, run, request, run, execute=True)
            by_id[request['job_id']] = job
            available -= 1
        if not job or job['status'] != 'succeeded':
            return None
        current = ops.request_data(config, request, run)
        if any(job['recipe'].get(k) != current[k] for k in ('role', 'shot_id', 'prompt', 'input_hashes', 'model', 'provider')):
            raise ValueError(f"{request['job_id']} 的素材或依赖已改变，请保留旧候选并准备新版")
        asset = job['asset']
        path = run / asset['path']
        if not path.is_file() or ops.sha(path) != asset['sha256']:
            raise ValueError('已生成的图片缺失或内容已变化：' + request['job_id'])
        return {'path': str(path.resolve()), 'sha256': asset['sha256'], 'source_job_id': job['recipe']['job_id']}

    def existing_refs(info, key):
        refs = []
        entries = info.get('reference_images') or ([index[key]] if key in index else [])
        for raw in entries:
            ref = raw if isinstance(raw, dict) else {'path': raw}
            path = (run / ref['path']).resolve()
            if not path.is_file() or (ref.get('sha256') and ops.sha(path) != ref['sha256']):
                raise ValueError('指定的设定图缺失或版本已变更：' + path.name)
            refs.append({**ref, 'path': str(path), 'sha256': ops.sha(path)})
        return refs

    def reference(request):
        if request.get('role') != 'image' or request.get('inputs') or request.get('prompt_file'):
            raise ValueError('设定图任务需要文字生图提示词。')
        ref = ensure_image(request)
        if ref:
            index[request['job_id']] = ref
        return ref

    def save_references():
        nonlocal initial_index
        if index == initial_index:
            return
        with ws.lock(run):
            latest = ops.read(index_path) if index_path.is_file() else {}
            ws.save(index_path, {**latest, **{k: v for k, v in index.items() if v != initial_index.get(k)}})
        initial_index = dict(index)

    ready, waiting = [], {}
    for sid, row in graph.items():
        shot = shots[sid]
        frame_file = run / 'frames' / (sid + '.json')
        frame = ops.read(frame_file) if frame_file.is_file() else {}
        if sid in requests:
            path, request = requests[sid]
            try:
                recipe = ops.request_data(config, request, path.parent)
                ops.require_continuity(config, run, recipe, graph=graph, ledger=(by_id, selected))
            except (ValueError, FileNotFoundError) as exc:
                # A changed source invalidates an unsubmitted managed rewrite and its approval.
                if not request.get('rewriter_source_file') or request['job_id'] in by_id:
                    waiting[sid] = str(exc)
                    continue
            else:
                ready.append(sid)
                continue
        elif selected.get(sid) in by_id:
            ready.append(sid)
            continue
        first = frame.get('first')
        if not first:
            scene = scene_for(shot, scenes)
            if scene not in plan.get('scenes', {}):
                raise ValueError(f'{sid} 缺少场景 {scene} 的设定图计划')
            scene_refs = existing_refs(scenes.get(scene, {}), scene) or [reference(plan['scenes'][scene])]
            if scene_refs[0]:
                index[scene] = scene_refs[0]
            people = shot.get('characters', list(cast))
            portraits = {}
            cast_ready = True
            for key in people:
                cid = key.get('character_id') if isinstance(key, dict) else key
                refs = existing_refs(cast.get(cid, {}), cid)
                if not refs:
                    portrait = (plan.get('characters') or {}).get(cid) or plan.get('reference')
                    if not portrait:
                        raise ValueError(f'{sid} 缺少人物 {cid} 的定妆资料')
                    refs = [reference(portrait)]
                if refs[0]:
                    index[cid] = refs[0]
                    for ref in refs:
                        item = portraits.setdefault(ref['sha256'], {**ref, 'character_ids': []})
                        item['character_ids'].append(cid)
                else:
                    cast_ready = False
            save_references()
            if not all(scene_refs) or not cast_ready:
                waiting[sid] = '等待本场空间或人物参考图'
                continue
            try:
                sources = ops.dependency_sources(config, run, row, by_id, selected, extract=True, graph=graph)
            except ValueError as exc:
                waiting[sid] = str(exc)
                continue
            # Combine available individual portraits when several are needed by one shot.
            character_refs = sorted(portraits.values(), key=lambda v: (sorted(v['character_ids']), v['sha256']))
            if len(character_refs) > 1:
                from video_harness.media import identity_board
                labels = [', '.join(sorted(ref['character_ids'])) for ref in character_refs]
                key = ops.fingerprint([(label, ref['sha256']) for label, ref in zip(labels, character_refs)])[:16]
                sheet = run / 'assets' / ('cast-' + key + '.png')
                if not sheet.is_file():
                    identity_board([ref['path'] for ref in character_refs], labels, sheet)
                character_refs = [{'path': str(sheet), 'sha256': ops.sha(sheet),
                                   'portrait_sources': character_refs}]
            references = sources + [dict(v, kind='scene_reference') for v in scene_refs] + [
                dict(v, kind='cast_reference') for v in character_refs]
            paths = list(dict.fromkeys(ref['path'] for ref in references))
            request = plan['shots'][sid]
            if request.get('role') != 'edit' or request.get('shot_id') != sid or request.get('inputs') or request.get('prompt_file'):
                raise ValueError(f'{sid} 的首帧计划与分镜不匹配')
            asset = ensure_image({**request, 'inputs': {config['models']['edit']['media_field']: paths}})
            if not asset:
                waiting[sid] = '等待首帧生成'
                continue
            first = {**asset, 'kind': 'generated', 'sources': references}
            frame = {**frame, 'shot_id': sid, 'first': first, 'target_end': frame.get('target_end')}
            ws.save(frame_file, frame)

        opening = (run / first['path']).resolve()
        if not opening.is_file() or ops.sha(opening) != first['sha256']:
            waiting[sid] = '已有首帧缺失或已变更'
            continue
        target = frame.get('target_end')
        target_plan = shot.get('target_end_frame') or {}
        if isinstance(target_plan, dict) and target_plan.get('enabled') and not target:
            if not target_plan.get('prompt'):
                waiting[sid] = '等待选定的目标尾帧'
                continue
            asset = ensure_image({'role': 'edit', 'job_id': f'prep-{sid}-end-v1', 'shot_id': sid,
                                  'prompt': target_plan['prompt'], 'inputs': {config['models']['edit']['media_field']: [str(opening)]}})
            if not asset:
                waiting[sid] = '等待目标尾帧生成'
                continue
            target = {**asset, 'kind': 'target_end'}
            ws.save(frame_file, {**frame, 'target_end': target})
        if 'scheduling' in config['production']:
            frame_recipe = {'role': 'video', 'shot_id': sid, 'input_hashes': {'input_image': [first['sha256']],
                            'input_last_frame': [target['sha256']] if target else []}}
            try:
                ops.require_prechecks(config, run, frame_recipe, ('frame',))
            except ValueError as exc:
                waiting[sid] = str(exc)
                continue
        prompt = run / 'prompts' / (sid + '-h3.txt')
        if not prompt.is_file() or not prompt.read_text(encoding='utf-8-sig').strip():
            waiting[sid] = '首帧已完成，等待 AI 按实际素材编写最终提示词'
            continue
        inputs = {'input_image': [str(opening)]}
        if target:
            target_path = (run / target['path']).resolve()
            if not target_path.is_file() or ops.sha(target_path) != target.get('sha256'):
                waiting[sid] = '目标尾帧已变更'
                continue
            inputs['input_last_frame'] = [str(target_path)]
        request = {'role': 'video', 'shot_id': sid, 'job_id': 'video-' + sid + '-v1',
                   'duration_ms': shot['duration_ms'], 'prompt_file': '../prompts/' + prompt.name, 'inputs': inputs}
        if ops.prompt_mode(config) == 'local':
            result = ops.rewrite(config, run, prompt, shot['duration_ms'], opening, target_path if target else None)
            request['rewriter_source_file'] = request.pop('prompt_file')
            request['rewriter_record'] = result['record_path']
        path = run / 'requests' / (sid + '.json')
        try:
            ops.require_continuity(config, run, ops.request_data(config, request, path.parent), graph=graph, ledger=(by_id, selected))
        except ValueError as exc:
            waiting[sid] = str(exc)
            continue
        ws.save(path, request)
        ready.append(sid)

    with ws.lock(run):
        path = run / 'queue.json'
        q = ops.read(path) if path.is_file() else {}
        pending = [{'shot_id': sid, 'reason': reason} for sid, reason in waiting.items()]
        if q.get('ready') != ready or q.get('waiting') != pending:
            ws.save(path, {**q, 'ready': ready, 'waiting': pending})

    if waiting:
        has_images = any(j['recipe']['role'] != 'video' and j['status'] in {'submitted','waiting_remote'} for j in by_id.values())
        if has_images:
            count = sum((run / 'frames' / (sid + '.json')).is_file() for sid in shots)
            return 'preparing_assets', f'分镜首帧已完成 {count}/{len(shots)}，正在制作就绪素材。', False
        sid = next(iter(waiting))
        phase = 'waiting_prompts' if '提示词' in waiting[sid] else 'waiting_materials'
        return phase, f'{sid}：{waiting[sid]}', False
    return None
