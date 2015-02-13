from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import models
from django.db.models import Count
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import ugettext_lazy as _
from django.template import Context, Template, TemplateSyntaxError, TemplateEncodingError
from django_fsm import FSMField, transition
import django.dispatch
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin, Group

# see evaluation.meta for the use of Translate in this file
from evap.evaluation.meta import LocalizeModelBase, Translate

import datetime
import random

# for converting state into student_state
STUDENT_STATES_NAMES = {
    'new': 'upcoming',
    'prepared': 'upcoming',
    'lecturerApproved': 'upcoming',
    'approved': 'upcoming',
    'inEvaluation': 'inEvaluation',
    'evaluated': 'evaluationFinished',
    'reviewed': 'evaluationFinished',
    'published': 'published'
}

class NotArchiveable(Exception):
    """An attempt has been made to archive something that is not archiveable."""
    pass


class Semester(models.Model, metaclass=LocalizeModelBase):
    """Represents a semester, e.g. the winter term of 2011/2012."""

    name_de = models.CharField(max_length=1024, unique=True, verbose_name=_("name (german)"))
    name_en = models.CharField(max_length=1024, unique=True, verbose_name=_("name (english)"))

    name = Translate

    created_at = models.DateField(verbose_name=_("created at"), auto_now_add=True)

    class Meta:
        ordering = ('-created_at', 'name_de')
        verbose_name = _("semester")
        verbose_name_plural = _("semesters")

    def __str__(self):
        return self.name

    @property
    def can_staff_delete(self):
        return all(course.can_staff_delete for course in self.course_set.all())

    @property
    def is_archiveable(self):
        return all(course.is_archiveable for course in self.course_set.all())

    @property
    def is_archived(self):
        if self.course_set.count() == 0:
            return False
        first_course_is_archived = self.course_set.first().is_archived
        assert(all(course.is_archived == first_course_is_archived for course in self.course_set.all()))
        return first_course_is_archived

    def archive(self):
        if not self.is_archiveable:
            raise NotArchiveable()
        for course in self.course_set.all():
            course._archive()

    @classmethod
    def get_all_with_published_courses(cls):
        return cls.objects.filter(course__state="published").distinct()
    
    @classmethod
    def active_semester(cls):
        return cls.objects.latest("created_at")


class Questionnaire(models.Model, metaclass=LocalizeModelBase):
    """A named collection of questions."""

    name_de = models.CharField(max_length=1024, unique=True, verbose_name=_("name (german)"))
    name_en = models.CharField(max_length=1024, unique=True, verbose_name=_("name (english)"))
    name = Translate

    description_de = models.TextField(verbose_name=_("description (german)"), blank=True, null=True)
    description_en = models.TextField(verbose_name=_("description (english)"), blank=True, null=True)
    description = Translate

    public_name_de = models.CharField(max_length=1024, verbose_name=_("display name (german)"))
    public_name_en = models.CharField(max_length=1024, verbose_name=_("display name (english)"))
    public_name = Translate

    teaser_de = models.TextField(verbose_name=_("teaser (german)"), blank=True, null=True)
    teaser_en = models.TextField(verbose_name=_("teaser (english)"), blank=True, null=True)
    teaser = Translate

    index = models.IntegerField(verbose_name=_("ordering index"), default=0)

    is_for_contributors = models.BooleanField(verbose_name=_("is for contributors"), default=False)
    obsolete = models.BooleanField(verbose_name=_("obsolete"), default=False)

    class Meta:
        ordering = ('obsolete', 'index', 'name_de')
        verbose_name = _("questionnaire")
        verbose_name_plural = _("questionnaires")

    def __str__(self):
        return self.name

    @property
    def can_staff_edit(self):
        return not self.contributions.exists()
    
    @property
    def can_staff_delete(self):
        return self.can_staff_edit


