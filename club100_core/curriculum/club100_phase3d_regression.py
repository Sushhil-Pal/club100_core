"""Club100 Phase 3D local, read-only regression tests.

Installation (copy this file into the ERP app's club100_core/curriculum directory):
    bench --site club100.local execute club100_core.curriculum.club100_phase3d_regression.run

No real payment, push delivery, enrollments or activity writes are performed.
Run on local development site only.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

MEMBER = 'C100-MEM-00051'
ENROLLMENT = 'C100-ENR-00050'
WORKOUT = 'C100-ENR-00050:2026-10-23:9'


def _check(name, fn, outcomes):
    try:
        info = fn()
        outcomes.append({'test': name, 'status': 'PASS', 'detail': info or ''})
    except Exception as exc:
        outcomes.append({'test': name, 'status': 'FAIL', 'detail': f'{type(exc).__name__}: {exc}'})


def _position_projection():
    import club100_core.curriculum.journey as journey
    doc = frappe.get_doc('Club100 Member Training Journey', MEMBER)
    assert doc.program == 'Everyday Fitness' and doc.level_code == 'L1'
    baseline = str(doc.modified)
    expected = [
        (datetime(2026, 10, 23, 13, 40), (1, 3, 1)),
        (datetime(2026, 10, 23, 14, 15), (1, 3, 2)),
        (datetime(2026, 10, 26, 18, 0), (1, 3, 2)),
    ]
    for timepoint, target in expected:
        with patch.object(journey, 'now_datetime', return_value=timepoint):
            result = journey._summary(doc)
        got = (result['cycleNumber'], result['weekNumber'], result['sessionNumber'])
        assert got == target, (timepoint, got, target)
        assert result['levelCode'] == 'L1'
        assert result.get('positionSource') == 'SelfSchedule'
    assert str(frappe.get_doc('Club100 Member Training Journey', MEMBER).modified) == baseline
    return 'Week 3 sessions 1→2; journey record unchanged'


def _renewal_arithmetic():
    import club100_core.curriculum.self_schedule as schedule
    enrollment = frappe.db.get_value('Club100 Enrollment', ENROLLMENT,
        ['name', 'member', 'program', 'plan', 'start_date', 'end_date'], as_dict=True)
    assert enrollment and enrollment.member == MEMBER
    for prior, ordinal, expected in [
        (20, 0, (1, 6, 1)),
        (48, 0, (2, 1, 1)),
        (20, 2, (1, 6, 3)),
    ]:
        with patch.object(schedule, 'previous_self_slot_count', return_value=prior):
            ctx = schedule.self_curriculum_context(enrollment)
        assert ctx is not None
        result = schedule.curriculum_for_slot(ctx, ordinal, datetime(2026, 10, 26, 18, 30))
        assert result and result['levelCode'] == 'L1'
        got = (result['cycleNumber'], result['weekNumber'], result['sessionNumber'])
        assert got == expected, (prior, ordinal, got, expected)
    return '20/48 prior-slot offsets and third renewed slot'


def _trial_to_paid_history():
    import club100_core.curriculum.self_schedule as schedule
    trial = frappe._dict(name='TEST-TRIAL', member=MEMBER, program='Everyday Fitness',
        plan='Self', pricing_model='Online Plan', start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 14), status='Active', is_trial=1, trial_status='Converted')
    paid = frappe._dict(name='TEST-PAID', member=MEMBER, program='Everyday Fitness',
        plan='Self', pricing_model='Online Plan', start_date=date(2026, 10, 15),
        end_date=date(2026, 11, 14))
    slots = [frappe._dict(weekday=w, start_time=t) for w, t in [
        ('Monday', '18:30:00'), ('Tuesday', '19:30:00'),
        ('Thursday', '20:30:00'), ('Sunday', '17:00:00')]]
    assert paid.start_date == trial.end_date + timedelta(days=1)
    db_mock = Mock(side_effect=[[trial], slots, [], []])
    with patch.object(frappe, 'get_all', db_mock):
        count = schedule.previous_self_slot_count(paid)
    assert db_mock.call_count == 4, db_mock.call_count
    assert count == 8, count
    assert (count // 48 + 1, (count % 48) // 4 + 1, count % 4 + 1) == (1, 3, 1)
    return '14-day trial: 8 scheduled slots; paid starts Week 3 Session 1'


def _conversion_guard():
    from club100_core.api.enrollment import _link_converted_self_trial
    trial = frappe._dict(name='TEST-TRIAL', member=MEMBER, program='Everyday Fitness',
        plan='Self', is_trial=1, end_date=date(2026, 10, 31), converted_enrollment=None)
    paid = SimpleNamespace(name='TEST-PAID', member=MEMBER, program='Everyday Fitness',
        plan='Self', website_source='PWA Commerce', payment_status='Paid',
        origin_trial_enrollment='TEST-TRIAL', start_date=date(2026, 11, 1))
    fake_write = Mock()
    with patch('club100_core.api.enrollment.frappe.db.sql'), \
         patch('club100_core.api.enrollment.frappe.db.get_value', return_value=trial), \
         patch('club100_core.api.enrollment.frappe.db.set_value', fake_write):
        _link_converted_self_trial(paid)
        fake_write.assert_called_once()
        assert fake_write.call_args.args[2] == {
            'trial_status': 'Converted', 'converted_enrollment': 'TEST-PAID'}
        trial.converted_enrollment = paid.name
        _link_converted_self_trial(paid)
        paid.start_date = date(2026, 10, 31)
        try:
            _link_converted_self_trial(paid)
        except Exception:
            pass
        else:
            raise AssertionError('Overlapping dates were allowed')
    return 'Verified-payment link, retry and overlap guard (writes mocked)'


def _activation_dispatch():
    import club100_core.curriculum.journey as journey
    doc = frappe.get_doc('Club100 Enrollment', ENROLLMENT)
    with patch.object(journey, '_initialize', return_value=None) as initializer:
        doc._ensure_active_self_journey()
    initializer.assert_called_once()
    assert initializer.call_args.args[0] == MEMBER
    assert initializer.call_args.args[1].name == ENROLLMENT
    return 'Active Self lifecycle dispatch (journey creation mocked)'


def _reminder_candidate():
    import club100_core.api.self_workout_notifications as reminders
    enrollment = frappe.db.get_value('Club100 Enrollment', ENROLLMENT,
        ['name', 'member', 'program', 'plan', 'start_date', 'end_date', 'status'], as_dict=True)
    fake_delivery = Mock(return_value={'sent': 1, 'failed': 0, 'skipped': False})
    with patch.object(reminders, 'now_datetime', return_value=datetime(2026, 10, 23, 13, 40)), \
         patch.object(reminders, '_eligible_enrollments', return_value=[enrollment]), \
         patch.object(reminders, '_send_once', fake_delivery):
        result = reminders.process_self_workout_reminders()
    fake_delivery.assert_called_once()
    _, workout = fake_delivery.call_args.args
    assert workout['id'] == WORKOUT and workout['title'] == 'Rhythm Cardio'
    assert workout['format'] == 'Pulse' and result['candidates'] == 1
    return 'Pulse / Rhythm Cardio selected; no push sent (delivery mocked)'


def run():
    """Run via bench execute; no application writes or external side effects."""
    if frappe.conf.get('developer_mode') not in (1, '1', True):
        frappe.throw('Regression suite is restricted to developer_mode sites.')
    frappe.set_user('Administrator')
    outcomes = []
    tests = [
        ('Self journey position projection', _position_projection),
        ('Renewal ordinal arithmetic', _renewal_arithmetic),
        ('Trial-to-paid historical slot count', _trial_to_paid_history),
        ('Trial conversion guards', _conversion_guard),
        ('Active Self journey dispatch', _activation_dispatch),
        ('Mocked reminder candidate', _reminder_candidate),
    ]
    try:
        for name, check in tests:
            _check(name, check, outcomes)
            frappe.db.rollback()  # Discard any unintended writes between checks.
    finally:
        frappe.db.rollback()
    for row in outcomes:
        print(f"{row['status']}: {row['test']} — {row['detail']}")
    passed = sum(row['status'] == 'PASS' for row in outcomes)
    print(f'PHASE 3D: {passed}/{len(outcomes)} CHECKS PASSED')
    if passed != len(outcomes):
        raise AssertionError('Phase 3D regression failures: inspect results above')
    return {'passed': passed, 'total': len(outcomes), 'checks': outcomes}
