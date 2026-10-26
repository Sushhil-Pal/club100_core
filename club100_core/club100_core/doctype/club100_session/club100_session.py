import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime


class Club100Session(Document):
    def validate(self):
        self.set_defaults_from_cohort()
        self.validate_target()
        self.assign_society_curriculum()
        self.validate_workout_format()
        self.validate_times()
        self.validate_online_details()

    def validate_target(self):
        # Self workouts are virtual and NEVER create a Club100 Session.
        if not self.cohort:
            frappe.throw("Trainer-led sessions must target a cohort.", frappe.ValidationError)
        if not self.trainer:
            frappe.throw("Trainer is required for cohort sessions.", frappe.ValidationError)

    def set_defaults_from_cohort(self):
        if not self.cohort:
            return

        cohort = frappe.db.get_value(
            "Club100 Cohort",
            self.cohort,
            [
                "program",
                "primary_trainer",
                "delivery_mode",
                "meeting_provider",
                "default_meeting_url",
            ],
            as_dict=True,
        )

        if not cohort:
            return

        if not self.program and cohort.program:
            self.program = cohort.program

        if not self.trainer and cohort.primary_trainer:
            self.trainer = cohort.primary_trainer

        if not self.delivery_mode and cohort.delivery_mode:
            self.delivery_mode = cohort.delivery_mode

        if not self.meeting_provider and cohort.meeting_provider:
            self.meeting_provider = cohort.meeting_provider

        if not self.meeting_url and cohort.default_meeting_url:
            self.meeting_url = cohort.default_meeting_url

    def assign_society_curriculum(self):
        """Freeze curriculum assignment when a new Society session is created.

        Only explicit cutover cohorts are managed. Existing/legacy sessions are
        never rewritten. New curriculum sessions must be appended in time order.
        """
        if not self.cohort:
            return
        if not self.is_new():
            if not self.society_curriculum_position:
                return  # legacy session
            old = self.get_doc_before_save()
            if old:
                frozen = ('cohort', 'society_curriculum_code', 'society_curriculum_position',
                          'society_original_slot_date', 'society_original_slot_time')
                if any(str(getattr(old, x, None) or '') != str(getattr(self, x, None) or '') for x in frozen):
                    frappe.throw('Society curriculum identity and original weekly slot cannot be changed.', frappe.ValidationError)
                moved = any(str(getattr(old, x, None) or '') != str(getattr(self, x, None) or '')
                            for x in ('session_date', 'start_time'))
                if moved:
                    if old.status in ('Completed', 'Live') or self.status in ('Completed', 'Live'):
                        frappe.throw('Live/completed Society sessions cannot be rescheduled.', frappe.ValidationError)
                    from frappe.utils import getdate, nowdate
                    if getdate(old.session_date) < getdate(nowdate()):
                        frappe.throw('Past Society sessions cannot be rescheduled.', frappe.ValidationError)
                    # Keep the stable ordinal and disallow overtaking other assigned sessions.
                    peers = frappe.get_all('Club100 Session', filters={
                        'cohort': self.cohort, 'society_curriculum_position': ['>', 0],
                        'name': ['!=', self.name]},
                        fields=['session_date', 'start_time', 'society_curriculum_position'],
                        limit_page_length=100000)
                    from club100_core.curriculum.society_weekly_schedule import _time_seconds
                    next_key = (str(self.session_date), _time_seconds(self.start_time))
                    position = int(self.society_curriculum_position)
                    for peer in peers:
                        peer_key = (str(peer.session_date), _time_seconds(peer.start_time))
                        if ((int(peer.society_curriculum_position) < position and peer_key >= next_key)
                            or (int(peer.society_curriculum_position) > position and peer_key <= next_key)):
                            frappe.throw('Rescheduling must preserve Society curriculum session order.', frappe.ValidationError)
            if old and old.status == 'Completed' and self.workout_format != old.workout_format:
                frappe.throw('Completed Society session format cannot be changed.', frappe.ValidationError)
            if old and old.status == 'Completed' and self.society_override_reason != old.society_override_reason:
                frappe.throw('Completed Society session override reason cannot be changed.', frappe.ValidationError)
            from club100_core.curriculum.society import position_from_ordinal
            prescribed = position_from_ordinal(int(self.society_curriculum_position) - 1)['format']
            if self.workout_format == prescribed:
                self.society_override_reason = None
            return
        cohort = frappe.db.get_value('Club100 Cohort', self.cohort,
                                     ['society', 'delivery_mode', 'program', 'society_curriculum_start_date'], as_dict=True)
        if not cohort or not cohort.society or cohort.delivery_mode != 'Onsite' or not cohort.society_curriculum_start_date:
            return
        from frappe.utils import getdate
        if not self.session_date:
            frappe.throw('Session date required for Society curriculum scheduling.', frappe.ValidationError)
        if getdate(self.session_date) < getdate(cohort.society_curriculum_start_date):
            return  # pre-cutover legacy data
        if self.status == 'Cancelled':
            frappe.throw('Create the Society curriculum session as Scheduled; cancel afterwards if needed.', frappe.ValidationError)
        from club100_core.curriculum.society import position_from_ordinal, validate_society_curriculum
        curricula = frappe.get_all('Club100 Program Curriculum', filters={
            'program': cohort.program, 'track': 'Society', 'level_code': 'MIXED', 'active': 1},
            pluck='name', limit_page_length=3)
        if len(curricula) != 1:
            frappe.throw('Expected one active Society MIXED curriculum.', frappe.ValidationError)
        curriculum = frappe.get_doc('Club100 Program Curriculum', curricula[0])
        validate_society_curriculum(curriculum)
        # Serialize curriculum position assignment with the weekly generator.
        frappe.db.sql('SELECT name FROM `tabClub100 Cohort` WHERE name=%s FOR UPDATE', (self.cohort,))
        prior = frappe.get_all('Club100 Session', filters={
            'cohort': self.cohort, 'society_curriculum_position': ['>', 0]},
            fields=['name', 'session_date', 'start_time', 'society_curriculum_position'],
            order_by='society_curriculum_position desc', limit_page_length=1)
        if prior:
            last = prior[0]
            if (str(self.session_date), str(self.start_time or '')) <= (str(last.session_date), str(last.start_time or '')):
                frappe.throw('New Society sessions must be scheduled after the last curriculum-assigned session.', frappe.ValidationError)
        ordinal = int(prior[0].society_curriculum_position) if prior else 0
        position = position_from_ordinal(ordinal)
        expected = position['format']
        if not frappe.db.exists('Club100 Workout Format', expected):
            frappe.throw('Missing linked Club100 Workout Format: ' + expected, frappe.ValidationError)
        self.society_curriculum_code = curriculum.name
        self.society_curriculum_position = ordinal + 1
        self.workout_format = expected

    def validate_workout_format(self):
        # Cohort sessions use their own format. Society-assigned sessions already
        # derive this from their curriculum in assign_society_curriculum().
        if self.is_new() and not self.workout_format:
            frappe.throw("Workout Format is required for new cohort sessions.", frappe.ValidationError)

    def validate_times(self):
        if not self.session_date or not self.start_time or not self.end_time:
            return

        start = get_datetime(f"{self.session_date} {self.start_time}")
        end = get_datetime(f"{self.session_date} {self.end_time}")

        if end <= start:
            frappe.throw("End Time must be after Start Time.")

    def validate_online_details(self):
        if self.delivery_mode in ("Online", "Hybrid"):
            if self.status in ("Scheduled", "Live") and not self.meeting_url:
                frappe.throw(
                    "Meeting URL is required for scheduled online or hybrid sessions."
                )