class Course(models.Model, metaclass=LocalizeModelBase):
    """Models a single course, e.g. the Math 101 course of 2002."""

    state = FSMField(default='new', protected=True)

    semester = models.ForeignKey(Semester, verbose_name=_("semester"))

    name_de = models.CharField(max_length=1024, verbose_name=_("name (german)"))
    name_en = models.CharField(max_length=1024, verbose_name=_("name (english)"))
    name = Translate

    # type of course: lecture, seminar, project
    kind = models.CharField(max_length=1024, verbose_name=_("type"))

    # bachelor, master, d-school course
    degree = models.CharField(max_length=1024, verbose_name=_("degree"))

    # default is True as that's the more restrictive option
    is_graded = models.BooleanField(verbose_name=_("is graded"), default=True)

    # students that are allowed to vote
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, verbose_name=_("participants"), blank=True)
    _participant_count = models.IntegerField(verbose_name=_("participant count"), blank=True, null=True, default=None)

    # students that already voted
    voters = models.ManyToManyField(settings.AUTH_USER_MODEL, verbose_name=_("voters"), blank=True, related_name='+')
    _voter_count = models.IntegerField(verbose_name=_("voter count"), blank=True, null=True, default=None)

    # when the evaluation takes place
    vote_start_date = models.DateField(null=True, verbose_name=_("first date to vote"))
    vote_end_date = models.DateField(null=True, verbose_name=_("last date to vote"))

    # who last modified this course
    last_modified_time = models.DateTimeField(auto_now=True)
    last_modified_user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="+", null=True, blank=True, on_delete=models.SET_NULL)

    course_evaluated = django.dispatch.Signal(providing_args=['request', 'semester'])

    class Meta:
        ordering = ('semester', 'degree', 'name_de')
        unique_together = (
            ('semester', 'degree', 'name_de'),
            ('semester', 'degree', 'name_en'),
        )
        verbose_name = _("course")
        verbose_name_plural = _("courses")

    def __str__(self):
        return self.name

    def clean(self):
        if self.vote_start_date and self.vote_end_date:
            if self.vote_start_date >= self.vote_end_date:
                raise ValidationError(_("The vote start date must be before the vote end date."))

    def save(self, *args, **kw):
        super().save(*args, **kw)

        # make sure there is a general contribution
        if not self.general_contribution:
            self.contributions.create(contributor=None)

    def is_fully_checked(self):
        """Shortcut for finding out whether all text answers to this course have been checked"""
        return not self.open_textanswer_set.exists()

    def is_fully_checked_except(self, ignored_answers):
        """Shortcut for finding out if all text answers to this course have been checked except for specified answers"""
        return not self.open_textanswer_set.exclude(pk__in=ignored_answers).exists()

    def can_user_vote(self, user):
        """Returns whether the user is allowed to vote on this course."""
        return (self.state == "inEvaluation"
            and datetime.date.today() <= self.vote_end_date
            and user in self.participants.all()
            and user not in self.voters.all())

    def can_user_see_results(self, user):
        if user.is_staff:
            return True
        if self.state == 'published':
            return self.can_publish_grades or self.is_user_contributor_or_delegate(user)
        return False

    @property
    def can_staff_edit(self):
        return not self.is_archived and self.state in ['new', 'prepared', 'lecturerApproved', 'approved', 'inEvaluation']

    @property
    def can_staff_delete(self):
        return self.can_staff_edit and not self.num_voters > 0

    @property
    def can_staff_review(self):
        return self.state in ['inEvaluation', 'evaluated'] and not self.is_fully_checked()

    @property
    def can_staff_approve(self):
        return self.state in ['new', 'prepared', 'lecturerApproved']

    @property
    def can_publish_grades(self):
        return self.num_voters >= settings.MIN_ANSWER_COUNT and float(self.num_voters) / self.num_participants >= settings.MIN_ANSWER_PERCENTAGE

    @transition(field=state, source=['new', 'lecturerApproved'], target='prepared')
    def ready_for_contributors(self):
        pass

    @transition(field=state, source='prepared', target='lecturerApproved')
    def contributor_approve(self):
        pass

    @transition(field=state, source=['new', 'prepared', 'lecturerApproved'], target='approved')
    def staff_approve(self):
        pass

    @transition(field=state, source='prepared', target='new')
    def revert_to_new(self):
        pass

    @transition(field=state, source='approved', target='inEvaluation')
    def evaluation_begin(self):
        pass

    @transition(field=state, source='inEvaluation', target='evaluated')
    def evaluation_end(self):
        pass

    @transition(field=state, source='evaluated', target='reviewed', conditions=[is_fully_checked])
    def review_finished(self):
        pass

    @transition(field=state, source='reviewed', target='published')
    def publish(self):
        pass

    @transition(field=state, source='published', target='reviewed')
    def revoke(self):
        pass

    @property
    def student_state(self):
        return STUDENT_STATES_NAMES[self.state]

    @property
    def general_contribution(self):
        try:
            return self.contributions.get(contributor=None)
        except Contribution.DoesNotExist:
            return None

    @property
    def num_participants(self):
        if self._participant_count is not None:
            return self._participant_count
        return self.participants.count()

    @property
    def num_voters(self):
        if self._voter_count is not None:
            return self._voter_count
        return self.voters.count()

    @property
    def due_participants(self):
        return self.participants.exclude(pk__in=self.voters.all())

    @property
    def responsible_contributor(self):
        return self.contributions.get(responsible=True).contributor

    @property
    def responsible_contributors_name(self):
        return self.responsible_contributor.full_name

    @property
    def responsible_contributors_username(self):
        return self.responsible_contributor.username

    @property
    def days_left_for_evaluation(self):
        return (self.vote_end_date - datetime.date.today()).days

    @property
    def has_enough_questionnaires(self):
        return self.general_contribution and all(self.contributions.aggregate(Count('questionnaires')).values())

    def is_user_editor_or_delegate(self, user):
        if self.contributions.filter(can_edit=True, contributor=user).exists():
            return True
        else:
            represented_users = user.represented_users.all()
            if self.contributions.filter(can_edit=True, contributor__in=represented_users).exists():
                return True

        return False

    def is_user_responsible_or_delegate(self, user):
        if self.contributions.filter(responsible=True, contributor=user).exists():
            return True
        else:
            represented_users = user.represented_users.all()
            if self.contributions.filter(responsible=True, contributor__in=represented_users).exists():
                return True

        return False

    def is_user_contributor(self, user):
        return self.contributions.filter(contributor=user).exists()

    def is_user_contributor_or_delegate(self, user):
        if self.is_user_contributor(user):
            return True
        else:
            represented_users = user.represented_users.all()
            if self.contributions.filter(contributor__in=represented_users).exists():
                return True
        return False

    def is_user_editor(self, user):
        return self.contributions.filter(contributor=user, can_edit=True).exists()

    def warnings(self):
        result = []
        if self.state == 'new' and not self.has_enough_questionnaires:
            result.append(_("Not enough questionnaires assigned"))
        if self.state in ['inEvaluation', 'evaluated', 'reviewed'] and not self.can_publish_grades:
            result.append(_("Not enough participants to publish results"))
        return result

    @property
    def textanswer_set(self):
        """Pseudo relationship to all text answers for this course"""
        return TextAnswer.objects.filter(contribution__in=self.contributions.all())

    @property
    def open_textanswer_set(self):
        """Pseudo relationship to all text answers for this course"""
        return TextAnswer.objects.filter(contribution__in=self.contributions.all(), checked=False)

    @property
    def checked_textanswer_set(self):
        """Pseudo relationship to all text answers for this course"""
        return TextAnswer.objects.filter(contribution__in=self.contributions.all(), checked=True)

    @property
    def likertanswer_set(self):
        """Pseudo relationship to all Likert answers for this course"""
        return LikertAnswer.objects.filter(contribution__in=self.contributions.all())

    @property
    def gradeanswer_set(self):
        """Pseudo relationship to all grade answers for this course"""
        return GradeAnswer.objects.filter(contribution__in=self.contributions.all())

    def _archive(self):
        """Should be called only via Semester.archive"""
        if not self.is_archiveable:
            raise NotArchiveable()
        self._participant_count = self.participants.count()
        self._voter_count = self.voters.count()
        self.save()

    @property
    def is_archived(self):
        assert((self._participant_count is None) == (self._voter_count is None))
        return self._participant_count is not None

    @property
    def is_archiveable(self):
        return not self.is_archived and self.state in ["new", "published"]

    def was_evaluated(self, request):
        self.course_evaluated.send(sender=self.__class__, request=request, semester=self.semester)


