"""Scoped Paris incident repair. Default: read-only audit; apply needs its hash.

No credentials or user records are printed. Recovery preimages are encrypted
outside the repository before an approved write. Primary results, durable Admin
overrides and pick/stat recalculation commit in one transaction. Existing mission
services then compensate incorrect awards and cancel their celebrations.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'ufc-picks-scraper'))

from bson import json_util
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from pymongo import AsyncMongoClient
from pymongo.read_concern import ReadConcern
import requests

from tapology_scraper.espn_etl import map_competitors_to_corners, transform_result
from app.modules.missions.application.bout_evaluation import (
    BoutResultMissionEvaluator, MissionEvaluationContextBuilder,
)
from app.modules.missions.application.orchestration import (
    MissionTriggerService, project_admin_result_to_canonical,
)
from app.modules.missions.domain.definitions import validate_mission_definition
from app.services.admin_card_commands import record_admin_command, result_values
from app.services.points_service import PointsService

EVENT_ID = 144513
ESPN_ID = '600059993'
ACTOR = 'Jose-approved-paris-result-repair'
REASON = 'Paris 2026-09-05: submission attempts were misread as winning methods'
PRIVATE_ROOT = Path.home() / '.codex' / 'private' / 'ufc-picks-repairs'


def digest(value):
    return hashlib.sha256(json_util.dumps(value, sort_keys=True).encode()).hexdigest()


class SnapshotCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, **kwargs):
        return SnapshotCollection([row for row in self.rows if all(row.get(k) == v for k, v in query.items())])

    async def find_one(self, query, **kwargs):
        return next(iter(self.find(query).rows), None)

    async def to_list(self, length=None):
        return copy.deepcopy(self.rows)


class SnapshotDB:
    """Only the read methods needed by the pure mission context builder."""
    def __init__(self, records):
        self.records = records

    def __getitem__(self, name):
        return SnapshotCollection(self.records[name])


class SessionCollection:
    def __init__(self, collection, session):
        self.collection, self.session = collection, session

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            return getattr(self.collection, name)(*args, **{**kwargs, 'session': self.session})
        return invoke


class SessionDB:
    def __init__(self, db, session):
        self.db, self.session = db, session

    def __getitem__(self, name):
        return SessionCollection(self.db[name], self.session)


def fetch_card():
    response = requests.get(
        'https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard',
        params={'dates': '20260905'}, timeout=25,
        headers={'User-Agent': 'UFC-Picks/1.0 (+https://ufcpicks.app) python-httpx/0.27.0'},
    )
    response.raise_for_status()
    return next(e for e in response.json()['events'] if str(e['id']) == ESPN_ID)


async def snapshot(db):
    records = {}
    for name in ('events', 'bouts', 'event_card_slots', 'picks', 'mission_assignments', 'admin_card_commands'):
        query = {'id': EVENT_ID} if name == 'events' else {'event_id': EVENT_ID}
        records[name] = await db[name].find(query).to_list(length=None)
    if len(records['events']) != 1 or str(records['events'][0].get('espn_event_id')) != ESPN_ID:
        raise ValueError('Event identity mismatch')
    return records


async def preview(records, card):
    corrected = copy.deepcopy(records)
    competitions = {str(c['id']): c for c in card['competitions']}
    repairs, blockers = [], []
    for bout in corrected['bouts']:
        current = bout.get('result') or {}
        if not current:
            continue  # This incident repair never imports a new fight result.
        competition = competitions.get(str(bout.get('espn_competition_id')))
        if not competition:
            continue
        proposed = transform_result(competition, map_competitors_to_corners(competition, bout))
        if proposed is None:
            blockers.append({'bout_id': bout['id'], 'reason': 'No coherent ESPN result declaration'})
            continue
        if proposed['method'] == current.get('method'):
            continue
        # Scope: method corruption only; no guessed winner/round correction.
        if (proposed['winner'] != current.get('winner') or proposed['round'] != current.get('round')
                or current.get('source') not in {'espn', 'card_data_v1'}
                or any(command.get('kind') == 'result' and command.get('bout_id') == bout['id']
                       for command in records['admin_card_commands'])):
            blockers.append({'bout_id': bout['id'], 'reason': 'Winner, round or authority requires review'})
            continue
        result = {**current, 'method': proposed['method'], 'method_detail': proposed['method_detail']}
        fields = project_admin_result_to_canonical(bout, result)
        if not fields:
            raise ValueError('Missing canonical result identity')
        canonical = fields['card_data_v1.result']
        canonical['method_detail'] = proposed['method_detail']
        canonical['ending_time_seconds'] = (bout['card_data_v1'].get('result') or {}).get('ending_time_seconds')
        repairs.append({'bout_id': bout['id'], 'before': copy.deepcopy(bout), 'result': result, 'fields': fields})
        bout['result'] = result
        for path, value in fields.items():
            bout['card_data_v1'][path.split('.', 1)[1]] = value

    points = PointsService(None)
    pick_changes = 0
    points_delta = 0
    for repair in repairs:
        winner_id = repair['fields']['card_data_v1.result']['winner_fighter_id']
        result = repair['result']
        for pick in corrected['picks']:
            if pick['bout_id'] != repair['bout_id']:
                continue
            score = await points.calculate_points(pick, result.get('winner_name', ''), result['method'], result.get('round'), winner_id)
            is_correct = points.winner_matches(pick, result.get('winner_name', ''), winner_id)
            pick_changes += (score, is_correct) != (pick.get('points_awarded'), pick.get('is_correct'))
            points_delta += score - int(pick.get('points_awarded') or 0)
            pick.update(points_awarded=score, is_correct=is_correct)

    builder = MissionEvaluationContextBuilder(SnapshotDB(corrected))
    missions = []
    for assignment in corrected['mission_assignments']:
        if assignment['status'] == 'VOID':
            continue
        definition = validate_mission_definition(assignment['definition_snapshot'])
        context = await builder.build(assignment, session=None, card_finalized=False)
        resolution = BoutResultMissionEvaluator._resolve(definition, context)
        target = BoutResultMissionEvaluator._assignment_status(resolution.status).value
        missions.append({
            'mission': definition.ui.name, 'before': assignment['status'], 'after': target,
            'progress': resolution.progress.text,
            'xp_to_compensate': int(assignment['xp']) if assignment['status'] == 'COMPLETED' and target != 'COMPLETED' else 0,
        })
    plan_id = digest({'event': EVENT_ID, 'records': records, 'repairs': repairs})
    return repairs, {
        'plan_id': plan_id, 'event_id': EVENT_ID, 'espn_event_id': ESPN_ID,
        'checked_stored_results': sum(bool(b.get('result')) for b in records['bouts']),
        'corrections': [{'bout_id': r['bout_id'], 'before': r['before']['result']['method'], 'after': r['result']['method']} for r in repairs],
        'pick_changes': pick_changes, 'points_delta': points_delta,
        'missions': missions, 'blockers': blockers, 'production_writes': False,
    }


async def save_backup(db, records, repairs, report):
    users = list({p['user_id'] for p in records['picks']} | {m['user_id'] for m in records['mission_assignments']})
    backup = copy.deepcopy(records)
    for name in ('mission_xp_ledger', 'mission_user_progression', 'mission_celebrations', 'mission_evaluation_runs'):
        backup[name] = await db[name].find({'user_id': {'$in': users}} if name != 'mission_evaluation_runs' else {'event_id': EVENT_ID}).to_list(length=None)
    backup['users'] = await db['users'].find({'_id': {'$in': users}}, {
        'total_points': 1, 'picks_total': 1, 'picks_correct': 1, 'perfect_picks': 1, 'accuracy': 1,
    }).to_list(length=None)
    directory = PRIVATE_ROOT / report['plan_id']
    directory.mkdir(parents=True, exist_ok=False)
    key = Fernet.generate_key()
    (directory / 'recovery.key').write_bytes(key)
    payload = {'records': backup, 'repairs': repairs, 'report': report}
    encrypted = Fernet(key).encrypt(json_util.dumps(payload).encode())
    (directory / 'preimages.enc').write_bytes(encrypted)
    assert Fernet(key).decrypt(encrypted) == json_util.dumps(payload).encode()
    return directory


async def apply_primary(db, records, repairs):
    async def transaction(session):
        bound = SessionDB(db, session)
        for repair in repairs:
            before = repair['before']
            fields = repair['fields']
            canonical = fields['card_data_v1.result']
            outcome = await bound['bouts'].update_one(
                {'_id': before['_id'], 'event_id': EVENT_ID, 'result': before['result'],
                 'card_data_v1.result_revision': before['card_data_v1']['result_revision']},
                {'$set': {'result': repair['result'], **fields}},
            )
            if outcome.matched_count != 1:
                raise ValueError('Stale result; transaction aborted')
            await record_admin_command(
                bound, kind='result', event_id=EVENT_ID, bout_id=repair['bout_id'],
                actor_id=ACTOR, reason=REASON,
                values=result_values(
                    outcome=canonical['outcome'], winner_fighter_id=canonical['winner_fighter_id'],
                    method_family=canonical['method_family'], method_detail=canonical['method_detail'],
                    ending_round=canonical['ending_round'], ending_time_seconds=canonical['ending_time_seconds'],
                ),
            )
            await PointsService(bound).calculate_and_assign_points(repair['bout_id'], repair['result'])
    async with db.client.start_session() as session:
        await session.with_transaction(transaction)


async def reconcile(db, repairs):
    trigger = MissionTriggerService(db)
    for repair in repairs:
        result = await trigger.on_bout_result(
            event_id=EVENT_ID, bout_id=repair['bout_id'],
            result_revision=repair['fields']['card_data_v1.result_revision'],
        )
        if result.errors:
            raise RuntimeError('Mission reconciliation needs a retry; encrypted recovery data retained')


async def run(args):
    values = dotenv_values(args.env_file)
    uri = values.get('MONGODB_URI')
    if not uri:
        raise ValueError('MONGODB_URI missing')
    async with AsyncMongoClient(uri, serverSelectionTimeoutMS=10000) as client:
        db = client.get_database('ufc_picks', read_concern=ReadConcern('majority'))
        if args.resume:
            directory = (PRIVATE_ROOT / args.resume).resolve()
            if directory.parent != PRIVATE_ROOT.resolve():
                raise ValueError('Invalid recovery directory')
            payload = json_util.loads(Fernet((directory / 'recovery.key').read_bytes()).decrypt((directory / 'preimages.enc').read_bytes()))
            for repair in payload['repairs']:
                bout = await db.bouts.find_one({'id': repair['bout_id']})
                if bout['card_data_v1']['result'] != repair['fields']['card_data_v1.result']:
                    raise ValueError('Result changed since repair; resume blocked')
            await reconcile(db, payload['repairs'])
            print(json.dumps({'resumed': True, 'event_id': EVENT_ID}))
            return
        card = fetch_card()
        records = await snapshot(db)
        repairs, report = await preview(records, card)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
        if not args.apply_plan:
            return
        if args.apply_plan != report['plan_id'] or report['blockers'] or not repairs:
            raise ValueError('Plan mismatch, blocked plan or no repairs')
        directory = await save_backup(db, records, repairs, report)
        if digest(await snapshot(db)) != digest(records):
            raise ValueError('Card changed during backup; no writes performed')
        await apply_primary(db, records, repairs)
        (directory / 'primary-committed').write_text(datetime.now(UTC).isoformat())
        await reconcile(db, repairs)
        after = await snapshot(db)
        _, verification = await preview(after, card)
        if verification['corrections'] or verification['blockers']:
            raise ValueError('Post-repair verification failed')
        print(json.dumps({'applied': True, 'event_id': EVENT_ID, 'verified': verification}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply-plan')
    action.add_argument('--resume')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        # Driver errors can contain credentials or user documents.
        print(json.dumps({'failed': True, 'error_type': type(error).__name__}))
        raise SystemExit(1)
