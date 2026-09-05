"""Synthetic end-to-end repair: two false SUBs, picks, XP and safe replay."""
from datetime import UTC, datetime

import pytest

from scripts import repair_paris_result_methods as repair
from app.modules.missions.application.bout_evaluation import BoutResultMissionEvaluator, EvaluateBoutResultCommand
from app.modules.missions.catalog import load_card_catalog
from app.modules.missions.indexes import apply_mission_indexes
from tests.integration.test_bout_mission_evaluation import canonical_bout, canonical_slot, assignment


@pytest.mark.asyncio
async def test_false_submission_completion_is_reversed_without_changing_predictions(test_db):
    await apply_mission_indexes(test_db)
    event_id = repair.EVENT_ID
    now = datetime.now(UTC)
    targets = [{'bout_id': i, 'matchup_revision': 1} for i in range(101, 107)]
    await test_db.events.insert_one({'id': event_id, 'espn_event_id': repair.ESPN_ID,
        'date': now, 'status': 'scheduled', 'card_data_v1': {'structure_revision': 1,
        'current_eligibility': {'card_snapshot_revision': 1, 'eligible_targets': targets,
        'denominator': 6, 'fingerprint': 'sha256:repair-test-eligibility'}}})
    feed = {'competitions': []}
    for i in range(101, 107):
        bout = canonical_bout(i)
        bout.update(event_id=event_id, espn_competition_id=str(i))
        bout['card_data_v1']['event_id'] = event_id
        if i <= 103:
            bout['result'] = {'winner': 'red', 'winner_name': f'Red {i}', 'outcome': 'red',
                'method': 'SUB', 'round': 3, 'time': '5:00', 'source': 'card_data_v1'}
            bout['status'] = 'completed'
            bout['card_data_v1'].update(status='completed', result_revision=1, result={
                'revision': 1, 'status': 'final', 'outcome': 'red_win',
                'winner_fighter_id': f'fighter-{i}-red', 'method_family': 'submission',
                'ending_round': 3, 'ending_time_seconds': 300})
            feed['competitions'].append({'id': str(i),
                'status': {'type': {'completed': True}, 'period': 3, 'displayClock': '5:00'},
                'competitors': [{'order': 1, 'winner': True, 'athlete': {'displayName': f'Red {i}'}},
                                {'order': 2, 'winner': False, 'athlete': {'displayName': f'Blue {i}'}}],
                'details': [{'type': {'text': 'Unofficial Winner Decision' if i < 103 else 'Unofficial Winner Submission'}},
                            {'type': {'text': 'Submission Attempt'}}]})
        await test_db.bouts.insert_one(bout)
        slot = canonical_slot(i, order=i - 100)
        slot.update(event_id=event_id, _id=f'{event_id}:{i}')
        await test_db.event_card_slots.insert_one(slot)
    await test_db.users.insert_one({'_id': 'jose', 'total_points': 1})
    prediction = {'_id': 'repair-pick', 'user_id': 'jose', 'bout_id': 101, 'event_id': event_id,
        'picked_fighter_name': 'Red 101', 'picked_fighter_id': 'fighter-101-red',
        'picked_method': 'DEC', 'points_awarded': 1, 'is_correct': True}
    await test_db.picks.insert_one(prediction)
    mission = assignment(load_card_catalog().get('CARD-V2-H-017'), assignment_id='repair-trio', selection={})
    mission['event_id'] = event_id
    mission['eligibility_snapshot'].update(eligible_targets=targets, denominator=6)
    await test_db.mission_assignments.insert_one(mission)
    evaluator = BoutResultMissionEvaluator(test_db)
    initial = await evaluator.evaluate(EvaluateBoutResultCommand(event_id, 103, 1))
    assert initial.assignments[0].status.value == 'COMPLETED'
    assert await evaluator.xp.total_for_user('jose') == 6

    records = await repair.snapshot(test_db)
    repairs, report = await repair.preview(records, feed)
    assert len(repairs) == 2 and not report['blockers']
    assert report['missions'][0]['progress'] == '1 / 3 submissions'
    assert report['points_delta'] == 1
    await repair.apply_primary(test_db, records, repairs)
    await repair.reconcile(test_db, repairs)
    fixed = await test_db.mission_assignments.find_one({'_id': 'repair-trio'})
    assert fixed['status'] == 'ACTIVE'
    assert fixed['progress']['progress']['text'] == '1 / 3 submissions'
    assert await evaluator.xp.total_for_user('jose') == 0
    fixed_pick = await test_db.picks.find_one({'_id': prediction['_id']})
    assert fixed_pick['points_awarded'] == 2
    assert fixed_pick['picked_method'] == 'DEC'
    assert fixed_pick['picked_fighter_id'] == prediction['picked_fighter_id']
    assert await test_db.admin_card_commands.count_documents({'event_id': event_id}) == 2
    assert await test_db.mission_celebrations.count_documents({'status': 'PENDING', 'kind': 'MISSION_COMPLETED'}) == 0
    entries = await test_db.mission_xp_ledger.count_documents({})
    await repair.reconcile(test_db, repairs)
    assert await test_db.mission_xp_ledger.count_documents({}) == entries


@pytest.mark.asyncio
async def test_stale_result_rolls_back_all_prior_result_updates(test_db):
    records = {'events': [], 'bouts': [], 'event_card_slots': [], 'picks': [],
               'mission_assignments': [], 'admin_card_commands': []}
    before = {'_id': 'first', 'id': 101, 'event_id': repair.EVENT_ID,
              'result': {'method': 'SUB'}, 'card_data_v1': {'result_revision': 1}}
    await test_db.bouts.insert_one(before.copy())
    changes = [{'bout_id': 101, 'before': before,
                'result': {'method': 'DEC', 'outcome': 'red', 'winner': 'red'},
                'fields': {'card_data_v1.result_revision': 2, 'card_data_v1.result': {
                    'outcome': 'red_win', 'winner_fighter_id': 'test', 'method_family': 'decision',
                    'method_detail': 'Decision', 'ending_round': 3, 'ending_time_seconds': 300}}}]
    import copy
    missing = copy.deepcopy(changes[0])
    missing.update(bout_id=102)
    missing['before'].update(_id='missing', id=102)
    with pytest.raises(ValueError, match='Stale result'):
        await repair.apply_primary(test_db, records, changes + [missing])
    assert (await test_db.bouts.find_one({'_id': 'first'}))['result']['method'] == 'SUB'
    assert await test_db.admin_card_commands.count_documents({}) == 0