class Contribution(models.Model):
    """A contributor who is assigned to a course and his questionnaires."""

    course = models.ForeignKey(Course, verbose_name=_("course"), related_name='contributions')
    contributor = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("contributor"), blank=True, null=True, related_name='contributions')
    questionnaires = models.ManyToManyField(Questionnaire, verbose_name=_("questionnaires"), blank=True, related_name="contributions")
    responsible = models.BooleanField(verbose_name=_("responsible"), default=False)
    can_edit = models.BooleanField(verbose_name=_("can edit"), default=False)

    order = models.IntegerField(verbose_name=_("contribution order"), default=-1)

    class Meta:
        unique_together = (
            ('course', 'contributor'),
        )
        ordering = ['order', ]

    def clean(self):
        # responsible contributors can always edit
        if self.responsible:
            self.can_edit = True

    @property
    def is_general(self):
        return self.contributor == None


class Question(models.Model, metaclass=LocalizeModelBase):
    """A question including a type."""

    QUESTION_KINDS = (
        ("T", _("Text Question")),
        ("L", _("Likert Question")),
        ("G", _("Grade Question")),
    )

    questionnaire = models.ForeignKey(Questionnaire)
    text_de = models.TextField(verbose_name=_("question text (german)"))
    text_en = models.TextField(verbose_name=_("question text (english)"))
    kind = models.CharField(max_length=1, choices=QUESTION_KINDS,
                            verbose_name=_("kind of question"))

    text = Translate

    class Meta:
        order_with_respect_to = 'questionnaire'
        verbose_name = _("question")
        verbose_name_plural = _("questions")

    @property
    def answer_class(self):
        if self.kind == "T":
            return TextAnswer
        elif self.kind == "L":
            return LikertAnswer
        elif self.kind == "G":
            return GradeAnswer
        else:
            raise Exception("Unknown answer kind: %r" % self.kind)

    @property
    def is_likert_question(self):
        return self.answer_class == LikertAnswer

    @property
    def is_text_question(self):
        return self.answer_class == TextAnswer

    @property
    def is_grade_question(self):
        return self.answer_class == GradeAnswer


class Answer(models.Model):
    """An abstract answer to a question. For anonymity purposes, the answering
    user ist not stored in the object. Concrete subclasses are `LikertAnswer`,
    `TextAnswer` and `GradeAnswer`."""

    question = models.ForeignKey(Question)
    contribution = models.ForeignKey(Contribution)

    class Meta:
        abstract = True
        verbose_name = _("answer")
        verbose_name_plural = _("answers")


class LikertAnswer(Answer):
    """A Likert-scale answer to a question with `1` being *strongly agree* and `5`
    being *strongly disagree*."""

    answer = models.IntegerField(verbose_name=_("answer"))

    class Meta:
        verbose_name = _("Likert answer")
        verbose_name_plural = _("Likert answers")


class GradeAnswer(Answer):
    """A grade answer to a question with `1` being best and `5` being worst."""

    answer = models.IntegerField(verbose_name=_("answer"))

    class Meta:
        verbose_name = _("grade answer")
        verbose_name_plural = _("grade answers")


class TextAnswer(Answer):
    """A free-form text answer to a question (usually a comment about a course
    or a contributor)."""

    elements_per_page = 5

    reviewed_answer = models.TextField(verbose_name=_("reviewed answer"), blank=True, null=True)
    original_answer = models.TextField(verbose_name=_("original answer"), blank=True)

    checked = models.BooleanField(verbose_name=_("answer checked"), default=False)
    hidden = models.BooleanField(verbose_name=_("hide answer"), default=False)

    class Meta:
        verbose_name = _("text answer")
        verbose_name_plural = _("text answers")

    @property
    def answer(self):
        return self.reviewed_answer or self.original_answer
    @answer.setter
    def answer(self, value):
        self.original_answer = value
        self.reviewed_answer = None


class FaqSection(models.Model, metaclass=LocalizeModelBase):
    """Section in the frequently asked questions"""

    order = models.IntegerField(verbose_name=_("section order"), default=-1)

    title_de = models.TextField(verbose_name=_("section title (german)"))
    title_en = models.TextField(verbose_name=_("section title (english)"))
    title = Translate

    class Meta:
        ordering = ['order', ]
        verbose_name = _("section")
        verbose_name_plural = _("sections")


class FaqQuestion(models.Model, metaclass=LocalizeModelBase):
    """Question and answer in the frequently asked questions"""

    section = models.ForeignKey(FaqSection, related_name="questions")

    order = models.IntegerField(verbose_name=_("question order"), default=-1)

    question_de = models.TextField(verbose_name=_("question (german)"))
    question_en = models.TextField(verbose_name=_("question (english)"))
    question = Translate

    answer_de = models.TextField(verbose_name=_("answer (german)"))
    answer_en = models.TextField(verbose_name=_("answer (german)"))
    answer = Translate

    class Meta:
        ordering = ['order', ]
        verbose_name = _("question")
        verbose_name_plural = _("questions")

class UserProfileManager(BaseUserManager):
    def create_user(self, username, password=None, email=None, first_name=None, last_name=None):
        if not username:
            raise ValueError(_('Users must have a username'))

        user = self.model(
            username=username,
            email=self.normalize_email(email),
            first_name=first_name,
            last_name=last_name
        )
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, username, password, email=None, first_name=None, last_name=None):
        user = self.create_user(
            username=username,
            password=password, 
            email=email, 
            first_name=first_name, 
            last_name=last_name
        )
        user.is_superuser = True
        user.save()
        user.groups.add(Group.objects.get(name="Staff"))
        return user


# taken from http://stackoverflow.com/questions/454436/unique-fields-that-allow-nulls-in-django
class EmailNullField(models.EmailField, metaclass=models.SubfieldBase):
    
    description = "EmailField that stores NULL but returns ''"

    def to_python(self, value):  # this is the value right out of the db, or an instance
       return value or ""

    def get_prep_value(self, value):  # catches value right before sending to db
       return value or None


class UserProfile(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=255, unique=True, verbose_name=_('username'))
    email = EmailNullField(max_length=255, unique=True, blank=True, null=True, verbose_name=_('email address'))
    title = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Title"))
    first_name = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("first name"))
    last_name = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("last name"))

    # delegates of the user, which can also manage their courses
    delegates = models.ManyToManyField("UserProfile", verbose_name=_("Delegates"), related_name="represented_users", blank=True)

    # users to which all emails should be sent in cc without giving them delegate rights
    cc_users = models.ManyToManyField("UserProfile", verbose_name=_("CC Users"), related_name="ccing_users", blank=True)

    # key for url based login of this user
    MAX_LOGIN_KEY = 2**31-1

    login_key = models.IntegerField(verbose_name=_("Login Key"), unique=True, blank=True, null=True)
    login_key_valid_until = models.DateField(verbose_name=_("Login Key Validity"), blank=True, null=True)

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')


    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = []

    objects = UserProfileManager()

    # needed e.g. for compatibility with contrib.auth.admin
    def get_full_name(self):
        return self.full_name

    # needed e.g. for compatibility with contrib.auth.admin
    def get_short_name(self):
        if self.first_name:
            return self.first_name
        return self.username

    @property
    def full_name(self):
        if self.last_name:
            name = self.last_name
            if self.first_name:
                name = self.first_name + " " + name
            if self.title:
                name = self.title + " " + name
            return name
        else:
            return self.username

    def __str__(self):
        return self.full_name;

    @property
    def is_active(self):
        return True

    @property
    def is_staff(self):
        return self.groups.filter(name='Staff').exists()

    @property
    def can_staff_delete(self):
        states_with_votes = ["inEvaluation", "reviewed", "evaluated", "published"]
        if any(course.state in states_with_votes and not course.is_archived for course in self.course_set.all()):
            return False
        return not self.is_contributor

    @property
    def is_participant(self):
        return self.course_set.exists()

    @property
    def is_contributor(self):
        return self.contributions.exists()

    @property
    def is_editor(self):
        return self.contributions.filter(can_edit=True).exists()

    @property
    def is_responsible(self):
        # in the user list, self.user.contributions is prefetched, therefore use it directly and don't filter it
        return any(contribution.responsible for contribution in self.contributions.all())

    @property
    def is_delegate(self):
        return self.represented_users.exists()

    @property
    def is_editor_or_delegate(self):
        return self.is_editor or self.is_delegate

    @property
    def is_contributor_or_delegate(self):
        return self.is_contributor or self.is_delegate

    @property
    def is_external(self):
        # do the import here to prevent a circular import
        from evap.evaluation.tools import is_external_email
        if not self.email:
            return True
        return is_external_email(self.email)

    @classmethod
    def email_needs_login_key(cls, email):
        # do the import here to prevent a circular import
        from evap.evaluation.tools import is_external_email
        return is_external_email(email)

    @property
    def needs_login_key(self):
        return UserProfile.email_needs_login_key(self.email)

    def generate_login_key(self):
        while True:
            key = random.randrange(0, UserProfile.MAX_LOGIN_KEY)
            if not UserProfile.objects.filter(login_key=key).exists():
                # key not yet used
                self.login_key = key
                break
        self.refresh_login_key()

    def refresh_login_key(self):
        self.login_key_valid_until = datetime.date.today() + datetime.timedelta(settings.LOGIN_KEY_VALIDITY)

def validate_template(value):
    """Field validator which ensures that the value can be compiled into a
    Django Template."""
    try:
        Template(value)
    except (TemplateSyntaxError, TemplateEncodingError) as e:
        raise ValidationError(str(e))


class EmailTemplate(models.Model):
    name = models.CharField(max_length=1024, unique=True, verbose_name=_("Name"))

    subject = models.CharField(max_length=1024, verbose_name=_("Subject"), validators=[validate_template])
    body = models.TextField(verbose_name=_("Body"), validators=[validate_template])

    @classmethod
    def get_review_template(cls):
        return cls.objects.get(name="Lecturer Review Notice")

    @classmethod
    def get_reminder_template(cls):
        return cls.objects.get(name="Student Reminder")

    @classmethod
    def get_publish_template(cls):
        return cls.objects.get(name="Publishing Notice")

    @classmethod
    def get_login_key_template(cls):
        return cls.objects.get(name="Login Key Created")

    @classmethod
    def get_evaluation_started_template(cls):
        return cls.objects.get(name="Evaluation Started")

    @classmethod
    def recipient_list_for_course(cls, course, recipient_groups):
        recipients = []

        if "responsible" in recipient_groups:
            recipients += [course.responsible_contributor]

        if "contributors" in recipient_groups:
            recipients += [c.contributor for c in course.contributions.exclude(contributor=None)]
        elif "editors" in recipient_groups:
            recipients += [c.contributor for c in course.contributions.exclude(contributor=None).filter(can_edit=True)]

        if "all_participants" in recipient_groups:
            recipients += course.participants.all()
        elif "due_participants" in recipient_groups:
            recipients += course.due_participants

        return recipients

    @classmethod
    def render_string(cls, text, dictionary):
        return Template(text).render(Context(dictionary, autoescape=False))

    def send_to_users_in_courses(self, courses, recipient_groups):
        user_course_map = {}
        for course in courses:
            responsible = course.responsible_contributor
            for user in self.recipient_list_for_course(course, recipient_groups):
                if user.email and user not in responsible.cc_users.all() and user not in responsible.delegates.all():
                    user_course_map.setdefault(user, []).append(course)

        for user, courses in user_course_map.items():
            self.send_to_user(user, courses)

    def send_to_user(self, user, courses=None, cc=True):
        if not user.email:
            return

        if cc:
            cc_users = set(user.delegates.all() | user.cc_users.all())
            cc_addresses = [p.email for p in cc_users if p.email]
        else:
            cc_addresses = []

        mail = EmailMessage(
            subject = self.render_string(self.subject, {'user': user, 'courses': courses}),
            body = self.render_string(self.body, {'user': user, 'courses': courses}),
            to = [user.email],
            cc = cc_addresses,
            bcc = [a[1] for a in settings.MANAGERS],
            headers = {'Reply-To': settings.REPLY_TO_EMAIL})
        mail.send(False)

    @classmethod
    def send_reminder_to_user(cls, user, due_in_number_of_days, due_courses):
        if not user.email:
            return

        template = cls.get_reminder_template()        
        subject = template.render_string(template.subject, {'user': user, 'due_in_number_of_days': due_in_number_of_days})
        body = template.render_string(template.body, {'user': user, 'due_in_number_of_days': due_in_number_of_days, 'due_courses': due_courses})

        mail = EmailMessage(
            subject = subject,
            body = body,
            to = [user.email],
            bcc = [a[1] for a in settings.MANAGERS],
            headers = {'Reply-To': settings.REPLY_TO_EMAIL})
        mail.send(False)
